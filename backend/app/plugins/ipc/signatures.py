"""处理函数签名的**可传输**描述 —— worker 与宿主共用的唯一一份。

为什么单独抽出来
----------------
插件的 handler 住在 worker 进程里，宿主进程要**替它挂 FastAPI 路由** ——
而 FastAPI 靠 ``__signature__`` 决定怎么解析请求体。宿主要能重建这个签名，
worker 就得把签名**传过去**。

而签名里最难受的是**注解**：

* ``def chart(book_id: int)`` —— 注解是 ``int``，传 ``"int"`` 就够；
* ``def save(payload: dict)`` —— 传 ``"dict"``；
* ``def rows(payload: dict[str, Any])`` —— 要传得出来还得传得回去；
* ``def up(file: UploadFile = File(...))`` —— **默认值里带着 FastAPI 的标记**，
  它决定了这个参数是文件还是表单体。

所以这里做两件事：**描述**（worker 侧）与**重建**（宿主侧），
且两边用的是同一份实现 —— 这正是 §4.5 说的「同一个量只允许一个权威实现」。

重建不了的（宿主不认识的类型）**一律降级成** ``Any``，**并在描述里标出来** ——
shadow 比对会看见这个差异，而不是让它悄悄变成另一种行为。

**但降级不等于「可以就这么用」。** 降级后的 ``Any`` 与原注解对 FastAPI 的
意义**不同**（``Any`` 让它少校验一点；一个错的注解会让它按错的方式解析，
那比 ``Any`` 危险得多）。所以宿主侧重建时用 ``strict=True``：
重建不了就**拒绝加载这个插件**，而不是让它带着一个「看起来能跑、其实行为
变了」的签名上线。这条闸门是阶段 2「行为不变」这个承诺的落脚点。
"""
from __future__ import annotations

import inspect
from datetime import date, datetime
from decimal import Decimal
from types import UnionType
from typing import (
    Any,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)
from uuid import UUID

from fastapi import UploadFile as FastapiUploadFile
# **从 ``fastapi.params`` 导入，不是从 ``fastapi`` 导入。**
# ``fastapi.File`` / ``fastapi.Form`` 是**函数**（返回 ``params.File``），
# 不是类 —— 拿它们做 ``isinstance`` 的第二个参数会直接抛
# 「isinstance() arg 2 must be a type」。``sdk_rules`` 里也是这么导的。
from fastapi.params import File as FastapiFile
from fastapi.params import Form as FastapiForm

from app.plugins.sdk_rules import is_upload_param

#: 宿主侧能**原样重建**的**内置**类型。只有这些。
#:
#: 不在表里的注解一律降级成 :data:`typing.Any` —— 宁可少一点类型信息，
#: 也不要重建出一个**看起来对其实不同**的注解（那比 ``Any`` 危险得多：
#: ``Any`` 只会让 FastAPI 少校验一点，而一个错的注解会让它按错的方式解析）。
REBUILDABLE_TYPES: dict[str, type] = {
    "dict": dict,
    "list": list,
    "int": int,
    "str": str,
    "float": float,
    "bool": bool,
    "bytes": bytes,
    "Any": Any,
    # ``Optional[int]`` 展开后是 ``Union[int, NoneType]``；``NoneType`` 不登记
    # 的话会被降级成 ``Any``，于是 ``Optional[int]`` 重建出来变成
    # ``Union[int, Any]`` —— 那**不是**同一个类型（``Any`` 会让 FastAPI
    # 少做一层校验）。
    "NoneType": type(None),
    # 这几个是 FastAPI 原生认识的标量类型，宿主**能原样重建**
    # （它们就是标准库里的那几个类），所以不该被降级。
    # 少了它们，一个 ``def when(at: datetime)`` 的插件在隔离模式下会
    # 直接装不上 —— 而它本来是能用的。
    "datetime": datetime,
    "date": date,
    "Decimal": Decimal,
    "UUID": UUID,
}

