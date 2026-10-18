"""门店报销明细工作簿 → 记账凭证。

SDK 1.1.0。后端入口必须是单文件：宿主用 spec_from_file_location 独立加载
backend/main.py，插件自身没有 sys.path，兄弟模块导入不可用。

职责边界：插件负责「读懂这张表、把关键词翻译成本账套的科目」；宿主负责账套
权限、凭证校验、事务、去重和来源追溯。插件不自行汇总业务口径，也不绕过
导入批次直接写库。
"""
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from fastapi import File, HTTPException, UploadFile
from openpyxl import load_workbook

# 科目规则（CONFIG_KEY）和表页映射（SHEETMAP_KEY）都是全插件一份，共用这张全局表。
# CONFIG_TABLE 仍保留在 manifest 里：宿主禁止升级时移除已登记的插件表，该表已建且为空。
CONFIG_TABLE = "plugin_f869d8c6ad46_config"
SHEETMAP_TABLE = "plugin_f869d8c6ad46_sheetmap"
CONFIG_KEY = "rules"
SHEETMAP_KEY = "sheets"
MARK = "cwq_reimburse"
RULES_VERSION = "reimburse-v1"

TEXT_COLUMNS = ("品名", "摘要", "名称", "项目", "内容")
AMOUNT_COLUMNS = ("金额", "合计/元", "合计", "报销金额")
DATE_COLUMNS = ("日期", "报销日期")
NOTE_COLUMNS = ("备注", "说明")

MONTH_RE = re.compile(r"(\d{1,2})\s*月份")
YEAR_MONTH_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
SLASH_DATE_RE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?")
TOKEN_AMOUNT_RE = re.compile(r"^(.*?)(\d+(?:\.\d+)?)$")
EXCLUDED_MARKERS = ("不包含", "不含", "没有", "未包含", "不计入")
TOTAL_MARKERS = ("合计", "总合计", "小计")
FOOTER_MARKERS = ("日期", "审批人意见", "审批意见", "负责人", "签字")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def _text(value):
    """把单元格收敛成可比较、可展示的字符串。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).replace("\n", " ").replace("\r", " ").strip()


def _norm(value):
    """全角转半角、去空白、转小写，用于表页名和关键词的包含匹配。"""
    folded = []
    for char in _text(value):
        code = ord(char)
        if code == 0x3000:
            continue
        if 0xFF01 <= code <= 0xFF5E:
            code -= 0xFEE0
        folded.append(chr(code))
    return "".join(folded).lower().replace(" ", "")


def _money(value):
    """金额统一成两位小数的十进制字符串。"""
    return str(value.quantize(Decimal("0.01")))


def _decimal(value):
    """把单元格读成 Decimal；空值或非法值返回 None，不猜测。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            return None
        return result if result.is_finite() else None
    raw = _text(value).replace(",", "").replace("，", "")
    raw = raw.replace("¥", "").replace("￥", "").replace("元", "").strip()
    if not raw:
        return None
    try:
        result = Decimal(raw)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _fail(message, status=422):
    raise HTTPException(status, message)


def _parse_date(value):
    """解析日期；2026/9/31 这类不存在的日期按无效处理，不静默改写成 9/30。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)
    if not raw:
        return None
    match = SLASH_DATE_RE.search(raw)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _iso_date(value):
    """把配置里的日期文本规整成 date；不可用时返回 None。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)
    return _parse_date(raw) if raw else None


# --------------------------------------------------------------------------
# 默认配置
# --------------------------------------------------------------------------

