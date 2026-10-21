#!/usr/bin/env python3
"""把验证库里的 **2027 年度科目表**清空 —— 让 ui_year_chart.mjs 可以反复跑。

    cd backend && .venv/bin/python ../tools/uitest/reset_uicheck_2027.py

只动验证库 cwq_uicheck，绝不碰开发库 cwq。
"""
import os
import sys
from pathlib import Path

# 本文件在 <repo>/tools/uitest/ 下，backend 在 <repo>/backend
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
import os
os.environ['DATABASE_URL'] = 'postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_uicheck'
os.environ['JWT_SECRET'] = 'uicheck-secret'
os.environ['APP_ENV'] = 'dev'
from sqlalchemy import delete, select
from app.core.db import SessionLocal
from app.models import Book, Period, PeriodOpening, Subject
db = SessionLocal()
book = db.scalar(select(Book).where(Book.name == '租金账套'))
period_ids = list(db.scalars(select(Period.id).where(Period.book_id == book.id, Period.year == 2027)))
db.execute(delete(PeriodOpening).where(PeriodOpening.period_id.in_(period_ids)))
db.execute(delete(Subject).where(Subject.book_id == book.id, Subject.year == 2027))
db.commit()
print('2027 科目表已清空，重跑可重复')
db.close()
