"""周期记账：每月固定支出按规则自动生成凭证。

SDK 1.1.0。后端入口必须是单文件：宿主用 spec_from_file_location 独立加载
backend/main.py，插件自身没有 sys.path，兄弟模块导入不可用。

三条触发路径共用同一套幂等引擎：
- 宿主定时任务 ``monthly_posting``（每日 07:17，扫全部账套的当月到期规则）；
- 页面手动执行（用户点「记账」，可指定月份补记）；
- on_period_close 结账钩子自动补记漏掉的月份。

**每条规则每个自然月最多成功记账一次**，这是本插件最硬的约束，由两层闸门保证：

1. 执行前先查台账。命中就把这条规则算作「已记账」直接跳过，不进凭证流程。
2. 台账表 ``(book_id, rule_id, period_year, period_month)`` 上有唯一约束。
   第一层是「先查后写」，两个触发器在同一时刻命中同一条规则时仍可能双双通过
   检查；真正挡住的是这一层 —— 第二条插入会被数据库拒绝，凭证不会重复生成。

因此手动执行与定时任务可以同时开着：先跑哪个都只记一次，后跑的会看到
「本月已记账」而跳过。

凭证一律由宿主创建（voucher_batch_create），插件不碰凭证表，也就不涉及过账、
编号和反记账这些必须由宿主护栏守住的规则。生成的凭证是 UNPOSTED，由用户照常
记账 —— 自动过账会跳过「上一期是否已结账」「号段是否整齐」这些宿主校验，
在这些前提不成立时把凭证写进不该写的期间。
"""
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

RULE_TABLE = "plugin_769c8e79a72c_rule"
RUN_TABLE = "plugin_769c8e79a72c_run"
MARK = "cwq_periodic"
RULES_VERSION = "periodic-v1"

START_LABEL_RE = re.compile(r"(\d{4})-(\d{1,2})")

MAX_RULES = 200
MAX_ENTRIES = 20
ENTRY_SUMMARY_MAX = 256
REMARK_MAX = 120


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def _text(value):
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("\r", " ").strip()


def _money(value):
    return str(Decimal(value).quantize(Decimal("0.01")))


def _decimal(value):
    """把用户输入的金额读成两位小数的 Decimal；非法输入直接报错，不猜。"""
    if isinstance(value, bool):
        raise HTTPException(422, "金额格式不正确")
    if isinstance(value, (int, float, Decimal)):
        raw = Decimal(str(value))
    else:
        text = _text(value).replace(",", "").replace("，", "").replace("¥", "").replace("￥", "")
        text = text.replace("元", "").strip()
        if not text:
            raise HTTPException(422, "金额不能为空")
        try:
            raw = Decimal(text)
        except InvalidOperation:
            raise HTTPException(422, "金额「%s」不是有效数字" % text[:20])
    if not raw.is_finite():
        raise HTTPException(422, "金额必须是有限数字")
    if raw <= 0:
        raise HTTPException(422, "金额必须大于 0（费用支出为正数，方向由分录借贷决定）")
    if raw != raw.quantize(Decimal("0.01")):
        raise HTTPException(422, "金额最多两位小数")
    if abs(raw) >= Decimal("1000000000"):
        raise HTTPException(422, "金额过大")
    return raw


def _fail(message, status=422):
    raise HTTPException(status, message)


def _period_label(year, month):
    return "%d年%d月" % (year, month)


def _month_end(year, month):
    """当月最后一天：2 月按闰年判定，4/6/9/11 月 30 天。"""
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _resolve_day(year, month, day):
    """把「每月第 N 天」落到具体日期。

    31 号在 2 月、30 号在 2 月都不存在。这里取当月最后一天，而不是跳过这个月：
    规则写的是「每月都记」，用户写 31 号时期待的是 2 月也有一笔（对 28 天的月份
    而言就是月末那天）。跳过会让 2 月静默漏记，而漏记比日期偏移更难发现。
    """
    last = _month_end(year, month)
    if day >= last.day:
        return last
    return date(year, month, day)


def _today():
    return date.today()


