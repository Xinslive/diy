"""示例业务规则：UTF-8 CSV 的一行生成一张两分录凭证。"""
import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException

TABLE = "plugin_25ac53a1cf75_config"


def register(registrar, ctx):
    def chart(book_id: int):
        return ctx.subject_read(book_id)

    def parse(ident: str, payload: dict):
        batch = ctx.imports.read(ident)
        config = ctx.config.get(TABLE, "mapping", book_id=batch["book_id"])
        values = config["value"]
        by_code = {s["code"]: s for s in ctx.subject_read(batch["book_id"])}
        selected = []
        for key in ("debit_code", "credit_code"):
            subject = by_code.get(values.get(key))
            if not subject or not subject["active"] or not subject["is_leaf"]:
                raise HTTPException(422, "请先保存本账套的借方、贷方末级科目配置")
            selected.append(subject["id"])
        limits = ctx.imports.limits()
        vouchers = []
        locations = []
        for source in batch["files"]:
            content = ctx.imports.source(ident, source["id"])["content"]
            try:
                reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
                if set(reader.fieldnames or []) != {"date", "amount", "summary"}:
                    raise HTTPException(422, {"file": source["filename"], "line": 1,
                        "message": "CSV 列必须为 date,amount,summary"})
                for row in reader:
                    line = reader.line_num
                    if len(vouchers) >= min(limits["max_vouchers"], limits["max_rows"] // 2):
                        raise HTTPException(413, "CSV 行数超过本批凭证或分录上限，请拆分文件")
                    try:
                        if None in row or any(v is None for v in row.values()):
                            raise ValueError("CSV 列数不一致")
                        voucher_date = date.fromisoformat(row["date"])
                        amount = Decimal(row["amount"])
                        if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
                            raise ValueError("金额必须为正数，最多两位小数")
                        if len(row["summary"]) > 256:
                            raise ValueError("摘要最多 256 字符")
                    except (ValueError, InvalidOperation) as exc:
                        raise HTTPException(422, {"file": source["filename"], "line": line, "message": str(exc)})
                    vouchers.append({"voucher_date": voucher_date.isoformat(), "remark": source["filename"] + ":" + str(line),
                        "entries": [{"subject_id": subject_id, "direction": direction,
                                     "amount": str(amount), "summary": row["summary"]}
                                    for subject_id, direction in zip(selected, ("DEBIT", "CREDIT"))]})
                    locations.append({"file_id": source["id"], "line": line})
            except (UnicodeError, csv.Error) as exc:
                raise HTTPException(422, {"file": source["filename"], "message": "需要 UTF-8 CSV：" + str(exc)})
        # 返回的宿主校验错误按 voucher 序号，可通过 locations 找回文件与行。
        return ctx.imports.preview(ident, vouchers, revision=payload["revision"],
            configuration={"rules": "csv-v1", "refs": [config], "locations": locations})

    registrar.get("/chart", chart)
    registrar.post("/parse/{ident}", parse)
