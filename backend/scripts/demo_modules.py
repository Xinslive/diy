"""新增模块的端到端验证：整理凭证 / Excel 导入 / 打印模板 / 打印预览。

与 demo_e2e.py 一样直接打真实 HTTP 接口，跑之前请先 init_db.py --reset。
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date

BASE = "http://127.0.0.1:8000"
TOKEN = ""

PASS = FAIL = 0
SECTION = ""


def section(title: str) -> None:
    global SECTION
    SECTION = title
    print(f"\n\033[1m[{title}]\033[0m")


def check(label: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m✓\033[0m {label}" + (f" — {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  \033[31m✗\033[0m {label}" + (f" — {extra}" if extra else ""))
    return cond


def call(method: str, path: str, body=None, files=None):
    url = BASE + path
    headers = {}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = None
    if files:
        boundary = "----cwqboundary9f2a"
        buf = io.BytesIO()
        for name, (fname, content) in files.items():
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(
                f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'.encode()
            )
            buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
            buf.write(content)
            buf.write(b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        data = buf.getvalue()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw)
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, raw.decode("utf-8", "replace")


def login(username="admin", password="123456"):
    global TOKEN
    st, data = call("POST", "/api/auth/login", {"username": username, "password": password})
    TOKEN = data["access_token"]


def xlsx(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> int:
    global TOKEN

    section("登录与账套")
    st, data = call("POST", "/api/auth/login", {"username": "admin", "password": "123456"})
    if st != 200:
        print(f"  ✗ 登录失败：{data}")
        return 1
    TOKEN = data["access_token"]
    check("管理员登录", st == 200, data["user"]["username"])

    # 自建专用账套，这样脚本不依赖 demo_e2e 跑过之后的数据状态，可以独立重复执行
    stamp = f"{date.today():%m%d}-{uuid.uuid4().hex[:6]}"
    st, made = call("POST", "/api/books", {
        "company_name": f"模块测试公司{stamp}",
        "name": f"模块测试账套{stamp}",
        "year": 2026,
        "start_month": 1,
    })
    if st != 201:
        print(f"  ✗ 建账套失败：{made}")
        return 1
    bid = made["id"]
    check("新建专用账套（脚本可重复运行）", st == 201,
          f"{made['name']} / 科目 {made['subjects_installed']} 个")

    st, periods = call("GET", f"/api/books/{bid}/periods")
    p12 = next(p for p in periods if p["month"] == 12)
    p11 = next(p for p in periods if p["month"] == 11)
    check("取到 2026-12 期间且未结账", p12["locked"] is False)
    check("账套生成 12 个会计期间", len(periods) == 12, f"{len(periods)} 期")

    # ---------------------------------------------------------------
    section("凭证录入（用于整理/导入测试）")
    subs = {s["code"]: s for s in call("GET", f"/api/books/{bid}/subjects/flat")[1]}
    dr_sub = subs["100201"]      # 银行存款-工商银行（明细级）
    cr_sub = subs["600101"]      # 主营业务收入-租金（明细级）
    exp_sub = subs["6602"]         # 管理费用（明细级，损益类）

    def add_voucher(d: str, amount: float, no: str | None = None):
        payload = {
            "voucher_date": d,
            "entries": [
                {"subject_id": cr_sub["id"], "direction": "CREDIT", "amount": str(amount), "summary": "测试收入"},
                {"subject_id": dr_sub["id"], "direction": "DEBIT", "amount": str(amount), "summary": "测试收入"},
            ],
            "source": "MANUAL",
        }
        if no:
            payload["voucher_no"] = no
        return call("POST", f"/api/books/{bid}/vouchers", payload)

    # 故意制造断号：0001 / 0003，且 0002 日期更晚
    st, v1 = add_voucher("2026-12-03", 100)
    check("建凭证1 记0001", st == 201 and v1["voucher_no"] == "记0001", v1.get("voucher_no"))
    st, v2 = add_voucher("2026-12-20", 200, no="记0003")
    check("建凭证2 记0003（跳过0002）", st == 201, v2.get("voucher_no"))

    # ---------------------------------------------------------------
    section("整理凭证 · 断号检查")
    st, gaps = call("POST", f"/api/books/{bid}/vouchers/organize/preview",
                    {"period_id": p12["id"], "strategy": "GAPS"})
    check("GAPS 体检返回", st == 200, f"{gaps.get('periods', [{}])[0].get('gaps')}")
    check("识别出断号 记0002", "记0002" in gaps["periods"][0]["gaps"], str(gaps["periods"][0]["gaps"]))
    check("GAPS 不改数据", gaps["total_changed"] == 0)

    section("整理凭证 · 按日期重排并压紧编号")
    st, plan = call("POST", f"/api/books/{bid}/vouchers/organize/preview",
                    {"period_id": p12["id"], "strategy": "SORT_DATE"})
    check("预览给出改号方案", st == 200 and plan["total_changed"] == 1, f"将改 {plan['total_changed']} 张")
    changes = {c["old_no"]: c["new_no"] for c in plan["periods"][0]["changes"] if c["changed"]}
    check("记0003 → 记0002", changes.get("记0003") == "记0002", str(changes))

    st, res = call("POST", f"/api/books/{bid}/vouchers/organize",
                   {"period_id": p12["id"], "strategy": "SORT_DATE"})
    check("执行整理", st == 200 and res["total_changed"] == 1, res.get("message", ""))
    st, after = call("GET", f"/api/books/{bid}/vouchers", None)
    nos = sorted(v["voucher_no"] for v in after["items"])
    check("改号后号段连续", nos == ["记0001", "记0002"], str(nos))

    section("整理凭证 · 编号交换不撞唯一约束")
    # 造三张号乱序的凭证，触发交换场景
    st, v3 = add_voucher("2026-12-05", 300, no="记0009")
    st, v4 = add_voucher("2026-12-06", 400, no="记0004")
    st, res = call("POST", f"/api/books/{bid}/vouchers/organize",
                   {"period_id": p12["id"], "strategy": "SORT_DATE"})
    check("乱序重排成功（两阶段避让生效）", st == 200, res.get("message", ""))
    st, after = call("GET", f"/api/books/{bid}/vouchers")
    nos = [v["voucher_no"] for v in sorted(after["items"], key=lambda x: x["voucher_no"])]
    check("重排后连号无洞", nos == ["记0001", "记0002", "记0003", "记0004"], str(nos))

    # ---------------------------------------------------------------
    section("凭证 Excel 导入")
    good = xlsx([
        ["日期", "凭证号", "摘要", "科目编码", "借方金额", "贷方金额", "附单据数", "备注"],
        ["2026-11-03", "", "收到押金", "100201", 3000, "", 1, "导入测试"],
        ["", "", "收到押金", "600101", "", 3000, "", ""],
        ["2026-11-08", "记0500", "支付水电", "6602", 800, "", 2, ""],
        ["", "", "支付水电", "100201", "", 800, "", ""],
    ])
    st, imp = call("POST", f"/api/books/{bid}/vouchers/import",
                   files={"file": ("11月凭证.xlsx", good)})
    check("上传并解析", st == 200, f"{imp.get('ok_count')} 通过 / {imp.get('error_count')} 失败")
    check("解析出 2 张凭证", imp.get("total") == 2, f"total={imp.get('total')}")
    check("两张都通过校验", imp.get("error_count") == 0, str(imp.get("errors"))[:150])
    batch_id = imp["batch_id"]
    check("第二张沿用指定凭证号 记0500",
          imp["vouchers"][1]["voucher_no"] == "记0500", str(imp["vouchers"][1].get("voucher_no")))

    section("凭证 Excel 导入 · 错误定位")
    bad = xlsx([
        ["日期", "凭证号", "摘要", "科目编码", "借方金额", "贷方金额"],
        ["2026-11-12", "", "科目写错", "9999", 500, ""],
        ["2026-11-15", "", "金额挂空（无贷方）", "100201", 300, ""],
    ])
    st, imp2 = call("POST", f"/api/books/{bid}/vouchers/import",
                    files={"file": ("坏数据.xlsx", bad)})
    check("坏数据被拦下", st == 200 and imp2["error_count"] == 2, f"{imp2.get('error_count')} 张失败")
    msgs = " ".join(e for v in imp2["vouchers"] for e in v["errors"])
    check("报出找不到科目", "找不到科目" in msgs, msgs[:120])
    check("报出借贷不平衡", "借贷不平衡" in msgs, msgs[:160])
    check("坏批次不污染账套", imp2["ok_count"] == 0)

    section("凭证 Excel 导入 · 确认入库")
    st, before = call("GET", f"/api/books/{bid}/vouchers")
    st, res = call("POST", f"/api/books/{bid}/vouchers/import/{batch_id}/commit")
    check("提交成功", st == 200 and res["created"] == 2, res.get("message"))
    st, after2 = call("GET", f"/api/books/{bid}/vouchers")
    check("凭证数增加 2 张", after2["total"] == before["total"] + 2,
          f"{before['total']} → {after2['total']}")
    st, res2 = call("POST", f"/api/books/{bid}/vouchers/import/{batch_id}/commit")
    check("重复提交被拒", st == 400, str(res2)[:90])

    st, disc = call("POST", f"/api/books/{bid}/vouchers/import/{imp2['batch_id']}/discard")
    check("作废坏批次", st == 200)

    st, tpl_bytes = call("GET", f"/api/books/{bid}/vouchers/import-template.xlsx")
    check("下载导入模板", st == 200 and tpl_bytes[:2] == b"PK", f"{len(tpl_bytes)} 字节")

    section("整理凭证 · 已结账期间拒绝")
    st, allv = call("GET", f"/api/books/{bid}/vouchers", None)
    ids = [v["id"] for v in allv["items"]]
    st, posted = call("POST", f"/api/books/{bid}/vouchers/post", {"ids": ids})
    check(f"{len(ids)} 张凭证全部记账（含导入）", st == 200 and posted["posted"] == len(ids),
          str(posted))
    st, res = call("POST", f"/api/books/{bid}/periods/{p12['id']}/carry-forward", {})
    check("结转损益", st == 200, str(res)[:110])
    st, res = call("POST", f"/api/books/{bid}/periods/{p12['id']}/close", {})
    check("结账成功", st == 200, str(res)[:110])
    st, res = call("POST", f"/api/books/{bid}/vouchers/organize",
                   {"period_id": p12["id"], "strategy": "RENUM"})
    check("已结账期间拒绝整理", st == 400 and "已结账" in str(res.get("detail", "")), str(res)[:90])

    st, res = call("POST", f"/api/books/{bid}/periods/{p12['id']}/unclose", {"revert_posting": True})
    check("反结账成功", st == 200, str(res)[:90])


    # ---------------------------------------------------------------
    section("打印模板 · 元信息与 CRUD")
    st, meta = call("GET", f"/api/books/{bid}/print/templates/meta")
    check("模板元信息", st == 200 and "A4" in [p["key"] for p in meta["papers"]])
    check("内置默认含 5 种范围", len(meta["defaults"]) == 5, str(list(meta["defaults"])))

    st, created = call("POST", f"/api/books/{bid}/print/templates", {
        "scope": "VOUCHER", "name": "自定义凭证 A5", "is_default": True,
        "content": {
            "paper": "A5", "orientation": "portrait",
            "title": {"text": "记 账 凭 证", "size": 14},
            "columns": [
                {"key": "line_no", "label": "", "width": 5, "align": "center"},
                {"key": "summary", "label": "摘要", "width": 35, "align": "left"},
                {"key": "subject", "label": "科目", "width": 35, "align": "left"},
                {"key": "debit", "label": "借方", "width": 12.5, "align": "right"},
                {"key": "credit", "label": "贷方", "width": 12.5, "align": "right"},
            ],
        },
    })
    check("新建打印模板", st == 201, f"id={created.get('id')}")
    tid = created["id"]
    check("列宽归一化到 100%", abs(sum(c["width"] for c in created["content"]["columns"]) - 100) < 0.01)

    st, patched = call("PUT", f"/api/books/{bid}/print/templates/{tid}", {
        "content": {"title": {"text": "记 帐 凭 证"}, "font": {"family": "仿宋", "size": 11}}
    })
    check("修改模板（只传局部）", st == 200 and patched["content"]["font"]["size"] == 11)
    check("未传的列被保留", len(patched["content"]["columns"]) == 5,
          f"{len(patched['content']['columns'])} 列")

    st, copied = call("POST", f"/api/books/{bid}/print/templates/{tid}/copy")
    check("复制模板", st == 200 and copied["id"] != tid, copied.get("name"))

    st, bad_tpl = call("POST", f"/api/books/{bid}/print/templates", {
        "scope": "VOUCHER", "name": "坏模板", "content": {"paper": "不存在的纸型"}})
    check("未知纸型被拒", st == 400 and "纸型" in str(bad_tpl.get("detail", "")), str(bad_tpl)[:90])

    st, bad_scope = call("POST", f"/api/books/{bid}/print/templates", {
        "scope": "PAYROLL", "name": "越界范围"})
    check("未知范围被拒", st == 400, str(bad_scope)[:90])

    # ---------------------------------------------------------------
    section("打印预览（HTML）")
    st, html_v = call("GET", f"/api/books/{bid}/print/vouchers.html", None)
    check("凭证打印页", st == 200, f"{len(html_v)} 字节")
    text = html_v.decode() if isinstance(html_v, bytes) else str(html_v)
    check("含中文大写合计", "合计大写" in text and "元" in text)
    check("含 @page 纸型", "@page" in text and "size:" in text)
    page_rule = next((l.strip() for l in text.splitlines() if "@page" in l), "(no @page)")
    check("套用 A5 自定义模板", "size: A5" in page_rule, page_rule)

    st, html_b = call("GET", f"/api/books/{bid}/print/balance.html")
    check("科目余额表打印页", st == 200 and "科目余额表" in html_b.decode())

    st, html_g = call("GET", f"/api/books/{bid}/print/general-ledger.html")
    check("总账打印页", st == 200 and "总　账" in html_g.decode())

    st, html_d = call("GET", f"/api/books/{bid}/print/detail-ledger.html?subject_id={dr_sub['id']}")
    check("明细账打印页", st == 200 and "期初余额" in html_d.decode())

    st, rtpls = call("GET", "/api/reports/templates")
    st, html_r = call("GET", f"/api/books/{bid}/print/report.html"
                           f"?template_id={rtpls[0]['id']}&period_id={p12['id']}")
    check("报表打印页", st == 200 and "<table" in html_r.decode(), f"{len(html_r)} 字节")

    st, del_res = call("DELETE", f"/api/books/{bid}/print/templates/{copied['id']}")
    check("删除副本", st == 200)

    # ---------------------------------------------------------------
    section("Excel 导出（回归）")
    st, x = call("GET", f"/api/books/{bid}/print/vouchers.xlsx?period_id={p12['id']}")
    check("凭证 xlsx 导出", st == 200 and x[:2] == b"PK")
    st, x2 = call("GET", f"/api/books/{bid}/print/balance.xlsx?period_id={p12['id']}")
    check("余额表 xlsx 导出", st == 200 and x2[:2] == b"PK")

    print("\n" + "=" * 70)
    total = PASS + FAIL
    print(f"共 {total} 项检查，通过 {PASS}，失败 {FAIL}")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