def _shift_month(year, month, delta):
    """年月平移，跨年自动进位。delta 为负表示往前推。"""
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _year_month(payload):
    """目标年月。缺省是当前月 —— 页面不选月份时，执行的就是「这个月」。"""
    today = _today()
    raw_year, raw_month = payload.get("year"), payload.get("month")
    try:
        year = int(raw_year) if raw_year not in (None, "") else today.year
        month = int(raw_month) if raw_month not in (None, "") else today.month
    except (TypeError, ValueError):
        _fail("年月格式不正确")
    if not 1 <= month <= 12:
        _fail("月份要在 1 到 12 之间")
    if not 1970 <= year <= 2999:
        _fail("年份超出合理范围")
    return year, month


# --------------------------------------------------------------------------
# 规则读取与规整
# --------------------------------------------------------------------------

def _clean_entry(raw, problems):
    """规整一条分录模板。

    存科目**编码**而非 id：规则要跨账套复用，执行时才按当前账套的科目表解析。
    """
    if not isinstance(raw, dict):
        return None
    code = _text(raw.get("code"))
    direction = _text(raw.get("direction")).upper()
    if direction not in ("DEBIT", "CREDIT"):
        problems.append("每条分录都要选择借方或贷方")
        return None
    if not code:
        problems.append("每条分录都要选择科目")
        return None
    return {"code": code, "direction": direction,
            "summary": _text(raw.get("summary"))[:ENTRY_SUMMARY_MAX]}


def _clean_entries(raw):
    """收敛分录模板；借方必须恰好一条 —— 每期支出就是一笔费用加一个对方科目。"""
    if not isinstance(raw, list) or not raw:
        _fail("每条规则至少要一条分录")
    if len(raw) > MAX_ENTRIES:
        _fail("一条规则最多 %d 条分录" % MAX_ENTRIES)
    problems = []
    entries = [entry for entry in (_clean_entry(item, problems) for item in raw) if entry]
    if problems:
        _fail(problems[0])
    debits = [entry for entry in entries if entry["direction"] == "DEBIT"]
    credits = [entry for entry in entries if entry["direction"] == "CREDIT"]
    if not debits:
        _fail("分录里至少要有一条借方")
    if not credits:
        _fail("分录里至少要有一条贷方")
    if len(debits) > 1:
        _fail("周期支出按一笔费用处理：借方只能有一条（拆成多条请手工记账）")
    return entries


def _clean_rule(payload, existing=None):
    """把前端提交收敛成一条完整规则。existing 用于编辑时保留 id 之外的全部字段。"""
    if not isinstance(payload, dict):
        _fail("规则内容不合法")
    name = _text(payload.get("name"))[:120]
    if not name:
        _fail("请填写规则名称")
    day = payload.get("day_of_month")
    try:
        day = int(day)
    except (TypeError, ValueError):
        _fail("请填写每月第几天（1 到 31）")
    if not 1 <= day <= 31:
        _fail("每月第几天要在 1 到 31 之间")
    amount = _decimal(payload.get("amount"))
    entries = _clean_entries(payload.get("entries"))
    remark = _text(payload.get("remark"))[:REMARK_MAX]
    attachments = payload.get("attachments")
    if not isinstance(attachments, int) or isinstance(attachments, bool):
        attachments = 1
    attachments = max(0, min(attachments, 99))
    enabled = payload.get("enabled") is not False

    start_label = _label(payload.get("start_label"), "起始年月")
    end_label = _label(payload.get("end_label"), "结束年月")
    if start_label and end_label and end_label < start_label:
        _fail("结束年月不能早于起始年月")
    return {
        "name": name,
        "day_of_month": day,
        "amount": _money(amount),
        "entries": entries,
        "remark": remark,
        "attachments": attachments,
        "enabled": enabled,
        "start_label": start_label,
        "end_label": end_label,
        "note": _text(payload.get("note"))[:200],
        "sort_no": int(payload.get("sort_no") or 0),
    }


def _label(raw, title):
    """规整成 YYYY-MM；空值表示不限。写错格式直接报错，不静默忽略 ——
    一个被静默忽略的起始年月会让规则悄悄少记几个月。"""
    text = _text(raw).replace("/", "-").replace("年", "-").replace("月", "")
    text = text.strip().strip("-")
    if not text:
        return ""
    match = START_LABEL_RE.fullmatch(text)
    if not match:
        _fail("%s请用 YYYY-MM 格式，例如 2026-01" % title)
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        _fail("%s的月份要在 1 到 12 之间" % title)
    return "%04d-%02d" % (year, month)


