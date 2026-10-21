"""打印与导出。

两条输出通道：

* **Excel** —— 用于二次加工、数据核对、导入别的系统。
* **HTML**  —— 用于「所见即所得」的预览与打印。预览 iframe 和最终打印走的是
  同一个渲染函数，所以模板改了什么，打出来就是什么样，不存在两套排版。

打印模板存在 ``print_template.content``（JSON）里，财务可以在前端设计器里
自助改版式，不需要改代码。
"""
from __future__ import annotations

import calendar
import functools
import html
import urllib.parse
from copy import deepcopy
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.reports import _visible_template
from app.api.vouchers import _book_subjects, _serialize
from app.core.db import get_db
from app.core.deps import get_book_link, get_current_user, get_my_book, require_book_write
from app.models import Book, Period, PrintTemplate, ReportTemplate, Subject, SysUser, UserBook, Voucher
# ``preview`` 是轻量的（只依赖 sqlalchemy），留在模块级。
#
# ``excel`` 则是**故意延迟导入**的：它在模块级 import openpyxl，而 openpyxl
# 会连带把 lxml 拖进来 —— 实测 openpyxl + lxml + olefile + xlrd 合计
# **18.7 MB / worker**。这份重量只为「用户偶尔导一次 Excel」而常驻，
# 而导出是一个请求的事，不是进程启动时必须具备的能力。
#
# 写法沿用仓库里已有的先例（app/api/books.py 的期初余额导入入口就是
# 函数内 ``from app.printing import excel as xl``），不是引入新约定。
from app.printing import preview as pv
from app.printing.template import (
    DEFAULT_TEMPLATES,
    DIRECTIONS,
    PAPERS,
    SCOPES,
    render_table_page,
    wrap_pages,
    render_voucher_sheet,
    split_voucher,
    validate_template,
    voucher_rows_per_page,
    voucher_slots_per_page,
)