def _seed_subject_rules():
    """按这份报销表的真实词汇预置关键词规则，科目编码一律留空。

    规则语义：all 全部命中、any 命中其一、not 任一命中即排除。打分用命中词
    的总长度，长词优先，所以「保洁 + 工资」压过单独的「工资」；同分时先配置
    的规则胜出，规则顺序本身就是优先级。

    编码留空是刻意的：科目编码在不同账套并不通用，插件不替用户猜。前端从
    该账套真实科目表里给出候选，一键回填即可。
    """
    def rule(terms, note=""):
        return {"all": list(terms), "any": [], "not": [],
                "code": "", "counterparty_code": "", "note": note}

    return [
        rule(["医", "社保"], "社保"),
        rule(["社保"], "社保"),
        rule(["管家", "工资"], "管家工资"),
        rule(["保洁", "工资"], "保洁工资"),
        rule(["会计", "工资"], "会计工资"),
        rule(["出纳", "工资"], "出纳工资"),
        rule(["财务", "工资"], "财务工资"),
        rule(["工程", "工资"], "工程工资"),
        rule(["总监", "工资"], "总监工资"),
        rule(["运营"], "OTA 运营"),
        rule(["工资"], "工资"),
        rule(["补贴"], "补贴"),
        rule(["红包"], "节日红包"),
        rule(["电费"], "电费"),
        rule(["水费"], "水费"),
        rule(["话费"], "通讯费"),
        rule(["固话"], "通讯费"),
        rule(["洗涤费"], "洗涤费"),
        rule(["消杀"], "消杀"),
        rule(["白蚁"], "消杀"),
        rule(["保洁", "超房"], "保洁超房"),
        rule(["垃圾"], "垃圾清运"),
        rule(["易耗品"], "易耗品"),
        rule(["低值"], "易耗品"),
        rule(["工程用具"], "低值易耗品"),
        rule(["扫把"], "低值易耗品"),
        rule(["拖把"], "低值易耗品"),
        rule(["电池"], "低值易耗品"),
        rule(["围裙"], "低值易耗品"),
        rule(["洗地毯"], "洗涤费"),
        rule(["空气清新剂"], "低值易耗品"),
        rule(["清洗剂"], "低值易耗品"),
        rule(["矿泉水"], "低值易耗品"),
        rule(["布草"], "低值易耗品"),
        rule(["四件套"], "低值易耗品"),
        rule(["清洗空调"], "修理费"),
        rule(["换", "玻璃"], "修理费"),
        rule(["拜拜"], "待确认"),
        rule(["工程款"], "工程款"),
        rule(["修理"], "修理费"),
        rule(["租电脑"], "固定资产/租赁"),
        rule(["家具"], "低值易耗品"),
        rule(["马桶垫"], "低值易耗品"),
        rule(["防滑贴"], "低值易耗品"),
        rule(["提示牌"], "低值易耗品"),
        rule(["标示贴"], "低值易耗品"),
        rule(["贴纸"], "低值易耗品"),
        rule(["防蚊窗网"], "低值易耗品"),
        rule(["水龙头"], "低值易耗品"),
        rule(["洗衣机"], "固定资产"),
        rule(["对讲机"], "低值易耗品"),
        rule(["保护膜"], "低值易耗品"),
        rule(["消防培训"], "培训费"),
        rule(["卫生检测"], "检测费"),
        rule(["印章"], "办公费"),
        rule(["公车维修"], "车辆费用"),
        rule(["停车"], "车辆费用"),
        rule(["搬运"], "搬运费"),
        rule(["工作茶"], "办公费"),
        rule(["刷单"], "刷单支出"),
        rule(["佣金"], "佣金支出"),
        rule(["删差评"], "平台服务费"),
        rule(["房型不对"], "客人补偿"),
        rule(["未入住退款"], "退款冲减"),
        rule(["转公户"], "转入公司账户"),
    ]


def _default_rules():
    """全局科目规则默认值。对方科目默认 1001。

    各账套的科目编码一致，所以规则只有一份，不按账套拆分。
    """
    return {
        "counterparty_code": "1001",
        "pending_code": "",
        "unmatched_mode": "pending",
        "default_code": "",
        "default_mark_pending": True,
        "date_mode": "default",
        "default_date": "",
        "remark_template": "{sheet}报销明细",
        "attachments": 1,
        "merge_same_subject": False,
        "split_packed": "auto",
        "subject_rules": _seed_subject_rules(),
    }



def _default_sheetmap():
    """表页关键词 → 账套名称，默认空，由用户在页面上配置后保存。"""
    return {"rules": []}


# --------------------------------------------------------------------------
# 关键词规则匹配
# --------------------------------------------------------------------------

def _rule_terms(rule):
    """把规则收敛成 (all, any, not) 三组归一化关键词。"""
    def terms(raw):
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            value = _norm(item)
            if value and value not in out:
                out.append(value)
        return out

    if not isinstance(rule, dict):
        return [], [], []
    return terms(rule.get("all")), terms(rule.get("any")), terms(rule.get("not"))


def _match_rule(text, rules):
    """返回 (规则, 下标, 得分)；没有命中返回 (None, -1, 0)。"""
    needle = _norm(text)
    if not needle:
        return None, -1, 0
    best = (None, -1, 0)
    for index, rule in enumerate(rules or []):
        all_terms, any_terms, not_terms = _rule_terms(rule)
        if not all_terms and not any_terms:
            continue
        if any(term in needle for term in not_terms):
            continue
        if all_terms and not all(term in needle for term in all_terms):
            continue
        if any_terms and not any(term in needle for term in any_terms):
            continue
        score = sum(len(term) for term in all_terms) + sum(len(term) for term in any_terms)
        if score > best[2]:
            best = (rule, index, score)
    return best