def _rules_of(ctx, book_id):
    rows = ctx.data.read(RULE_TABLE, book_id=book_id, limit=MAX_RULES + 1)
    return sorted(rows, key=lambda row: (row.get("sort_no") or 0, row.get("id") or 0))


def _rule_row(ctx, book_id, rule_id):
    for row in ctx.data.read(RULE_TABLE, book_id=book_id, limit=MAX_RULES + 1):
        if row.get("id") == rule_id:
            return row
    return None


def _rule_active(row, year, month):
    """规则是否覆盖该年月。区间为空表示不限。

    比较用 YYYY-MM 字符串：定长零填充的年月串字典序即时间序，比拆成整数比较
    更不容易写错，也不会因为闰年之类的边界搞反。
    """
    if row.get("enabled") is False:
        return False
    label = "%04d-%02d" % (year, month)
    start = _text(row.get("start_label"))
    end = _text(row.get("end_label"))
    if start and label < start:
        return False
    if end and label > end:
        return False
    return True


# --------------------------------------------------------------------------
# 凭证构造
# --------------------------------------------------------------------------

def _resolve_subjects(ctx, book_id, rules):
    """把规则里的科目编码批量换成当前账套的 subject_id。

    同一编码只报一次问题，并区分「不存在」「已停用」「不是末级」：只说「找不到」
    的话，用户不知道该去建科目还是去启用它。
    """
    index = {_text(subject["code"]): subject for subject in ctx.subject_read(book_id)}
    wanted = []
    for row in rules:
        for entry in (row.get("entries") or []):
            code = _text(entry.get("code"))
            if code and code not in wanted:
                wanted.append(code)
    resolved, problems = {}, []
    for code in wanted:
        subject = index.get(code)
        label = "%s %s" % (code, _text(subject.get("name"))) if subject else code
        if subject is None:
            problems.append("科目编码 %s 在本账套不存在" % code)
            continue
        if not subject.get("active"):
            problems.append("科目 %s 已停用，不能记账" % label)
            continue
        if not subject.get("is_leaf"):
            problems.append("科目 %s 不是末级科目，不能记账" % label)
            continue
        resolved[code] = subject["id"]
    return resolved, problems


def _build_draft(ctx, row, resolved, year, month):
    """把一条规则展开成目标月份的凭证草稿。

    金额按规则固定值记账，分录方向由规则模板决定：借方是费用，贷方是付款账户。
    凭证日期取规则里「每月第 N 天」在该月的实际日期。
    """
    amount = _money(_decimal(row.get("amount")))
    day = int(row.get("day_of_month") or 1)
    when = _resolve_day(year, month, day)
    entries = []
    for entry in row.get("entries") or []:
        code = _text(entry.get("code"))
        subject_id = resolved.get(code)
        if subject_id is None:
            continue
        entries.append({
            "subject_id": subject_id,
            "direction": entry.get("direction"),
            "amount": amount,
            "summary": _text(entry.get("summary")) or _text(row.get("name"))[:ENTRY_SUMMARY_MAX],
            "aux_ref": {MARK: {"rule": _text(row.get("name")), "period": _period_label(year, month)}},
        })
    debit = sum(Decimal(item["amount"]) for item in entries if item["direction"] == "DEBIT")
    credit = sum(Decimal(item["amount"]) for item in entries if item["direction"] == "CREDIT")
    if debit != credit:
        return None, "借方合计 %s 与贷方合计 %s 不相等" % (_money(debit), _money(credit))
    if not entries:
        return None, "分录的科目都没有解析成功"
    return {
        "voucher_date": when.isoformat(),
        "remark": _text(row.get("remark")) or "%s %s" % (_period_label(year, month), _text(row.get("name"))),
        "attachments": int(row.get("attachments") or 0),
        "entries": entries,
    }, ""


# --------------------------------------------------------------------------
# 执行引擎
# --------------------------------------------------------------------------

def _runs_of(ctx, book_id, rule_ids=None):
    """读执行台账，按账套一次读全（data.read 单次上限 500 行）。

    幂等的判断键是 **(规则, 年, 月)** 而不是期间序号：插件读不到宿主的 period
    表，拿不到可靠的 period_ord，而年月是用户自己指定、宿主校验过必然存在的
    东西。用年月做键，跨年度、账套起始年不同的情形下都不会错判。
    """
    rows = ctx.data.read(RUN_TABLE, book_id=book_id, limit=500)
    if rule_ids is not None:
        wanted = set(rule_ids)
        rows = [row for row in rows if row.get("rule_id") in wanted]
    return {(row.get("rule_id"), int(row.get("period_year") or 0), int(row.get("period_month") or 0)): row
            for row in rows}


