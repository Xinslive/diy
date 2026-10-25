// 凭证页科目格左下角那个余额：**滚动** + **敲完才更新**。
//
//   node tools/uitest/ui_voucher_balance.mjs
//
// 守的是 2026-10-25 用户提的两条：
//   ① 同一张凭证里同一科目出现三次借 100，三行分别显示 100 / 200 / 300
//      （以前是「整张合计」，三行都显示 300）；
//   ② 敲字的过程中余额一动不动，光标离开那一格才一步到位
//      （输入 1 / 10 / 100，余额一直是 300；离开那一格才从 300 变成 400）。
//
// 另外顺带验了**时点**：打开一张**月中的**老凭证，那一格显示的是
// 「这张之前账上有多少 + 本张累计到该行」，不是整期期末。
//
// **真账一个字节都不动**：写接口一律拦下来（回一个假的凭证）。
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:5173'
const OUT = fileURLToPath(new URL('../../.shots/', import.meta.url))
const fails = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1560, height: 980 } })
page.on('pageerror', e => fails.push('pageerror: ' + e.message + '｜' + String(e.stack || '').split('\n')[1]))
const sleep = ms => page.waitForTimeout(ms)
const rows = () => page.locator('.vg-row:not(.vg-head):not(.vg-total)')

async function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  console.log(ok ? '✓' : '✗', name, ok ? '' : `— 实际 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`)
  if (!ok) fails.push(`${name}: ${JSON.stringify(got)} != ${JSON.stringify(want)}`)
}

/** 这一行科目格左下角那个余额（去掉千分位；没有余额时是空） */
async function hintOf(i) {
  const el = rows().nth(i).locator('.subj-balance')
  if (!(await el.count())) return ''
  const t = (await el.textContent()).trim()
  if (!t) return ''
  return t.replace(/,/g, '')
}

/** 往金额格里填数：点一下 → 全选 → 打字（fill() 会把数插到光标处，不能用） */
async function fillAmt(loc, text) {
  await loc.click(); await sleep(150)
  await page.keyboard.press('ControlOrMeta+A')
  if (text) { await page.keyboard.type(text); await sleep(250) }
  else { await page.keyboard.press('Backspace'); await sleep(200) }
}

/** 在某一行选科目：打字 → 回车。panel 里筛出来的第一条就是它 */
async function pickSubject(i, kw) {
  await rows().nth(i).locator('.sp-input').click(); await sleep(400)
  await page.keyboard.type(kw); await sleep(650)
  await page.keyboard.press('Enter'); await sleep(350)
}

/** 页面里发一个 GET（带登录态），拿后端给的数 */
async function apiGet(path) {
  return page.evaluate(async (p) => {
    const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
    const r = await (await fetch(p, { headers: h })).json()
    return r
  }, path)
}

