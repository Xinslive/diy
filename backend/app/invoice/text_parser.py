"""从电子发票 PDF / OCR 文本中提取结构化字段。

设计要点：
- 每页一张"电子发票（普通发票）"，头部锚点统一（购/销方信息、项目表头、合 计、价税合计）。
- 项目明细区段在「项目名称 规格型号 ...」表头之后、「合 计/价税合计」之前。
- 字段抽取以"标签 anchor + 正则"为主，避免依赖按行位置。
- 解析失败不抛异常，而是把 parse_warning 标到返回结构里，由前端提示用户人工核对。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict, replace
from decimal import Decimal
from typing import List, Optional, Tuple

from app.core.config import settings

try:
    import pypdfium2 as p
except ImportError:  # pragma: no cover - 运行环境通过 requirements 安装
    p = None


#: 清洗规则见 ``app.core.text_clean``（单一出处，模型守卫/打印层共用同一份）。
#:
#: PDF 文字层会把 UTF-16 的 BOM 漏成 U+FFFE 混进项目名，例如
#: 「*电线电缆*广州五羊ZC\ufffeBV2.5平方线」。这份字符原样存进
#: ``entry.summary``，之后**每一份**含它的明细账/总账导出都会整份失败
#: （Excel 底层是 XML，lxml 直接抛
#: ``All strings must be XML compatible``，一个字节都出不来）。
#:
#: 解析层就把它挡掉：源头干净了，历史数据可以一次性订正，以后也不会
#: 再攒出新的脏字符。
from app.core.text_clean import scrub_control_chars as scrub_control_chars  # noqa: E402,F401


def _jsonable(value):
    """Decimal → 字符串。

    解析结果要存进 JSONB（payload），而 JSON 没有 Decimal —— 硬塞会
    ``TypeError``。转成字符串还有一个好处：**存进去的就是发票上印的
    那个数**（"1.41"），不是 float 的二进制近似值（1.4100000000000001）。
    """
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


# ---------- 文本提取 ----------


def max_pdf_pages() -> int:
    """单次解析允许的 PDF 页数上限。"""
    return max(1, int(settings.invoice_max_pdf_pages))


def max_text_chars() -> int:
    """单次解析允许的文字总量上限（所有页加起来）。"""
    return max(1, int(settings.invoice_max_text_chars))


def extract_pages_text(pdf_path: str) -> List[str]:
    """按页读取纯文本。失败时返回空字符串占位。

    **两道闸（审查 H-07）：页数与文字总量。**

    * **页数闸在读页之前判**（``len(doc)``）—— 500 页的 PDF 逐页取文本层
      本身就是几秒的 CPU 与几十 MB 的内存，而且它会一路走到「一页一张发票」
      的解析链上。超了就该让用户分批，而不是把整本扫描件吞下去。
    * **文字总量闸逐页累加** —— 一页 100 万字符的「文字层」同样能把内存打满；
      上限按**所有页加起来**算，不看单页。

    两道闸都报清楚「实际是多少、上限是多少、怎么办」。
    """
    if p is None:
        raise RuntimeError("缺少 pypdfium2 依赖，请安装后再解析 PDF")
    pages_limit, chars_limit = max_pdf_pages(), max_text_chars()
    doc = p.PdfDocument(pdf_path)
    try:
        pages = len(doc)
        if pages > pages_limit:
            raise ValueError(
                f"这份 PDF 有 {pages} 页，超过单次解析上限 {pages_limit} 页；"
                "请分成几批上传")
        out: List[str] = []
        total = 0
        for i in range(pages):
            try:
                tp = doc[i].get_textpage()
                # PDF 文字层会混进 U+FFFE 之类的 XML 非法字符（见
                # ``scrub_control_chars``）。挡在这里，脏字符就不会进摘要。
                text = scrub_control_chars(tp.get_text_range() or "")
            except Exception:
                text = ""
            total += len(text)
            if total > chars_limit:
                raise ValueError(
                    f"这份 PDF 的文字共 {total} 个字符，超过单次解析上限 "
                    f"{chars_limit} 个字符；请分成几批上传")
            out.append(text)
        return out
    finally:
        doc.close()


def page_count(pdf_path: str) -> int:
    """页数。批量上传时用它把「页码 → 来自哪个源文件」对上。"""
    if p is None:
        raise RuntimeError("缺少 pypdfium2 依赖，请安装后再解析 PDF")
    doc = p.PdfDocument(pdf_path)
    try:
        return len(doc)
    finally:
        doc.close()


# ---------- 数据模型 ----------

@dataclass
class InvoiceLine:
    raw: str = ""
    name: str = ""              # 项目名（含 *类别*前缀；规格型号可能粘在后面）
    # **给摘要用的项目名** —— 发票上「项目名称」那一格里印的原文。
    #
    # 为什么和 name 分开：PDF 的文字层没有列的信息，规格型号会紧跟项目名
    # 粘在一起（"*油料*粮油米面6930536003708"），而**折扣行的同一格里没有
    # 规格型号**，只有干净的 "*油料*粮油米面" —— 那是发票上印着的项目名。
    # 两条规则（按 name 匹配、显示与摘要用 display_name）互不影响：
    # display_name 一定是 name 的**子串**，所以用户照着界面写的关键词
    # 按 name 匹配时一定命中（见 ``_strip_trailing_spec`` 末尾那道校验）。
    display_name: str = ""      # 摘要/界面用的干净项目名
    spec: str = ""              # 规格型号
    unit: str = ""              # 单位
    # 金额一律用 **Decimal**，不用 float。
    #
    # 为什么：float 的二进制表示不精确，0.68 + 1.5 + 1.41 会算出
    # 5.990000000000001 —— 界面上就显示出「1.4100000000000001」这种
    # 没人看得懂的尾数，而「各项目之和 = 发票金额」这条恒等式差 1e-16
    # 就对不上。Decimal 从**文本**直接解析（"1.41" → Decimal("1.41")），
    # 是一位不多一位不少。
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    amount: Optional[Decimal] = None    # 不含税金额（可能为负）
    tax_rate: str = ""          # "13%" / "1%" / "不征税"
    tax: Optional[Decimal] = None
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
    total_excl_tax: Optional[Decimal] = None
    total_tax: Optional[Decimal] = None
    total_incl_tax: Optional[Decimal] = None
    total_in_c_c: str = ""
    remark: str = ""
    issuer: str = ""


@dataclass
class Invoice:
    page_index: int
    #: 这张发票**跨了几页**。分页票（一张发票拆成几页印）合成之后是 >1；
    #: 界面上的「页码」列据此显示成「1-3」而不是「1」——
    #: 只显示起始页，用户会以为第 2、3 页没被读进来。
    page_span: int = 1
    meta: InvoiceMeta = field(default_factory=InvoiceMeta)
    lines: List[InvoiceLine] = field(default_factory=list)
    raw_text: str = ""
    parse_warning: str = ""

    def to_dict(self) -> dict:
        return {
            "page_index": self.page_index,
            "page_span": self.page_span,
            "meta": {k: _jsonable(v) for k, v in asdict(self.meta).items()},
            "lines": [{k: _jsonable(v) for k, v in asdict(l).items()} for l in self.lines],
            "parse_warning": self.parse_warning,
        }


# ---------- 头部正则 ----------

RE_INVOICE_NO = re.compile(r"(?<!\d)(\d{20})(?!\d)")
RE_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
RE_TAX_ID = re.compile(r"\b([0-9A-Z]{18,20})\b")
RE_AMOUNT = re.compile(r"(?:¥|￥)\s*(\d+(?:\.\d+)?)")
#: 「合 计」那一行：``¥不含税金额``，后面可能跟 ``¥税额``。
#: 免税/不征税的票，税额那一栏印的是 ``*``（不是数字）—— 电信服务那张票
#: 印的就是「¥199.50 *」。
RE_SUM_LINE = re.compile(
    r"[¥￥]\s*(?P<excl>\d+(?:\.\d+)?)"
    r"(?:\s*[¥￥]\s*(?P<tax>\d+(?:\.\d+)?))?"
    r"(?:\s*\*)?"
)
RE_ISSUER = re.compile(r"开票人[:：]\s*(\S+)")
#: 明细区的表头。**每个字之间都允许有空白** ——
#:
#: PDF 的文字层有时会把一个汉字拆成独立的一格（"电 子 发 票"、"项 目 名 称"），
#: 而原来那条只认连续的"项目名称"。于是那一页的表头认不出来，
#: ``_extract_detail_block`` 找不到表头就返回空串 → 明细一行都提不出来 →
#: 整张票报「没有可入账的发票明细」。
#:
#: 实测就是一张这样的票：号码、日期、买卖方、价税合计全都认得出来，
#: 只有明细是空的 —— 因为表头那一行印的是"项 目 名称 规格型号 …"。
#:
#: **只认第一列的表头「项目名称」**，不再要求后面紧跟「规格型号」。
#:
#: 「规格型号」是**普通货物发票**的版式。发票还有很多种版式：建筑服务票的
#: 列是「项目名称 建筑服务发生地 建筑项目名称 金额 税率/征收率 税额」，
#: 压根没有「规格型号」那一列 —— 要求它就等于把这种票整批挡在门外
#: （号码、日期、买卖方、合计全都认得出来，就是明细提不出来）。
#:
#: 认宽了的风险很小：认错了也只是把「表头之前的一小段」算进明细区，
#: 而 ``_split_items`` 只从带 ``*`` 的行开始切 —— 前面那点文字会被丢掉。
RE_TABLE_HEADER = re.compile(r"项\s*目\s*名\s*称")

#: 建筑服务票的表头里有这一列，说明明细的**名字区是三个格子**：
#: 项目名称、建筑服务发生地、建筑项目名称。别的版式没有它。
BUILDING_SERVICE_HEADER = re.compile(r"建\s*筑\s*服\s*务\s*发\s*生\s*地")
#: 通行费（高速公路通行费）版式的表头：多出「车牌号 车辆类型 通行日期起
#: 通行日期止」四列，**没有**「单位 数量 单价」那三列。
#:
#: 这一版式必须单独认 —— 通用办法「取最后两个数字当数量与单价」会把
#: **通行日期**（20260419 这种 8 位数）当成数量和单价：金额碰巧是对的，
#: 数量与单价却是彻底的胡说，还会跟着进到凭证的明细里。
TOLL_HEADER = re.compile(r"车\s*牌\s*号|车\s*辆\s*类\s*型|通\s*行\s*日\s*期")
#: 车牌号：省份字 + 字母 + 5~6 位数字字母（如「闽C889BJ」）。
#: 通行费那一版式里，这一列是**最好认的锚点** —— 锚点之前就是项目名称。
_TOLL_PLATE_RE = re.compile(r"^[\u4e00-\u9fff][A-Z][A-Z0-9]{5,6}$")
#: 通行日期（YYYYMMDD，票面上是 8 位连写，如 20260419）。
_TOLL_DATE_RE = re.compile(r"^\d{8}$")
#: 票面把**空的规格型号**印成「无」。只认「独立成词」的「无」——
#: 「无碳钢」这种照旧不动。
RE_NONE_CELL = re.compile(r"(?<!\w)无(?!\w)")
RE_BIG_AMOUNT_HEADER = re.compile(r"价税合计（大写）\s*（小写）")
# 大写金额（「价税合计（大写）」那一行）。
#
# 原来是一条正则 ``([零壹…整]+)\s*¥\d+(?:\.\d+)?`` 直接 search 全文，而那条
# 写法是**二次回溯**的：载荷是「N 个中文数字 + N 个空白 + 没有 ¥」时，正则
# 先贪婪吃掉 N 个中文数字、再吃掉 N 个空白、然后要求 ¥ 失败；回溯时 ``\s*``
# 逐个退格，而 ``search()`` 会在**每一个起始位置**重试整段匹配 —— 于是总代价
# 是 O(n²)。一次 20MB 的上传能烧掉约 9.7 分钟 CPU（审计 P2-1）。
#
# 现在拆成两步：先用 finditer 找出**极大**的中文数字串（单字符类没有回溯
# 歧义，线性），再在每个串的**末尾**往后看是不是「空白 + ¥ + 数字」。
# 空白段在各个数字串之间互不重叠，总代价 O(n)。
#
# 语义与原来那条正则**完全一致**：原来那条的 ``+`` 也不需要回溯就能匹配
# （极大串的下一个字符必然还是数字类字符，不可能是 ¥），所以「最靠前的
# 那个匹配」在两种写法下是同一个。
RE_BIG_AMOUNT_DIGITS = re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆角分整]+")
RE_BIG_AMOUNT_TAIL = re.compile(r"\s*¥\d+(?:\.\d+)?")
END_MARKERS = ("合 计", "价税合计", "合计：", "价税合计（大写）")
# 末尾：金额 + 税率（不征税/免税/%）+ 可选税额
#
# **税率那一栏印的是星号「\*」** —— 免税的票就这么印（电信服务那张票：
# 「… 无 项 1 199.5 199.50 * *」，金额 199.50、税额没有、价税合计 199.50）。
# 原来这条正则只认「数字% / 不征税 / 免税」，于是**整行都匹配不上**，
# ``_parse_item`` 直接返回 None —— 那一行的明细就没了。
# 一张 35 页的票里有 9 页是这种版式，全是电信费：9 张票一行明细都提不出来，
# 报「没有可入账的发票明细」。
#
# 税额那一栏同样可以是星号（不征税/免税的票没有税额数字），它表示 **0**。
TAIL_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s+(\d+%|不征税|免税|\*)"
    r"(?:\s+(?:(-?\d+(?:\.\d+)?)|(\*)))?"
)
# 项目名末尾的常见单位词白名单。仅在前导空白存在时剥，避免误伤产品名本身
# 以这些字结尾的合法情况（如"扫把"/"配件"/"机票款"等）。中英文单字/缩写/2-3 字单位。
#
# **「车」「平方米」「季度」「项」是实测补上的**（一张 35 页的票上就撞见
# 这四个）：垃圾清运费按「车」计、电梯保养按「季度」计、面积按「平方米」
# 计、电信服务按「项」计。白名单里没有它们的时候，这一格就会**漏进项目名**
# ——「*生活服务*垃圾清运费车」「*生产生活服务*电梯保养季度」，
# 摘要也跟着变成那样，用户在界面上看到的就是票上没有的字。
_UNIT_TAIL_RE = re.compile(
    r"[ \t]+("
    r"个|只|条|根|张|片|块|包|本|页|颗|粒|枚|台|对|双|盒|袋|瓶|箱|桶|罐|打|辆|车|艘|架|部|座|份|把|支|件|套|捆|副|卷|册|具|扇|幅|帧|亩|次|步|趟|回|遍|场|局|排|列|群|组|付|束|团|堆|项|"
    r"平方米|千克|公斤|克|斤|两|吨|毫克|微克|毫米|厘米|分米|米|千米|公里|英寸|英尺|码|海里|升|毫升|小时|分钟|秒钟|千瓦时|元|角|分|天|周|月|年|季度|季|度|"
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
    "台", "对", "双", "盒", "袋", "瓶", "箱", "桶", "罐", "打", "辆", "车", "艘", "架",
    "部", "座", "份", "把", "支", "件", "套", "捆", "副", "卷", "册", "具", "扇",
    "幅", "帧", "亩", "次", "步", "趟", "回", "遍", "场", "局", "排", "列", "群",
    "组", "付", "束", "团", "堆", "项", "吨",
    # 中文 2-3 字
    "千克", "公斤", "克", "斤", "两", "钱", "毫克", "微克", "毫米", "厘米", "分米",
    "米", "平方米", "千米", "公里", "英寸", "英尺", "码", "海里", "升", "毫升", "小时", "分钟",
    "秒钟", "千瓦时", "千瓦", "元", "角", "分", "天", "周", "月", "年", "季", "季度", "度",
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
    r"备|注|成品油|通\s*行\s*费|（大写）|（小写）|"
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

#: 表头里那些**列名**。一张票的表头是好几列的标题拼在一行上的 ——
#: 通行费那张票印的是「项目名称 车牌号 车辆类型 通行日期起 通行日期止
#: 金 额 税率/征收率 税 额」。
#:
#: 老办法是「按像不像公司名」挑买卖方，而「通**行**日期」里的「行」正好
#: 命中公司名后缀表（银行支行）—— **整行表头被当成了购方公司名**，
#: 真正的销方（高速公路公司）挤不进前两个候选，直接被丢掉。
#:
#: 后果比"认错名字"更糟：购方成了表头、**销方成了用户自己公司**，
#: 凭证上的对方单位就写成了自己 —— 金额全对，账却是错的。
RE_COLUMN_TITLE = re.compile(
    r"项目名称|规格型号|车牌号|车辆类型|通行日期|建筑服务发生地|建筑项目名称|"
    r"金\s*额|税\s*额|税\s*率|征收率|单\s*位|数\s*量|单\s*价"
)

#: 中文之间的空格。PDF 文字层有时把一个汉字单独放一格
#: （「福 建 省 泉 州紫欣工贸有限公司」），那是**排版**产物，不是名字里的空格。
#: 只夹在两个汉字之间的空白才去掉；英文词之间的空格留着（"ABC Trading Co"）。
RE_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])")


def _column_title_count(line: str) -> int:
    """这一行里有几个**列名**。≥2 个就说明它是一张票的表头，不是公司名。"""
    return len({m.group(0) for m in RE_COLUMN_TITLE.finditer(line)})


def _looks_like_company(line: str) -> bool:
    s = line.strip()
    if len(s) < 4 or len(s) > 60:
        return False
    if LABEL_LINE_RE.match(s):
        return False
    # **表头不是公司名。** 一行里出现两个以上列名（项目名称、车牌号、金额…）
    # 就是票面的表头，不是买卖方 —— 见 ``RE_COLUMN_TITLE`` 那段说明：
    # 认错的后果是「销方写成用户自己公司」，账错得悄无声息。
    if _column_title_count(s) >= 2:
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


def find_big_amount(text: str) -> str:
    """找出「价税合计（大写）」那一行的大写金额，返回中文数字串；没有就空串。

    拆成「找极大中文数字串 + 看它后面是不是 ¥金额」两步之后，整段文本的
    处理代价是**线性**的（审计 P2-1：原来那条正则对 20MB 的恶意载荷
    要烧掉约 9.7 分钟 CPU）。
    """
    for match in RE_BIG_AMOUNT_DIGITS.finditer(text):
        if RE_BIG_AMOUNT_TAIL.match(text, match.end()):
            return match.group(0)
    return ""


def _extract_totals(text: str) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """「合 计」与「价税合计」那两行里的三个金额。

    返回 ``(不含税金额, 税额, 价税合计)``；认不出来时三个都是 None。

    票面上这两行长这样：

    * 有税的票：``¥442.48 ¥57.52`` / ``伍佰圆整 ¥500.00``
    * 免税或不征税的票：``¥199.50 *`` / ``壹佰玖拾玖圆伍角整 ¥199.50``
      —— 税额那一栏印的是 ``*``，**不是数字**。

    老办法是「在全文里凑三个 ¥ 数字」，免税/不征税的票只有**两个**
    （不含税与价税合计各一个），凑不出来 —— 于是不含税金额与税额双双
    是 None，界面上「合计」那一栏空着，看着像没解析出来。

    判据是**行**：第一条带 ¥ 的行是「合 计」行；带**大写金额**的那一行
    是「价税合计」行。认不出就返回 None，由调用方退回老办法。
    """
    lines = [line.strip() for line in text.splitlines()]

    sum_line = None
    for line in lines:
        if RE_AMOUNT.search(line):
            sum_line = line
            break
    if sum_line is None:
        return (None, None, None)
    m = RE_SUM_LINE.search(sum_line)
    if not m:
        return (None, None, None)
    excl = Decimal(m.group("excl"))
    tax = Decimal(m.group("tax")) if m.group("tax") else Decimal("0")

    incl = None
    for line in lines:
        for big in RE_BIG_AMOUNT_DIGITS.finditer(line):
            tail = RE_BIG_AMOUNT_TAIL.match(line, big.end())
            if tail:
                incl = Decimal(tail.group(0).strip().lstrip("¥￥"))
                break
        if incl is not None:
            break
    if incl is None:
        return (None, None, None)
    return (excl, tax, incl)


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

    # 公司名称要和税号对应，不能直接取全文最前面两个「像公司名」的行。
    # 不动产发票会在买卖方信息之前印「不动产经营租赁服务」；「经营」也会
    # 命中公司名特征，旧逻辑于是把项目类别当购方，后面的真实买卖方整体错位。
    candidates: list[tuple[int, str]] = []
    tax_id_lines: list[tuple[str, int]] = []
    seen: set[str] = set()
    for line_index, line in enumerate(raw_no_invoice_no.split("\n")):
        # 汉字之间的空格是 PDF 排版产物（「福 建 省 泉 州紫欣工贸有限公司」），
        # 去掉它才是票面上印的那个名字。
        s = RE_CJK_GAP.sub("", line.strip())
        if s and s not in seen and _looks_like_company(s):
            candidates.append((line_index, s))
            seen.add(s)
        compact = line
        previous = None
        while previous != compact:
            previous = compact
            compact = re.sub(r"([0-9A-Z])\s(?=[0-9A-Z])", r"\1", compact)
        tax_id_lines.extend((tax_id, line_index) for tax_id in RE_TAX_ID.findall(compact))

    # 文本层里公司名通常紧挨对应税号之前。按每个税号找最近的公司名，
    # 而不是把税号前的票种/项目类别当成买方。找不到对应名称时留空，交给
    # 规则未命中提示人工处理，不从远处猜一个可能错误的对方名称。
    if ids and len(tax_id_lines) == len(ids):
        def name_for_tax_id(tax_id: str) -> str:
            line_index = next((index for value, index in tax_id_lines if value == tax_id), None)
            if line_index is None:
                return ""
            return next((name for candidate_index, name in reversed(candidates)
                         if candidate_index < line_index), "")

        meta.buyer_name = name_for_tax_id(ids[0])
        if len(ids) >= 2:
            meta.seller_name = name_for_tax_id(ids[1])
    else:
        # 兼容税号被 PDF 拆到多行等无法定位行号的旧版式。
        if len(candidates) >= 1:
            meta.buyer_name = candidates[0][1]
        if len(candidates) >= 2:
            meta.seller_name = candidates[1][1]

    # 合计：**先按行取**「合 计」与「价税合计」那两行 —— 免税/不征税的票
    # 税额那一栏印的是星号，只有两个 ¥ 数字，靠「凑三个」是凑不出来的。
    excl, tax, incl = _extract_totals(text)
    if incl is not None and abs(excl + tax - incl) < Decimal("0.02"):
        meta.total_excl_tax = excl
        meta.total_tax = tax
        meta.total_incl_tax = incl
    else:
        # 按行认不出来（票面不是常见版式）→ 退回老办法：在全文里凑
        # 「不含税 + 税 = 价税合计」那三个数。
        amounts = RE_AMOUNT.findall(text)
        totals = [Decimal(x) for x in amounts]
        for i in range(len(totals) - 2):
            a, b, c = totals[i], totals[i + 1], totals[i + 2]
            # 判据是「不含税 + 税 = 价税合计」。阈值用 Decimal —— 拿 0.02 这个
            # float 去比，等于把判据也变成了近似值。
            if abs(a + b - c) < Decimal("0.02"):
                meta.total_excl_tax = a
                meta.total_tax = b
                meta.total_incl_tax = c
                break
        if meta.total_incl_tax is None and totals:
            meta.total_incl_tax = totals[-1]

    meta.total_in_c_c = find_big_amount(text)

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
            # 汉字之间的空格是排版产物，去掉（见 RE_CJK_GAP）。
            s = RE_CJK_GAP.sub("", line.strip())
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

def _extract_detail_block(text: str) -> tuple[str, str]:
    """明细区段：表头之后第一个 `*` 字符 → 文本末尾。_split_items 会按 `*` 块自动切。

    连带把**表头那一行**一起返回：不同版式的票，明细的列不一样
    （建筑服务票多一列「建筑服务发生地」），名字那一段要按版式切。
    """
    m = RE_TABLE_HEADER.search(text)
    if not m:
        return "", ""
    line_end = text.find("\n", m.start())
    header = text[m.start(): line_end if line_end > 0 else len(text)]
    tail = text[m.end():]
    star_match = re.search(r"\*", tail)
    if not star_match:
        return "", ""
    return tail[star_match.start():], header


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


#: 规格型号那格里几乎只有数字/字母（条码、"100*120"、"330ml*6"），
#: 而项目名那格是中文。用这两条判据把两者分开。
_RE_HAS_DIGIT = re.compile(r"\d")
_RE_HAS_HAN = re.compile(r"[\u4e00-\u9fff]")


def _strip_trailing_spec(cell: str) -> str:
    """从「项目名称 + 规格型号」这一格里挑出**项目名**那一段。

    PDF 的文字层没有列的信息，两个格子会被拼在一起：
    ``"粮油米面 6930536003708"`` 其实是「项目名称=粮油米面」加
    「规格型号=6930536003708」。摘要要的是前者。

    判据是**末段像不像规格**：有数字、没有汉字。认不出来就整段留着 ——
    摘要多几个字只是不好看，把项目名劈成两半却是错的。
    """
    parts = cell.split()
    if len(parts) >= 2:
        last = parts[-1]
        if _RE_HAS_DIGIT.search(last) and not _RE_HAS_HAN.search(last):
            parts = parts[:-1]
    return "".join(parts)


def _split_toll_cells(raw_tail: str) -> tuple[str, str]:
    """通行费版式的那一行：``名称 [车牌号] [车辆类型] [通行日期起] [通行日期止]``。

    返回 ``(项目名称, 车牌号)``。

    **车牌号是这一版式里最好认的一列**（省份字 + 字母 + 5~6 位），
    用它当锚点：锚点之前就是项目名称。
    「通 行 费」那种被 PDF 拆成单字的行也不怕 —— 名字由锚点**之前**的
    那些字拼成，拆成几个 token 也照样拼得回去。

    认不出车牌号时退回「末尾的通行日期」当锚点（日期是 8 位连写，
    比「车辆类型」可靠得多 —— 「客车」「货车」这些词本身也可能是项目名的一部分）。
    """
    tokens = raw_tail.split()
    for index, token in enumerate(tokens):
        if _TOLL_PLATE_RE.match(token):
            return "".join(tokens[:index]), token
    for index in range(len(tokens) - 1, -1, -1):
        if _TOLL_DATE_RE.match(tokens[index]):
            return "".join(tokens[:index]), ""
    return "".join(tokens), ""


def _parse_item(raw: str, header: str = "") -> Optional[InvoiceLine]:
    flat = re.sub(r"\s+", " ", raw).strip()
    # 扫所有候选 TAIL_RE 匹配，挑最后合理位置
    #
    # **「税率印成星号」的匹配单独放一档。** 星号既是「免税」的记号，
    # 也是项目名里的分隔符 —— 一行末尾若是「… 13.00 *」（星号是名字的
    # 终止符），星号档的匹配会认成「金额 13.00、税率 *」，把真正的金额
    # 与税额丢掉。所以：**有真税率的匹配优先**，实在没有才用星号档。
    best = None
    best_star = None
    for cand in TAIL_RE.finditer(flat):
        try:
            amt = Decimal(cand.group(1))
        except (ArithmeticError, ValueError):
            continue
        # 合理性：金额必须含 .XX，且整数部分 < 9 位（避免被银行账号/合同号错位）
        if "." not in cand.group(1):
            continue
        head = cand.group(1).split(".")[0].lstrip("-").lstrip("0") or "0"
        if len(head) > 8:
            continue
        if cand.group(2) == "*":
            best_star = cand
        else:
            best = cand
    if best is None:
        best = best_star
    if best is None:
        return None
    m = best
    amount = amt
    tax_rate = m.group(2)
    if tax_rate == "*":
        # 票面把「免税」印成星号。界面上直接显示「*」没人看得懂，
        # 而这一栏是给人看的 —— 显示「免税」，语义与票面一致
        # （免税的票没有税额，进项不可抵扣）。
        tax_rate = "免税"
    # 税额同样走 Decimal —— 「1.41」进、「1.41」出，不经 float。
    # 税额那一栏印星号（免税/不征税）时就是 0。
    tax = Decimal(m.group(3)) if m.group(3) is not None else Decimal("0")
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
    # 票面把**空的规格型号**印成「无」—— 电信服务那张票就是这么印的
    # （「*电信服务*电信服务 无 项 1 199.5 199.50 * *」）。那个「无」是
    # 「没有」的意思：它既不是项目名的一部分，也不是一个规格型号。
    # 只认「独立成词」的「无」（前后都不是字）—— 「无碳钢」这种照旧不动。
    raw_tail = RE_NONE_CELL.sub(" ", raw_tail)
    # 项目名截断：第二个 * 之后起算的第二个空格及以后的内容视为单位/数量/单价等泄漏，丢弃。
    # rest 仍使用 raw_tail 完整内容，以便继续抽取 unit/quantity/unit_price。
    _parts = raw_tail.split()
    # **通行费版式**：这一版式压根没有「单位 数量 单价」那几列 ——
    # 名字区后面是「车牌号 车辆类型 通行日期起 通行日期止」四列。
    # 老办法「取最后两个数字当数量与单价」会把**通行日期**（20260419 这种
    # 8 位数）当成数量和单价：金额碰巧是对的，数量与单价却是彻底的胡说。
    is_toll = bool(TOLL_HEADER.search(header))
    if is_toll:
        # 车牌号是这一版式里**最好认**的一列，用它当锚点，锚点之前就是项目名。
        name_tail, _plate = _split_toll_cells(raw_tail)
        if _plate:
            # 车牌号拼在名字后面（跟别处「名字带规格型号」一个口径），
            # 这样摘要里能看出是哪辆车 —— 通行费的摘要没有车牌号等于没头没脑。
            name_tail = name_tail + _plate
    elif BUILDING_SERVICE_HEADER.search(header) and len(_parts) >= 3:
        # **建筑服务票**：名字区是**三个**格子 —— 项目名称、建筑服务发生地、
        # 建筑项目名称。
        #
        # 发生地是地址（对本账没用，还占地方），留下「项目名称 + 建筑项目名称」：
        # 一个是税局归的类目（其它建筑服务），一个是「干了什么活」（零星修缮）。
        # 两个都留着，摘要才认得出这张票是干什么的。
        name_tail = "".join([_parts[0], *_parts[2:]])
    elif len(_parts) >= 2:
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
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    spec = ""
    unit = ""

    is_discount = amount < 0
    numbers = [Decimal(x) for x in RE_NUMBER.findall(rest)]

    # 数量 / 单价 提取。PDF 标准列序：... 数量 单价 金额 税率 税额，
    # 但 PDF 拆行常造成前缀只剩 1 个数字（通常是数量）。所以 len==1 也按 quantity 提取。
    #
    # **通行费版式不适用**：它没有这两列，那两个 8 位数是通行日期。
    if not is_discount and numbers and not is_toll:
        if len(numbers) >= 2:
            quantity = numbers[-2]
            unit_price = numbers[-1]
        else:
            quantity = numbers[-1]
        # 倒序从 rest 中剥离这些数字字面量（最后一次出现位置）
        for v in (unit_price, quantity):
            if v is None:
                continue
            literal_pat = re.compile(r"-?\d+(?:\.\d+)?")
            matches = list(literal_pat.finditer(rest))
            for ml in reversed(matches):
                try:
                    if abs(Decimal(ml.group()) - v) < Decimal("0.000001"):
                        rest = rest[: ml.start()] + rest[ml.end():]
                        break
                except (ArithmeticError, ValueError):
                    continue

    rest = rest.strip()
    # 单位词提取：用 _UNIT_TOKENS 白名单替代原来的 RE_CHINESE_SHORT 末尾 1-3 字启发式，
    # 避免"设基金/服务/费"等被错识别为 unit，并补上 kg/pcs/m 等英文/缩写单位。
    spec = rest
    if is_toll:
        # 通行费没有「单位」列，规格型号那一格放的是**车牌号**。
        unit = ""
        spec = _plate
    elif not is_discount:
        unit, spec = _extract_unit(rest)

    # 摘要用的项目名：``spec`` 这一段是「项目名称格 + 规格型号格」，
    # 摘掉末尾那段像规格型号的碎片，剩下的才是发票上印的项目名。
    #
    # ``name`` 保持原样（拼着规格型号）—— 科目规则按 name 匹配，摘要按
    # display_name，两边互不影响。
    display_tail = _strip_trailing_spec(spec)
    display_name = f"*{head_cat}*{display_tail}"
    if display_name.endswith("其他"):
        display_name = display_name[:-2]
    if not display_name or display_name not in name:
        # 剥过头了就退回 name。
        #
        # **display_name 必须是 name 的子串**：界面上显示的是 display_name，
        # 用户照着界面写的关键词要能被「按 name 匹配」的规则命中。剥规格
        # 型号剥过头（多行项目名的发票上会这样）时，宁可摘要多几个字，
        # 也不能让用户照着界面写的关键词匹配不上。
        display_name = name

    return InvoiceLine(
        raw=raw,
        name=name,
        display_name=display_name,
        spec=spec,
        unit=unit,
        quantity=quantity,
        unit_price=unit_price,
        amount=amount,
        tax_rate=tax_rate,
        tax=tax,
        is_discount=is_discount,
    )


def _discount_belongs_to(item_name: str, discount_name: str) -> bool:
    """折扣行是不是在给这个项目打折。

    判据是**名字相容**：折扣行的项目名要么是上面那个项目名的前缀，
    要么反过来。发票上折扣行紧跟在它所属的项目后面，名字是同一个
    （只是折扣行那一格没有规格型号），所以相容就足以认领。

    不用「金额相等」或「序号相邻」这种更脆的判据：前者对多张发票不通用，
    后者在一张发票里有多行折扣时就认错了人。
    """
    if not item_name or not discount_name:
        return False
    return item_name.startswith(discount_name) or discount_name.startswith(item_name)


def merge_discount_lines(lines: List[InvoiceLine]) -> List[InvoiceLine]:
    """把折扣行并进它上面的项目行 —— 凭证按**项目**列支，不按发票的原始行。

    发票上「项目 + 紧随其后的折扣行」是**同一个项目**的两笔金额
    （正数 + 负数 = 净额）。凭证上要列的是净额那一条：
    列成两行就是一条正一条负，负的那条是**红字分录** —— 用户看着别扭，
    而红字在不少导出与报表口径里会被当成异常。

    顺带解决摘要的名字：折扣行的「项目名称」格里只有项目名，没有规格型号，
    所以它比项目行干净（"*油料*粮油米面" vs "*油料*粮油米面6930536003708"）。
    合并时把它记成 display_name —— 摘要与界面上显示的是发票上印的那个名字。

    认不出归属的折扣行**原样留着**（不硬塞给上面那个项目）：
    那是发票上的真实一行，金额对不对交给既有的金额校验去报，
    比悄悄并到别的项目上强。
    """
    out: List[InvoiceLine] = []
    for line in lines:
        if line.is_discount and out and _discount_belongs_to(out[-1].name, line.name):
            prev = out[-1]
            prev.amount = (prev.amount or 0) + (line.amount or 0)
            prev.tax = (prev.tax or 0) + (line.tax or 0)
            # 折扣行的项目名格子里**没有规格型号**，是发票上印的干净名字。
            # 只在它确实是项目名的前缀时才用它 —— 这样 display_name 永远是
            # name 的前缀，按 name 匹配的规则不会因为显示名变短而漏命中。
            candidate = line.display_name or line.name
            if candidate and candidate in prev.name:
                prev.display_name = candidate
            continue
        if not line.display_name:
            line.display_name = line.name
        out.append(line)
    return out


def extract_lines(text: str) -> List[InvoiceLine]:
    block, header = _extract_detail_block(text)
    if not block:
        return []
    items = _split_items(block)
    out: List[InvoiceLine] = []
    for it in items:
        parsed = _parse_item(it, header)
        if parsed is not None:
            out.append(parsed)
    return merge_discount_lines(out)


def parse_page(page_index: int, text: str) -> Invoice:
    inv = Invoice(page_index=page_index, raw_text=text)
    inv.meta = extract_meta(text)
    inv.lines = extract_lines(text)
    if inv.meta.total_incl_tax is not None and inv.lines:
        sum_amount = sum((l.amount or 0 for l in inv.lines), Decimal("0"))
        sum_tax = sum((l.tax or 0 for l in inv.lines), Decimal("0"))
        # 全部 Decimal，差多少就是多少 —— 不再是 1e-16 那种「差一点」。
        if abs(sum_amount + sum_tax - inv.meta.total_incl_tax) > Decimal("0.05"):
            inv.parse_warning = (
                f"金额校验未通过：明细 {sum_amount:.2f} + 税额 {sum_tax:.2f}"
                f" = {sum_amount + sum_tax:.2f}，与价税合计 {inv.meta.total_incl_tax:.2f} 不一致。"
            )
    if not inv.meta.invoice_no:
        inv.parse_warning = (inv.parse_warning + "; " if inv.parse_warning else "") + "未识别到发票号码。"
    return inv


#: 金额校验警告的前缀。合成发票时要**丢掉每页自己的那一条** ——
#: 它比的是「本页明细之和」与「本页价税合计」，而分页票上每页只有小计，
#: 本来就对不上；换一条合并之后再算的。
_AMOUNT_WARNING_PREFIX = "金额校验未通过"

#: 分页票的页脚标记，形如「共3页 第2页」。这是**最硬的证据**：
#: 票面自己说这几页是同一张票。
_PAGINATION_RE = re.compile(r"共\s*(\d+)\s*页")


def pagination_marker(text: str) -> tuple[int, int] | None:
    """页脚上的「共N页 第M页」→ ``(总页数, 本页页码)``；没有就返回 None。

    只有**两样都在同一页上**才算数：「第3页」单独出现没有意义（页脚里
    「第几页」也可能指别的），「共3页」也可能出现在别的上下文里。
    """
    total_match = _PAGINATION_RE.search(text or "")
    if not total_match:
        return None
    total = int(total_match.group(1))
    page_match = re.search(r"第\s*(\d+)\s*页", text or "")
    if not page_match:
        return None
    page = int(page_match.group(1))
    if page > total:
        return None
    return total, page


def _lines_total(lines: List[InvoiceLine]) -> tuple[Decimal, Decimal]:
    amount = sum((l.amount or 0 for l in lines), Decimal("0"))
    tax = sum((l.tax or 0 for l in lines), Decimal("0"))
    return amount, tax


def _merge_page_group(group: List[Invoice]) -> Invoice:
    """把同一张发票的几个页合成一张发票。

    合成后的取值口径：

    * **明细**：各页明细按页序接起来，再过一次折扣合并 —— 折扣行在票上
      紧跟它所属的项目，而跨页时那个项目可能印在上一页的末尾，
      只在页内合并会认不出归属。
    * **价税合计**：取**与合并后明细对得上**的那一页；对不上就取最大的一张
      （分页票的总计印在最后一页，它一定是最大的那个数）。
    * **备注、开票人、大写金额**：总在最后那一页，空的就往后找。
    """
    base = group[0]
    meta = replace(base.meta)
    lines = merge_discount_lines([line for inv in group for line in inv.lines])
    sum_amount, sum_tax = _lines_total(lines)
    line_total = sum_amount + sum_tax

    exact = None
    largest = None
    for inv in group:
        total = inv.meta.total_incl_tax
        if total is None:
            continue
        if largest is None or total > largest.meta.total_incl_tax:
            largest = inv
        if exact is None and abs(total - line_total) <= Decimal("0.05"):
            exact = inv
    best = exact or largest
    if best is not None:
        meta.total_excl_tax = best.meta.total_excl_tax
        meta.total_tax = best.meta.total_tax
        meta.total_incl_tax = best.meta.total_incl_tax
    for inv in group:
        if not meta.remark and inv.meta.remark:
            meta.remark = inv.meta.remark
        if not meta.issuer and inv.meta.issuer:
            meta.issuer = inv.meta.issuer
        if not meta.total_in_c_c and inv.meta.total_in_c_c:
            meta.total_in_c_c = inv.meta.total_in_c_c

    merged = Invoice(
        page_index=base.page_index,
        page_span=len(group),
        meta=meta,
        lines=lines,
        raw_text="\n".join(inv.raw_text for inv in group),
    )
    # 每页自己的金额警告丢掉（比的是本页小计），其余警告留着
    kept: List[str] = []
    for inv in group:
        for part in (inv.parse_warning or "").split("; "):
            part = part.strip()
            if part and not part.startswith(_AMOUNT_WARNING_PREFIX):
                kept.append(part)
    merged.parse_warning = "; ".join(kept)
    if merged.meta.total_incl_tax is not None and merged.lines:
        if abs(line_total - merged.meta.total_incl_tax) > Decimal("0.05"):
            merged.parse_warning = "; ".join(filter(None, [
                merged.parse_warning,
                f"{_AMOUNT_WARNING_PREFIX}：明细 {sum_amount:.2f} + 税额 {sum_tax:.2f}"
                f" = {line_total:.2f}，与价税合计 {merged.meta.total_incl_tax:.2f} 不一致。",
            ]))
    return merged


def _group_is_one_invoice(group: List[Invoice]) -> bool:
    """这几页是**一张发票**的续页，还是**同一张票传了两遍**？

    两条判据，任意一条成立就当续页：

    1. 票面自己标了「共N页 第M页」—— 最硬的证据；
    2. 合并后的明细之和正好等于票上的价税合计 —— 说明这几页是**互补**的
       （各出几行，加起来正好是整张票）。

    两条都不成立就**不合并**：那样它们多半是同一张票传了两遍，
    留着让原有的查重去报「重复发票号码」—— 查重没有被放宽。
    """
    for inv in group:
        if pagination_marker(inv.raw_text):
            return True
    # 这里**刻意不**调用 merge_discount_lines：那个函数是原地改金额的
    # （prev.amount += discount.amount），在这里先跑一遍、_merge_page_group
    # 里再跑一遍，跨页的折扣行就会被**并两次** —— 金额凭空多出一截。
    # 折扣合并只是把金额在行之间挪位置，**总和不变**，所以直接用原始行求和
    # 得到的判据与合并后完全一样。
    raw_lines = [line for inv in group for line in inv.lines]
    sum_amount, sum_tax = _lines_total(raw_lines)
    line_total = sum_amount + sum_tax
    for inv in group:
        total = inv.meta.total_incl_tax
        if total is not None and abs(total - line_total) <= Decimal("0.05"):
            return True
    return False


def _group_is_full(group: List[Invoice], marker: tuple[int, int] | None) -> bool:
    """这一组已经凑齐了（该另起一组了）。

    两种情形：

    * 这一页自称「第1页」—— 那它是**另一张**票的第一页；
    * 这一组已经有票面说的那么多页（共N页，凑满N页）—— 再加就是下一张了。

    这条判断是「同一张票传两遍」不被误合并的关键：第二遍的第1页自称
    「共3页 第1页」，于是另起一组，两组各合成一张发票，号码相同 ——
    查重照旧报「重复发票号码」。
    """
    if not marker:
        return False
    total, page = marker
    return page == 1 or len(group) >= total


def _group_pages(invoices: List[Invoice]) -> List[List[Invoice]]:
    """把**相邻、同号**的页分成组。"""
    groups: List[List[Invoice]] = []
    for inv in invoices:
        no = inv.meta.invoice_no
        marker = pagination_marker(inv.raw_text)
        if groups and no and groups[-1][-1].meta.invoice_no == no \
                and not _group_is_full(groups[-1], marker):
            groups[-1].append(inv)
        else:
            groups.append([inv])
    return groups


def merge_invoice_pages(invoices: List[Invoice]) -> List[Invoice]:
    """把**同一张发票的多个页**合成一张发票。

    为什么需要：增值税电子发票的「分页」票会把一张发票拆成几页印，每页都
    印着**同一个发票号码**、同一个买卖方，每页只有自己那几行明细，
    价税合计只印在最后一页。解析层原来「一页一张发票」，于是 3 页的发票
    被当成 3 张发票 —— 它们号码相同，于是「本次上传中有重复发票号码」
    把整批拦下，用户看着一头雾水：明明只传了一张。

    怎么分组：**相邻**且发票号码相同的页归成一组，凑满票面说的页数
    （「共3页」）或遇到下一个「第1页」就另起一组。相邻 + 凑页数是判重
    不被放宽的关键 —— 同一张票传两遍会分成两组，查重照旧报错。

    组内还要再判一次是不是同一张票（见 ``_group_is_one_invoice``），
    判不是就**不合并**。
    """
    if len(invoices) < 2:
        return invoices
    out: List[Invoice] = []
    for group in _group_pages(invoices):
        if len(group) == 1 or not _group_is_one_invoice(group):
            out.extend(group)
        else:
            out.append(_merge_page_group(group))
    return out


def parse_pdf(pdf_path: str) -> List[Invoice]:
    pages = extract_pages_text(pdf_path)
    return [parse_page(i, t) for i, t in enumerate(pages)]