def _plan(ctx, book_id, year, month, only=None, include_done=False):
    """算出这个月该生成哪些凭证，返回 (计划, 跳过的规则, 问题)。

    这里不碰任何宿主数据，只做规则判断，所以页面可以在生成前先给人看一遍。
    """
    rules = _rules_of(ctx, book_id)
    if only:
        wanted = set(only)
        rules = [row for row in rules if row.get("id") in wanted]
    if not rules:
        return [], [], ["这个账套还没有周期规则"]
    active = [row for row in rules if _rule_active(row, year, month)]
    if not active:
        return [], [], ["没有规则覆盖 %s（检查规则的启用状态和起止年月）" % _period_label(year, month)]
    runs = _runs_of(ctx, book_id, [row.get("id") for row in active])
    resolved, problems = _resolve_subjects(ctx, book_id, active)
    plan, skipped = [], []
    for row in active:
        key = (row.get("id"), year, month)
        draft, problem = _build_draft(ctx, row, resolved, year, month)
        if draft is None:
            skipped.append({"id": row.get("id"), "name": _text(row.get("name")), "reason": problem})
            continue
        record = runs.get(key)
        if record is not None and not include_done:
            skipped.append({"id": row.get("id"), "name": _text(row.get("name")),
                            "reason": "%s 已记账（凭证号 %s）" % (_period_label(year, month),
                                                     _text(record.get("voucher_no")) or "未知"),
                            "done": True, "run_id": record.get("id")})
            continue
        plan.append({"rule": row, "draft": draft, "run": record})
    return plan, skipped, problems


def _execute(ctx, book_id, year, month, only, trigger):
    """执行计划里的规则，每条规则每月至多成功一次。

    顺序是**先占台账，再建凭证**，这是幂等能真正生效的关键：

    台账的 ``(book_id, rule_id, period_year, period_month)`` 唯一约束就是闸门。
    如果先建凭证后写台账，两个触发器（定时任务与手动执行）在同一刻命中同一条
    规则时，双方都可能在对方写入之前通过「查台账」，结果记两遍账。改成先插台账
    之后，第二个执行者会在这里撞唯一约束失败，被当成「本月已记」跳过，凭证
    流程根本不会走到。

    占位成功后再调宿主建凭证；建失败就把占位删掉，让这个月还能重试 —— 否则
    一条配置写错的规则会把这个月永久「占住」，改好了也记不上。
    """
    plan, skipped, problems = _plan(ctx, book_id, year, month, only)
    created, failed = [], []
    if problems:
        return {"created": created, "skipped": skipped, "failed": failed, "problems": problems}
    for item in plan:
        row, draft = item["rule"], item["draft"]
        rule_id = row.get("id")
        # 先占位。唯一约束在这里生效：撞了就说明这个月已经有执行者了。
        try:
            claimed = ctx.data.insert(RUN_TABLE, {
                "rule_id": rule_id,
                "period_year": year,
                "period_month": month,
                "voucher_id": None,
                "voucher_no": "",
                "voucher_date": draft["voucher_date"],
                "amount": row.get("amount"),
                "trigger": trigger,
                "remark": "%s %s %s" % (_period_label(year, month), _text(row.get("name")), trigger),
            }, book_id=book_id)
        except Exception:
            # 唯一约束冲突 = 这个月已经记过。读回那一行，把凭证号报给用户，
            # 免得页面上只显示一句没有指向的「失败」。
            record = _runs_of(ctx, book_id, [rule_id]).get((rule_id, year, month))
            if record is not None:
                skipped.append({"id": rule_id, "name": _text(row.get("name")),
                                "reason": "%s 已记账（凭证号 %s）" % (_period_label(year, month),
                                                         _text(record.get("voucher_no")) or "生成中"),
                                "done": True, "run_id": record.get("id")})
            else:
                failed.append({"id": rule_id, "name": _text(row.get("name")),
                               "reason": "占用本月记账名额失败，请稍后重试"})
            continue
        # 占位成功，建凭证。
        try:
            vouchers = ctx.voucher_batch_create(book_id, [draft])
        except HTTPException as exc:
            ctx.data.delete(RUN_TABLE, claimed["id"], book_id=book_id)
            failed.append({"id": rule_id, "name": _text(row.get("name")), "reason": _detail(exc)})
            continue
        except (ValueError, ArithmeticError) as exc:
            ctx.data.delete(RUN_TABLE, claimed["id"], book_id=book_id)
            failed.append({"id": rule_id, "name": _text(row.get("name")), "reason": str(exc)[:200]})
            continue
        voucher = vouchers[0] if vouchers else {}
        try:
            ctx.data.update(RUN_TABLE, claimed["id"], {
                "voucher_id": voucher.get("id"),
                "voucher_no": _text(voucher.get("voucher_no")),
            }, book_id=book_id)
        except Exception as exc:  # 凭证已建、台账回填失败：明确报出，不静默
            failed.append({"id": rule_id, "name": _text(row.get("name")),
                           "reason": "凭证已生成（%s），但台账回填失败：%s；"
                                     "本月不会重复记账，请到「执行记录」核对凭证号"
                                     % (_text(voucher.get("voucher_no")) or "?", str(exc)[:120])})
            continue
        created.append({"id": rule_id, "name": _text(row.get("name")),
                        "voucher_no": _text(voucher.get("voucher_no")),
                        "voucher_id": voucher.get("id"),
                        "amount": row.get("amount"), "voucher_date": draft["voucher_date"]})
    if created:
        ctx.audit_write("period_run")
    return {"created": created, "skipped": skipped, "failed": failed, "problems": problems}


