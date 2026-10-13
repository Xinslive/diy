"""上传入口的**共享**容量闸。

一个请求能把整个进程的内存吃光，只要两步：把上传的文件无上限读进内存，
再交给一个「把每一行都累积进列表」的解析器。本项目有过两个这样形态的入口：

* 凭证 Excel 导入（``POST .../vouchers/import``）
* 期初余额解析（``POST .../openings/parse``）—— 它更直接：
  ``grid = [list(r) for r in ws.iter_rows()]`` 一次性把**整个工作表**物化成
  二维列表，比逐行累积更早失控。

这两处曾经各自长成不同形态：导入侧有三道闸（字节数 / 解压体积 / 行数与凭证数），
期初解析侧**一道都没有** —— 原实现是 ``raw = await file.read()``，
无参数即「有多少读多少」（审计 P1-4）。

而且它绕过了守卫测试：``tests/test_viewer_matrix.py`` 把它按
「只解析不落库」列进写接口豁免清单，守卫测试于是只检查「有没有挂
``require_book_write``」，容量这一维度根本没人看。豁免的是**写权限**，
不是**容量** —— 两件事必须分开考核（见 ``tests/test_upload_capacity_matrix.py``）。

所以闸住在共享工具里，而不是各写一遍：

1. :func:`read_upload` —— 接口层限**读取字节数**（先看 ``file.size`` 预检，
   再按上限 +1 读一次，覆盖 size 不可信的情况）；
2. :func:`check_archive_size` —— 按 zip 中央目录**声明的**解压体积预检，
   不真正解压就能挡住「几百 KB 解压成几百 MB」；
3. :class:`RowBudget` —— 解析过程中数**行数**。前两道都可以被伪造
   （中央目录里的 ``file_size`` 可以随便写），行数不会。

阈值复用 ``settings.import_*``：期初解析与凭证导入是同一种上传入口，
要的也正是「一个请求最多读多少字节 / 解压多大 / 多少行」。配置项本身住在
``app/core/config.py``，不在本模块的改动范围，语义上却完全一致。

约定：容量类拒绝一律抛 :class:`UploadTooLarge`，由路由层翻译成 **413**
（文件太大是「换个更小的文件再来」，不是「你参数写错了」的 400）。
它继承 ``ValueError``，这样不认识它的旧调用方仍然按「参数不合法」处理，
而不会被穿透成 500。
"""
from __future__ import annotations

import io
import zipfile

from app.core.config import settings


class UploadTooLarge(ValueError):
    """容量超限。

    继承 ``ValueError`` 而不是直接抛 ``HTTPException``：这个模块也要被
    服务层（``importer.parse_workbook``）调用，那里没有 HTTP 语义。
    状态码由路由层决定（413）。
    """


def human_bytes(n: int) -> str:
    """给人看的体积。容量类报错必须说清「多大 / 上限多少」，不然用户只会重试。"""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def upload_limit() -> int:
    """读取字节数上限（两个上传入口共用一把尺）。"""
    return settings.import_max_bytes


def unzipped_limit() -> int:
    """解压后体积上限。"""
    return settings.import_max_unzipped_bytes


async def read_upload(file, *, limit: int | None = None,
                      label: str = "文件",
                      hint: str = "请拆成多份分别上传") -> bytes:
    """把上传文件读进内存，**带上限**读。

    两道检查，缺一不可：

    1. ``file.size`` 是 Starlette 收到请求时记下的字节数，先用它拒掉明显超限的
       —— 连读都不读，超大文件不会先在内存里出现一次。
    2. 再按 ``limit + 1`` 读一次。``size`` 不可信（自己构造的请求、没有
       Content-Length 的分块请求都可能拿不到），``limit + 1`` 这一读才是
       真正的兜底。

    原来两处入口分别是 ``file.file.read()``（无参数 = 有多少读多少）和
    ``await file.read()``（同一个意思）。Nginx 那条 50m 限的是**请求体**，
    不是解压后体量，也不能替代这一道。
    """
    cap = upload_limit() if limit is None else limit
    size = getattr(file, "size", None)
    if size is not None and size > cap:
        raise UploadTooLarge(
            f"{label} {human_bytes(size)} 超过上限 {human_bytes(cap)}，{hint}")
    # UploadFile.file 是同步对象（SpooledTemporaryFile），
    # 即使在 async 路由里也只能同步读 —— 这里不丢线程，
    # 因为有了 limit，读进来的是一个有上限的字节串。
    data = file.file.read(cap + 1)
    if len(data) > cap:
        raise UploadTooLarge(f"{label} 超过上限 {human_bytes(cap)}，{hint}")
    return data


def check_archive_size(data: bytes, *, limit: int | None = None,
                       label: str = "Excel") -> None:
    """按 zip 目录里**声明的**解压大小预检，不真正解压。

    xlsx 就是 zip。一份几百 KB 的文件完全可以解压出几百 MB 的 XML ——
    「上传大小没超限」在这条路上不构成任何保护。这里读中央目录
    （``ZipFile`` 只解析目录，不读文件内容）把各成员的 ``file_size`` 加起来，
    超了就拒。

    只能当**第二道**闸：中央目录里的 size 是可以随便写的。真正的兜底是
    解析时逐行计数（:class:`RowBudget`）。

    不是 zip 就放行 —— 解析器稍后会自己报「无法读取 Excel 文件」，
    在这里拦一道只会把一个清楚的报错换成一个含糊的。
    """
    cap = unzipped_limit() if limit is None else limit
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            total = sum(info.file_size for info in zf.infolist())
    except (zipfile.BadZipFile, OSError):
        return  # 不是 zip（或坏到读不了目录）：交给解析器报错
    if total > cap:
        raise UploadTooLarge(
            f"{label} 解压后约 {human_bytes(total)}，超过上限 {human_bytes(cap)}。"
            "请拆成多份分别导入，或删除与本表无关的工作表后重试")


class RowBudget:
    """行数预算：在**遍历过程中**计数，不先 count 一遍。

    先 count 照样会把整个工作表读进内存（期初解析就是一次性物化成二维列表的），
    那正是要防的事。所以它是「边读边数，超了立刻中止」。

    上限值本身可以被打偏（解析器读到的行数由对方决定），但它是三道闸里
    唯一不能靠伪造中央目录绕过的一道。
    """

    def __init__(self, limit: int, *, what: str = "行",
                 hint: str = "请拆成多份分别上传") -> None:
        self.limit = limit
        self.what = what
        self.hint = hint
        self.count = 0

    def tick(self, n: int = 1) -> int:
        """计 n 行，返回累计行数；超过上限抛 :class:`UploadTooLarge`。"""
        self.count += n
        if self.count > self.limit:
            raise UploadTooLarge(
                f"这份表超过 {self.limit} {self.what}，超过单次上限。{self.hint}")
        return self.count

    def guard_rows(self, rows) -> list:
        """物化一个迭代器成列表，并在过程中计数（期初解析用）。"""
        out = []
        for row in rows:
            out.append(row)
            self.tick()
        return out
