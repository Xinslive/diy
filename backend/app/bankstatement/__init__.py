"""银行对账单的解析、台账与流水制证。

分三层，各管各的：

* :mod:`app.bankstatement.formats` —— **容器识别**（看文件头，不看扩展名）；
* :mod:`app.bankstatement.grid` —— **统一网格**（任何容器 → 行 × 列）；
* :mod:`app.bankstatement.semantics` —— **语义**（表头、列映射、方向、归一、自检）。

业务在这一层之上：:mod:`app.bankstatement.service`（台账、规则、画像、制证）
与 :mod:`app.bankstatement.matching`（发票 ↔ 流水的结算科目匹配）。
"""
