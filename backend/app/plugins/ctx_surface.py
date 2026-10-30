"""ctx 暴露面的**登记表**（纯数据，不依赖任何运行时设施）。

为什么单独一个文件
------------------
这张表登记「插件面上每个方法的返回值**能不能过 JSON**」。

它必须能被**两侧**读到：

* 宿主侧 —— 判断一次调用的结果该怎么编码（阶段 2 走 IPC 之后）；
* worker 侧 —— 插件后端代码搬进独立进程之后，worker 要按这张表
  知道每个能力怎么传。

而 worker 进程**不许 import** ``app.core.db``（那会把数据库连接拖进
不该有它的地方）。所以这张表不能住在 ``public_api.py`` 里 ——
那个模块 import 了数据库。

于是它住在**只放数据**的地方：这里 import 的东西只有 ``typing``。

**这张表必须覆盖插件面上的每一个方法** —— 少登记一个，阶段 2 就会在
「插件调了它、宿主回不去」的地方才发现，而那正是线上才会暴露的路径。
少登记的代价远大于登记的麻烦，所以覆盖性由测试强制（见
``tests/test_plugin_sdk_rules.py``）。

键是「ctx 上的调用路径」。``plugin_id`` 是字符串属性，不是方法，不在此列。

**为什么 ``data.*`` 能写 "json"**：插件自有表的结构是插件自己定的，
一个 BYTEA 列就能让 ``data.read`` 的返回值带二进制，而宿主在编译期
无从知道哪张表有这类列。所以这条不是靠自觉 ——
:func:`app.plugins.migrations.validate_tables` 直接不许插件自有表出现
二进制列。有那道闸在，这里写 "json" 才是**被保证的事实**，而不是一句愿望。
"""
from __future__ import annotations

#: 取值：
#:   * ``"json"``  —— 返回值是 dict / list / 标量，直接可序列化；
#:   * ``"bytes"`` —— 返回值**含二进制**，走 IPC 要分块（或落临时文件再传路径）。
CTX_METHOD_SERIALIZATION: dict[str, str] = {
    # —— 账套与科目 ——
    "book_read": "json",
    "book_list": "json",
    "book_write": "json",
    "ledger_read": "json",
    # —— 明细账（共享卡要导出「账面上的明细账」：同一本账，与屏幕同一份行）——
    "detail_ledger_read": "json",
    "subject_read": "json",
    "period_list": "json",
    # —— 对账单（共享卡对账：宿主只贡献解析器与绑定清单）——
    "statement.parse": "json",
    "statement.read": "json",
    # —— 流水行 ↔ 账上分录的配对（共享卡对账的「按金额初筛」）——
    # 复用宿主那把尺子（``app.bankstatement.allocate``），插件不自己写第二份。
    "entry_match": "json",
    # —— 导出 Excel：宿主生成 .xlsx，回传 base64（插件沙箱只放行 openpyxl 读侧）——
    "export_xlsx": "json",
    # —— 上传读取：返回体里带原始字节 ——
    "upload_read": "bytes",
    # —— 凭证 ——
    "voucher_create": "json",
    "voucher_batch_create": "json",
    # —— 审计 ——
    "audit_write": "json",
    # —— 插件自有表 ——
    "data.read": "json",
    "data.count": "json",
    "data.insert": "json",
    "data.insert_many": "json",
    "data.update": "json",
    "data.delete": "json",
    # —— 插件设置 ——
    "settings.get": "json",
    "settings.put": "json",
    # —— 导入批次 ——
    "imports.create": "json",
    "imports.read": "json",
    "imports.list": "json",
    # 源文件原始内容：二进制。
    "imports.source": "bytes",
    "imports.limits": "json",
    "imports.origin": "json",
    "imports.preview": "json",
    "imports.edit": "json",
    "imports.submit": "json",
    "imports.cancel": "json",
    # —— 账套级配置 ——
    "config.get": "json",
    "config.put": "json",
}

#: 会返回二进制的那些方法。走 IPC 时它们要**分块**（落临时文件传路径）。
CTX_METHODS_WITH_BYTES: tuple[str, ...] = tuple(
    name for name, kind in CTX_METHOD_SERIALIZATION.items() if kind == "bytes")