def _conflicting_codes(text, rules):
    """同一段文字命中的其他「不同科目编码」规则，用来标出需要人工确认的行。"""
    needle = _norm(text)
    codes = []
    for rule in rules or []:
        all_terms, any_terms, not_terms = _rule_terms(rule)
        if not all_terms and not any_terms:
            continue
        if any(term in needle for term in not_terms):
            continue
        if all_terms and not all(term in needle for term in all_terms):
            continue
        if any_terms and not any(term in needle for term in any_terms):
            continue
        code = _text(rule.get("code"))
        if code and code not in codes:
            codes.append(code)
    return codes


# --------------------------------------------------------------------------
# 工作簿解析
# --------------------------------------------------------------------------

def _cell_rows(sheet):
    """把工作表读成二维文本表，丢掉完全空白的行。"""
    rows = []
    for row in sheet.iter_rows(values_only=True):
        values = [_text(cell) for cell in row]
        while values and values[-1] == "":
            values.pop()
        if any(value != "" for value in values):
            rows.append(values)
    return rows


def _find_header(rows):
    """定位表头行，返回 (行号, {列名: 列号})；不是报销表返回 None。"""
    names = [_norm(name) for name in TEXT_COLUMNS + AMOUNT_COLUMNS + DATE_COLUMNS + NOTE_COLUMNS]
    for index, row in enumerate(rows[:12]):
        cells = [_norm(cell) for cell in row]
        filled = [cell for cell in cells if cell]
        if not filled:
            continue
        if not any(any(cell in name or name in cell for name in names[:4]) for cell in filled):
            continue
        if not any(any(cell in name or name in cell for name in names[4:8]) for cell in filled):
            continue
        columns = {}
        for position, cell in enumerate(row):
            key = _norm(cell)
            if not key:
                continue
            for name, label in zip(names, TEXT_COLUMNS + AMOUNT_COLUMNS + DATE_COLUMNS + NOTE_COLUMNS):
                if key.startswith(name) or name in key:
                    columns.setdefault(label, position)
        return index, columns
    return None


def _first(columns, labels):
    for label in labels:
        if label in columns:
            return columns[label]
    return None


def _pick(row, position):
    if position is None or position >= len(row):
        return ""
    return row[position]


def _split_packed_tokens(text, total):
    """摘要型表页的一格里可能塞了多个「项目 + 金额」。

    只有拆出来的金额之和与该行合计**完全相等**时才拆分。这份表里确实存在
    「摘要写了 3 个数、合计却是另一个数」的行（摘要 1158.76 / 合计 4258.79），
    那种行不拆，按整行处理并提示人工确认，绝不替用户编造拆分金额。
    """
    chunks = [chunk for chunk in re.split(r"\s+", _text(text)) if chunk]
    if len(chunks) <= 1:
        return None
    parsed = []
    for chunk in chunks:
        match = TOKEN_AMOUNT_RE.match(chunk)
        if not match:
            return None
        name = match.group(1).strip("：:、,，")
        value = _decimal(match.group(2))
        if value is None:
            return None
        parsed.append((name or _text(text), value))
    if sum((value for _name, value in parsed), Decimal("0")) != total:
        return None
    return parsed


def _looks_packed(text):
    """摘要看起来是「多个项目 + 金额」但没能对上行合计。

    只有这种才值得提示：单个「易耗品6678」本来就该是一行，提示它是噪音。
    """
    chunks = [chunk for chunk in re.split(r"\s+", _text(text)) if chunk]
    if len(chunks) <= 1:
        return False
    return all(TOKEN_AMOUNT_RE.match(chunk) for chunk in chunks)


def _sheet_month(title):
    """从标题取月份。「优客7月份报销明细单」这类与表页名对不上的会被抓出来。"""
    match = YEAR_MONTH_RE.search(title)
    if match:
        return int(match.group(2)), int(match.group(1))
    match = MONTH_RE.search(title)
    if match:
        month = int(match.group(1))
        if 1 <= month <= 12:
            return month, None
    return None, None