router = APIRouter(prefix="/api/books/{book_id}/print", tags=["打印与导出"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _deep_merge(base, override):
    """模板补丁叠加：递归合并，两边都是 dict 时逐层合并。"""
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else v
        return out
    return base if override is None else override


def _filename(name: str) -> str:
    return "attachment; filename*=UTF-8''" + urllib.parse.quote(name)


def _period(db: Session, book_id: int, period_id: int | None) -> Period:
    """取**本账套**的会计期间；给了 id 而不属于本账套 → 404。

    **归属校验必须在这一处。** ``period_id`` 是前端传来的任意整数，不是授权
    凭据：``Period.ord`` 是**账套内**相对序号，别家的期间拿过来会被当成
    「本账套的某一期」去取数 —— 于是「按本账套的某一期取数、贴上别家的期间
    标签」（HTTP 200 + 错数据），顺带泄露别家账套的期间码（存在了、启用到哪
    年哪月）。

    这条通道是 **9 个**打印/导出端点共用的（凭证 / 余额表 / 总账 / 明细账 /
    报表的打印与导出），所以校验收在这里 —— 逐个端点补一遍，早晚有一处漏。

    与 ``app/api/reports.py`` 的写法一致：不存在与不是你的落在同一句 404 上，
    这条接口才不是跨账套的存在性探测器。
    """
    if period_id:
        p = db.get(Period, period_id)
        if p is None or p.book_id != book_id:
            raise HTTPException(404, "会计期间不存在")
        return p
    p = db.scalar(
        select(Period).where(Period.book_id == book_id)
        .order_by(Period.ord.desc()).limit(1))
    if p is None:
        raise HTTPException(404, "会计期间不存在")
    return p


def _visible_templates(book_id: int, user_id: int):
    """**当前这个人**在当前账套里看得见的模板：三层并集。

    加上 ``print_template.user_id``（迁移 0023）之后，模板分三层归属，见
    ``models/printing.py``：系统基线（两个 NULL，人人有）、账套模板
    （``book_id = X``，本账套）、个人私有副本（``user_id = 我``，只给我）。

    **别人的私有副本不在可见范围里，这是这一层唯一的存在理由。** 它一旦漏出来
    后果比「跨账套看得见别的版式」更重：那不是串了口径，是把一个人对凭证纸的
    个人偏好贴到全公司眼前。所以这里只按 ``user_id = 自己`` 取，绝不按
    「不是我的就排除」的黑名单方式实现 —— 判据只有一个等式，不认识的人自动落
    在外面。

    ``print_template.book_id IS NULL`` 那一层是「全局兜底」：迁移之前建的存量
    模板没有任何一个账套真的拥有它们（见 ``models/printing.py``），把它们回填到
    任意一个账套等于替人断言归属。算进可见范围是让老数据继续能打；它们
    **不参与**「设为默认」的互相清除，是让新数据从一开始就有主。
    """
    # **不能用 ``book_id.in_([book_id, None])``** —— 这是本次实测踩到的坑：
    # SQL 是三值逻辑，``NULL IN (6, NULL)`` 求值成 ``NULL``（不是 true），
    # 于是全局兜底模板会被 WHERE **悄悄滤掉**，一条都不返回。
    # 那个写法只在「没有 NULL 行」时与本式等价，而本式的全部意义恰恰是那几行。
    return or_(
        # 私有副本：判据是「owner 就是我」这一个等式，天然排掉所有人。
        PrintTemplate.user_id == user_id,
        # 另两层都要求「不是私有的」，免得私有副本从这一支再进来一次。
        and_(
            PrintTemplate.user_id.is_(None),
            or_(PrintTemplate.book_id == book_id, PrintTemplate.book_id.is_(None)),
        ),
    )


def _is_baseline(t: PrintTemplate) -> bool:
    """系统基线：不属于任何账套，也不属于任何个人（迁移 0022 种下的那两份）。

    它是**写时复制的源** —— 改它会先复制一份只有改动者看得见的私有副本，
    基线自己一个字节都不动。所以「基线」这个判定在读写两侧都要用到，
    写成函数是为了让两侧不可能各写一份。
    """
    return t.book_id is None and t.user_id is None


def _scoped_template(db: Session, book: Book, template_id: int, user_id: int) -> PrintTemplate:
    """按 id 取模板，并且校验它对**当前这个人**可见。

    ``template_id`` 是前端传来的任意整数，**不是授权凭据**：没有这一层校验，
    任何账套都能拿着别人的 id 进打印页，任何人都能拿着别人的私有副本 id
    进设计器。口径必须与 ``_template`` 的取用口径一致（同一份
    ``_visible_templates``），否则会出现「列表里看不见、却能按 id 打出来」
    这种更糟的状态。

    **可见性直接写进 WHERE，而不是 ``db.get`` 之后再判断。** 写成
    ``db.get(...) + if not 可见`` 的话，可见与否就有两份实现
    （SQL 一个表达式、Python 一个 if），迟早有一处先改、另一处没改 ——
    眼前这处就是这么漏的：先按 id 拿到行，再拿 ``book_id not in (book.id, None)``
    判断，那份判断里**根本没有 user_id**，私有副本会从缝里漏过去。
    """
    t = db.scalar(
        select(PrintTemplate)
        .where(PrintTemplate.id == template_id,
               _visible_templates(book.id, user_id))
    )
    if t is None:
        # 跨账套、别人的私有副本与不存在返回同一个 404：不区分「没有」和
        # 「不是你的」，否则这条接口本身就成了一个存在性探测器。
        raise HTTPException(404, "打印模板不存在")
    return t


def _scoped_report_template(db: Session, book: Book, template_id: int) -> ReportTemplate:
    """按 id 取**报表**模板，并且校验它对当前账套可见。

    与上面 ``_scoped_template`` 是同一个道理：``report_template_id`` 同样是
    前端传来的任意整数，同样不是授权凭据。钱不会漏 —— ``evaluate`` 取数按
    调用方的账套过滤科目 —— 但另一家账套的**报表名、行标签、列标签和取数
    公式**会整份印出来，而那正是「这家单位怎么算账」。三个调用点（报表打印、
    报表 Excel 导出、结果落库）以前都只判了「模板存在」，没有判「归属」。

    看得见的口径与打印版式模板**不一样**，所以不能直接用 ``_scoped_template``：
    报表模板的官方基线是 ``book_id IS NULL``（所有账套共看的那一份），可见
    范围是「基线 + 本账套」，判定在 ``app.api.reports._visible_template``。

    这里**导入**它而不是把判定重抄一遍：可见口径一旦分成两份实现，迟早有一处
    先改、另一处没改，于是「列表里看不见、却能按 id 打印出来」这种更糟的状态
    又回来了 —— 眼前这三处就是这么漏的。同理，不存在与不是你的必须落在同一句
    404 上，那句接口才不是跨账套的存在性探测器。
    """
    return _visible_template(db, template_id, book)


def _template(db: Session, book: Book, scope: str, template_id: int | None,
              user_id: int) -> PrintTemplate:
    if template_id is not None and template_id != 0:
        return _scoped_template(db, book, template_id, user_id)
    # 0 是明确选择内置版式；未传 id 才沿用账套默认模板。
    t = None if template_id == 0 else db.scalar(
        select(PrintTemplate)
        .where(PrintTemplate.scope == scope, PrintTemplate.is_default.is_(True),
               _visible_templates(book.id, user_id))
        # 三层由近及远：**我的私有副本 → 本账套模板 → 全局基线**。ASC 排序下
        # 布尔表达式算出 false 的排前面，于是 ``user_id IS NULL``（我的副本算
        # false、其余算 true）先把私有副本顶上来；再按 ``book_id IS NULL``
        # 把本账套模板排在基线前面。全程不依赖 id 大小这类偶然。
        #
        # 前端 ``utils/printTemplate.js::effectiveDefaultId`` 照这个顺序复算
        # 一遍，好让「默认」那个标记指的就是真会印出来的那份。改这里就得改那里。
        .order_by(PrintTemplate.user_id.is_(None),
                  PrintTemplate.book_id.is_(None), PrintTemplate.id)
        .limit(1)
    )
    if t is None:
        # 兜底用内置默认值，保证任何时候都能打印
        class _Virtual:
            id = None
            name = "内置默认"
            remark = ""
            content = DEFAULT_TEMPLATES[scope]

        v = _Virtual()
        v.scope = scope          # 实例属性：validate_template 需要读 t.scope
        return v
    return t


def _tpl_dict(t) -> dict:
    return validate_template(t.scope, t.content or {})


def _with_orientation(tpl: dict, orientation: str | None) -> dict:
    """按本次打印的要求临时改方向 —— 只影响这一次，不落库。

    **为什么单独开这条路**：方向原先只有一条入口，就是改模板存起来。但临时
    换向是高频且一次性的小事 —— 同一本账的明细账平时竖着打，某几张要横着
    夹进汇报材料，为此去改「默认模板」等于把整本账的默认方向都换掉，
    后面每一次正常打印都得记得改回来。改回去这件事必然有人忘。

    所以这里只在**这一页**生效：新函数返回一份新字典，``tpl`` 本身不动，
    模板表里的那份内容更是原封不动。下一次不带参数打印，拿回来的仍是原来
    的方向。

    不给参数就原样返回同一个对象 —— 不复制，也就没有「为了打印而复制出
    一份字典」这种看不出来的开销。

    **纸型为什么不在这里**：纸型已经全局只剩 A4 了（``validate_template`` 会
    把任何纸型归一成 A4），再加一个「临时换纸」的口子只会让调用方以为还有
    别的纸可选，然后拿到一个被静默改掉的值 —— 那个值和用户按下的按钮对不上，
    比直接不支持更坏。现在这里只剩方向这一个可临时覆盖的维度。
    """
    if orientation is None:
        return tpl
    if orientation not in DIRECTIONS:
        raise HTTPException(400, f"不支持的方向：{orientation}")
    # 覆盖没给的字段取模板自己的值；这一层只负责「替换用户指定的那几项」。
    return {**tpl, "orientation": orientation}


# ===================================================================== 打印模板 CRUD


class TemplateIn(BaseModel):
    scope: str
    name: str
    paper: Optional[str] = None
    content: dict = Field(default_factory=dict)
    is_default: bool = False
    remark: str = ""
    # 「新建即私有」这条口子的开关。**默认 False 是刻意的**（fail-closed）：
    # 不带这个标志的调用方拿到的仍是老契约 —— 建账套模板，OWNER 门禁不变
    # （tests/test_print_template.py::test_新建的模板归属当前账套 依赖这一点）。
    # 要放开的那条路必须**显式**开口子；否则哪天有人给 ``is_default`` 之类
    # 改了默认值、或给这个模型开了 ``extra="allow"`` 再传进来，权限会跟着
    # 一起放宽 —— 而那种改动看起来跟权限毫无关系。
    private: bool = False


class TemplatePatch(BaseModel):
    name: Optional[str] = None
    paper: Optional[str] = None
    content: Optional[dict] = None
    is_default: Optional[bool] = None
    remark: Optional[str] = None


#: 设计器**给用户选**的纸型。全局只剩 A4 —— 模板里存的其它纸型只是历史数据，
#: 由 ``validate_template`` 归一成 A4，不该再出现在选择框里让人以为还能选。
#: （``template.PAPERS`` 仍保留全部纸型，那是渲染/校验用的白名单，不是选项表。）
SELECTABLE_PAPERS = ("A4",)


@router.get("/templates/meta")
def template_meta(book: Book = Depends(get_my_book)):
    """设计器需要的元信息：可选纸型、字段字典、默认模板。

    **挂 ``get_my_book``**：这条路由在 ``/api/books/{book_id}/print`` 前缀下，
    路径里带着账套 id。它返回的是**设计器**要用的元信息（纸型白名单、字段
    字典、内置默认版式），而没有认证依赖时，任何人都能拿任意账套 id 取到
    内置版式的结构 —— 既没有权限门槛，也把「这个账套存在」这件事泄了出去。

    依赖挂在**函数**上而不是路径上：路径里那个 ``{book_id}`` 由前缀提供，
    ``get_my_book`` 读的是同一个 Path 参数（见 ``app/core/deps.py``），
    两边对得上。
    """
    return {
        "scopes": list(SCOPES),
        "papers": [{"key": k, **PAPERS[k], "landscape": k.endswith("_LANDSCAPE")}
                   for k in SELECTABLE_PAPERS],
        "defaults": DEFAULT_TEMPLATES,
    }


def _tpl_dict_of(t: PrintTemplate) -> dict:
    """列表/详情共用的序列化。**三层归属要在这里说清**，前端才能判断
    「这份我能改吗、改了会落到哪一层」。漏掉任何一个字段，前端就只能靠猜。"""
    return {
        "id": t.id, "scope": t.scope, "name": t.name, "paper": t.paper,
        "is_default": t.is_default, "remark": t.remark,
        "book_id": t.book_id, "user_id": t.user_id,
        "is_private": t.user_id is not None,
        "is_baseline": _is_baseline(t),
        "forked_from": t.forked_from_id,
    }


@router.get("/templates")
def templates(book: Book = Depends(get_my_book), db: Session = Depends(get_db),
              user: SysUser = Depends(get_current_user)):
    # 按「这个人 + 这个账套」过滤：跨账套、别人的私有副本一律不返回
    # （见 _visible_templates）。
    rows = db.scalars(
        select(PrintTemplate)
        .where(_visible_templates(book.id, user.id))
        .order_by(PrintTemplate.user_id.is_(None), PrintTemplate.book_id.is_(None),
                  PrintTemplate.scope, PrintTemplate.id)
    ).all()
    # **我 fork 过的那份基线不摆出来。** 摆出来就是列表里两份同名，用户得先猜
    # 哪一份在用 —— 而真正在用的恰恰是看不见的那份。报表模板那边是同一招
    # （app/api/reports.py：列表按 code 去重、副本优先）。
    # 判据用 ``forked_from_id`` 而不是 name：名字是用户自己可改的，改完就对不上。
    forked = {t.forked_from_id for t in rows
              if t.user_id == user.id and t.forked_from_id is not None}
    return [dict(_tpl_dict_of(t), content=t.content)
            for t in rows if not (_is_baseline(t) and t.id in forked)]


@router.get("/templates/{template_id}")
def template_detail(template_id: int, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    t = _scoped_template(db, book, template_id, user.id)
    merged = _tpl_dict(t)
    # 三层归属字段与列表接口保持一致（见 _tpl_dict_of）。
    return dict(_tpl_dict_of(t), content=merged, raw=t.content)


@router.post("/templates", status_code=201)
def create_template(payload: TemplateIn, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                    user: SysUser = Depends(get_current_user),
                    _link: UserBook = Depends(require_book_write)):
    # 两条路，一句话分开的是**「这份版式会落到哪一层、谁会看见它」**：
    #
    #  * ``private=False``（默认，老契约）：落在账套那一层（``book_id=book.id,
    #    user_id=NULL``），是要跟同事共用的东西，所以门禁仍按 OWNER 收紧。
    #  * ``private=True``（新建即私有）：落在个人那一层（``book_id=None,
    #    user_id=user.id``），只有改动者一个人看得见 —— 爆炸半径 1，
    #    因此**不挂** OWNER 门禁。
    #
    # 为什么私有那条路的门禁只要 ``require_book_write``（上面那个依赖）：
    # 它已经是「这个人在这个账套不是 VIEWER」的判据，而改动只对改动者可见，
    # 和 update_template 里写时复制那一支是同一道理由（见那里的注释）。
    # 真正的 OWNER 级门禁留给「会影响别人」的路径。
    #
    # require_book_write 两条路都仍然挂着：它让「每个写接口都挂了写依赖」保持
    # 机械可查（tests/test_viewer_matrix.py 遍历路由表检查），与 books.py 一致。
    if not payload.private:
        _require_global_template_write(link, user)
    if payload.scope not in SCOPES:
        raise HTTPException(400, f"未知打印范围：{payload.scope}，可选：{'、'.join(SCOPES)}")
    try:
        content = validate_template(payload.scope, payload.content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    t = PrintTemplate(
        book_id=None if payload.private else book.id,
        user_id=user.id if payload.private else None,
        # **私有新建不挂 forked_from_id。** 挂了就等于宣称「这一份是从某份基线
        # 复制来的」，而语义上这是**新画的一张**，不是把谁的基线据为私有。判据
        # 本身是有副作用的：``templates`` 列表会按 forked_from_id 把「我已经
        # fork 过的那份基线」藏起来（见那里的注释），挂错会让基线凭空从列表
        # 里消失，而我从没 fork 过它。
        scope=payload.scope, name=payload.name,
        paper=content.get("paper", "A4"),
        content=content, is_default=payload.is_default, remark=payload.remark)
    db.add(t)
    db.flush()
    if t.is_default:
        _clear_other_defaults(db, t)
    # 审计 detail 里写明落在哪一层。「账套模板」和「只有你可见的个人版式」
    # 事后看是两回事，日志上要分得开；action 名不变，避免多出一类查询不到
    # 自己写过的动作。
    _log(db, user, book.id, "CREATE_PRINT_TEMPLATE", t.id,
         f"新增{'我的私有版式（只有你可见）' if payload.private else ''}打印模板 {t.name}")
    db.commit()
    db.refresh(t)
    # 归属字段跟列表/详情接口对齐（见 _tpl_dict_of）：前端保存完要靠它决定
    # 「选中的那份」换成哪个 id（私有那份和账套那份不是同一行）。
    return dict(_tpl_dict_of(t), content=t.content)


@router.put("/templates/{template_id}")
def update_template(template_id: int, payload: TemplatePatch, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                    user: SysUser = Depends(get_current_user),
                    _link: UserBook = Depends(require_book_write)):
    t = _scoped_template(db, book, template_id, user.id)
    forked = False
    if _is_baseline(t):
        # **写时复制的那一步。** 目标是系统基线时不直接改它 —— 改成了大家都被
        # 改，基线就不成其为基线了。整份复制成 ``user_id = 当前用户`` 的私有副本，
        # 后面这次保存落在副本上，基线一个字节都不动。
        #
        # 门禁在这里**故意只要求「能写这个账套」**（``require_book_write`` 依赖，
        # 上面已经挂好）：改动只对改动者一个人可见，爆炸半径是 1，不是全站。
        # 真正的 OWNER 级门禁留给「会影响别人」的两层（账套模板、基线本身）。
        # 「用户也要可改」这句话在这一行落地：只有 OWNER 才能改基线的话，
        # 改版式就成了一个人的特权，而不是每个人都能顺手做的事。
        t = _fork_private(db, user, t)
        forked = True
    elif t.user_id != user.id:
        # 账套模板：改它等于改全账套的版式，门禁与建库以来一致，不放宽。
        _require_global_template_write(link, user)
    # 私有副本（t.user_id == user.id）：只有主人自己看得见，也就只有他自己有资格
    # 改 —— 管理员也不开后门，「仅自己可见」要真的只有自己。
    before = t.content
    # 局部更新：先把补丁叠到「已保存的模板」上，再叠加内置默认值兜底。
    # 直接把补丁丢给 validate_template 会让默认值里的 paper/margins 盖掉用户已保存的值。
    patched = _deep_merge(t.content or {}, payload.content) if payload.content is not None else (t.content or {})
    try:
        merged = validate_template(t.scope, patched)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if payload.name is not None:
        t.name = payload.name
    if payload.remark is not None:
        t.remark = payload.remark
    if payload.is_default is not None:
        t.is_default = payload.is_default
    t.content = merged
    t.paper = merged.get("paper", "A4")
    db.flush()
    if t.is_default:
        _clear_other_defaults(db, t)
    _log(db, user, book.id, "UPDATE_PRINT_TEMPLATE", t.id,
         f"修改打印模板 {t.name}" + ("（基线→我的私有副本）" if forked else ""),
         before={"content": before}, after={"content": merged})
    db.commit()
    # **id 会变**：改基线时前端拿到的是新副本的 id，必须切过去，否则下一次保存
    # 又会拿旧 id 再复制一份，越改越多。``forked`` 明说这件事发生过。
    return {"id": t.id, "content": t.content, "forked": forked,
            "name": t.name, "is_private": True if forked else t.user_id is not None}


@router.delete("/templates/{template_id}")
def delete_template(template_id: int, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                    user: SysUser = Depends(get_current_user),
                    _link: UserBook = Depends(require_book_write)):
    t = _scoped_template(db, book, template_id, user.id)
    if _is_baseline(t):
        # 基线是出厂版式，所有账套共看那一份。删掉它不是「改我的偏好」，
        # 是把系统重新变回「没有凭证版式可用」—— 所以这条路直接封掉。
        # 403 而不是 404：它本来就在每个人的列表里，说不说它存在都不泄密。
        raise HTTPException(
            403, "系统基线模板不能删除。它是所有账套共用的出厂版式；"
                 "要按自己的习惯改，直接保存即可 —— 改动会变成只有你可见的个人版式。")
    if t.user_id != user.id:
        _require_global_template_write(link, user)
    name = t.name
    db.delete(t)
    _log(db, user, book.id, "DELETE_PRINT_TEMPLATE", template_id, f"删除打印模板 {name}")
    db.commit()
    return {"ok": True}


@router.post("/templates/{template_id}/copy")
def copy_template(template_id: int, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                  user: SysUser = Depends(get_current_user),
                  _link: UserBook = Depends(require_book_write)):
    """复制一份再改 —— 改模板前先留个底，财务自己就能做。

    **复制出来的是账套模板**，不是私有副本：这条口子的语义是「在本账套里另存
    一份给同事用」，所以门禁与别的账套级写操作一样按 OWNER 收紧（否则「复制」
    就是个绕过点）。只想给自己留一份的人不用走这里 —— 改基线时系统自动复制
    私有副本（见 ``update_template``），或者直接删掉自己那份副本就回到基线。
    """
    src = _scoped_template(db, book, template_id, user.id)
    _require_global_template_write(link, user)
    t = PrintTemplate(book_id=book.id, scope=src.scope, name=f"{src.name} 副本",
                      paper=src.paper,
                      content=dict(src.content or {}), is_default=False,
                      remark=src.remark)
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id, "scope": t.scope, "name": t.name, "content": t.content}


def _clear_other_defaults(db: Session, keep: PrintTemplate) -> None:
    # 按 **(user_id, book_id, scope)** 分组，三层各算各的：
    #  * A 账套「设为默认」不该把 B 账套的默认模板一起清掉 —— 那等于
    #    「在 A 账套点一下，B 账套的版式就变回旧的了」；
    #  * **我把自己的私有副本设为默认，更不该把基线的默认标记清掉** ——
    #    那等于我点一下，全公司明天的凭证版式都变回出厂值。
    # 所以分组键必须带上 user_id：这是「改动不影响别人」在默认标记上的那条。
    #
    # NULL 在这里是「某一层的全体」，不是「没填」，所以两个条件都写成
    # 三值逻辑里的 IS NULL 分支（照抄 book_id 的老写法）。
    db.query(PrintTemplate).filter(
        PrintTemplate.scope == keep.scope, PrintTemplate.id != keep.id,
        PrintTemplate.is_default.is_(True),
        PrintTemplate.user_id == keep.user_id if keep.user_id is not None
        else PrintTemplate.user_id.is_(None),
        PrintTemplate.book_id == keep.book_id if keep.book_id is not None
        else PrintTemplate.book_id.is_(None)
    ).update({PrintTemplate.is_default: False}, synchronize_session=False)


def _fork_private(db: Session, user: SysUser, src: PrintTemplate) -> PrintTemplate:
    """把一份**基线**整份复制成「只有 ``user`` 看得见」的私有副本。

    幂等：已经 fork 过就返回那一份。不做幂等的话，在同一个基线上连按两次保存就
    攒出两份一模一样的私有副本，而列表里两份同名 —— 用户得自己猜哪份在用，
    取默认模板时也可能挑到旧的那份。判据是 ``forked_from_id`` 而不是 name：
    名字是用户自己可改的，改完就对不上了。

    副本**继承基线的 ``is_default``**：我 fork 的正是我每天在用的那份（不指定
    模板打印时打出来的就是它），不继承的话我的私有副本会立刻变成一张没人用的
    死模板，而打印仍然走基线 —— 改了半天没生效。基线那一行的 ``is_default``
    一个字节都不动（它在另一组，见 ``_clear_other_defaults``）。
    """
    existing = db.scalar(
        select(PrintTemplate)
        .where(PrintTemplate.user_id == user.id,
               PrintTemplate.forked_from_id == src.id)
    )
    if existing is not None:
        return existing
    t = PrintTemplate(
        book_id=None, user_id=user.id, forked_from_id=src.id,
        scope=src.scope, name=src.name, paper=src.paper,
        content=deepcopy(src.content or {}),
        is_default=src.is_default, remark=src.remark)
    db.add(t)
    try:
        db.flush()
    except IntegrityError:
        # 唯一索引 uq_print_template_user_fork 兜住了「两个窗口同时保存同一份
        # 基线」：先查后插在真并发下仍会双双插进去，那时由索引把第二个拦下。
        # 这里不把它当错误抛出去，而是认了同一份副本继续走 —— 用户看到的结果
        # 就是「保存成功，两边改到同一份上」，而不是一个 500。
        #
        # 回滚是安全的：``_fork_private`` 在 ``update_template`` 里是**第一件**
        # 改动，本次请求此刻还没往事务里写别的东西。
        db.rollback()
        existing = db.scalar(
            select(PrintTemplate)
            .where(PrintTemplate.user_id == user.id,
                   PrintTemplate.forked_from_id == src.id)
        )
        if existing is None:
            raise
        return existing
    if t.is_default:
        _clear_other_defaults(db, t)
    return t


def _log(db, user, book_id, action, entity_id, detail, before=None, after=None) -> None:
    from app.accounting.audit_helper import log

    log(db, action=action, entity="print_template", entity_id=entity_id, user=user,
        book_id=book_id, detail=detail, before=before, after=after)


def _require_global_template_write(link: UserBook, user: SysUser) -> None:
    """改**会被别人看到**的打印模板，需要本账套 OWNER（管理员放行）。

    **调用方只有两类**：新建模板、改账套模板。基线改不动（``update_template``
    会先复制成私有副本），别人的私有副本连看都看不见，所以它们都不走这里。

    **为什么不能只看「当前账套能不能写」**：模板内容进 ``<style>``（P0-2），
    改错一次，整批单据都要重打。``require_book_write`` 问的是「非 VIEWER 即可」，
    而「能改别人的模板」从来不是一个人被授权去做的事。对齐
    ``app/api/system.py`` 里 OWNER 的定义（「账套负责人，可改科目/模板/授权」）
    与 ``app/api/books.py::update_book`` 的先例。

    **账套模板已经按账套隔离了，为什么还不能放宽**，两个理由：

    1. **它仍然是「全账套共看的那一份」。** 一个人在账套里改版式，同事明天
       打印出来的凭证就跟着变了 —— 隔离到账套只挡住了**别的账套**，没解决
       「同账套里别人被动跟着变」。``book_id IS NULL`` 的基线与「我自己的私有
       副本」都拆出去了（前者写时复制，后者只归本人），这一层剩下的正是
       影响面 = 整个账套的那部分。
    2. **门禁本身不是这次新加的。** 它是上一批为了止血加的收紧；把它撤掉
       属于「拿掉一道已经生效的防线」，而放宽的前提是「已经有更好的那道」，
       不是「原来的问题看起来没那么严重」。模板是 JSON 自由文本，样式注入只有
       渲染层二次夹取兜底；门禁是写入路径上唯一一道按**角色**判断的关卡。

    fail-closed：角色认不出来一律当拒绝，不做「不是 VIEWER 就放行」的黑名单判断
    （见 ``frontend/src/stores/session.js::canWrite`` 里同一段推理）。
    """
    if user.is_admin:
        return
    if link.role != "OWNER":
        raise HTTPException(
            403, "打印模板是账套共用的版式（账套里每个人打印时都会用到），"
                 "只有账套所有者或管理员可以修改。只想改自己那份的话，"
                 "打开系统基线模板直接保存即可 —— 改动会变成只有你可见的个人版式")


# ===================================================================== HTML 打印
#
# **预览和打印走的是同一个渲染函数 _render_scope()**，唯一区别是模板从哪来：
# 正式打印读数据库里已保存的模板，设计器预览直接吃前端传来的草稿 JSON。
# 这样「所见即所得」是结构上成立的 —— 世界上只有一套排版代码，没有第二处
# 可以跑偏。以前预览必须传 template_id 读已保存内容，所以改完版式要点保存
# 才看得到变化。


#: 打印页的 CSP。
#:
#: 打印页是**纯样式文档**：一份内联 ``<style>`` + 静态表格，没有脚本、没有外链
#: 资源、没有 ``<img>``/``<link>``/``<form>``。所以这里可以收得很紧：
#:
#: * ``default-src 'none'`` —— 除下面显式放行的，其余一律拒绝；
#: * ``style-src 'unsafe-inline'`` —— 版式全靠内联 ``<style>``（``_page_css``），
#:   ``'unsafe-inline'`` 是模板引擎的固有要求，不能去掉；
#: * ``script-src 'none'`` —— **这条是 P0-2 的兜底**：即使模板内容绕过校验
#:   （老数据、直接改库）注入进了 ``</style><script>…``，脚本也不会执行。
#:   模板校验是第一道防线，这一条是第二道，两道独立成立。
#:
#: ``frame-ancestors 'self'`` 防止打印页被外站 iframe 套娃。
#:
#: 注意 nginx 那层也该有全局 CSP（由部署工作包负责）。这一条是**应用层**的：
#: 即便反代没配、或有人直接用后端端口起服务，防护也仍然成立。
PRINT_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'none'; "
    "img-src 'none'; "
    "font-src 'none'; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)


def _html_response(doc: str) -> Response:
    """打印页响应。

    带 CSP 头（见 ``PRINT_CSP``）和 ``nosniff``。``nosniff`` 是配套的：
    没有 CSP 时它至少能挡住 MIME 嗅探这一条道，两者一起才是一道完整的门。
    """
    return Response(
        content=doc,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": PRINT_CSP,
            "X-Content-Type-Options": "nosniff",
            # 打印页内容随模板变，但不能被任何一层缓存住：模板改完重新预览必须
            # 立刻看到新版本，旧版式发出去才是账务事故。
            "Cache-Control": "no-store",
        },
    )


def _subject_paths(subs: dict[int, Subject]) -> dict[int, str]:
    """科目 id -> 「一级/二级/.../末级」的完整层级路径。

    凭证的科目列要印的是这条路径，而不是「编码 名称」。路径按 ``parent_id``
    一路往上拼，**不是**把编码里的分隔符换成斜杠 —— 编码怎么分段是各账套自己
    定的，拿它拼出来的层级跟真实科目树未必对得上（父级改过编码就废了）。

    每个科目只往上走一次（memo），一张凭证十几条分录、一屏几百行，都不该
    重复爬树。
    """
    memo: dict[int, str] = {}

    def path(sid, chain: frozenset) -> str:
        if sid is None:
            return ""
        if sid in memo:
            return memo[sid]
        sub = subs.get(sid)
        if sub is None:
            return ""
        # parent_id 指回自己就是环（A -> A -> A ...），再往上走会一直递归下去。
        # 发现走过的 id 就返回空串当「没有前缀」，于是这一支只剩自己一个名字 ——
        # 印出来的路径是「自己」而不是「自己/自己/自己...」，也不会把整页打印卡死。
        # 深度也封一道顶，兜住没有成环但链异常长的情况。
        if sid in chain or len(chain) > 32:
            return ""
        prefix = path(sub.parent_id, chain | {sid})
        memo[sid] = f"{prefix}/{sub.name}" if prefix else sub.name
        return memo[sid]

    return {sid: path(sid, frozenset()) for sid in subs}


def _attach_subject_paths(voucher: dict, paths: dict[int, str]) -> dict:
    """给每条分录补 ``subject_path``，渲染层直接取用。

    兜底回科目名：科目被删、跨账套串了 id 这类情况下，印一个能对上的名字，
    好过印空白让人以为这里本来就没填。
    """
    for e in voucher.get("entries") or []:
        e["subject_path"] = paths.get(e.get("subject_id")) or e.get("subject_name", "")
    return voucher


def _voucher_html(db: Session, book: Book, period: Period, tpl: dict,
                  status: Optional[str] = None, max_pages: Optional[int] = None,
                  note: str = "", voucher_ids: Optional[list[int]] = None) -> str:
    """记账凭证：A4 一页两张、A5 一页一张，每张最多 5 行摘要、超出续到下一张。

    ``max_pages`` 限的是**原始凭证张数**（只给预览用，保证改版式时的响应速度），
    不是分片数 —— 否则一张 12 行的凭证会把「只预览 3 张」变成只看它自己。
    """
    from app.accounting.vouchers import query_vouchers

    if voucher_ids is None:
        vouchers, _ = query_vouchers(db, book.id, from_period=period, to_period=period,
                                     status=status, page_size=5000)
    else:
        ids = list(dict.fromkeys(voucher_ids))
        vouchers = list(db.scalars(select(Voucher).where(
            Voucher.book_id == book.id, Voucher.id.in_(ids)
        )).all())
        if len(vouchers) != len(ids):
            raise HTTPException(404, "所选凭证不存在或不属于当前账套")
    vouchers = sorted(vouchers, key=lambda v: (v.voucher_date, v.voucher_no, v.id))
    shown = vouchers[:max_pages] if max_pages else vouchers

    period_codes = {}
    if voucher_ids is not None and shown:
        periods = db.scalars(select(Period).where(
                Period.book_id == book.id,
                Period.id.in_({v.period_id for v in shown}),
            )).all()
        period_codes = {p.id: p.code for p in periods}

    rows_per_page = voucher_rows_per_page(tpl)
    slots = voucher_slots_per_page(tpl)

    # 先分片再排版：一张 12 行的凭证会变成 记001 - 001/003 / 002/003 / 003/003，
    # 它们占三个槽位，可能跨物理页。分页编号的写法由模板的 split_format 决定。
    split_format = (tpl.get("voucher") or {}).get("split_format")
    # 科目一次取好，下面逐张共用（审计严重 2）。这里原来只传两个参数，
    # 于是走 `_serialize` 的兜底分支，每张凭证发一条「全账套科目」查询 ——
    # 列表接口早已在 vouchers.py::query 修好同一个坑（一次取好复用，审计 P3-12），
    # 打印层漏了：这里 page_size=5000，满月的账就是 5000 次同样的 SELECT。
    subs = _book_subjects(db, book.id)
    subj_paths = _subject_paths(subs)
    pieces: list[dict] = []
    for v in shown:
        voucher = _attach_subject_paths(_serialize(db, v, subs), subj_paths)
        voucher["period"] = period_codes.get(v.period_id, period.code)
        pieces.extend(split_voucher(voucher, rows_per_page, split_format))

    if not pieces:
        pieces = split_voucher({
            "voucher_no": "", "voucher_date": "", "entries": [], "total_debit": 0,
            "total_credit": 0, "remark": "", "attachments": 0,
        }, rows_per_page)

    sheets = [pieces[i:i + slots] for i in range(0, len(pieces), slots)]
    total_pages = len(sheets)

    ctx = pv.context(db, book, period)
    docs = [render_voucher_sheet(tpl, ctx, group, i, total_pages)
            for i, group in enumerate(sheets, start=1)]

    return _with_note(wrap_pages(docs), note)


def _with_note(doc: str, note: str) -> str:
    if not note:
        return doc
    return doc.replace('<div class="page">',
                       f'<p class="page-note">{html.escape(note)}</p><div class="page">', 1)


def _check_print_rows(rows: list, kind: str) -> list:
    """HTML 打印通道的行数闸。

    为什么 Excel 那边有、HTML 这边没有：``xl.MAX_EXPORT_ROWS`` 挡的是
    openpyxl 全内存构建（``wb.save`` 把整张表序列化成一个 zip）。HTML 打印页同样
    是**一次性在内存里拼完整份文档**（``render_table_page`` → ``wrap_pages``），
    明细账跨全科目跨全年能到十几万行，而这三处页码硬编码 ``1, 1``
    （不分页、一次全出）—— 没有上限就是一个请求把进程内存吃光。

    上限**复用** Excel 通道那一个，不另立一份数字：两个通道面对的是同一份账、
    同样的量级，写成两个常量迟早会被调成不一致。异常也复用
    ``xl.ExportTooLarge``，由 :func:`_print_too_large` 翻译成 413 ——
    与 ``_xlsx`` 装饰器的处理完全一致（明确失败，而不是静默截断出缺行的账）。
    """
    n = len(rows)
    from app.printing import excel as xl
    if n > xl.MAX_EXPORT_ROWS:
        raise xl.ExportTooLarge(
            f"{kind}共 {n:,} 行，超过单次打印上限 {xl.MAX_EXPORT_ROWS:,} 行。"
            "请缩小查询范围（按科目或按期间分段打印）后重试。")
    return rows


def _report_print_col(col: dict) -> dict:
    """报表的一列 -> 一个打印列。

    ``key`` 必须是 ``c{col_no}``：``preview.report_rows`` 按列号摊行，
    渲染器与 Excel 导出都靠这个 key 找值（双栏表里「资产」和「负债和所有者
    权益」同名两列，靠列号才分得开）。

    对齐按列的 kind 定，不看报表类型：STATIC 是项目名（左对齐）、
    LINE_NO 是行次（居中）、其余是金额（右对齐 + 千分位）。

    ``width`` 用 ReportCol 的像素值除 7 当权重 —— 与 ``excel.report_excel``
    里的换算一致，两条通道看到的列宽比例才相同。真正定宽的是
    ``validate_template``：它把权重归一化到 100%。
    """
    kind = col.get("kind")
    if kind == "STATIC":
        align = "left"
    elif kind == "LINE_NO":
        align = "center"
    else:
        align = "right"
    try:
        width = float(col.get("width") or 120) / 7
    except (TypeError, ValueError):
        width = 120 / 7
    return {
        "key": f"c{col.get('col_no')}",
        "label": col.get("label", ""),
        "width": max(0.5, width),
        "align": align,
        "indent": 0,
    }


def _print_too_large(fn):
    """把打印通道的行数超限翻译成 HTTP 语义。

    只翻译 ``ExportTooLarge``，不碰别的 ``ValueError`` —— ``_render_scope``
    同时服务五个 ``.html`` 接口与设计器预览，各自有各的 HTTP 语义，
    在这里统一改写会把它们原本的报错（缺科目 400、模板不合法 422）也搅浑。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # 延迟导入：``except`` 子句里的名字**必须在 try 之前就已在作用域内**，
        # 否则 openpyxl 抛的异常还没进到这里就先撞一个 NameError ——
        # 而这条路径恰恰只在「行数超限」这种异常场景下才会走到。
        from app.printing import excel as xl
        try:
            return fn(*args, **kwargs)
        except xl.ExportTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc

    return wrapper


@_print_too_large
def _render_scope(
    db: Session,
    book: Book,
    scope: str,
    tpl: dict,
    *,
    period_id: Optional[int] = None,
    from_period_id: Optional[int] = None,
    subject_id: Optional[int] = None,
    from_subject_id: Optional[int] = None,
    to_subject_id: Optional[int] = None,
    report_template_id: Optional[int] = None,
    include_unposted: bool = False,
    leaf_only: bool = False,
    subject_scope: str = "all",
    status: Optional[str] = None,
    voucher_ids: Optional[list[int]] = None,
    max_pages: Optional[int] = None,
    note: str = "",
) -> str:
    """把一个「打印范围」渲染成完整 HTML。五个 .html 接口和预览接口都调它。"""
    from app.ledger import queries

    period = _period(db, book.id, period_id)

    if scope == "VOUCHER":
        return _voucher_html(db, book, period, tpl, status=status,
                             max_pages=max_pages, note=note, voucher_ids=voucher_ids)

    if scope == "BALANCE":
        # 区间口径：与页面、Excel 导出走**同一个** from_period，
        # 否则屏幕上一段区间、纸上却是单期，数字对不上（审计 7.3 同款毛病）
        bal_from = None
        if from_period_id:
            bal_from = _period(db, book.id, from_period_id)
            if bal_from.ord > period.ord:
                raise HTTPException(400, "起始期间不能晚于结束期间")
        # 科目表每个年度各有一份：跨年区间取**区间涉及的所有年度**的并集。
        subjects = queries.subjects_for_periods(
            db, book.id, [bal_from, period] if bal_from else [period])
        rows, check = queries.balance_sheet_of_accounts(
            db, book.id, subjects, period, include_unposted, from_period=bal_from)
        # 范围跟屏幕上的三档单选一致，别让纸上多出一千多行明细
        rows = queries.filter_balance_rows(rows, subject_scope)
        # **check 一起传下去**：合计行与试算行按它算（叶子级、与档位无关），
        # 与 Excel 导出同一份实现 —— 打印件与导出件必须是同一张纸。
        doc = render_table_page(tpl, pv.context(db, book, period),
                                _check_print_rows(pv.balance_rows(rows, check),
                                                  "科目余额表"), 1, 1)
        return _with_note(doc, note)

    to_period = period
    from_period = _period(db, book.id, from_period_id) if from_period_id else to_period

    if scope == "LEDGER":
        subjects = queries.subjects_for_periods(db, book.id, [from_period, to_period])
        if leaf_only:
            subjects = [s_ for s_ in subjects if s_.is_leaf]
        rows = queries.general_ledger(db, book.id, subjects, from_period, to_period,
                                      include_unposted)
        ctx = pv.context(db, book, to_period,
                         period=f"{from_period.code} 至 {to_period.code}"
                         if from_period.id != to_period.id else to_period.code)
        doc = render_table_page(tpl, ctx,
                                _check_print_rows(pv.general_rows(rows), "总账"), 1, 1)
        return _with_note(doc, note)

    if scope == "DETAIL_LEDGER":
        if not subject_id:
            raise HTTPException(400, "明细账预览需要先选择一个科目")
        payload = queries.detail_ledger(
            db, book.id, subject_id, from_period, to_period, include_unposted,
            from_subject_id=from_subject_id, to_subject_id=to_subject_id,
        )
        subj = payload["subject"]
        subject_caption = f"{subj['code']} {subj['name']}"
        if payload.get("subject_range"):
            subject_caption += (
                f"（{payload['subject_range']['from']['code']} 至 "
                f"{payload['subject_range']['to']['code']}）"
            )
        ctx = pv.context(db, book, to_period,
                         subject=subject_caption,
                         period=f"{payload['from_period']} 至 {payload['to_period']}"
                         if payload["from_period"] != payload["to_period"] else payload["from_period"])
        doc = render_table_page(tpl, ctx,
                                _check_print_rows(pv.detail_rows(payload), "明细账"), 1, 1)
        return _with_note(doc, note)

    if scope == "REPORT":
        from app.reports.evaluate import evaluate

        if not report_template_id:
            raise HTTPException(400, "报表预览需要先选择一张报表")
        # 归属校验与 evaluate 之前：报表名/行标签/列标签/取数公式整份都会
        # 出现在纸上，钱虽不漏，口径会漏（理由见 _scoped_report_template）。
        rtpl = _scoped_report_template(db, book, report_template_id)
        payload = pv.visible_report_payload(evaluate(db, report_template_id, book, period, False))

        tpl = dict(tpl)
        tpl["title"] = {**tpl.get("title", {}), "text": rtpl.name}
        # 表号只对法定双栏表有意义：DEFAULT_TEMPLATES["REPORT"] 是所有报表共用的
        # 默认模板，资产负债表要印「会小企01表」，利润表挂上去就成了「会小企01表
        # 利润表」。因此按报表类型清空 —— 空串不是 None，_deep_merge 里赢过默认值。
        if getattr(rtpl, "type", "") != "BALANCE_SHEET":
            tpl["header"] = {**(tpl.get("header") or {}), "form_no": ""}

        # 打印列**直接来自报表模板的列**，一个报表列对一个打印列。
        # 以前是硬拼「项　目」列 + 每个 payload 列一个 value 列：会小企01表有
        # 8 列（第 1/5 列是项目名、第 2/6 列是行次），那样会印出 9 列，
        # 还多出一列左右都不属于的 phantom「项　目」列。行次列与两侧项目名
        # 都由 preview.report_rows 按 col_no 摊到了行上（c1…c8）。
        cols = payload.get("cols", [])
        if cols:
            tpl["columns"] = [_report_print_col(c) for c in cols]
            tpl = validate_template("REPORT", tpl)      # 重新归一化列宽到 100%

        # end_date 是给页眉中间那格用的（要「期末」而不是期间的第一天）；
        # 没给时 render 会按期间码自己推当月最后一天。
        ctx = pv.context(db, book, period,
                         end_date=date(period.year, period.month,
                                       calendar.monthrange(period.year, period.month)[1]).isoformat())
        doc = render_table_page(tpl, ctx, pv.report_rows(payload), 1, 1)
        return _with_note(doc, note)

    raise HTTPException(400, f"未知的打印范围：{scope}")


@router.get("/vouchers.html")
def print_vouchers(book_id: int, period_id: Optional[int] = None,
                   template_id: Optional[int] = None, status: Optional[str] = None,
                   voucher_ids: Optional[str] = None,
                   orientation: Optional[str] = None,
                   book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  user: SysUser = Depends(get_current_user)):
    """记账凭证：一页几张由模板的 voucher.slots_per_page 决定（1 或 2），
    每张最多 5 行摘要，超出的行挪到下一张凭证并加分页号。"""
    selected_ids = None
    if voucher_ids is not None:
        try:
            selected_ids = list(dict.fromkeys(
                int(value.strip()) for value in voucher_ids.split(',') if value.strip()
            ))
        except ValueError as exc:
            raise HTTPException(422, "所选凭证编号格式无效") from exc
        if not selected_ids or any(voucher_id <= 0 for voucher_id in selected_ids):
            raise HTTPException(422, "至少选择一张有效凭证")
    tpl = _tpl_dict(_template(db, book, "VOUCHER", template_id, user.id))
    # 「一页几张」只由模板的 slots_per_page 决定，不跟着这次的打印参数变 ——
    # 用户在模板里定的密度就是这本的账密度，不该被一次性的打印悄悄改掉。
    tpl = _with_orientation(tpl, orientation)
    return _html_response(_render_scope(db, book, "VOUCHER", tpl,
                                        period_id=period_id, status=status,
                                        voucher_ids=selected_ids))


@router.get("/balance.html")
def print_balance(book_id: int, period_id: Optional[int] = None,
                  from_period_id: Optional[int] = None,
                  template_id: Optional[int] = None, include_unposted: bool = Query(False),
                  subject_scope: str = Query("all"),
                  orientation: Optional[str] = None,
                  book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  user: SysUser = Depends(get_current_user)):
    tpl = _with_orientation(_tpl_dict(_template(db, book, "BALANCE", template_id, user.id)), orientation)
    return _html_response(_render_scope(db, book, "BALANCE", tpl,
                                        period_id=period_id, from_period_id=from_period_id,
                                        include_unposted=include_unposted,
                                        subject_scope=subject_scope))


@router.get("/general-ledger.html")
def print_general(book_id: int, period_id: Optional[int] = None,
                  from_period_id: Optional[int] = None, template_id: Optional[int] = None,
                  include_unposted: bool = Query(False), leaf_only: bool = Query(False),
                  orientation: Optional[str] = None,
                  book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  user: SysUser = Depends(get_current_user)):
    tpl = _with_orientation(_tpl_dict(_template(db, book, "LEDGER", template_id, user.id)), orientation)
    return _html_response(_render_scope(db, book, "LEDGER", tpl, period_id=period_id,
                                        from_period_id=from_period_id,
                                        include_unposted=include_unposted,
                                        leaf_only=leaf_only))


@router.get("/detail-ledger.html")
def print_detail(book_id: int, subject_id: int, period_id: Optional[int] = None,
                 from_period_id: Optional[int] = None, template_id: Optional[int] = None,
                 from_subject_id: Optional[int] = None, to_subject_id: Optional[int] = None,
                 include_unposted: bool = Query(False),
                 orientation: Optional[str] = None,
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  user: SysUser = Depends(get_current_user)):
    tpl = _with_orientation(_tpl_dict(_template(db, book, "DETAIL_LEDGER", template_id, user.id)), orientation)
    return _html_response(_render_scope(db, book, "DETAIL_LEDGER", tpl, period_id=period_id,
                                        from_period_id=from_period_id, subject_id=subject_id,
                                        from_subject_id=from_subject_id,
                                        to_subject_id=to_subject_id,
                                        include_unposted=include_unposted))


@router.get("/report.html")
def print_report(book_id: int, template_id: int, period_id: int,
                 print_template_id: Optional[int] = None,
                 orientation: Optional[str] = None,
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  user: SysUser = Depends(get_current_user)):
    tpl = _with_orientation(_tpl_dict(_template(db, book, "REPORT", print_template_id, user.id)), orientation)
    return _html_response(_render_scope(db, book, "REPORT", tpl, period_id=period_id,
                                        report_template_id=template_id))


# --------------------------------------------------------------- 设计器实时预览


class PreviewIn(BaseModel):
    """设计器预览请求。

    ``content`` 是**还没保存的草稿**，服务端只用来渲染、绝不落库 ——
    这样财务可以一边调版式一边看效果，满意了再点「保存模板」。
    """
    scope: str
    content: dict = Field(default_factory=dict)
    period_id: Optional[int] = None
    from_period_id: Optional[int] = None
    subject_id: Optional[int] = None
    from_subject_id: Optional[int] = None
    to_subject_id: Optional[int] = None
    report_template_id: Optional[int] = None


@router.post("/preview")
def preview_draft(payload: PreviewIn, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db)):
    """按草稿模板渲染一页 HTML。预览与正式打印共用 _render_scope，排版不会走样。"""
    if payload.scope not in SCOPES:
        raise HTTPException(400, f"未知打印范围：{payload.scope}")
    try:
        tpl = validate_template(payload.scope, payload.content)
    except ValueError as e:
        # 草稿正处于「半成品」状态（比如把所有列都删了）时不该 500。
        # 把原因回给前端，由它在预览区里就地提示，而不是弹一个红色报错。
        raise HTTPException(422, f"模板暂时无法预览：{e}")

    note = ""
    if payload.scope == "VOUCHER":
        note = "预览只显示前 3 张凭证，打印时输出该期间全部凭证。"
    try:
        doc = _render_scope(
            db, book, payload.scope, tpl,
            period_id=payload.period_id,
            from_period_id=payload.from_period_id,
            subject_id=payload.subject_id,
            from_subject_id=payload.from_subject_id,
            to_subject_id=payload.to_subject_id,
            report_template_id=payload.report_template_id,
            max_pages=3 if payload.scope == "VOUCHER" else None,
            note=note,
        )
    except HTTPException as e:
        # 行数超限的 413 原样放行：它说的不是「模板草稿还没填完」，
        # 而是「这份账太大，请缩小范围」，翻译成 422 会让用户一直改模板。
        if e.status_code == 413:
            raise
        # 缺期间 / 缺科目这类「参数没选全」不该让预览框变成红色报错
        raise HTTPException(422, e.detail)
    return _html_response(doc)



# ===================================================================== Excel 导出


def _xlsx(fn):
    """把 Excel 导出层的领域异常翻译成 HTTP 语义。

    * ``ExportTooLarge`` → 413 + 可操作提示（缩小范围再导出）。
      导出侧是全内存构建的（见 ``excel.MAX_EXPORT_ROWS``），超限时**必须**
      失败得明确：静默截断出来的账表缺行，交给审计方是错的。
    * 其它 ValueError → 400。这类值在 API 层已经校验过，这里是兜底，
      不该以 500 的形式漏给用户。

    装饰器而不是每个接口写一遍 try/except：六个导出接口的这段逻辑完全相同，
    复制六份迟早有一份漏掉。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # 同 _print_too_large：except 子句的名字必须先于 try 就位。
        from app.printing import excel as xl
        try:
            return fn(*args, **kwargs)
        except xl.ExportTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, f"导出失败：{exc}") from exc

    return wrapper


@router.get("/vouchers.xlsx")
@_xlsx
def export_vouchers(book_id: int, period_id: Optional[int] = None, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db), status: Optional[str] = None):
    from app.accounting.vouchers import query_vouchers
    from app.printing import excel as xl

    period = _period(db, book.id, period_id)
    rows, _ = query_vouchers(db, book.id, from_period=period, to_period=period,
                             status=status, page_size=5000)
    # 科目一次取好逐张复用（审计严重 2）—— 与 `_voucher_html` 同一个坑的同一处修法。
    subs = _book_subjects(db, book.id)
    data = xl.vouchers_excel([_serialize(db, v, subs) for v in rows], period=period.code)
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"记账凭证_{book.name}_{period.code}.xlsx")})