/** 分 → 显示文本（与前端 moneyText 同一套写法：负号、千分位、两位小数） */
function centsToText(cents) {
  const neg = cents < 0n
  const a = neg ? -cents : cents
  const yuan = (a / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${neg ? '-' : ''}${yuan}.${String(a % 100n).padStart(2, '0')}`
}

/** "12345.67" → 1234567n（分）。字符串定点，不经过浮点。 */
function toCents(s) {
  const t = String(s ?? '0')
  const neg = t.startsWith('-')
  const [i, f = ''] = (neg ? t.slice(1) : t).split('.')
  const c = BigInt(i || '0') * 100n + BigInt((f + '00').slice(0, 2))
  return neg ? -c : c
}

// 写接口一律拦下来：真账一个字节都不动
await page.route('**/api/books/*/vouchers**', async (route) => {
  const req = route.request()
  if (req.url().includes('/neighbour')) {
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ prev_id: null, next_id: null }) })
  }
  if (req.method() === 'GET') return route.continue()
  await route.fulfill({
    status: req.method() === 'POST' ? 201 : 200,
    contentType: 'application/json',
    body: JSON.stringify({
      id: 987654, voucher_no: '记-0001', voucher_date: '2026-12-11', status: 'UNPOSTED',
      remark: '', attachments: 0, period_id: 0, total_debit: '0', total_credit: '0', entries: []
    })
  })
})

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
await page.waitForSelector('.sidebar .sb-list li', { timeout: 15000 })
await sleep(1800)
await page.locator('.topbar .el-select').first().click(); await sleep(800)
await page.locator('.el-select-dropdown__item:visible', { hasText: '启明物业' }).first().click()
await sleep(1800)

// 顶栏可能停在最新的那个年度（下一年的 12 期，一张凭证也没有）。
// 显式切到凭证最多的那个月，免得验在一个空月份上。
const wanted = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const book = localStorage.getItem('cwq_book')
  const ps = await (await fetch(`/api/books/${book}/periods`, { headers: h })).json()
  const p = ps.filter(x => x.vouchers > 0).sort((a, b) => b.vouchers - a.vouchers)[0]
  return p ? { id: p.id, code: p.code, vouchers: p.vouchers } : null
})
if (!wanted) throw new Error('这本账里一张凭证都没有，没法验收')
await page.evaluate(id => localStorage.setItem('cwq_period', String(id)), wanted.id)
await page.reload({ waitUntil: 'networkidle' }); await sleep(2000)
console.log(`   （顶栏定在 ${wanted.code}，该期 ${wanted.vouchers} 张凭证）`)

// ============================================================ 一、滚动 + 敲完才更新
await page.goto(`${BASE}/#/vouchers/new`); await sleep(2000)

// 三行同一科目（库存现金），各借 100
for (let i = 0; i < 3; i++) {
  await pickSubject(i, 'kcxj')
  await fillAmt(rows().nth(i).locator('.td-amt.d .amt-input'), '100')
}
// 焦点还在第三行的金额格里 —— 那一格的值**还没定下来**（这正是要验的那件事）。
// 先离开它，三行才算都定了。
await page.keyboard.press('Tab'); await sleep(500)

const h1 = await hintOf(0), h2 = await hintOf(1), h3 = await hintOf(2)
console.log(`   （三行的余额：${h1} / ${h2} / ${h3}）`)
const num = t => Number(String(t).replace(/,/g, '').replace(/[^0-9.\-]/g, '') || 0)
check('第二行比第一行多 100（滚动，不是整张合计）', num(h2) - num(h1), 100)
check('第三行比第二行多 100', num(h3) - num(h2), 100)

// 第四行：先选科目 —— 此时它显示的是「累计到本行为止」（还是前 300）
await pickSubject(3, 'kcxj'); await sleep(400)
const h4a = await hintOf(3)
check('第四行刚选上科目时，累计到本行为止 = 第三行那个数', num(h4a), num(h3))

// 一个数字一个数字地敲：1 → 10 → 100，余额一动不动
const cell = rows().nth(3).locator('.td-amt.d .amt-input')
await cell.click(); await sleep(150)
await page.keyboard.press('ControlOrMeta+A')
for (const ch of ['1', '0', '0']) {
  await page.keyboard.type(ch)
  await sleep(220)
  check(`敲到「${ch}」时余额还停在原地`, await hintOf(3), h4a)
}
await page.screenshot({ path: OUT + 'ui-voucher-balance-typing.png' })

// 光标离开这一格 → 一步到位
await page.keyboard.press('Tab'); await sleep(400)
const h4b = await hintOf(3)
check('光标离开这一格，余额一步到位（+100）', num(h4b) - num(h4a), 100)

// ============================================================ 二、老凭证看的是**它之前**的账
// 挑一张**不是最后一张**的老凭证：它后面还有别的凭证，
// 显示的数要是「它之前」的账 —— 期末的账会把后面那些也算进去。
const target = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const book = localStorage.getItem('cwq_book')
  const pid = localStorage.getItem('cwq_period')
  const list = await (await fetch(
    `/api/books/${book}/vouchers?from_period_id=${pid}&to_period_id=${pid}&sort=no_asc&page_size=50`,
    { headers: h })).json()
  const items = list.items || list.rows || list.data || []
  if (items.length < 2) return null
  const v = items[0]                       // 这个月的第一张：它前面什么都没有
  const full = await (await fetch(`/api/books/${book}/vouchers/${v.id}`, { headers: h })).json()
  return { id: v.id, no: v.voucher_no, entries: full.entries || [], periodId: pid }
})

if (!target) {
  console.log('   （这个月只有一张凭证，时点那条验不了 —— 跳过）')
} else {
  const bookId = await page.evaluate(() => localStorage.getItem('cwq_book'))
  const balances = await apiGet(
    `/api/books/${bookId}/ledger/voucher-balances?period_id=${target.periodId}&voucher_id=${target.id}`)
  const subjects = await apiGet(`/api/books/${bookId}/subjects/flat`)
  const dirOf = new Map((subjects || []).map(s => [String(s.id), s.direction]))

  await page.goto(`${BASE}/#/vouchers/${target.id}`); await sleep(2200)

  // 逐行算：这一格 = 基数（这张之前）+ 本张累计到本行为止
  const acc = new Map()
  let ok = 0, bad = 0
  for (let i = 0; i < target.entries.length; i++) {
    const e = target.entries[i]
    const sid = String(e.subject_id)
    const c = toCents(e.amount)
    acc.set(sid, (acc.get(sid) || 0n) + (e.direction === 'DEBIT' ? c : -c))
    const base = toCents(balances.balances?.[sid])
    const signed = base + (acc.get(sid) || 0n)
    const shown = dirOf.get(sid) === 'CREDIT' ? -signed : signed
    // 比**数值**（页面上带千分位，这里不带）
    const want = shown === 0n ? 0 : Number(shown) / 100
    const got = await hintOf(i)
    const gotNum = got ? Number(got) : 0
    if (gotNum === want) ok++; else { bad++; console.log(`   · 第 ${i + 1} 行：实际 ${got}，期望 ${want}`) }
  }
  check('老凭证的每一行都是「这张之前 + 累计到本行」', bad, 0)
  console.log(`   （验了 ${ok + bad} 行）`)
  await page.screenshot({ path: OUT + 'ui-voucher-balance-existing.png' })
}

await browser.close()
console.log(fails.length ? `\n✗ 失败 ${fails.length} 项：\n` + fails.map(f => '  - ' + f).join('\n')
  : '\n✓ 全部通过')
process.exit(fails.length ? 1 : 0)