def _read_items(rows, header_index, columns, date_at, split_packed, packed_layout):
    """把表头以下的数据行读成明细项。"""
    text_at = _first(columns, TEXT_COLUMNS)
    amount_at = _first(columns, AMOUNT_COLUMNS)
    note_at = _first(columns, NOTE_COLUMNS)
    items, declared, warnings = [], None, []
    for line, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        first = _norm(_pick(row, 0))
        if first and any(first.startswith(_norm(marker)) for marker in TOTAL_MARKERS):
            value = _decimal(_pick(row, amount_at))
            if value is not None and declared is None:
                declared = value
            continue
        text = _pick(row, text_at)
        note = _pick(row, note_at)
        amount = _decimal(_pick(row, amount_at))
        if not text and amount is None:
            continue
        if amount is None:
            # 「日期：」「审批人意见：」这类签字栏不是数据行，静默跳过。
            if not any(first.startswith(_norm(marker)) for marker in FOOTER_MARKERS):
                warnings.append({"line": line, "text": text[:40], "message": "该行没有金额，已跳过"})
            continue
        excluded = any(marker in _text(note) for marker in EXCLUDED_MARKERS)
        packed = _split_packed_tokens(text, amount) if packed_layout and split_packed == "auto" else None
        if packed:
            for name, value in packed:
                items.append({"line": line, "text": name, "amount": value,
                              "excluded": excluded, "date": _text(_pick(row, date_at))})
        elif packed_layout and split_packed == "auto" and _looks_packed(text):
            warnings.append({"line": line, "text": text[:40],
                             "message": "摘要里的金额与行合计对不上，按整行处理"})
        else:
            items.append({"line": line, "text": text, "amount": amount,
                          "excluded": excluded, "date": _text(_pick(row, date_at))})
    return items, declared, warnings


def _read_sheet(sheet, split_packed):
    """解析单个表页。隐藏表页和识别不出的表页也读出来，标明不纳入即可。"""
    result = {"name": sheet.title, "state": sheet.sheet_state, "items": [], "warnings": []}
    if sheet.sheet_state != "visible":
        result.update(status="skipped", reason="隐藏表页")
        return result
    rows = _cell_rows(sheet)
    if not rows:
        result.update(status="skipped", reason="表页没有内容")
        return result
    result["title"] = _text(rows[0][0]) if rows and rows[0] else ""
    month, year = _sheet_month(result["title"])
    result["month"], result["year"] = month, year
    found = _find_header(rows)
    if not found:
        result.update(status="skipped", reason="未识别为报销明细表页")
        return result
    header_index, columns = found
    result["layout"] = "packed" if "摘要" in columns else "item"
    result["header_line"] = header_index + 1
    date_at = _first(columns, DATE_COLUMNS) if result["layout"] == "packed" else None
    items, declared, warnings = _read_items(rows, header_index, columns, date_at, split_packed,
                                            result["layout"] == "packed")
    if date_at is not None:
        invalid = sorted({item["date"] for item in items
                          if item["date"] and _parse_date(item["date"]) is None})
        if invalid:
            warnings.append({"line": 0, "text": "、".join(invalid[:3]),
                             "message": "表内日期无效，将改用配置的默认凭证日期"})
    result["items"] = items
    result["declared_total"] = _money(declared) if declared is not None else None
    result["warnings"] = warnings
    total = sum((item["amount"] for item in items), Decimal("0"))
    result["count"] = len(items)
    result["total"] = _money(total)
    if total == 0:
        result.update(status="empty", reason="没有可记账的明细金额")
        return result
    if declared is not None and declared != total:
        result["warnings"].append({
            "line": 0, "text": "",
            "message": "明细合计 " + _money(total) + " 与表内合计 " + _money(declared) +
                       " 相差 " + _money(total - declared) + "（可能是有意不计入合计的行）"})
    result["status"] = "ready"
    return result


def _read_workbook(content, split_packed="auto"):
    """读工作簿。隐藏表页、无明细表页照样读出来，由调用方决定纳不纳入。"""
    try:
        workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # openpyxl 抛的异常类型不稳定，只给用户可读信息
        _fail("无法读取该文件，请确认是未加密的 .xlsx：" + _text(exc))
    try:
        return [_read_sheet(sheet, split_packed) for sheet in workbook.worksheets]
    finally:
        workbook.close()


# --------------------------------------------------------------------------
# 配置装载
# --------------------------------------------------------------------------

def _clean_rules(raw):
    """收敛规则结构，丢掉空关键词，避免脏数据进入配置。"""
    out = []
    for rule in raw or []:
        if not isinstance(rule, dict):
            continue
        all_terms, any_terms, not_terms = _rule_terms(rule)
        if not all_terms and not any_terms:
            continue
        out.append({"all": all_terms, "any": any_terms, "not": not_terms,
                    "code": _text(rule.get("code")), "counterparty_code": _text(rule.get("counterparty_code")),
                    "note": _text(rule.get("note"))[:60]})
    return out


def _merge(defaults, saved):
    saved = saved if isinstance(saved, dict) else {}
    result = dict(defaults)
    result.update(saved)
    if not isinstance(result.get("subject_rules"), list):
        result["subject_rules"] = defaults["subject_rules"]
    return result


def _global_row(ctx, key):
    for row in ctx.data.read(SHEETMAP_TABLE, limit=10):
        if row.get("key") == key:
            return row
    return None