@router.get("/balance.xlsx")
@_xlsx
def export_balance(book_id: int, period_id: Optional[int] = None,
                   from_period_id: Optional[int] = None,
                   book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), include_unposted: bool = Query(False),
                   subject_scope: str = Query("all")):
    """科目余额表导出。带 from_period_id 导出的是那一段区间。

    **区间必须一路带到文件里。** 屏幕上一段区间、下载下来是单期，
    用户不会以为导出坏了，只会把纸上那一份当成对的（总账导出栽过一次，
    见 ``export_general`` 的 docstring）。
    """
    from app.ledger import queries
    from app.printing import excel as xl

    period = _period(db, book.id, period_id)
    from_period = None
    if from_period_id:
        from_period = _period(db, book.id, from_period_id)
        if from_period.ord > period.ord:
            raise HTTPException(400, "起始期间不能晚于结束期间")
    subjects = queries.subjects_for_periods(
        db, book.id, [from_period, period] if from_period else [period])
    rows, check = queries.balance_sheet_of_accounts(
        db, book.id, subjects, period, include_unposted, from_period=from_period)
    # 与屏幕同一个范围口径：屏幕上「仅一级科目」而文件里是全表，用户只会
    # 把文件那份当成对的（这段 docstring 说的区间问题是同一个毛病）。
    rows = queries.filter_balance_rows(rows, subject_scope)
    is_range = from_period is not None and from_period.id != period.id
    # 文件名把区间说清楚，避免一个月一份的命名习惯盖住「这是跨三个月的」
    label = f"{from_period.code}~{period.code}" if is_range else period.code
    data = xl.balance_excel({"rows": rows, "check": check, "period": label},
                            book_name=book.name)
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"科目余额表_{label}.xlsx")})