#: 上面那些类型**允许来自哪些模块**。
#:
#: 名字相同不代表同一个东西：插件自己定义一个叫 ``date`` 的类，
#: 宿主重建出来的却是标准库的 ``date`` —— 那就是「看起来对其实不同」。
#: 所以重建时**类型名与模块名一起**核对。
REBUILDABLE_TYPE_MODULES = frozenset({"builtins", "datetime", "decimal", "uuid"})

#: 宿主侧能**原样重建**的**泛型**来源。
#:
#: 键是 ``typing.get_origin(...)`` 的 ``__name__``，值是重建时要用的那个
#: typing 构造。``Union`` 与 ``UnionType`` 都在列：``Optional[int]`` 的来源
#: 是 ``typing.Union``，而 ``int | None`` 的来源是 ``types.UnionType`` ——
#: 两者语义相同，名字不同，少一个就少一类。
REBUILDABLE_ORIGINS: dict[str, object] = {
    "dict": dict,
    "list": list,
    "tuple": tuple,
    "set": set,
    "frozenset": FrozenSet,
    "Union": Union,
    "Optional": Optional,
    "UnionType": UnionType,
    "Sequence": Sequence,
    "Iterable": Iterable,
    "List": List,
    "Dict": Dict,
    "Tuple": Tuple,
    "Set": Set,
}

#: 描述里的注解种类。
KIND_EMPTY = "empty"      # 没有注解
KIND_NONE = "none"        # 注解是 None
KIND_ANY = "any"          # 注解是 Any
KIND_TYPE = "type"        # 一个具体的类
KIND_GENERIC = "generic"  # 带泛参的类型
KIND_NAME = "name"        # 一个**名字**（PEP 563 的字符串注解）
KIND_OPAQUE = "opaque"    # 宿主重建不了
KIND_ELLIPSIS = "ellipsis"  # ``tuple[int, ...]`` 里的那个 ``...``


class UnrebuildableSignature(Exception):
    """宿主的签名重建不了这条声明。

    带着 ``problems``（人能读的说明列表）—— 宿主据此给出一条**中文**错误，
    而不是把 ``Any`` 悄悄塞进去。
    """

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("；".join(self.problems))


def describe_annotation(annotation) -> dict:
    """把一个注解描述成 JSON 可表达的形式。"""
    if annotation is inspect.Parameter.empty:
        return {"kind": KIND_EMPTY}
    if annotation is None:
        return {"kind": KIND_NONE}
    if annotation is Any:
        return {"kind": KIND_ANY}
    if annotation is Ellipsis:
        return {"kind": KIND_ELLIPSIS}
    if isinstance(annotation, str):
        # **PEP 563 的字符串注解**。
        #
        # 插件代码在沙箱里跑时，注解**在运行期就是字符串** ——
        # ``sandbox.py`` 自己有 ``from __future__ import annotations``，
        # 而 ``compile()`` 默认**继承调用方的 future 标志**，于是插件源码
        # 也被按 PEP 563 编译。
        #
        # 这不是 bug（FastAPI 自己会用 ``get_type_hints`` 解析它们），
        # 但描述层必须**认得**它，否则每个注解都会退化成 opaque。
        return {"kind": KIND_NAME, "name": annotation,
                "rebuildable": annotation in REBUILDABLE_TYPES}
    origin = get_origin(annotation)
    if origin is not None:
        origin_name = getattr(origin, "__name__", str(origin))
        return {
            "kind": KIND_GENERIC,
            "origin": origin_name,
            "args": [describe_annotation(arg) for arg in get_args(annotation)],
            "rebuildable": origin_name in REBUILDABLE_ORIGINS,
        }
    if isinstance(annotation, type):
        name = annotation.__name__
        return {
            "kind": KIND_TYPE,
            "name": name,
            "module": annotation.__module__,
            # 能不能原样重建，**在描述时就写清楚** —— shadow 比对要靠它
            # 区分「信息真的丢了」和「只是没打算重建」。
            "rebuildable": (name in REBUILDABLE_TYPES
                            and annotation.__module__ in REBUILDABLE_TYPE_MODULES),
        }
    return {"kind": KIND_OPAQUE, "text": str(annotation)}