def _rules(ctx):
    """科目规则是全插件一份，存在全局表里（key=rules），写它需要管理员。"""
    row = _global_row(ctx, CONFIG_KEY)
    if row is None or not isinstance(row.get("value"), dict):
        return _default_rules(), row
    return _merge(_default_rules(), row["value"]), row


def _put_global(ctx, key, value):
    row = _global_row(ctx, key)
    if row is None:
        ctx.data.insert(SHEETMAP_TABLE, {"key": key, "value": value, "version": 1})
        return 1
    version = int(row.get("version") or 0) + 1
    ctx.data.update(SHEETMAP_TABLE, row["id"], {"value": value, "version": version})
    return version


def _sheetmap_row(ctx):
    return _global_row(ctx, SHEETMAP_KEY)



def _sheetmap(ctx):
    row = _sheetmap_row(ctx)
    if row is None or not isinstance(row.get("value"), dict):
        return _default_sheetmap(), row
    return row["value"], row


def _resolve_book(target, books):
    """账套名称 → 账套：先精确匹配，再按去空格包含匹配。"""
    if not target:
        return None
    needle = _norm(target)
    if not needle:
        return None
    for book in books:
        if _norm(book["name"]) == needle:
            return book
    hits = [book for book in books
            if needle in _norm(book["name"]) or needle in _norm(book.get("company_name") or "")]
    return hits[0] if len(hits) == 1 else None


def _match_sheet(name, sheetmap):
    """表页名 → 账套规则；同长度取先配置的那条。"""
    needle = _norm(name)
    best = (None, 0)
    for rule in sheetmap.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        keyword = _norm(rule.get("keyword"))
        if not keyword or keyword not in needle:
            continue
        if len(keyword) > best[1]:
            best = (rule, len(keyword))
    return best[0]


def _subject_index(subjects):
    return {_text(subject["code"]): subject for subject in subjects}


def _resolve_codes(index, wanted):
    """把 (用途, 科目编码) 批量换成 subject_id，同一编码只报一次问题。

    区分「不存在」「已停用」「不是末级」三种情况，用户才知道该去改什么。
    """
    labels = {}
    for label, code in wanted:
        code = _text(code)
        if code and code not in labels:
            labels[code] = _text(label)
    resolved, problems = {}, []
    for code, label in labels.items():
        subject = index.get(code)
        if subject is None:
            problems.append("科目编码 %s（%s）在本账套不存在" % (code, label or "未标注用途"))
            continue
        if not subject.get("active"):
            problems.append("科目 %s（%s）已停用：%s" % (code, label or "未标注用途", _text(subject.get("name"))))
            continue
        if not subject.get("is_leaf"):
            problems.append("科目 %s（%s）不是末级科目：%s"
                            % (code, label or "未标注用途", _text(subject.get("name"))))
            continue
        resolved[code] = subject["id"]
    return resolved, problems


# --------------------------------------------------------------------------
# 凭证生成
# --------------------------------------------------------------------------

def _fill_template(template, sheet):
    """摘要模板只替换已知占位符；模板写错时退回表页名，不让整批解析失败。"""
    text = _text(template) or "{sheet}报销明细"
    month = sheet.get("month")
    try:
        return text.format(sheet=sheet["name"], title=sheet.get("title") or sheet["name"],
                           month=str(month) if month else "", book="").strip()
    except (KeyError, IndexError, ValueError):
        return sheet["name"]


def _voucher_date(sheet, config, problems, warnings, override=None):
    """凭证日期：本次统一指定的日期 > 账套配置日期 > 表内日期。

    统一指定是最高优先级：一次报销文件分发给多个账套时，日期只设一次，
    本次生成的所有凭证都用它，不会因为各账套配置不同而分叉。
    """
    if override is not None:
        return override
    configured = _iso_date(config.get("default_date"))
    if _text(config.get("date_mode")) == "sheet":
        for item in sheet.get("items") or []:
            parsed = _parse_date(item.get("date"))
            if parsed:
                return parsed
        warnings.append({"line": 0, "text": sheet["name"], "message": "表内没有可用日期，改用配置日期"})
    if configured is None:
        problems.append("请先在规则里配置默认凭证日期，或在分发时指定凭证日期")
    elif sheet.get("month") and configured.month != sheet["month"]:
        warnings.append({"line": 0, "text": sheet.get("title") or sheet["name"],
                         "message": "标题月份与凭证日期的月份不一致，请确认是不是本月报销"})
    return configured


