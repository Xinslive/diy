"""从电子发票 PDF / OCR 文本中提取结构化字段。

设计要点：
- 每页一张"电子发票（普通发票）"，头部锚点统一（购/销方信息、项目表头、合 计、价税合计）。
- 项目明细区段在「项目名称 规格型号 ...」表头之后、「合 计/价税合计」之前。
- 字段抽取以"标签 anchor + 正则"为主，避免依赖按行位置。
- 解析失败不抛异常，而是把 parse_warning 标到返回结构里，由前端提示用户人工核对。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple

try:
    import pypdfium2 as p
except ImportError:  # pragma: no cover - 运行环境通过 requirements 安装
    p = None


# ---------- 文本提取 ----------

def extract_pages_text(pdf_path: str) -> List[str]:
    """按页读取纯文本。失败时返回空字符串占位。"""
    if p is None:
        raise RuntimeError("缺少 pypdfium2 依赖，请安装后再解析 PDF")
    doc = p.PdfDocument(pdf_path)
    out: List[str] = []
    for i in range(len(doc)):
        try:
            tp = doc[i].get_textpage()
            out.append(tp.get_text_range() or "")
        except Exception:
            out.append("")
    return out


# ---------- 数据模型 ----------

@dataclass
class InvoiceLine:
    raw: str = ""
    name: str = ""              # 项目名（含 *类别*前缀）
    spec: str = ""              # 规格型号
    unit: str = ""              # 单位
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None    # 不含税金额（可能为负）
    tax_rate: str = ""          # "13%" / "1%" / "不征税"
    tax: Optional[float] = None
    is_discount: bool = False   # 金额为负且数量/单价为空 → 折扣行


@dataclass
class InvoiceMeta:
    invoice_no: str = ""
    issue_date: str = ""        # YYYY-MM-DD
    issue_date_raw: str = ""
    buyer_name: str = ""
    buyer_tax_id: str = ""
    seller_name: str = ""
    seller_tax_id: str = ""
    total_excl_tax: Optional[float] = None
    total_tax: Optional[float] = None
    total_incl_tax: Optional[float] = None
    total_in_c_c: str = ""
    remark: str = ""
    issuer: str = ""


@dataclass
class Invoice:
    page_index: int
    meta: InvoiceMeta = field(default_factory=InvoiceMeta)
    lines: List[InvoiceLine] = field(default_factory=list)
    raw_text: str = ""
    parse_warning: str = ""

    def to_dict(self) -> dict:
        return {
            "page_index": self.page_index,
            "meta": asdict(self.meta),
            "lines": [asdict(l) for l in self.lines],
            "parse_warning": self.parse_warning,
        }


# ---------- 头部正则 ----------

RE_INVOICE_NO = re.compile(r"(?<!\d)(\d{20})(?!\d)")
RE_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
RE_TAX_ID = re.compile(r"\b([0-9A-Z]{18,20})\b")
RE_AMOUNT = re.compile(r"(?:¥|￥)\s*(\d+(?:\.\d+)?)")
RE_ISSUER = re.compile(r"开票人[:：]\s*(\S+)")
RE_TABLE_HEADER = re.compile(r"项目名称\s*规\s*格\s*型\s*号")
RE_BIG_AMOUNT_HEADER = re.compile(r"价税合计（大写）\s*（小写）")
RE_BIG_AMOUNT_LINE = re.compile(r"([零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆角分整]+)\s*¥\d+(?:\.\d+)?")
END_MARKERS = ("合 计", "价税合计", "合计：", "价税合计（大写）")
# 末尾：金额 + 税率（不征税/免税/%）+ 可选税额
TAIL_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s+(\d+%|不征税|免税)(?:\s+(-?\d+(?:\.\d+)?))?"
)
# 项目名末尾的常见单位词白名单。仅在前导空白存在时剥，避免误伤产品名本身
# 以这些字结尾的合法情况（如"扫把"/"配件"/"机票款"等）。中英文单字/缩写/2-3 字单位。
_UNIT_TAIL_RE = re.compile(
    r"[ \t]+("
    r"个|只|条|根|张|片|块|包|本|页|颗|粒|枚|台|对|双|盒|袋|瓶|箱|桶|罐|打|辆|艘|架|部|座|份|把|支|件|套|捆|副|卷|册|具|扇|幅|帧|亩|次|步|趟|回|遍|场|局|排|列|群|组|付|束|团|堆|"
    r"千克|公斤|克|斤|两|吨|毫克|微克|毫米|厘米|分米|米|千米|公里|英寸|英尺|码|海里|升|毫升|小时|分钟|秒钟|千瓦时|元|角|分|天|周|月|年|季|度|"
    r"kg|KG|g|G|mg|MG|lb|oz|l|L|ml|ML|m|cm|mm|km|MM|KM|inch|ft|yd|mi|pcs|PCS|pc|PC|set|SET|pair|PAIR|dozen|DOZ|gross|kV|KV|kw|KW|kwh|KWH|hp"
    r")\s*$"
)
RE_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
RE_CHINESE_SHORT = re.compile(r"([一-鿿]{1,3})\s*$")
# 已知计量单位白名单（中英文 + 缩写 + 2-3 字），仅用于"末尾单位词剥离"。
# 用 frozenset 而不是正则，便于 _extract_unit 做最长前缀匹配（避免"克"抢了"千克"）。
_UNIT_TOKENS = frozenset({
    # 中文 1 字
    "个", "只", "条", "根", "张", "片", "块", "包", "本", "页", "颗", "粒", "枚",
    "台", "对", "双", "盒", "袋", "瓶", "箱", "桶", "罐", "打", "辆", "艘", "架",
    "部", "座", "份", "把", "支", "件", "套", "捆", "副", "卷", "册", "具", "扇",
    "幅", "帧", "亩", "次", "步", "趟", "回", "遍", "场", "局", "排", "列", "群",
    "组", "付", "束", "团", "堆", "吨",
    # 中文 2-3 字
    "千克", "公斤", "克", "斤", "两", "钱", "毫克", "微克", "毫米", "厘米", "分米",
    "米", "千米", "公里", "英寸", "英尺", "码", "海里", "升", "毫升", "小时", "分钟",
    "秒钟", "千瓦时", "千瓦", "元", "角", "分", "天", "周", "月", "年", "季", "度",
    # 英文 / 缩写
    "kg", "KG", "g", "G", "mg", "MG", "lb", "oz",
    "l", "L", "ml", "ML", "m³", "cm³",
    "m²", "cm²", "mm²", "km²",
    "m", "cm", "mm", "km", "MM", "KM", "inch", "ft", "yd", "mi",
    "pcs", "PCS", "pc", "PC", "set", "SET", "pair", "PAIR", "dozen", "DOZ", "gross",
    "kV", "KV", "kw", "KW", "kwh", "KWH", "hp", "kVA",
})

def _extract_unit(rest: str) -> Tuple[str, str]:
    """从 rest 末尾抓取单位词（最长匹配），返回 (unit, rest_不含单位词)。

    解决 RE_CHINESE_SHORT 末尾贪婪抓 1-3 字造成的"设基金/服务/费"等被误识别为 unit，
    以及英文单位 kg/pcs/m 等不被识别的问题。
    """
    s = rest.rstrip()
    if not s:
        return "", rest
    for L in (3, 2, 1):
        if len(s) >= L and s[-L:] in _UNIT_TOKENS:
            return s[-L:], s[:-L].rstrip()
    return "", rest

# 明显的 PDF 标签行（不是公司名）
LABEL_LINE_RE = re.compile(
    r"^(合|计|价税合计（大写）|（小写）|价税合计|项目名称|规格型号|"
    r"规\s*格\s*型\s*号|单|位|数|量|价|额|税率/征收率|税 额|"
    r"名称|名称：|名称:|统一社会信用代码/纳税人识别号：|"
    r"统一社会信用代码/纳税人识别号:|电子发票（普通发票）|电子发票|"
    r"发票号码：|发票号码:|开票日期：|开票日期:|开票人：|开票人:|"
    r"备|注|成品油|（大写）|（小写）|"
    r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆角分整]+)$"
)


# 真公司名常见特征：含"公司"、"集团"、"分行"、"支行"、"店"、"厂"、"中心"、"工作室"、"社"、"部"、"(有限)" 等
RE_COMPANY_HINT = re.compile(
    r"(公司|集团|分行|支行|分店|商店|商场|总店|分司|分社|分店|"
    r"厂|中心|工作室|商行|百货|超市|批发|市场|茶行|茶庄|"
    r"事务所|旅社|旅店|酒店|宾馆|饭店|餐馆|食堂|"
    r"加油站|油站|药店|医院|门诊|诊所|学校|学院|大学|"
    r"管理局|管理处|委员会|办公室|服务处|事务所|分社|事务所|"
    r"供应商|提供商|经销|代理|经营部|经营|店|铺|站|社|部|处|厂|行|馆|阁|堂)"
)


def _looks_like_company(line: str) -> bool:
    s = line.strip()
    if len(s) < 4 or len(s) > 60:
        return False
    if LABEL_LINE_RE.match(s):
        return False
    if RE_INVOICE_NO.match(s):
        return False
    if RE_DATE.match(s):
        return False
    if RE_TAX_ID.match(s):
        return False
    if RE_AMOUNT.match(s):
        return False
    if s[0] in "¥￥" or s[0].isdigit():
        return False
    # 必须含"公司/集团/分店..."等典型后缀，避免被"价税合计"、"电子发票"等标签混入
    if not RE_COMPANY_HINT.search(s):
        return False
    han = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return han >= max(2, int(len(s) * 0.5))


def _norm_date(y: str, m: str, d: str) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def extract_meta(text: str) -> InvoiceMeta:
    meta = InvoiceMeta()

    m = RE_INVOICE_NO.search(text)
    if m:
        meta.invoice_no = m.group(1)

    m = RE_DATE.search(text)
    if m:
        meta.issue_date = _norm_date(m.group(1), m.group(2), m.group(3))
        meta.issue_date_raw = m.group(0)

    cleaned = RE_INVOICE_NO.sub("", text)
    # 归一化前先保留一份（用于位置定位）
    raw_no_invoice_no = cleaned
    # 归一化：迭代删除"X<空格>X"中的空格（PDF 把税号字间距拉大时的产物）
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = re.sub(r"([0-9A-Z])\s(?=[0-9A-Z])", r"\1", cleaned)
    ids = RE_TAX_ID.findall(cleaned)
    if len(ids) >= 1:
        meta.buyer_tax_id = ids[0]
    if len(ids) >= 2:
        meta.seller_tax_id = ids[1]

    # 公司名：用 tax_id 在文本中的位置反推 —— buyer_tax_id 之前的最近真公司名；两者之间的真公司名为 seller
    # 按 cleaned（未归一化、去发票号）顺序扫所有"像公司名"的行，前两个对应 buyer/seller
    candidates: list[str] = []
    seen: set[str] = set()
    for line in raw_no_invoice_no.split("\n"):
        s = line.strip()
        if not s or s in seen:
            continue
        if _looks_like_company(s):
            candidates.append(s)
            seen.add(s)
        if len(candidates) >= 2:
            break
    if len(candidates) >= 1:
        meta.buyer_name = candidates[0]
    if len(candidates) >= 2:
        meta.seller_name = candidates[1]

    amounts = RE_AMOUNT.findall(text)
    floats = [float(x) for x in amounts]
    for i in range(len(floats) - 2):
        a, b, c = floats[i], floats[i + 1], floats[i + 2]
        if abs(a + b - c) < 0.02:
            meta.total_excl_tax = a
            meta.total_tax = b
            meta.total_incl_tax = c
            break
    if meta.total_incl_tax is None and floats:
        meta.total_incl_tax = floats[-1]

    m = RE_BIG_AMOUNT_LINE.search(text)
    if m:
        meta.total_in_c_c = m.group(1)

    m_remark = re.search(r"备\s*注[：:](.*?)(?=开票人|$)", text, re.S)
    if m_remark:
        meta.remark = m_remark.group(1).strip()

    issuer_positions = [mm.start() for mm in re.finditer(r"开票人[:：]", text)]
    if issuer_positions:
        tail = text[issuer_positions[-1]:]
        after_lines = tail.split("\n")
        # 跳过紧邻的 PDF 标签子串
        label_substrings = (
            "电子发票", "发票号码", "开票日期", "项目名称", "规格型号",
            "名称：", "名称:", "统一社会信用代码", "纳税人识别号",
            "开票人", "合 计", "价税合计", "备", "注",
            "购买方", "销售方", "¥", "￥",
        )
        # PDF 把"购买方信息"等拆成单字行："购"/"买"/"方"/"销"/"售"/"信"/"息" 等
        single_char_labels = set("购买方销售信息合计价税备注规格型号数量单位金额税率征收率")
        for line in after_lines[1:]:
            s = line.strip()
            if not s:
                continue
            # 日期（如 2026年08月28日）：含"年"和"日"
            if "年" in s and "月" in s and "日" in s:
                continue
            # 单字 PDF 拆字行
            if len(s) <= 1:
                continue
            if any(sub in s for sub in label_substrings):
                continue
            if LABEL_LINE_RE.match(s):
                continue
            if _looks_like_company(s):
                continue
            if RE_INVOICE_NO.match(s):
                continue
            if RE_TAX_ID.match(s):
                continue
            if len(s) == 1 and "一" <= s <= "鿿":
                continue
            # 跳过明细项目行（以 * 开头 或 含典型单位/数字列）
            if s.startswith("*"):
                continue
            # 含连续 4+ 数字的字符串通常是 金额/数量/税号/账号，不是人名
            if re.search(r"\d{4,}", s):
                continue
            if re.search(r"\d+\.\d+|\d+%", s) and len(s) > 8:
                continue
            # 人名长度合理（中文姓名通常 2-6 字）
            if len(s) > 8:
                continue
            if any(u in s for u in ("吨", "升", "件", "个", "套", "盒", "袋", "瓶", "份", "把", "PCS", "pcs", "kV", "KW")):
                continue
            if "不征税" in s or "免税" in s:
                continue
            if "开票人" in s or "名称" in s or "纳税人" in s:
                continue
            if any(c in s for c in ("，", "。", "、", ";", "；", "：", ":")) and len(s) > 4:
                # 长带标点行（如银行账号/订单号）通常不是人名
                continue
            meta.issuer = s
            break

    return meta

# ---------- 项目明细 ----------

def _extract_detail_block(text: str) -> str:
    """明细区段：表头之后第一个 `*` 字符 → 文本末尾。_split_items 会按 `*` 块自动切。"""
    m = RE_TABLE_HEADER.search(text)
    if not m:
        return ""
    tail = text[m.end():]
    star_match = re.search(r"\*", tail)
    if not star_match:
        return ""
    return tail[star_match.start():]


def _split_items(block: str) -> List[str]:
    items: List[str] = []
    cur: List[str] = []
    for line in block.split("\n"):
        if line.startswith("*"):
            stripped = line.lstrip("*")
            # 整行只有星号（空名终止符独立成行）→ 视为延续
            if not stripped.strip():
                if cur is not None:
                    cur.append(line)
                continue
            # 星号后立即空白（PDF 把双星号终止符与下一段挤在同一行）→ 视为延续
            if stripped[0].isspace():
                if cur is not None:
                    cur.append(line)
                continue
            if cur:
                items.append("\n".join(cur))
            cur = [line]
        elif line.strip():
            if cur:
                cur.append(line)
    if cur:
        items.append("\n".join(cur))
    return items


def _parse_item(raw: str) -> Optional[InvoiceLine]:
    flat = re.sub(r"\s+", " ", raw).strip()
    # 扫所有候选 TAIL_RE 匹配，挑最后合理位置
    best = None
    for cand in TAIL_RE.finditer(flat):
        try:
            amt = float(cand.group(1))
        except ValueError:
            continue
        # 合理性：金额必须含 .XX，且整数部分 < 9 位（避免被银行账号/合同号错位）
        if "." not in cand.group(1):
            continue
        head = cand.group(1).split(".")[0].lstrip("-").lstrip("0") or "0"
        if len(head) > 8:
            continue
        best = cand
    if best is None:
        return None
    m = best
    amount = float(m.group(1))
    tax_rate = m.group(2)
    tax = float(m.group(3)) if m.group(3) is not None else 0.0
    prefix = flat[: m.start()].strip()

    # 项目名：第一个 `*类 名*`（类名中允许空格）
    name_match = re.search(r"\*([^\*]+?)\*(.*)", prefix)
    if not name_match:
        return None
    head_cat = name_match.group(1).strip()
    # PDF 文本提取常因拆行在类别里塞入空格 / 换行（如 "*水 冰雪*"），与 name_tail
    # 一样压成无空白，避免 *X Y* 这种伪空格残留。
    head_cat = re.sub(r"\s+", "", head_cat)
    raw_tail = name_match.group(2).strip().rstrip("*").strip()
    # 项目名截断：第二个 * 之后起算的第二个空格及以后的内容视为单位/数量/单价等泄漏，丢弃。
    # rest 仍使用 raw_tail 完整内容，以便继续抽取 unit/quantity/unit_price。
    _parts = raw_tail.split()
    if len(_parts) >= 2:
        # 先用空格拼接，便于下一步识别"前导空格 + 常见单位词"误入 name_tail 的情况
        # （如 "*XX*扫描仪 个 1 200" -> name_tail 应是 "扫描仪" 而不是 "扫描仪个"）。
        # 即便 len(_parts) < 3（折扣行常见，产品名 + 规格 贴在一起没有 unit/qty/price 列），
        # 仍走相同的二次清理 + 压空格流程，确保 name 不会留下 PDF 拆行带来的中间空白。
        spaced_tail = " ".join(_parts[:2])
        _m_unit_tail = _UNIT_TAIL_RE.search(spaced_tail)
        if _m_unit_tail:
            spaced_tail = spaced_tail[: _m_unit_tail.start()].rstrip()
        # 二次清理后再把内部空格压掉，避免 PDF 拆行把产品名从中间劈开：
        # "*配电控制设备*监控电源 适配器 个 10 25.74" -> name="*配电控制设备*监控电源适配器"
        name_tail = spaced_tail.replace(" ", "")
    else:
        name_tail = raw_tail
    name = f"*{head_cat}*{name_tail}"
    # 噪声关键词：name 末尾的 "其他" 是税局备注归类的尾巴，对报销无意义，统一剥掉。
    if name.endswith("其他"):
        name = name[:-2]

    rest = raw_tail
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    spec = ""
    unit = ""

    is_discount = amount < 0
    floats = [float(x) for x in RE_NUMBER.findall(rest)]

    # 数量 / 单价 提取。PDF 标准列序：... 数量 单价 金额 税率 税额，
    # 但 PDF 拆行常造成前缀只剩 1 个数字（通常是数量）。所以 len==1 也按 quantity 提取。
    if not is_discount and floats:
        if len(floats) >= 2:
            quantity = floats[-2]
            unit_price = floats[-1]
        else:
            quantity = floats[-1]
        # 倒序从 rest 中剥离这些数字字面量（最后一次出现位置）
        for v in (unit_price, quantity):
            if v is None:
                continue
            literal_pat = re.compile(r"-?\d+(?:\.\d+)?")
            matches = list(literal_pat.finditer(rest))
            for ml in reversed(matches):
                try:
                    if abs(float(ml.group()) - v) < 1e-6:
                        rest = rest[: ml.start()] + rest[ml.end():]
                        break
                except ValueError:
                    continue

    rest = rest.strip()
    # 单位词提取：用 _UNIT_TOKENS 白名单替代原来的 RE_CHINESE_SHORT 末尾 1-3 字启发式，
    # 避免"设基金/服务/费"等被错识别为 unit，并补上 kg/pcs/m 等英文/缩写单位。
    spec = rest
    if not is_discount:
        unit, spec = _extract_unit(rest)

    return InvoiceLine(
        raw=raw,
        name=name,
        spec=spec,
        unit=unit,
        quantity=quantity,
        unit_price=unit_price,
        amount=amount,
        tax_rate=tax_rate,
        tax=tax,
        is_discount=is_discount,
    )


def extract_lines(text: str) -> List[InvoiceLine]:
    block = _extract_detail_block(text)
    if not block:
        return []
    items = _split_items(block)
    out: List[InvoiceLine] = []
    for it in items:
        parsed = _parse_item(it)
        if parsed is not None:
            out.append(parsed)
    return out


def parse_page(page_index: int, text: str) -> Invoice:
    inv = Invoice(page_index=page_index, raw_text=text)
    inv.meta = extract_meta(text)
    inv.lines = extract_lines(text)
    if inv.meta.total_incl_tax is not None and inv.lines:
        sum_amount = sum((l.amount or 0) for l in inv.lines)
        sum_tax = sum((l.tax or 0) for l in inv.lines)
        if abs(sum_amount + sum_tax - inv.meta.total_incl_tax) > 0.05:
            inv.parse_warning = (
                f"金额校验未通过：明细 {sum_amount:.2f} + 税额 {sum_tax:.2f}"
                f" = {sum_amount + sum_tax:.2f}，与价税合计 {inv.meta.total_incl_tax:.2f} 不一致。"
            )
    if not inv.meta.invoice_no:
        inv.parse_warning = (inv.parse_warning + "; " if inv.parse_warning else "") + "未识别到发票号码。"
    return inv


def parse_pdf(pdf_path: str) -> List[Invoice]:
    pages = extract_pages_text(pdf_path)
    return [parse_page(i, t) for i, t in enumerate(pages)]