def can_rebuild(described: dict | None) -> bool:
    """宿主能不能**原样**重建这个注解。

    「能降级成 ``Any``」不算「能重建」—— 降级后的语义与原注解**不同**。
    这个区分是阶段 2 的「行为不变」承诺的落脚点。
    """
    if not isinstance(described, dict):
        return False
    kind = described.get("kind")
    if kind in (KIND_EMPTY, KIND_NONE, KIND_ANY, KIND_ELLIPSIS):
        return True
    if kind == KIND_NAME:
        return described.get("name") in REBUILDABLE_TYPES
    if kind == KIND_TYPE:
        return (described.get("name") in REBUILDABLE_TYPES
                and described.get("module") in REBUILDABLE_TYPE_MODULES)
    if kind == KIND_GENERIC:
        if described.get("origin") not in REBUILDABLE_ORIGINS:
            return False
        return all(can_rebuild(arg) for arg in described.get("args") or [])
    return False


def rebuild_annotation(described: dict | None) -> Any:
    """把描述还原成注解；还原不了就是 :data:`typing.Any`。"""
    if not isinstance(described, dict):
        return Any
    kind = described.get("kind")
    if kind == KIND_EMPTY:
        return inspect.Parameter.empty
    if kind == KIND_NONE:
        return None
    if kind == KIND_ANY:
        return Any
    if kind == KIND_ELLIPSIS:
        return Ellipsis
    if kind == KIND_NAME:
        return REBUILDABLE_TYPES.get(described.get("name"), Any)
    if kind == KIND_GENERIC:
        origin = REBUILDABLE_ORIGINS.get(described.get("origin"))
        if origin is None:
            return Any
        args = tuple(rebuild_annotation(arg) for arg in described.get("args", []))
        try:
            return origin[args] if args else origin
        except TypeError:
            return Any
    if kind == KIND_TYPE:
        return REBUILDABLE_TYPES.get(described.get("name"), Any)
    return Any


def describe_default(default) -> dict:
    """描述一个默认值。

    只有**标量**默认值能原样重建；其余一律标成 ``opaque`` —— 宿主的重建
    逻辑看到它就知道「这个参数带着文件/表单标记」，需要按标记处理而不是按值处理。

    **文件与表单标记单独成一类**（``file`` / ``form``）：它们不是「值」，
    而是「FastAPI 怎么解析这个参数」的指令。丢掉它，宿主的路由就收不到
    上传的文件 —— 而症状是 422，不是 500，极难往这儿想。
    必填性也一起带过去（``File(...)`` 必填，``File(None)`` 选填）。
    """
    if default is inspect.Parameter.empty:
        return {"kind": "empty"}
    if default is None:
        return {"kind": "none"}
    if isinstance(default, (FastapiFile, FastapiForm)):
        return {"kind": "file" if isinstance(default, FastapiFile) else "form",
                "required": default.default is Ellipsis}
    if isinstance(default, bool) or isinstance(default, (int, float, str)):
        return {"kind": "value", "value": default}
    if isinstance(default, (list, tuple)):
        return {"kind": "value", "value": list(default)}
    return {"kind": "opaque", "text": type(default).__name__}


def rebuild_default(described: dict | None) -> Any:
    """还原默认值；还原不了就是 :data:`inspect.Parameter.empty`（= 必填）。"""
    if not isinstance(described, dict):
        return inspect.Parameter.empty
    kind = described.get("kind")
    if kind == "empty":
        return inspect.Parameter.empty
    if kind == "none":
        return None
    if kind == "value":
        return described.get("value")
    if kind == "file":
        return FastapiFile(...) if described.get("required") else FastapiFile(None)
    if kind == "form":
        return FastapiForm(...) if described.get("required") else FastapiForm(None)
    return inspect.Parameter.empty