def _plan_items(sheet, config):
    """逐条明细决定科目与是否待确认；这里只做判断，不碰宿主数据。"""
    rules = config.get("subject_rules") or []
    plan = []
    for item in sheet.get("items") or []:
        if item["amount"] == 0:
            continue
        matched, _index, _score = _match_rule(item["text"], rules)
        code = _text(matched.get("code")) if isinstance(matched, dict) else ""
        counterparty = _text(matched.get("counterparty_code")) if isinstance(matched, dict) else ""
        reasons = []
        if matched is None:
            reasons.append("没有匹配的关键词规则")
        elif not code:
            reasons.append("关键词规则没有配置科目编码")
        elif len(_conflicting_codes(item["text"], rules)) > 1:
            reasons.append("同一行命中多个不同科目关键词")
        plan.append({"item": item, "code": code if not reasons else "",
                     "counterparty": counterparty, "reasons": reasons,
                     "label": "「%s」第 %s 行" % (item["text"][:16], item["line"])})
    return plan


def _fallback(config):
    """未命中时用哪个科目，以及落到该科目后还要不要标记待确认。

    两种口径由账套自己定：
    - pending（默认）：未命中的行进「待确认科目」，预览里黄色高亮，必须看过；
    - default：未命中的行直接记到「默认科目」，是否仍高亮由 default_mark_pending 决定。
    """
    mode = _text(config.get("unmatched_mode")) or "pending"
    default_code = _text(config.get("default_code"))
    pending_code = _text(config.get("pending_code"))
    if mode == "default":
        return True, default_code, bool(config.get("default_mark_pending"))
    return False, pending_code, True


def _build_voucher(sheet, config, index, problems, override=None):
    """一个表页一张凭证：借方按明细，贷方按对方科目归集。"""
    warnings = []
    voucher_date = _voucher_date(sheet, config, problems, warnings, override)
    if voucher_date is None:
        return None, warnings
    default_code = _text(config.get("counterparty_code"))
    use_default, fallback_code, mark_pending = _fallback(config)
    if use_default and not fallback_code:
        problems.append("未命中处理设为「使用默认科目」，但没有配置默认科目")
    if not use_default and not fallback_code:
        problems.append("请先在规则里配置待确认科目")
    if not default_code:
        problems.append("请先在规则里配置对方科目")
    plan = _plan_items(sheet, config)
    if not plan:
        return None, warnings

    wanted = [("对方科目", default_code)]
    if use_default:
        wanted.append(("未命中的默认科目", fallback_code))
    else:
        wanted.append(("待确认科目", fallback_code))
    resolved_entry = {}
    for entry in plan:
        if entry["reasons"]:
            if use_default:
                entry["code"] = fallback_code
                if not mark_pending:
                    entry["reasons"] = []
            else:
                entry["code"] = fallback_code
        else:
            entry["reasons"] = []
        wanted.append((entry["label"] + " 的借方科目", entry["code"]))
        if entry["counterparty"]:
            wanted.append((entry["label"] + " 的对方科目", entry["counterparty"]))
    resolved, issues = _resolve_codes(index, wanted)
    problems.extend(issues)
    if problems:
        return None, warnings

    slots, order, credits = {}, [], {}
    defaulted = 0
    for entry in plan:
        item = entry["item"]
        subject_id = resolved[entry["code"]]
        counterparty_id = resolved[entry["counterparty"]] if entry["counterparty"] else resolved[default_code]
        if entry["reasons"]:
            defaulted += 1
        merge = bool(config.get("merge_same_subject")) and not entry["reasons"]
        key = (subject_id, item["text"], None if merge else item["line"])
        slot = slots.get(key)
        if slot is None:
            slot = {"subject_id": subject_id, "amount": Decimal("0"),
                    "summary": item["text"][:256], "line": item["line"], "reasons": []}
            slots[key] = slot
            order.append(slot)
        slot["amount"] += item["amount"]
        for reason in entry["reasons"]:
            if reason not in slot["reasons"]:
                slot["reasons"].append(reason)
        credits[counterparty_id] = credits.get(counterparty_id, Decimal("0")) + item["amount"]

    def marker(line, reasons, extra):
        payload = {"sheet": sheet["name"], "line": line, "pending": bool(reasons)}
        if reasons:
            payload["reasons"] = reasons
        payload.update(extra)
        return {MARK: payload}

    entries, total = [], Decimal("0")
    for slot in order:
        total += slot["amount"]
        entries.append({"subject_id": slot["subject_id"], "direction": "DEBIT",
                        "amount": _money(slot["amount"]), "summary": slot["summary"],
                        "aux_ref": marker(slot["line"], slot["reasons"], {})})
    for subject_id, amount in credits.items():
        entries.append({"subject_id": subject_id, "direction": "CREDIT", "amount": _money(amount),
                        "summary": sheet["name"][:256],
                        "aux_ref": marker(0, [], {"counterparty": True})})
    attachments = config.get("attachments")
    draft = {
        "voucher_date": voucher_date.isoformat(),
        "remark": _fill_template(config.get("remark_template"), sheet)[:120],
        "attachments": attachments if isinstance(attachments, int) and not isinstance(attachments, bool)
                       and 0 <= attachments <= 99 else 1,
        "entries": entries,
    }
    meta = {"sheet": sheet["name"], "title": sheet.get("title") or sheet["name"],
            "total": _money(total), "count": len(order),
            "pending": sum(1 for slot in order if slot["reasons"]),
            "defaulted": defaulted,
            "fallback_mode": "default" if use_default else "pending",
            "warnings": warnings}
    return {"draft": draft, "meta": meta}, warnings


