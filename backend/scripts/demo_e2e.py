#!/usr/bin/env python
"""端到端演示：走通 建账套 → 录凭证 → 记账 → 结转损益 → 结账 → 账簿/报表 → 批量反结账。

同时验证四条会计护栏确实拦得住。
    .venv/bin/python scripts/demo_e2e.py
"""
from __future__ import annotations

import sys
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> bool:
    checks.append((name, ok, extra))
    print(f"  {PASS if ok else FAIL} {name}{(' — ' + extra) if extra else ''}")
    return ok


def step(n: int, title: str) -> None:
    print(f"\n\033[1m[{n}] {title}\033[0m")


def guard(name: str, resp, expect_status: int = 400) -> None:
    ok = resp.status_code == expect_status
    check(name, ok, f"HTTP {resp.status_code} {str(resp.json())[:90] if resp.content else ''}")


def main() -> int:
    client = TestClient(app)

    # ---------------------------------------------------------------- 1 登录
    step(1, "登录与账套权限")
    r = client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
    guard("错误密码被拒绝", client.post("/api/auth/login", json={"username": "admin", "password": "x"}), 401)
    check("登录成功", r.status_code == 200, r.text[:80])
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}

    books = client.get("/api/books", headers=H).json()
    check("admin 能看到自己的账套", len(books) >= 1, str([b["name"] for b in books]))

    # 每次跑都新建一个专属账套 —— 否则第二次运行会撞上上一轮遗留的
    # 已结账 3 月，「已结账不能新增凭证」护栏会把整条演示打断。
    tag = uuid.uuid4().hex[:6]
    r = client.post("/api/books", headers=H, json={
        "company_name": f"演示公司{tag}", "name": f"租金账套{tag}", "year": 2026, "start_month": 1,
    })
    check("新建账套", r.status_code == 201, r.text[:120])
    BID = r.json()["id"]

    # 初始化第 1 步：确认沿用系统标准科目表。
    # 新账套的初始化是**空**的，期初余额写不进去（409）——
    # 演示脚本不导入外部科目表，所以走「沿用标准表」这条同样是完成的路。
    r = client.post(f"/api/books/{BID}/init/subjects/keep", headers=H)
    check("初始化第 1 步（沿用标准科目表）", r.status_code == 200, r.text[:120])

    periods = client.get(f"/api/books/{BID}/periods", headers=H).json()
    check("账套含 2026 全年 12 期", len(periods) == 12)
    P = {p["code"]: p["id"] for p in periods}

    subjects = client.get(f"/api/books/{BID}/subjects/flat", headers=H).json()
    S = {s["code"]: s["id"] for s in subjects}
    check("标准科目表已装载", len(subjects) >= 40, f"{len(subjects)} 个明细科目")

    # ---------------------------------------------------------------- 2 期初
    step(2, "录入期初余额")
    # 金额一律「借正贷负」：银行存款 200000 借方为正，权益类科目是贷方余额故取负
    openings = [
        {"subject_id": S["100201"], "amount": "200000"},   # 银行存款
        {"subject_id": S["4001"], "amount": "-80000"},     # 实收资本（贷方余额）
        {"subject_id": S["4104"], "amount": "-120000"},    # 未分配利润（贷方余额）
    ]
    r = client.post(f"/api/books/{BID}/periods", headers=H, json=openings)
    check("期初余额写入", r.status_code == 200, r.text[:80])

    bal = client.get(f"/api/books/{BID}/ledger/balance", headers=H, params={"period_id": P["2026-03"]}).json()
    bank = next(x for x in bal["rows"] if x["code"] == "100201")
    check("期初 200000 正确派生到 3 月", Decimal(bank["opening"]) == Decimal("200000"),
          f"期初={bank['opening']}")
    check("期初试算平衡", bal["check"]["opening_balanced"], str(bal["check"]))

    # ---------------------------------------------------------------- 3 凭证
    step(3, "凭证录入与护栏")

    def add_voucher(d: str, entries: list[dict], remark: str = "") -> dict:
        return client.post(f"/api/books/{BID}/vouchers", headers=H,
                           json={"voucher_date": d, "entries": entries, "remark": remark})

    bad = add_voucher("2026-03-05", [
        {"subject_id": S["100201"], "direction": "DEBIT", "amount": "1000", "summary": "测试"},
        {"subject_id": S["600101"], "direction": "CREDIT", "amount": "999", "summary": "测试"},
    ])
    guard("借贷不平衡被应用层拦截", bad)

    r = add_voucher("2026-03-05", [
        {"subject_id": S["100201"], "direction": "DEBIT", "amount": "120000", "summary": "2026年3月租金收款"},
        {"subject_id": S["600101"], "direction": "CREDIT", "amount": "120000", "summary": "2026年3月租金收款"},
    ], "3月租金")
    check("凭证 1 创建成功", r.status_code == 201, r.text[:100])
    v1 = r.json()

    r = add_voucher("2026-03-10", [
        {"subject_id": S["6602"], "direction": "DEBIT", "amount": "18000", "summary": "3月管理费用"},
        {"subject_id": S["100201"], "direction": "CREDIT", "amount": "18000", "summary": "3月管理费用"},
    ], "3月管理费用")
    check("凭证 2 创建成功", r.status_code == 201, r.text[:100])
    v2 = r.json()

    r = add_voucher("2026-03-15", [
        {"subject_id": S["640101"], "direction": "DEBIT", "amount": "5000", "summary": "3月租金成本"},
        {"subject_id": S["100201"], "direction": "CREDIT", "amount": "5000", "summary": "3月租金成本"},
    ], "3月成本")
    check("凭证 3 创建成功", r.status_code == 201)
    v3 = r.json()

    q = client.get(f"/api/books/{BID}/vouchers", headers=H, params={"keyword": "租金"}).json()
    check("按摘要检索凭证", q["total"] >= 2, f"命中 {q['total']} 张")

    nxt = client.get(f"/api/books/{BID}/ledger/voucher-no-preview", headers=H,
                     params={"period_id": P["2026-03"]}).json()
    check("凭证号自动顺延", nxt["next"] == "记0004", nxt["next"])

    # ---------------------------------------------------------------- 4 记账
    step(4, "记账")
    bal = client.get(f"/api/books/{BID}/ledger/balance", headers=H,
                     params={"period_id": P["2026-03"]}).json()
    bank = next(x for x in bal["rows"] if x["code"] == "100201")
    check("未记账凭证不进账簿（期末仍为期初 200000）",
          Decimal(bank["closing"]) == Decimal("200000"), f"期末={bank['closing']}")

    r = client.post(f"/api/books/{BID}/vouchers/post", headers=H,
                    json={"ids": [v1["id"], v2["id"], v3["id"]]})
    check("批量记账", r.status_code == 200, r.text[:80])

    bal = client.get(f"/api/books/{BID}/ledger/balance", headers=H,
                     params={"period_id": P["2026-03"]}).json()
    bank = next(x for x in bal["rows"] if x["code"] == "100201")
    check("记账后银行存款 = 200000+120000-18000-5000 = 297000",
          Decimal(bank["closing"]) == Decimal("297000"), f"期末={bank['closing']}")
    check("本期发生额试算平衡", bal["check"]["period_balanced"], str(bal["check"]))
    check("期末余额试算平衡", bal["check"]["closing_balanced"], str(bal["check"]))

    # ---------------------------------------------------------------- 5 结转损益
    step(5, "结转损益")
    r = client.post(f"/api/books/{BID}/periods/{P['2026-03']}/carry-forward", headers=H)
    check("结转损益生成凭证", r.status_code == 200, r.text[:160])
    cf = r.json()
    check("结转出 2 张凭证（收入类 / 成本费用类）", len(cf["vouchers"]) == 2, str(cf["vouchers"]))

    listed = client.get(f"/api/books/{BID}/vouchers", headers=H,
                        params={"period_id": P["2026-03"], "source": "CARRY_FORWARD"}).json()
    ids = [v["id"] for v in listed["items"]]
    client.post(f"/api/books/{BID}/vouchers/post", headers=H, json={"ids": ids})

    bal = client.get(f"/api/books/{BID}/ledger/balance", headers=H,
                     params={"period_id": P["2026-03"]}).json()
    rev = next(x for x in bal["rows"] if x["code"] == "600101")
    cost = next(x for x in bal["rows"] if x["code"] == "640101")
    profit = next(x for x in bal["rows"] if x["code"] == "4103")
    check("收入科目结转后余额为 0", Decimal(rev["closing"]) == 0, f"期末={rev['closing']}")
    check("成本科目结转后余额为 0", Decimal(cost["closing"]) == 0, f"期末={cost['closing']}")
    check("本年利润 = 120000-18000-5000 = 97000",
          Decimal(profit["closing"]) == Decimal("97000"), f"期末={profit['closing']}")
    check("结转后仍试算平衡", bal["check"]["closing_balanced"], str(bal["check"]))

    # ---------------------------------------------------------------- 6 结账
    step(6, "结账与写保护")
    r = client.post(f"/api/books/{BID}/periods/{P['2026-03']}/close", headers=H)
    check("结账成功", r.status_code == 200, r.text[:140])

    guard("已结账期间新增凭证被拒", add_voucher("2026-03-20", [
        {"subject_id": S["100201"], "direction": "DEBIT", "amount": "100", "summary": "x"},
        {"subject_id": S["600101"], "direction": "CREDIT", "amount": "100", "summary": "x"},
    ]))
    guard("已结账期间删除凭证被拒",
          client.delete(f"/api/books/{BID}/vouchers/{v1['id']}", headers=H))
    guard("已结账期间改凭证被拒",
          client.put(f"/api/books/{BID}/vouchers/{v1['id']}", headers=H,
                     json={"voucher_date": "2026-03-05", "entries": [
                         {"subject_id": S["100201"], "direction": "DEBIT", "amount": "1", "summary": "x"},
                         {"subject_id": S["600101"], "direction": "CREDIT", "amount": "1", "summary": "x"}]}))

    # ---------------------------------------------------------------- 7 报表
    step(7, "报表（可配置公式引擎）")
    templates = client.get("/api/reports/templates", headers=H).json()
    tpl = {t["code"]: t["id"] for t in templates}
    check("内置资产负债表 / 利润表模板已安装", "BALANCE_SHEET" in tpl and "INCOME_SHEET" in tpl)

    bs = client.get(f"/api/reports/{tpl['BALANCE_SHEET']}/render", headers=H,
                    params={"book_id": BID, "period_id": P["2026-03"]}).json()
    check("资产负债表无公式错误", not bs["errors"], str(bs["errors"][:2]))
    check("资产负债表是一行两栏的 8 列", len(bs["cols"]) == 8, f"{len(bs['cols'])} 列")

    # 双栏表：左右两半共用同一行，所以**不能**按 label 找行 ——
    # 「资产总计」在第 32 行的左半边，「负债和所有者权益总计」在第 29 行的右半边，
    # 两者的数值分别在第 3 列（期末）和第 7 列（期末）。
    def row_by_label(bs, label, side="left"):
        def name(r):
            if side == "left":
                return r["label"]
            return (r.get("style") or {}).get("label_right", "")

        return next((r for r in bs["rows"] if name(r) == label), None)

    def cell(row, col_no):
        """按**列号**取格，不按下标：双栏表里列号与下标差一位，且左右两半会重名。"""
        return next(c["value"] for c in row["cells"] if c["col_no"] == col_no)

    assets = cell(row_by_label(bs, "资产总计"), 3)
    le = cell(row_by_label(bs, "负债和所有者权益总计", "right"), 7)
    # 平衡校验行不在表结构里，用 style 的分列 kind 认它
    # （行级 kind 只是两半的默认值，真正生效的是 kind_left/right），
    # 比匹配那串长得像提示语的 label 稳
    chk_row = next(r for r in bs["rows"] if (r.get("style") or {}).get("kind_left") == "CHECK")
    chk = cell(chk_row, 3)
    check("资产总计 == 负债和所有者权益总计", Decimal(assets) == Decimal(le),
          f"{assets} vs {le}")
    check("表内平衡校验行 = 0", Decimal(chk) == 0, str(chk))

    is_ = client.get(f"/api/reports/{tpl['INCOME_SHEET']}/render", headers=H,
                     params={"book_id": BID, "period_id": P["2026-03"]}).json()
    check("利润表无公式错误", not is_["errors"], str(is_["errors"][:2]))
    net = cell(next(r for r in is_["rows"] if r["label"] == "四、净利润"), 2)
    check("净利润 = 97000", Decimal(net) == Decimal("97000"), str(net))

    # 自定义公式：把平衡校验行的期末余额改成用 ROUND 和 IF 加工一遍，验证引擎可编辑。
    # 地址跟着会小企01表的坐标走：资产总计在第 32 行 C 列，负债和所有者权益总计在第 29 行 G 列。
    # 报表模板是全局资源（P1-5），改公式必须显式带 book_id 说明「以哪个账套的身份改」。
    r = client.put(f"/api/reports/{tpl['BALANCE_SHEET']}/cells?book_id={BID}", headers=H,
                   json=[{"row_no": 33, "col_no": 3, "formula": "=ROUND(C32-G29,2)"}])
    check("可编辑单元格公式", r.status_code == 200, r.text[:80])
    bs2 = client.get(f"/api/reports/{tpl['BALANCE_SHEET']}/render", headers=H,
                     params={"book_id": BID, "period_id": P["2026-03"]}).json()
    chk2 = cell(next(r for r in bs2["rows"]
                     if (r.get("style") or {}).get("kind_left") == "CHECK"), 3)
    check("改公式后重算结果不变", Decimal(chk2) == Decimal(chk), f"{chk} -> {chk2}")
    client.put(f"/api/reports/{tpl['BALANCE_SHEET']}/cells?book_id={BID}", headers=H,
               json=[{"row_no": 33, "col_no": 3, "formula": "C32-G29"}])

    # ---------------------------------------------------------------- 8 账簿
    step(8, "账簿")
    gl = client.get(f"/api/books/{BID}/ledger/general", headers=H,
                    params={"period_id": P["2026-03"], "leaf_only": True}).json()
    check("总账返回明细科目行", len(gl["rows"]) > 10, f"{len(gl['rows'])} 行")
    bank_row = next(r for r in gl["rows"] if r["code"] == "100201")
    check("总账银行存款期末 297000", Decimal(bank_row["closing"]) == Decimal("297000"),
          str(bank_row["closing"]))

    dl = client.get(f"/api/books/{BID}/ledger/detail", headers=H,
                    params={"subject_id": S["100201"], "period_id": P["2026-03"]}).json()
    check("明细账含期初行 / 逐笔 / 合计",
          dl["rows"][0]["kind"] == "OPENING" and dl["rows"][-1]["kind"] == "TOTAL",
          f"{len(dl['rows'])} 行")
    check("明细账期末与总账一致",
          Decimal(dl["rows"][-1]["balance"]) == Decimal(bank_row["closing"]))

    x = client.get(f"/api/books/{BID}/print/balance.xlsx", headers=H,
                   params={"period_id": P["2026-03"]})
    check("导出科目余额表 Excel", x.status_code == 200 and len(x.content) > 5000,
          f"{len(x.content)} bytes")
    x = client.get(f"/api/books/{BID}/print/vouchers.xlsx", headers=H,
                   params={"period_id": P["2026-03"]})
    check("导出记账凭证 Excel", x.status_code == 200 and len(x.content) > 5000,
          f"{len(x.content)} bytes")

    # ---------------------------------------------------------------- 9 批量反结账
    step(9, "批量反结账（核心体验）")

    # 先把 4 月也做上，制造一个已结账区间
    r = client.post(f"/api/books/{BID}/vouchers", headers=H, json={
        "voucher_date": "2026-04-08", "entries": [
            {"subject_id": S["100201"], "direction": "DEBIT", "amount": "90000", "summary": "4月租金"},
            {"subject_id": S["600101"], "direction": "CREDIT", "amount": "90000", "summary": "4月租金"}]})
    client.post(f"/api/books/{BID}/vouchers/post", headers=H, json={"ids": [r.json()["id"]]})
    cf4 = client.post(f"/api/books/{BID}/periods/{P['2026-04']}/carry-forward", headers=H)
    v4 = client.get(f"/api/books/{BID}/vouchers", headers=H,
                    params={"period_id": P["2026-04"], "source": "CARRY_FORWARD"}).json()
    client.post(f"/api/books/{BID}/vouchers/post", headers=H,
                json={"ids": [x["id"] for x in v4["items"]]})
    c4 = client.post(f"/api/books/{BID}/periods/{P['2026-04']}/close", headers=H)
    check("4 月结转损益并结账", c4.status_code == 200, c4.text[:100])
    print("  · 3、4 月均已结账")

    pv = client.get(f"/api/books/{BID}/periods/{P['2026-03']}/unclose-preview", headers=H).json()
    check("反结账影响范围预览", pv["periods"] == ["2026-03", "2026-04"], str(pv))

    r = client.post(f"/api/books/{BID}/periods/{P['2026-03']}/unclose", headers=H,
                    json={"revert_posting": True})
    check("批量反结账成功", r.status_code == 200, r.text[:160])
    uc = r.json()
    check("解锁 2 个期间", len(uc["periods"]) == 2, str(uc["periods"]))
    check("区间凭证回到未记账", uc["unposted_vouchers"] == 7,
          f"{uc['unposted_vouchers']} 张（3月5张 + 4月1张 + 结转2张）")

    periods_after = client.get(f"/api/books/{BID}/periods", headers=H).json()
    check("3、4 月均已解锁", not any(p["locked"] for p in periods_after if p["code"] in ("2026-03", "2026-04")))

    # 反结账后账面回退到「未记账」口径
    bal = client.get(f"/api/books/{BID}/ledger/balance", headers=H,
                     params={"period_id": P["2026-03"]}).json()
    bank = next(x for x in bal["rows"] if x["code"] == "100201")
    check("反结账后账簿回到期初 200000（凭证已回未记账）",
          Decimal(bank["closing"]) == Decimal("200000"), f"期末={bank['closing']}")

    # 重新记账 + 结账，验证链路可恢复
    allv = client.get(f"/api/books/{BID}/vouchers", headers=H,
                      params={"from_period_id": P["2026-03"], "to_period_id": P["2026-04"],
                              "page_size": 200}).json()
    client.post(f"/api/books/{BID}/vouchers/post", headers=H,
                json={"ids": [v["id"] for v in allv["items"]]})
    r = client.post(f"/api/books/{BID}/periods/{P['2026-03']}/close", headers=H)
    check("重新结账成功（反结账可逆）", r.status_code == 200, r.text[:120])

    bal = client.get(f"/api/books/{BID}/ledger/balance", headers=H,
                     params={"period_id": P["2026-03"]}).json()
    bank = next(x for x in bal["rows"] if x["code"] == "100201")
    check("数据完全恢复（297000）", Decimal(bank["closing"]) == Decimal("297000"),
          f"期末={bank['closing']}")

    # ---------------------------------------------------------------- 10 审计
    step(10, "审计日志（凭证号可复用 → 必须留快照）")
    logs = client.get(f"/api/books/{BID}/audit-logs", headers=H).json()
    actions = {l["action"] for l in logs}
    check("记录了创建/记账/结账/反结账/结转",
          {"CREATE", "POST", "CLOSE", "UNCLOSE"} <= actions, str(sorted(actions)))
    close_log = next(l for l in logs if l["action"] == "UNCLOSE")
    check("反结账日志含范围说明", "反结账" in close_log["detail"], close_log["detail"][:70])

    # ---------------------------------------------------------------- 11 年度结转
    step(11, "年度结转")
    for m in range(5, 13):
        pid = P[f"2026-{m:02d}"]
        posted = client.get(f"/api/books/{BID}/vouchers", headers=H,
                            params={"period_id": pid}).json()
        if posted["total"]:
            client.post(f"/api/books/{BID}/vouchers/post", headers=H,
                        json={"ids": [v["id"] for v in posted["items"]]})
        r = client.post(f"/api/books/{BID}/periods/{pid}/close", headers=H)
        check(f"{P[f'2026-{m:02d}'] and f'2026-{m:02d}'} 结账", r.status_code == 200, r.text[:80])

    bal12 = client.get(f"/api/books/{BID}/ledger/balance", headers=H,
                       params={"period_id": P["2026-12"]}).json()
    bank12 = next(x for x in bal12["rows"] if x["code"] == "100201")["closing"]
    check("2026 年末银行存款 = 200000+97000+90000 = 387000",
          Decimal(bank12) == Decimal("387000"), str(bank12))

    r = client.post(f"/api/books/{BID}/year-end", headers=H, json={})
    check("年度结转生成 2027 账套", r.status_code == 200, r.text[:140])
    yb = r.json()
    check("期初余额已结转", yb["carried_subjects"] > 0, f"{yb['carried_subjects']} 个科目")

    new_book = client.get("/api/books", headers=H).json()
    check("账套列表出现 2027 账套", any(b["year"] == 2027 for b in new_book),
          str([b["name"] for b in new_book]))

    nb = next(b for b in new_book if b["year"] == 2027)
    nper = client.get(f"/api/books/{nb['id']}/periods", headers=H).json()
    nbal = client.get(f"/api/books/{nb['id']}/ledger/balance", headers=H,
                      params={"period_id": nper[0]["id"]}).json()
    bank27 = next((x for x in nbal["rows"] if x["code"] == "100201"), None)
    check("2027 年期初银行存款 = 2026 年期末（结转链完整）",
          bank27 and Decimal(bank27["opening"]) == Decimal(bank12),
          f"2027 期初={bank27['opening'] if bank27 else 'N/A'} / 2026 期末={bank12}")
    check("2027 年期初试算平衡", nbal["check"]["opening_balanced"], str(nbal["check"]))

    # ---------------------------------------------------------------- 汇总
    print("\n" + "=" * 70)
    failed = [c for c in checks if not c[1]]
    print(f"共 {len(checks)} 项检查，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    for name, _, extra in failed:
        print(f"  {FAIL} {name} — {extra}")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
