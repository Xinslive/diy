"""PDF 对账单 → 网格。

PDF 是这批对账单里**最难**的一种容器：它没有「行 × 列」的天然结构，
只有一堆画在页面上的文字。银行的对账单是**真表格**（有框线），但框线
不保证每一家都画。

这里用两层策略：

**第一层（主）：按 PDF 里的文字对象还原。**

pdfium 能给出页面上每一个「文字对象」（也就是一次绘制的一段文字）的
矩形范围。于是：

- **表头行** = 认出的字段最多的一行文字；
- **列** = 表头那一行里每段文字的 x 范围（一个表头格就是一段文字）；
- **行** = 用「日期列」当锚：日期格里折行的两段文字（``2026-09-26 07:``
  与 ``20:10``）能拼成一个日期，就归成同一格；拼不成就是新的一行；
- **单元格** = 一行里落在同一列 x 范围的几段文字，按 y 顺序首尾相接。

**为什么不用「逐字符取坐标」**：这一批 PDF 用的是 CID 字体，
pdfium 返回的字符框**宽度是错的**（一个 8pt 的汉字会返回 60pt 宽的框），
按字符框排序会得到「兴业行银」这种交错结果。而**文字对象级别的矩形
是准的** —— 它就是绘制时用的那几个矩形。

**第二层（兜底）：文本流 + 锚点。** 连文字对象都取不到（扫描件）时，
交给上层报「这份 PDF 没有文本层」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.bankstatement.aliases import match_field
from app.bankstatement.grid import Grid

#: 一段文字算「像一个日期」的判据：整段里**只有一个**日期样式。
_DATE_PATTERN = re.compile(r"\d{8}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")

#: 一段文字**整个**就是一个日期/时间时的严格判据。
#:
#: 比 ``_DATE_PATTERN`` 严得多：后者只要「里面有一个日期」，前者要求
#: 「通篇都是日期」。用后者判断「这两段文字是不是同一个日期折行成两段」——
#: 否则表尾的「本期总笔数：6」也会被拼进日期里，日期格当场作废。
_DATE_ONLY = re.compile(
    r"^(?:\d{8}(?:\d{2,6})?|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:\d{1,2}(?::?\d{1,2})*)?)$")


@dataclass
class _Run:
    """页面上的一段文字（PDF 的一次绘制）。"""

    left: float
    bottom: float
    right: float
    top: float
    text: str

    @property
    def y(self) -> float:
        return (self.bottom + self.top) / 2

    @property
    def height(self) -> float:
        return max(0.5, self.top - self.bottom)


def _extract_runs(page) -> tuple[list[_Run], Any]:
    """一页上的所有文字对象，外加这一页的 textpage。"""
    import pypdfium2 as pdfium

    textpage = page.get_textpage()
    runs: list[_Run] = []
    for obj in page.get_objects():
        if obj.type != 1:  # 1 = 文字；2 = 路径（框线）
            continue
        try:
            left, bottom, right, top = obj.get_bounds()
        except Exception:  # noqa: BLE001
            continue
        if right - left <= 0 and top - bottom <= 0:
            continue
        # 宽度退化到 1pt 以下的文字块是**不可见的残留**：PDF 里的表单域
        # 名字（``elect_sign``）与换行标记都长这样。留着它，账户余额那一格
        # 会变成「elect_sign90,272.91」，金额当场解析不出来。
        if (right - left) < 1.0:
            continue
        try:
            text = textpage.get_text_bounded(left, bottom, right, top)
        except Exception:  # noqa: BLE001
            continue
        text = _clean(text)
        if not text:
            continue
        runs.append(_Run(left, bottom, right, top, text))
    return runs, textpage


def _clean(text: str) -> str:
    """去掉绘制时带出来的换行与首尾空白。

    保留中间的空格 —— 「交易时间」和「交易 时间」是两回事（虽然归一化
    之后一样），而 ``' 账号：79600188000046872 '`` 里的前后空格只是排版。
    """
    return re.sub(r"[\r\n\t]+", " ", str(text or "")).strip()


def _line_groups(runs: list[_Run]) -> list[list[_Run]]:
    """把文字按 y 聚成「视觉行」。

    判据是**竖直方向的重叠**：两段文字的竖直区间重合超过一半，就算同一行。
    比「基线相同」稳 —— 同一格里不同的文字对象基线能差 1pt 以上，
    而相邻两行的间距有 4pt 以上，重叠率区分得开。
    """
    ordered = sorted(runs, key=lambda run: (-run.y, run.left))
    groups: list[list[_Run]] = []
    for run in ordered:
        placed = False
        for group in groups:
            top = max(item.top for item in group)
            bottom = min(item.bottom for item in group)
            overlap = min(top, run.top) - max(bottom, run.bottom)
            smallest = min(top - bottom, run.height)
            if smallest > 0 and overlap / smallest >= 0.5:
                group.append(run)
                placed = True
                break
        if not placed:
            groups.append([run])
    return groups


def _header_score(group: list[_Run]) -> int:
    """这一行像不像表头：认出的字段数。"""
    fields = {match_field(run.text)[0] for run in group if match_field(run.text)}
    return len(fields)


def _split_header_run(textpage, run: _Run) -> list[tuple[str, float, float]]:
    """一段表头文字里塞了多个列名时，把它们按 x 拆开。

    少数银行把整行表头画成**一段**文字（「序号 交易日期 时间 借/贷 …」）。
    这时列边界不能从文字对象拿到，得在它的 x 范围里二分出每个词的边界。

    二分的判据是「取到左半边时，左半边里有几个词」：词数变了就说明
    跨过了一个词的边界。
    """
    tokens = [token for token in re.split(r"\s+", run.text) if token]
    if len(tokens) <= 1:
        return [(run.text, run.left, run.right)]

    def probe(x: float) -> list[str]:
        try:
            text = textpage.get_text_bounded(run.left, run.bottom, x, run.top)
        except Exception:  # noqa: BLE001
            return []
        return [token for token in re.split(r"\s+", _clean(text)) if token]

    boundaries: list[float] = []
    low, high = run.left, run.right
    for index in range(1, len(tokens)):
        target = index
        left, right = low, high
        for _ in range(24):
            middle = (left + right) / 2
            if len(probe(middle)) > target:
                right = middle
            else:
                left = middle
        boundaries.append(left)
    pieces: list[tuple[str, float, float]] = []
    previous = run.left
    for index, token in enumerate(tokens):
        end = boundaries[index] if index < len(boundaries) else run.right
        pieces.append((token, previous, end))
        previous = end
    return pieces


def _assign_column(run: _Run, columns: list[tuple[float, float, str]]) -> int | None:
    """一段文字属于哪一列。

    判据是**与表头 x 区间的重叠**：重叠最多的那一列。重叠为零时退化成
    「离表头最近的那一列」—— 有的银行把数字在格子里右对齐，数字的中点
    会跑到表头文字左边，光看重叠会认错。
    """
    best_index: int | None = None
    best_overlap = 0.0
    for index, (left, right, _label) in enumerate(columns):
        overlap = min(run.right, right) - max(run.left, left)
        if overlap > best_overlap:
            best_overlap = overlap
            best_index = index
    if best_index is not None:
        return best_index
    best_distance = None
    for index, (left, right, _label) in enumerate(columns):
        if run.left < left:
            distance = left - run.right
        elif run.right > right:
            distance = run.left - right
        else:
            distance = 0.0
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def _looks_like_one_date(text: str) -> bool:
    """整段文字里只有一个日期样式 —— 也就是「这是一个日期，不是两个」。"""
    return len(_DATE_PATTERN.findall(text)) == 1


def _is_date_text(text: str) -> bool:
    """整段文字**通篇**都是日期/时间（允许折行后少了空格）。"""
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    return bool(_DATE_ONLY.match(compact))


def _date_cells(runs: list[_Run]) -> list[list[_Run]]:
    """把日期列里的文字按「格」分组。

    日期在格子里折行是常事（``2026-09-26 07:`` + ``20:10``）。判据不是
    距离，而是**拼起来还是一个日期**：能拼成就同格，拼不成就是新的一行。
    """
    ordered = sorted(runs, key=lambda run: (-run.y, run.left))
    cells: list[list[_Run]] = []
    for run in ordered:
        if cells:
            current = cells[-1]
            merged = "".join(item.text for item in current) + run.text
            if _is_date_text(merged):
                current.append(run)
                continue
        cells.append([run])
    return cells


def extract_grid(data: bytes) -> Grid | None:
    """把一份 PDF 对账单读成网格；读不出来返回 ``None``。

    **两层**：先按文字对象还原（准），取不到就退回纯文本流切分（粗）。
    """
    grid = _extract_grid_by_runs(data)
    if grid is not None:
        return grid
    return extract_grid_from_text(data)


def _extract_grid_by_runs(data: bytes) -> Grid | None:
    """第一层：按文字对象的矩形还原表格。"""
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(data)
    except Exception:  # noqa: BLE001 —— pdfium 的异常类型随版本变
        return None

    all_runs: list[_Run] = []
    textpage = None
    try:
        for index in range(len(document)):
            page = document[index]
            runs, textpage = _extract_runs(page)
            all_runs.extend(runs)
            page.close()
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001
            pass

    if not all_runs:
        return None

    groups = _line_groups(all_runs)
    if not groups:
        return None

    # 表头 = 认出的字段最多的那一组
    header_group = max(groups, key=_header_score)
    if _header_score(header_group) < 2:
        return None
    header_bottom = min(run.bottom for run in header_group)

    # 列 = 表头那一组里每段文字的 x 范围
    columns: list[tuple[float, float, str]] = []
    for run in sorted(header_group, key=lambda item: item.left):
        pieces = _split_header_run(textpage, run)
        for text, left, right in pieces:
            columns.append((left, right, text))
    if not columns:
        return None

    # PDF 的 y 轴**朝上**：页面上「在表头上方」的文字 y 更大。
    # 判反了会把元信息当数据、把数据当元信息（那一版就是这个问题）。
    meta_lines: list[str] = []
    data_runs: list[_Run] = []
    for group in groups:
        if group is header_group:
            continue
        bottom = min(run.bottom for run in group)
        if bottom < header_bottom:
            data_runs.extend(group)
        else:
            meta_lines.append(" ".join(run.text for run in sorted(group, key=lambda item: item.left)))

    if not data_runs:
        return None

    # 行 = 以日期列当锚
    date_index = _find_date_column(columns, data_runs)
    rows: list[list[str]] = []
    if date_index is not None:
        date_runs = [run for run in data_runs if _assign_column(run, columns) == date_index]
        cells = _date_cells(date_runs)
        centers = [sum(run.y for run in cell) / len(cell) for cell in cells]
        # 行与行之间取中点当分界：落在分界之外、又不在任何行里的文字
        # 是表尾的合计/提示语，归 footer（它们是元信息，不是流水）。
        bounds = []
        for index in range(len(centers) - 1):
            bounds.append((centers[index] + centers[index + 1]) / 2)
        # 最后一行往下也留一格：再下面就是「本期总笔数」这类合计语，
        # 它们是元信息，不是流水。
        # 最后一行**往下**再留半格：再下面就是「本期总笔数」这类合计语。
        lower_edge = centers[-1] - (centers[-2] - centers[-1]) / 2 if len(centers) > 1 \
            else centers[-1] - 12.0
        buckets: list[list[_Run]] = [[] for _ in cells]
        footer_runs: list[_Run] = []
        for run in data_runs:
            # 行带：第 i 行 = (bounds[i], bounds[i-1])，centers 与 bounds 都是
            # 从上往下递减的，所以「y 大于某个分界」就是「属于更靠上的行」。
            index = len(bounds)
            for position, bound in enumerate(bounds):
                if run.y > bound:
                    index = position
                    break
                index = position + 1
            if run.y < lower_edge:
                footer_runs.append(run)
                continue
            buckets[index].append(run)
        for bucket in buckets:
            if not bucket:
                continue
            rows.append(_row_cells(bucket, columns))
        footer_lines = [" ".join(run.text for run in sorted(footer_runs, key=lambda item: -item.y))] \
            if footer_runs else []
    else:
        # 没有日期列：按 y 间隙把数据行切开（阈值取间隙中位数的两倍）
        ordered = sorted(data_runs, key=lambda run: -run.y)
        gaps = [ordered[i].y - ordered[i + 1].y for i in range(len(ordered) - 1)]
        gaps = [gap for gap in gaps if gap > 0]
        threshold = (sorted(gaps)[len(gaps) // 2] * 2) if gaps else 6.0
        current: list[_Run] = []
        previous_y = None
        for run in ordered:
            if previous_y is not None and previous_y - run.y > threshold:
                rows.append(_row_cells(current, columns))
                current = []
            current.append(run)
            previous_y = run.y
        if current:
            rows.append(_row_cells(current, columns))

    header_cells = [label for _left, _right, label in columns]
    grid_rows = [header_cells] + rows
    return Grid(container="PDF", sheet="PDF", rows=grid_rows, meta=meta_lines, footer=footer_lines)


def _find_date_column(columns, data_runs: list[_Run]) -> int | None:
    """哪一列是日期列。

    先看表头认不认得出（``交易日期`` / ``交易时间`` / ``日期``）；
    认不出就找**数据里全是日期**的那一列。
    """
    for index, (_left, _right, label) in enumerate(columns):
        hit = match_field(label)
        if hit and hit[0] in ("DATE", "TIME", "POSTING_DATE"):
            return index
    for index in range(len(columns)):
        values = [run.text for run in data_runs if _assign_column(run, columns) == index]
        dates = [value for value in values if _looks_like_one_date(value)]
        if dates and len(dates) >= max(1, len(values) // 2):
            return index
    return None


def _row_cells(bucket: list[_Run], columns) -> list[str]:
    """一行里的文字 → 每列一个字符串。"""
    cells = [""] * len(columns)
    per_column: dict[int, list[_Run]] = {}
    for run in bucket:
        index = _assign_column(run, columns)
        if index is None:
            continue
        per_column.setdefault(index, []).append(run)
    for index, runs in per_column.items():
        # 同一格里折行的几段文字首尾相接，**不加分隔符**：
        # 「福建省高速公路集」+「团有限公司电子收」+「费管理中心」
        # 拼出来才是完整的户名。
        cells[index] = "".join(run.text for run in sorted(runs, key=lambda item: -item.y))
    return cells


# ----------------------------------------------------------------------
# 第二层：纯文本流 + 锚点切分
# ----------------------------------------------------------------------
#
# **什么时候才走这一层**：取不到文字对象的坐标（有些 PDF 的字体信息缺失，
# 或者整页是图片）。这时只剩一条路：把页面的纯文本按行取出来，用
# 「表头是锚 + 金额正则 + 长数字串」把每行切成格子。
#
# **它比坐标那一层弱**：字符位置不是真实坐标，表头与数据行的列宽对不上时
# 会切错。所以它只当兜底 —— 坐标那层能用就用坐标那层。
#
# **切分规则**：
#
# 1. 表头行 = 认出的字段最多的那一行；
# 2. **段数与表头相同 → 按位置对号入座**（最准的一档）；
# 3. 段数不同 → **按内容认**：日期 → 日期列，方向词 → 方向列，
#    长数字串 → 对方账号列，金额 → 按表头里「收入/支出/余额」出现的先后
#    依次对上；剩下的按顺序填空。

_AMOUNT_TOKEN = re.compile(r"^[¥￥]?[-+]?[\d,]+(?:\.\d+)?$")
_DATE_TOKEN = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$|^\d{8}$")
_ACCOUNT_TOKEN = re.compile(r"^\d{6,}(\*{2,}\d+)?$|^\d{6,}$")


def plain_text_lines(data: bytes) -> list[str]:
    """把 PDF 的纯文本按行取出来（没有坐标的那条路）。

    **一行 = 一个视觉行**。银行的表格在纯文本里通常就是一行一行印的，
    所以「按行切」这一步在多数情况下是对的。
    """
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(data)
    except Exception:  # noqa: BLE001
        return []
    lines: list[str] = []
    try:
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range()
            except Exception:  # noqa: BLE001
                text = ""
            finally:
                textpage.close()
                page.close()
            for line in str(text or "").splitlines():
                line = line.strip()
                if line:
                    lines.append(line)
    except Exception:  # noqa: BLE001
        return lines
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001
            pass
    return lines


def _header_index(lines: list[str]) -> int:
    """哪一行是表头：认出的字段最多的那一行。"""
    best_index = -1
    best_score = 0
    for index, line in enumerate(lines):
        fields = {match_field(token)[0] for token in line.split() if match_field(token)}
        if not fields:
            continue
        has_money = bool(fields & {"AMOUNT", "IN", "OUT"})
        has_date = bool(fields & {"DATE", "TIME", "POSTING_DATE"})
        if not (has_money and has_date):
            continue
        if len(fields) > best_score:
            best_score = len(fields)
            best_index = index
    return best_index


def _grid_from_lines(lines: list[str]) -> Grid | None:
    """纯文本行 → 网格。"""
    header_index = _header_index(lines)
    if header_index < 0:
        return None
    header = lines[header_index].split()
    if len(header) < 3:
        return None
    # 表头各列对应的字段（认不出的列留 None）
    fields = [match_field(token)[0] if match_field(token) else None for token in header]
    # 「收入 / 支出 / 余额 / 金额」这几列在表头里的先后次序 ——
    # 数据行里的金额按这个次序依次往上对。
    money_order = [i for i, f in enumerate(fields) if f in ("IN", "OUT", "AMOUNT", "BALANCE")]

    meta_lines = lines[:header_index]
    rows: list[list[str]] = [list(header)]
    for line in lines[header_index + 1:]:
        tokens = line.split()
        if not tokens:
            continue
        cells = _cells_for_line(tokens, header, fields, money_order)
        if cells is None:
            continue
        rows.append(cells)
    footer = [line for line in lines[header_index + 1:] if _is_footer_line(line)]
    return Grid(container="PDF", sheet="PDF", rows=rows, meta=meta_lines, footer=footer)


def _is_footer_line(line: str) -> bool:
    return bool(re.search(r"合计|小计|笔数|共\s*\d+\s*笔|打印|第\s*\d+\s*页", line))


def _cells_for_line(tokens: list[str], header: list[str], fields: list[str | None],
                    money_order: list[int]) -> list[str] | None:
    """一行文本 → 每列一个字符串。"""
    width = len(header)
    # **档一：段数与表头相同 → 按位置对号入座。**
    if len(tokens) == width:
        return list(tokens)
    # **档二：按内容认。**
    cells = [""] * width
    money_slots = list(money_order)
    money_used = 0
    date_used = False
    direction_used = False
    for token in tokens:
        if _DATE_TOKEN.match(token) and not date_used:
            date_used = True
            for index, field in enumerate(fields):
                if field in ("DATE", "TIME", "POSTING_DATE"):
                    cells[index] = token
                    break
            continue
        if _AMOUNT_TOKEN.match(token) and money_used < len(money_slots):
            cells[money_slots[money_used]] = token
            money_used += 1
            continue
        if _ACCOUNT_TOKEN.match(token):
            for index, field in enumerate(fields):
                if field in ("CP_ACCOUNT", "PAYER_ACCOUNT", "PAYEE_ACCOUNT"):
                    cells[index] = token
                    break
            continue
        if match_field(token) and not direction_used:
            hit = match_field(token)[0]
            if hit == "DIRECTION":
                direction_used = True
                for index, field in enumerate(fields):
                    if field == "DIRECTION":
                        cells[index] = token
                        break
                continue
        # 剩下的按顺序填进还没填的列
        for index in range(width):
            if not cells[index]:
                cells[index] = token
                break
    if not any(cells):
        return None
    return cells


def extract_grid_from_text(data: bytes) -> Grid | None:
    """第二层：纯文本流 → 网格。取不到就返回 ``None``。"""
    lines = plain_text_lines(data)
    if not lines:
        return None
    return grid_from_text_lines(lines)


def grid_from_text_lines(lines: list[str]) -> Grid | None:
    """已经拿到文本行 → 网格。

    OCR 兜底走的是同一个入口：OCR 给回来的也是一页页文本，
    与 PDF 文本层给的形状一样。
    """
    try:
        return _grid_from_lines(lines)
    except Exception:  # noqa: BLE001 —— 兜底层崩了也不能带倒主路径
        return None


def has_text_layer(data: bytes) -> bool:
    """这份 PDF 有没有文本层。

    **没有文本层 = 扫描件** —— 那才轮到 OCR 兜底。有文本层却解析不出来
    的话是版面认错（第一层的问题），OCR 同样认不对，白花一次钱。
    """
    return bool(plain_text_lines(data))