def _detail(exc):
    """把宿主的错误还原成人能读的一句话。

    不用 ``getattr`` 取 detail：插件包的静态扫描把反射类调用列为不允许，
    写在这里会让整个包在安装阶段被拒。HTTPException 一定有 detail，所以按
    类型分支既安全又直白。
    """
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str):
            return detail[:200]
        if isinstance(detail, dict):
            return (_text(detail.get("message")) or "宿主拒绝了这次记账")[:200]
    return str(exc)[:200]
    return str(exc)[:200]


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------

def register(registrar, ctx):
    def chart(book_id: int):
        ctx.book_read(book_id)
        return ctx.subject_read(book_id)

    def list_rules(book_id: int):
        """规则 + 执行台账。前端一次拿全，切账套不再发第二个请求。"""
        ctx.book_read(book_id)
        rows = _rules_of(ctx, book_id)
        runs = ctx.data.read(RUN_TABLE, book_id=book_id, limit=500)
        return {
            "rules": [_present(row) for row in rows],
            "runs": [_present_run(row) for row in sorted(runs, key=lambda item: item.get("id") or 0, reverse=True)],
            "max_rules": MAX_RULES,
        }

    def save_rule(book_id: int, payload: dict):
        """新增或更新一条规则。写入 book 级表，需要账套写权限。"""
        ctx.book_write(book_id)
        ident = payload.get("id")
        cleaned = _clean_rule(payload)
        if ident is None:
            rows = _rules_of(ctx, book_id)
            if len(rows) >= MAX_RULES:
                _fail("一个账套最多 %d 条周期规则" % MAX_RULES)
            if any(_text(row.get("name")) == cleaned["name"] for row in rows):
                _fail("这个账套已经有同名规则「%s」" % cleaned["name"])
            created = ctx.data.insert(RULE_TABLE, cleaned, book_id=book_id)
            ctx.audit_write("rule_created")
            return _present(created)
        ident = int(ident)
        row = _rule_row(ctx, book_id, ident)
        if row is None:
            _fail("规则不存在，可能已被删除")
        for other in _rules_of(ctx, book_id):
            if other.get("id") != ident and _text(other.get("name")) == cleaned["name"]:
                _fail("这个账套已经有同名规则「%s」" % cleaned["name"])
        changed = ctx.data.update(RULE_TABLE, ident, cleaned, book_id=book_id)
        ctx.audit_write("rule_updated")
        return _present({**row, **cleaned, "id": ident, "updated": changed.get("updated")})

    def delete_rule(book_id: int, rule_id: int):
        ctx.book_write(book_id)
        row = _rule_row(ctx, book_id, int(rule_id))
        if row is None:
            _fail("规则不存在")
        runs = [item for item in ctx.data.read(RUN_TABLE, book_id=book_id, limit=500)
                if item.get("rule_id") == int(rule_id)]
        if runs:
            _fail("这条规则已经在 %d 个月记过账，先在「执行记录」里撤销对应记录再删除"
                  % len({(item.get("period_year"), item.get("period_month")) for item in runs}))
        ctx.data.delete(RULE_TABLE, int(rule_id), book_id=book_id)
        ctx.audit_write("rule_deleted")
        return {"deleted": 1, "name": _text(row.get("name"))}

    def preview(book_id: int, payload: dict):
        """试算：只算不记。页面在执行前先让人看清楚这个月要生成什么。"""
        ctx.book_read(book_id)
        year, month = _year_month(payload)
        plan, skipped, problems = _plan(ctx, book_id, year, month, only=payload.get("rule_ids"))
        return {
            "year": year, "month": month,
            "plans": [_present_plan(item) for item in plan],
            "skipped": skipped, "problems": problems,
        }

    def run(book_id: int, payload: dict):
        """手动执行。目标月份默认取当前月，可显式指定以补记之前的月份。"""
        ctx.book_write(book_id)
        year, month = _year_month(payload)
        only = payload.get("rule_ids")
        if only is not None and (not isinstance(only, list) or not all(isinstance(i, int) for i in only)):
            _fail("规则筛选参数不合法")
        result = _execute(ctx, book_id, year, month, only, "manual")
        result.update(year=year, month=month)
        return result

    def undo_run(book_id: int, run_id: int):
        """撤销一条执行记录。

        只删台账，**不动凭证**：凭证是账务事实，插件无权删。删完台账后这个月会
        重新变成「可生成」，用户可以在凭证页自己决定那张凭证去留。
        """
        ctx.book_write(book_id)
        run_id = int(run_id)
        target = None
        for row in ctx.data.read(RUN_TABLE, book_id=book_id, limit=500):
            if row.get("id") == run_id:
                target = row
                break
        if target is None:
            _fail("执行记录不存在")
        ctx.data.delete(RUN_TABLE, run_id, book_id=book_id)
        ctx.audit_write("run_undone")
        return {"deleted": 1, "rule_id": target.get("rule_id"),
                "voucher_no": _text(target.get("voucher_no"))}

    def on_period_close(event: dict):
        """结账后补记漏掉的月份。

        两个前提决定了这里能做什么、不能做什么：

        1. 结账事件在**期间锁定之后**才发出（宿主先 ``p.locked = True`` 再发
           事件），所以刚被封的那个月已经不能记账了。补记对象只能是**更早的
           月份** —— 用户一般是在月底封账，而���金、管理费这类支出往往要到下个
           月才补记凭证，正是这条路径的用武之地。
        2. 钩子事件只带 ``period_id``，插件读不到宿主的 period 表，也拿不到该
           期间的年月与序号。所以回补范围由**台账本身**决定：找出每条规则最近
           一次记账的年月，往前找缺口，从缺口往回补。年月直接存在台账行上，
           不用去猜账套的起始年。

        补不上的月份（早于任何台账记录、超过回补上限）不会静默跳过：凭证创建
        会被宿主以「已结账，不能记账」拒绝，失败原因落在规则的失败列表里，
        页面下次打开就能看到。
        """
        book_id = event.get("book_id")
        if not book_id:
            return
        try:
            ctx.book_write(book_id)
        except HTTPException:
            # 结账人不一定有该账套的写权限；钩子失败不能连累结账（宿主已把
            # 钩子包在 try 里，这里只是不再重复抛一次）。
            return
        runs = ctx.data.read(RUN_TABLE, book_id=book_id, limit=500)
        by_rule = {}
        for row in runs:
            by_rule.setdefault(row.get("rule_id"), []).append(
                (int(row.get("period_year") or 0), int(row.get("period_month") or 0)))
        today = _today()
        for row in _rules_of(ctx, book_id):
            if row.get("enabled") is False:
                continue
            done = by_rule.get(row.get("id")) or []
            latest = max(done) if done else None
            if latest is None:
                # 从没记过账：只有规则自己声明了起始年月才补，否则不猜。
                # start_label 形如 "2026-01"，是用户在页面上填的、宿主期间表里
                # 必然存在的月份；凭空往前推一年补房租比不补危险得多。
                start = _text(row.get("start_label"))
                parsed = START_LABEL_RE.fullmatch(start)
                if not parsed:
                    continue
                year, month = int(parsed.group(1)), int(parsed.group(2))
            else:
                year, month = latest
            # 从最近一次已记月份往后逐月补，最多回补 3 个月。
            gaps = 0
            for _step in range(4):
                if gaps > 3:
                    break
                year, month = (year, month + 1) if month < 12 else (year + 1, 1)
                if (year, month) > (today.year, today.month):
                    break
                if (year, month) in done:
                    continue
                gaps += 1
                _execute(ctx, book_id, year, month, [row.get("id")], "catchup")

    def monthly_posting(context):
        """宿主定时任务：每天扫描一次，把当月到期的规则都记上。

        为什么是每天而不是每月一次：规则自己带「每月第 N 天」，但这条任务在
        **每个账套的期间可能不同**时仍要能补 —— 跨账套共用一个调度点时，按天
        扫描 + 幂等闸门是最稳的组合，一天跑一次的成本只是几十行台账查询。

        只处理当月，且只处理执行身份有写权限的账套；权限在宿主那边实时校验，
        任务本身不会因为某本账权限不足而整批失败。

        任务在宿主里默认是停用的（管理员在「扩展管理 → 计划任务」里启用并指定
        执行身份后才真正跑）。没启用时这段代码根本不会执行。
        """
        today = _today()
        for book in ctx.book_list():
            if not book.get("can_write"):
                continue
            book_id = book["id"]
            try:
                ctx.book_write(book_id)
            except HTTPException:
                continue
            # 只补当月以及最多 3 个月内的缺口：跨月不补，避免停用半年后突然
            # 一次性生成半年的凭证，那种规模应该是人工确认过的。
            for back in (0, 1, 2, 3):
                year, month = _shift_month(today.year, today.month, -back)
                _execute(ctx, book_id, year, month, None, "scheduled")

    registrar.get("/chart", chart)
    registrar.get("/rules", list_rules)
    registrar.post("/rules", save_rule)
    registrar.post("/rules/{rule_id}/delete", delete_rule)
    registrar.post("/preview", preview)
    registrar.post("/run", run)
    registrar.post("/runs/{run_id}/undo", undo_run)
    registrar.hook("on_period_close", on_period_close)
    registrar.task("monthly_posting", monthly_posting)


