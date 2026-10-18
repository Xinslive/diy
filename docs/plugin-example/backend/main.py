from fastapi import HTTPException


def register(registrar, ctx):
    table = "plugin_4b8db6655ff5_note"

    def notes(book_id: int):
        ctx.book_read(book_id)
        return ctx.data.read(table, book_id=book_id)

    def save(payload: dict):
        book_id, label = payload.get("book_id"), payload.get("label")
        if type(book_id) is not int or not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
            raise HTTPException(400, "请选择账套并填写 1 到 120 字笔记")
        result = ctx.data.insert(table, {"label": label.strip()}, book_id=book_id)
        ctx.audit_write("note_saved")
        return result

    def closed(event):
        ctx.audit_write("period_closed")

    registrar.get("/notes", notes)
    registrar.post("/notes", save)
    registrar.hook("on_period_close", closed)