# --------------------------------------------------------------------------
# 分析（不落库）
# --------------------------------------------------------------------------

def _analyze(ctx, content, split_packed):
    """读文件并给出每个表页的归属与金额。这一步不创建任何导入批次。"""
    books = ctx.book_list()
    sheetmap, _row = _sheetmap(ctx)
    sheets = _read_workbook(content, split_packed)
    for sheet in sheets:
        if sheet["status"] != "ready":
            continue
        rule = _match_sheet(sheet["name"], sheetmap)
        if rule is None:
            sheet.update(status="unmatched", reason="没有匹配的表页关键词")
            continue
        book = _resolve_book(rule.get("book"), books)
        if book is None:
            sheet.update(status="unmatched",
                        reason="表页关键词「%s」没有对应到可访问的账套" % _text(rule.get("keyword")))
            continue
        sheet.update(status="matched", book_id=book["id"], book_name=book["name"],
                     book_keyword=_text(rule.get("keyword")), can_write=bool(book.get("can_write")))
    for sheet in sheets:
        # items 里的金额对前端没有意义，报表只带汇总，避免把整个文件塞回浏览器。
        sheet.pop("items", None)
    return {"sheets": sheets, "books": books}


def _targets_for_book(sheets, sheetmap, books, book_id):
    """当前账套应当纳入的表页名。"""
    names = []
    for sheet in sheets:
        if sheet["status"] != "ready":
            continue
        rule = _match_sheet(sheet["name"], sheetmap)
        target = _resolve_book(rule.get("book") if rule else "", books)
        if rule is not None and target is not None and target["id"] == book_id:
            names.append(sheet["name"])
    return names


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------

