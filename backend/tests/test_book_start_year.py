"""账套「起始年度」：范围放开，但**边界要有**。

用户说的是一句大白话：「起始年度能选的范围太少了，我想要想选哪年就选哪年」。
以前那个下拉只有 6 个写死的候选（去年到四年后），想建一本 1998 年起步的账，
界面上根本选不出来 —— 那不是「候选少」，是**没给出口**，剩下的路只有「去改库」。

所以起始年度放开到 1900–2100（`BookIn.start_year` 的边界）。但边界不是形式：
`period.ord` 是按 `(year - start_year) * 12 + month` 推算的，
起始年度一旦离谱，铺出来的期间序号整条都跟着歪，而那时候再回头
改的就不是「起始年度」这一个字段了。

所以这里钉三件事：
  1. 很早的年份（比如 1998）**能建**，而且那一年 12 个期间真的铺出来了；
  2. 越界的年份在**接口层**就被挡回去（422），不是建完再报错；
  3. 存下来的 `start_year` 就是用户选的那个年份，一字不差。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.api.schemas import START_YEAR_MAX, START_YEAR_MIN
from app.models import Book, Period


def _payload(**over) -> dict:
    tag = uuid.uuid4().hex[:8]
    return {
        "company_name": f"起始年度测试{tag}",
        "name": f"账套{tag}",
        "start_year": 1998,
        "start_month": 1,
        **over,
    }


def test_可以从很早的年份起账(client, auth, db):
    """以前界面上选不出 1998，只能去改库；现在接口本来就收。"""
    r = client.post("/api/books", json=_payload(start_year=1998), headers=auth)
    assert r.status_code == 201, r.text
    assert r.json()["start_year"] == 1998, r.json()

    book = db.scalar(select(Book).where(Book.id == r.json()["id"]))
    assert book.start_year == 1998, "存下来的起始年度跟选的不一样"

    # 起始年度那一年 12 个期间要真的铺出来，否则「从 1998 年开始记」是句空话
    periods = db.scalars(
        select(Period).where(Period.book_id == book.id, Period.year == 1998)
    ).all()
    assert len(periods) == 12, f"1998 年只铺了 {len(periods)} 个期间"


def test_起始年度越界被接口挡下(client, auth):
    """越界在建之前就挡，不要建完再报错 —— 期间序号已经按它推算过了。"""
    for bad in (START_YEAR_MIN - 1, START_YEAR_MAX + 1):
        r = client.post("/api/books", json=_payload(start_year=bad), headers=auth)
        assert r.status_code == 422, f"起始年度 {bad} 居然建成了：{r.text}"


def test_边界上的年份能建(client, auth):
    for good in (START_YEAR_MIN, START_YEAR_MAX):
        r = client.post("/api/books", json=_payload(start_year=good), headers=auth)
        assert r.status_code == 201, f"起始年度 {good} 建不了：{r.text}"
        assert r.json()["start_year"] == good


def test_起始年度必须是整数(client, auth):
    """「1998.5」「二〇二六」这类不是年份的输入要挡在接口外，
    而不是转成一个整数悄悄记进去。"""
    for bad in ("1998.5", "二〇二六", ""):
        r = client.post("/api/books", json=_payload(start_year=bad), headers=auth)
        assert r.status_code == 422, f"起始年度 {bad!r} 居然建成了：{r.text}"