@router.get("/general-ledger.xlsx")
@_xlsx
def export_general(book_id: int, period_id: Optional[int] = None,
                   from_period_id: Optional[int] = None,
                   book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                   include_unposted: bool = Query(False), leaf_only: bool = Query(False)):
    from app.printing import excel as xl
    """总账导出。

    原来这个接口**只认 period_id**，把 from/to 都当成同一期：前端页面显示的
    是「起始月 ~ 结束月」这一段，下载下来的却是起始月到结束月的**全部**
    （因为 period_id 之外没有下界）。屏幕上是一页、文件里是全账，数字对不上，
    用户只会以为导出坏了。审计第 7.3 节把「总账导出：区间与页面显示一致」
    列为验收项。

    现在与 general-ledger.html 的口径一致：认 from_period_id，缺省则退化成
    单期导出（老调用方不受影响）。
    """
    from app.ledger import queries

    to_period = _period(db, book.id, period_id)
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
        # **区间倒置 → 400，与另外三条通道一致。**
        #
        # 以前这里是「交换起止期后继续跑」：同一组参数在余额表导出、总账打印、
        # 明细账导出上都是 400「起始期间不能晚于结束期间」，唯独总账 Excel
        # 导出给一份文件。用户若在页面上看到过那句报错，改走导出却拿到文件，
        # 会以为页面报错是多余的；反过来，先导出再在页面核对时两边对不上。
        #
        # 闸门在共享实现里也有（``queries.balance_sheet_of_accounts`` 比 ord），
        # 但这里**先交换**了，共享层看到的就是正序 —— 所以必须在这条路上先挡。
        if from_period.ord > to_period.ord:
            raise HTTPException(400, "起始期间不能晚于结束期间")
    else:
        from_period = to_period

    subjects = queries.subjects_for_periods(db, book.id, [from_period, to_period])
    if leaf_only:
        subjects = [s for s in subjects if s.is_leaf]
    rows = queries.general_ledger(db, book.id, subjects, from_period, to_period, include_unposted)
    data = xl.general_ledger_excel(
        {"rows": rows, "from_period": from_period.code, "to_period": to_period.code},
        book_name=book.name)
    span = from_period.code if from_period.id == to_period.id else f"{from_period.code}-{to_period.code}"
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"总账_{span}.xlsx")})