def register(registrar, ctx):
    def chart(book_id: int):
        ctx.book_read(book_id)
        return ctx.subject_read(book_id)

    def suggest(book_id: int):
        """按关键词给科目候选。候选来自该账套真实科目表，不是编造编码。

        关键词和科目名很少逐字相等（规则写「社保」，科目叫「社会保险费」），
        所以按汉字重合度打分：重合越高越靠前，名称越短越具体。
        """
        ctx.book_read(book_id)
        config, _row = _rules(ctx)
        subjects = [s for s in ctx.subject_read(book_id) if s.get("active") and s.get("is_leaf")]
        out = []
        for position, rule in enumerate(config.get("subject_rules") or []):
            all_terms = _rule_terms(rule)[0]
            chars = sorted({char for term in all_terms for char in term})
            ranked = []
            if chars:
                for subject in subjects:
                    name = _norm(subject.get("name"))
                    if not name:
                        continue
                    hits = sum(1 for char in chars if char in name)
                    ratio = hits / len(chars)
                    if ratio >= 0.5:
                        ranked.append((-ratio, len(name), name, subject))
                ranked.sort(key=lambda item: item[:3])
            out.append({"index": position, "keywords": all_terms,
                        "code": _text(rule.get("code")),
                        "candidates": [{"code": hit[3]["code"], "name": hit[3]["name"]}
                                       for hit in ranked[:5]]})
        return out

    def rules():
        config, row = _rules(ctx)
        sheetmap, sheetmap_row = _sheetmap(ctx)
        return {"config": config,
                "config_version": int(row.get("version") or 0) if row else 0,
                "sheetmap": sheetmap,
                "sheetmap_row_id": sheetmap_row.get("id") if sheetmap_row else None,
                "sheetmap_version": int(sheetmap_row.get("version") or 0) if sheetmap_row else 0}

    def save_config(payload: dict):
        """科目规则全局共用，存在全局表里，写入需要管理员；非管理员会收到 403。"""
        merged = _merge(_default_rules(), payload.get("config"))
        merged["subject_rules"] = _clean_rules(merged.get("subject_rules"))
        version = _put_global(ctx, CONFIG_KEY, merged)
        ctx.audit_write("rules_saved")
        return {"version": version, "value": merged}


    def save_sheetmap(payload: dict):
        """表页映射是全局表，写入需要管理员；非管理员会收到 403。"""
        raw = payload.get("rules")
        if not isinstance(raw, list) or len(raw) > 200:
            _fail("表页映射最多 200 条")
        cleaned = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            keyword = _text(item.get("keyword"))
            if not keyword or len(keyword) > 40:
                _fail("每条表页映射需要 1 到 40 字的关键词")
            cleaned.append({"keyword": keyword, "book": _text(item.get("book")),
                            "enabled": item.get("enabled") is not False})
        value = {"rules": cleaned}
        _put_global(ctx, SHEETMAP_KEY, value)
        ctx.audit_write("sheetmap_saved")
        return {"sheetmap": value, "count": len(cleaned)}

    def analyze(files: list[UploadFile] = File(...)):
        """上传后先看解析结果：哪个表页归哪个账套、多少金额、哪里要人工确认。"""
        if not files:
            _fail("请选择报销明细文件")
        if len(files) > 1:
            _fail("一次只解析一个工作簿")
        payload = ctx.upload_read(files[0])
        content = payload["content"] if isinstance(payload, dict) else payload
        if not content[:2] == b"PK":
            _fail("请上传 .xlsx 文件（当前不支持 .xls）")
        return _analyze(ctx, content, "auto")

    def parse(ident: str, payload: dict):
        """宿主导入批次的解析入口：一个批次一本账套，只收映射到该账套的表页。"""
        batch = ctx.imports.read(ident)
        book_id = batch["book_id"]
        sources = batch.get("files") or []
        if not sources:
            _fail("批次里没有源文件")
        content = ctx.imports.source(ident, sources[0]["id"])["content"]
        books = ctx.book_list()
        book = next((item for item in books if item["id"] == book_id), None)
        if book is None:
            _fail("无权访问该账套")
        if not book.get("can_write"):
            _fail("该账套是只读的，不能生成凭证")
        config, snapshot = _rules(ctx)
        sheetmap, _row = _sheetmap(ctx)
        override = None
        if payload.get("voucher_date"):
            override = _iso_date(payload.get("voucher_date"))
            if override is None:
                _fail("凭证日期格式不对，请用 YYYY-MM-DD")
        sheets = _read_workbook(content, config.get("split_packed") or "auto")
        wanted = payload.get("sheets")
        if wanted is None:
            wanted = _targets_for_book(sheets, sheetmap, books, book_id)
        if not isinstance(wanted, list) or not wanted:
            _fail("没有选择要生成凭证的表页")
        if len(wanted) > 200:
            _fail("单批表页过多，请拆分文件")
        index = _subject_index(ctx.subject_read(book_id))
        by_name = {sheet["name"]: sheet for sheet in sheets}
        limits = ctx.imports.limits()
        vouchers, metas, locations, problems = [], [], [], []
        for name in wanted:
            name = _text(name)
            sheet = by_name.get(name)
            if sheet is None:
                problems.append("文件里没有表页「%s」" % name)
                continue
            if sheet["status"] != "ready":
                problems.append("表页「%s」不生成凭证：%s" % (name, sheet.get("reason") or sheet["status"]))
                continue
            rule = _match_sheet(name, sheetmap)
            target = _resolve_book(rule.get("book") if rule else "", books)
            if rule is None or target is None:
                problems.append("表页「%s」没有配置表页关键词映射" % name)
                continue
            if target["id"] != book_id:
                problems.append("表页「%s」映射到账套「%s」，不在本批次" % (name, target["name"]))
                continue
            built, warnings = _build_voucher(sheet, config, index, problems, override)
            if built is None:
                problems.append("表页「%s」没有生成凭证，请先解决上面的科目或日期配置" % name)
                continue
            built["meta"]["warnings"] = warnings + sheet.get("warnings", [])
            vouchers.append(built["draft"])
            metas.append(built["meta"])
            locations.append({"file_id": sources[0]["id"], "sheet": name})
        if problems:
            _fail({"message": "有 %d 处需要先处理" % len(problems), "problems": problems[:20]})
        cap = min(int(limits.get("max_vouchers") or 5000), int(limits.get("max_rows") or 20000))
        if len(vouchers) > cap:
            _fail("生成的凭证数超过本批上限 %d，请减少表页" % cap)
        return ctx.imports.preview(
            ident, vouchers, revision=payload.get("revision"),
            configuration={"rules": RULES_VERSION, "refs": [snapshot], "metas": metas,
                           "locations": locations, "book_id": book_id,
                           "book_name": _text(book.get("name"))})

    registrar.get("/chart", chart)
    registrar.get("/suggest", suggest)
    registrar.get("/rules", rules)
    registrar.post("/rules", save_config)
    registrar.post("/sheetmap", save_sheetmap)
    registrar.post("/analyze", analyze)
    registrar.post("/parse/{ident}", parse)