def describe_signature(handler) -> list[dict]:
    """把处理函数的参数描述成 JSON 可表达的列表。

    **优先用解析后的注解**（``typing.get_type_hints``）：插件代码在沙箱里
    是按 PEP 563 编译的（见 :func:`describe_annotation` 的说明），裸读的
    注解是字符串；解析之后拿到的才是真的类型，描述出来才有信息。
    解析不了就退回裸注解 —— 退回比抛异常好：插件的路由**不该**因为
    宿主这边的一个注解问题就整个装不上。
    """
    described = []
    try:
        hints = get_type_hints(handler)
    except Exception:  # noqa: BLE001 —— 解析失败不是致命错
        hints = {}
    for param in inspect.signature(handler).parameters.values():
        annotation = hints.get(param.name, param.annotation)
        described.append({
            "name": param.name,
            "kind": param.kind.name,
            "annotation": describe_annotation(annotation),
            "default": describe_default(param.default),
            # 文件 / 表单参数：FastAPI 靠**默认值上的标记**认出它，
            # 靠**注解**认出 UploadFile。这个标志位是给宿主侧用的 ——
            # 宿主要照着原样重建，标错了路由就收不到文件。
            "upload": is_upload_param(param),
        })
    return described


def signature_problems(described: list[dict] | None) -> list[str]:
    """这条签名里，宿主**重建不出来**的地方（人能读的说明）。

    空列表 = 重建得出来。
    """
    problems: list[str] = []
    for item in described or []:
        name = item.get("name") or "?"
        default_kind = (item.get("default") or {}).get("kind")
        upload = bool(item.get("upload"))
        if default_kind in ("file", "form"):
            # 文件 / 表单参数由标记驱动，注解不参与判断。
            continue
        if upload:
            # 没有 File/Form 标记、但注解是 UploadFile 的参数：
            # 宿主要把注解重建回 UploadFile（见 rebuild_signature）。
            continue
        if not can_rebuild(item.get("annotation")):
            annotation = (item.get("annotation") or {}).get("name") \
                or (item.get("annotation") or {}).get("text") \
                or (item.get("annotation") or {}).get("kind")
            problems.append(f"参数 {name} 的注解「{annotation}」宿主重建不了")
    return problems


def rebuild_signature(described: list[dict] | None, *, strict: bool = False) -> inspect.Signature:
    """把描述还原成 :class:`inspect.Signature`（宿主侧挂路由用）。

    ``strict=True`` 时重建不了就抛 :class:`UnrebuildableSignature`，
    而不是悄悄降级成 ``Any`` —— 降级会让 FastAPI **按另一种方式**解析请求，
    那与「行为不变」相悖。宿主据此拒绝加载这个插件，并给出中文原因。
    """
    problems = signature_problems(described)
    if strict and problems:
        raise UnrebuildableSignature(problems)
    params = []
    for item in described or []:
        kind = getattr(inspect.Parameter, item.get("kind", "POSITIONAL_OR_KEYWORD"),
                       inspect.Parameter.POSITIONAL_OR_KEYWORD)
        default_kind = (item.get("default") or {}).get("kind")
        if default_kind == "file":
            annotation = FastapiUploadFile
        elif default_kind == "form":
            annotation = rebuild_annotation(item.get("annotation"))
        elif item.get("upload"):
            # 注解本身就是 UploadFile（没有 File 标记）：重建回 UploadFile。
            annotation = FastapiUploadFile
        else:
            annotation = rebuild_annotation(item.get("annotation"))
        try:
            param = inspect.Parameter(
                item["name"], kind,
                default=rebuild_default(item.get("default")),
                annotation=annotation,
            )
        except ValueError:
            # 参数名不合法或顺序不对（默认值在前）——宿主宁可要一个宽松的签名，
            # 也不要一个根本装不上的签名。
            param = inspect.Parameter(
                item.get("name", "arg"), inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=inspect.Parameter.empty, annotation=Any,
            )
        params.append(param)
    return inspect.Signature(params)
