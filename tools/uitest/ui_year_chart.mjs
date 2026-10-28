/**
 * 年度账套的**浏览器实测**：科目表页跟着顶栏的年度走，导出某一年不动另一年。
 *
 * 这条路径是用户报的那个缺陷的正脸：
 * 「往一个 2027 年一张凭证都没有的账套里导入科目，被提示『账套已有凭证或
 * 期初余额，不能覆盖科目表』—— 原因只是上一个年度（2026）有记账。」
 *
 * 跑法（必须从仓库根）：
 *     node tools/uitest/ui_year_chart.mjs
 *
 * 前提：后端 :8000 + 前端 :5173 已经起来，且后端指向一个**已迁移到 head**、
 * **2027 年还没有科目表**的库。整套准备（不碰开发库 cwq）：
 *
 *     cd backend
 *     .venv/bin/python -c "import psycopg; c=psycopg.connect('postgresql://cwq:cwq123@localhost:15432/postgres', autocommit=True); c.execute('CREATE DATABASE cwq_uicheck')"
 *     DATABASE_URL='postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_uicheck' \
 *       JWT_SECRET='uicheck-secret' APP_ENV=dev .venv/bin/python scripts/init_db.py
 *     # 再给账套补上 2027 年的 12 个期间（不补的话顶栏的年度下拉里没有 2027）
 *     DATABASE_URL='...' JWT_SECRET='uicheck-secret' APP_ENV=dev .venv/bin/python -c "
 *       from sqlalchemy import select
 *       from app.core.db import SessionLocal
 *       from app.models import Book
 *       from app.accounting.periods import ensure_periods
 *       db = SessionLocal(); b = db.scalar(select(Book).where(Book.name=='租金账套'))
 *       ensure_periods(db, b, 2027); db.commit()"
 *
 *     # 起后端（指向上面那个库）与前端
 *     DATABASE_URL='...' JWT_SECRET='uicheck-secret' APP_ENV=dev \
 *       .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
 *     cd ../frontend && npm run dev
 *
 * **重跑前要先清掉 2027 的科目表**（上一次的导入还在那儿，「2027 应当是空的」
 * 这条断言就会红）：见本文件末尾的 reset 片段。
 */
import { chromium } from 'playwright'

const B = 'http://localhost:5173'
const errors = []
const step = (n, f) =>
  f()
    .then(() => console.log('✓', n))
    .catch((e) => {
      console.log('✗', n, '—', String(e.message).split('\n')[0])
      errors.push(n)
    })

const b = await chromium.launch()
const ctx = await b.newContext({ viewport: { width: 1560, height: 1000 } })
const p = await ctx.newPage()
p.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
p.on('pageerror', (e) => errors.push('pageerror: ' + e.message))

await p.goto(B, { waitUntil: 'networkidle' })
await p.fill('input[type="text"]', 'admin')
await p.fill('input[type="password"]', '123456')
await p.click('button:has-text("登录")')
await p.waitForSelector('.book-page', { timeout: 15000 })

/** 顶栏的年度下拉：`.ctx-year` 有两个（年、月），第一个是年。 */
async function pickYear(year) {
  await p.click('.ctx-year >> nth=0')
  await p.waitForSelector('.el-select-dropdown__item:visible', { timeout: 5000 })
  await p.click(`.el-select-dropdown__item:visible:has-text("${year} 年")`)
  await p.waitForTimeout(700)
}

/** 读工具栏上那句「X 年度科目表」里的 X。 */
async function toolbarYear() {
  const text = await p.locator('text=年度科目表').first().innerText()
  return text.match(/(\d{4})\s*年度科目表/)?.[1] ?? null
}

/** 读当前年度科目表的科目行数（科目表页是一张表，不是树）。 */
async function subjectCount() {
  return p.locator('.el-table__row').count()
}

/** 读当前年度科目表第一行的编码与名称。 */
async function firstRow() {
  const cells = await p.locator('.el-table__row').first().locator('td').allInnerTexts()
  return cells.map((t) => t.trim()).filter(Boolean).join('/')
}

await step('进入科目表页', async () => {
  await p.evaluate(() => { location.hash = '#/subjects' })
  await p.waitForSelector('text=年度科目表', { timeout: 10000 })
  await p.waitForTimeout(800)
})

let y26Count = 0
let y26Sample = ''

await step('默认显示 2026 年度科目表', async () => {
  const y = await toolbarYear()
  if (y !== '2026') throw new Error(`默认年度是 ${y}，不是 2026`)
  y26Count = await subjectCount()
  if (!y26Count) throw new Error('2026 年度科目表是空的 —— 前置条件不成立')
  y26Sample = await firstRow()
  console.log(`   2026 年科目数 ${y26Count}，首个 ${y26Sample}`)
})

await step('切到 2027 年：工具栏跟着变，科目表是空的', async () => {
  await pickYear(2027)
  const y = await toolbarYear()
  if (y !== '2027') throw new Error(`切了年度，工具栏还写着 ${y}`)
  const n = await subjectCount()
  if (n !== 0) throw new Error(`2027 年应当一张科目都没有，却有 ${n} 个`)
})

await step('导入 2027 年度的科目表', async () => {
  await p.click('button:has-text("导入并覆盖")')
  // el-upload 的 file input 是**隐藏**的（display:none），只能等它 attached，
  // 等 visible 会永远超时
  await p.waitForSelector('input[type="file"]', { state: 'attached', timeout: 8000 })
  await p.setInputFiles('input[type="file"]', '/tmp/chart2027.xlsx')
  await p.click('.el-dialog button:has-text("解析并预览")')
  await p.waitForSelector('text=文件校验通过', { timeout: 15000 })
  const ok = await p.locator('text=文件校验通过').count()
  if (!ok) throw new Error('预览没通过校验')
})

await step('确认框里写明是 2027 年度', async () => {
  await p.click('.el-dialog button:has-text("确认覆盖")')
  await p.waitForSelector('.el-message-box', { timeout: 8000 })
  const text = await p.locator('.el-message-box__message').innerText()
  if (!text.includes('2027')) throw new Error(`确认框里没有年度：${text}`)
  await p.click('.el-message-box button:has-text("确认覆盖")')
  await p.waitForSelector('text=科目表已覆盖导入', { timeout: 15000 })
})

await step('2027 年出现新科目（编码是 2027 自己那套）', async () => {
  await p.waitForTimeout(600)
  const joined = (await p.locator('.el-table__row').allInnerTexts()).join('|')
  if (!joined.includes('2027库存现金')) throw new Error(`没看到导入的科目：${joined.slice(0, 200)}`)
})

await step('切回 2026 年：科目表一行都没变', async () => {
  // 导入成功后弹窗**不会自动关**（done 步有个「完成」按钮），
  // 不关掉它，顶栏就被遮罩挡住点不动。
  await p.click('.el-dialog button:has-text("完成")')
  await p.waitForSelector('.el-dialog', { state: 'hidden', timeout: 15000 })
  await p.waitForTimeout(500)
  await pickYear(2026)
  const y = await toolbarYear()
  if (y !== '2026') throw new Error(`切回年度失败，工具栏写着 ${y}`)
  const n = await subjectCount()
  if (n !== y26Count) throw new Error(`2026 年科目数变了：${y26Count} → ${n}`)
  const first = await firstRow()
  if (first !== y26Sample) throw new Error(`2026 年首个科目变了：${y26Sample} → ${first}`)
})

await b.close()

if (errors.length) {
  console.log(`\n✗ ${errors.length} 项没过：${errors.join(' / ')}`)
  process.exit(1)
}
console.log('\n✓ 全部通过')