# --------------------------------------------------------------------------
# 展示
# --------------------------------------------------------------------------

def _present(row):
    """规则回给前端时统一成普通 dict，避免 datetime 之类过不了 JSON。"""
    if row is None:
        return None
    return {
        "id": row.get("id"),
        "book_id": row.get("book_id"),
        "name": _text(row.get("name")),
        "day_of_month": int(row.get("day_of_month") or 1),
        "amount": _text(row.get("amount")) or "0.00",
        "entries": row.get("entries") or [],
        "remark": _text(row.get("remark")),
        "attachments": int(row.get("attachments") or 0),
        "enabled": row.get("enabled") is not False,
        "start_label": _text(row.get("start_label")),
        "end_label": _text(row.get("end_label")),
        "note": _text(row.get("note")),
        "sort_no": int(row.get("sort_no") or 0),
    }


def _present_run(row):
    return {
        "id": row.get("id"),
        "rule_id": row.get("rule_id"),
        "period_label": _period_label(int(row.get("period_year") or 0), int(row.get("period_month") or 0)),
        "voucher_id": row.get("voucher_id"),
        "voucher_no": _text(row.get("voucher_no")),
        "voucher_date": _text(row.get("voucher_date")),
        "amount": _text(row.get("amount")) or "0.00",
        "trigger": _text(row.get("trigger")),
        "remark": _text(row.get("remark")),
    }


def _present_plan(item):
    rule, draft = item["rule"], item["draft"]
    debit = sum(Decimal(entry["amount"]) for entry in draft["entries"] if entry["direction"] == "DEBIT")
    return {
        "rule_id": rule.get("id"),
        "name": _text(rule.get("name")),
        "day_of_month": int(rule.get("day_of_month") or 1),
        "amount": _text(rule.get("amount")),
        "voucher_date": draft["voucher_date"],
        "remark": draft["remark"],
        "debit_total": _money(debit),
        "entries": [{"direction": entry["direction"], "amount": entry["amount"],
                     "summary": entry["summary"], "subject_id": entry["subject_id"]}
                    for entry in draft["entries"]],
        "already_done": item.get("run") is not None,
    }