@router.get("/report.xlsx")
@_xlsx
def export_report(book_id: int, template_id: int, period_id: int, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db)):
    from app.printing import excel as xl
    from app.reports.evaluate import evaluate

    period = _period(db, book.id, period_id)
    # 与打印同一道闸：导出的那份 xlsx 会带上报表名与全部取数公式，
    # 「能打出来但导不出来」不是理由，理由见 _scoped_report_template。
    tpl = _scoped_report_template(db, book, template_id)
    payload = pv.visible_report_payload(evaluate(db, template_id, book, period, False))
    payload["name"] = tpl.name
    data = xl.report_excel(payload, book_name=book.name)
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"{tpl.name}_{period.code}.xlsx")})


@router.get("/detail-ledger.xlsx")
@_xlsx
def export_detail_ledger(book_id: int, subject_id: int, period_id: Optional[int] = None,
                         from_period_id: Optional[int] = None,
                         from_subject_id: Optional[int] = None,
                         to_subject_id: Optional[int] = None,
                         include_unposted: bool = Query(False),
                         book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    from app.ledger import queries
    from app.printing import excel as xl

    to_period = _period(db, book.id, period_id)
    if to_period.book_id != book.id:
        raise HTTPException(404, "会计期间不存在")
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
    else:
        from_period = to_period

    payload = queries.detail_ledger(
        db, book.id, subject_id, from_period, to_period, include_unposted,
        from_subject_id=from_subject_id, to_subject_id=to_subject_id,
    )
    data = xl.detail_ledger_excel(payload, book_name=book.name)
    span = from_period.code if from_period.id == to_period.id else f"{from_period.code}-{to_period.code}"
    code = payload["subject"]["code"]
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"明细账_{code}_{span}.xlsx")})
