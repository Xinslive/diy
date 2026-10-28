/**
 * **只读账号（VIEWER）的界面自查** —— 2026-10-28 那批改动的正主。
 *
 * 验两件**只有只读账号才验得到**的事（管理员压根触发不到）：
 *
 *  ① **不许看到假的权限错误**：
 *     * 打开「流水明细」→ 不弹「当前账套为只读权限」（而流水行好好地显示着）；
 *     * 打开凭证页 → 不吃 403，「前一张 / 后一张」**可点**（翻凭证是「看」不是「改」）；
 *     * 全程控制台干净、**连 plan-vouchers 那个请求都不该发**（只读账号压根不调它）。
 *
 *  ② **存疑凭证跟着顶栏的会计期间走**：
 *     顶栏停在 A 月就只摆 A 月的，换到 B 月列表跟着变，空态说得清是哪个期间。
 *
 * 它**自己造、自己清**账号（`seed_viewer_case.py`）：跑之前先清同名残留，
 * 跑完清掉 —— 上一轮失败留下的账号不许混进这一轮。
 *
 * 用法：node tools/uitest/ui_flow_readonly.mjs
 */
import { execFileSync } from 'node:child_process'
import { chromium } from 'playwright'
import { gotoStatementPeriod, selectStatement } from './lib/statement_period.mjs'

const BASE = 'http://127.0.0.1:5173'
const ROOT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统'
const PY = `${ROOT}/backend/.venv/bin/python`
const SEED = `${ROOT}/tools/uitest/seed_viewer_case.py`
const OUT = `${ROOT}/.shots`

const errors = []
const ok = (n) => console.log('✓', n)
const bad = (n, d) => { console.log('✗', n, '—', d); errors.push(`${n}: ${d}`) }
async function step(name, fn) {
  try { await fn(); ok(name) } catch (e) { bad(name, e.message.split('\n')[0]) }
}

const seed = () => JSON.parse(execFileSync(PY, [SEED], {
  cwd: `${ROOT}/backend`, encoding: 'utf8',
}))
const unseed = () => {
  try { execFileSync(PY, [SEED, '--clean'], { cwd: `${ROOT}/backend`, encoding: 'utf8' }) }
  catch (e) { console.log('（清理临时账号失败：' + e.message.split('\n')[0] + '）') }
}

const viewer = seed()
console.log(`临时只读账号：${viewer.username} @ 账套 ${viewer.book_id} ${viewer.book_name}`)

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1539, height: 954 } })

/** 这趟里出现过的接口错误（403 之类）。 */
const apiErrors = []
/** 这趟里发过的 plan-vouchers 请求（只读账号**不该**发它）。 */
const planCalls = []
/** 返回 403 的接口（要能说清是哪个接口）。 */
const forbidden = []
page.on('pageerror', (e) => bad('JS error', e.message))
page.on('request', (r) => {
  if (r.url().includes('plan-vouchers')) planCalls.push(r.url())
})
page.on('response', async (r) => {
  if (r.status() === 403) forbidden.push(`${r.request().method()} ${r.url()}`)
})
page.on('console', (m) => {
  if (m.type() !== 'error') return
  const text = m.text()
  // 「Failed to load resource: … 403」这种是网络层的；AxiosError 是跟着来的。
  if (text.includes('403') || text.includes('AxiosError')) apiErrors.push(text.slice(0, 160))
})
/** 盯住那条红字提示 —— toast 会消失，只在 1 秒后取一次快照会漏检。 */
await page.addInitScript(() => {
  window.__cwqToasts = []
  const obs = new MutationObserver((records) => {
    for (const r of records) {
      for (const n of r.addedNodes) {
        if (n.nodeType !== 1) continue
        if (n.classList?.contains('el-message')) {
          window.__cwqToasts.push((n.innerText || '').trim())
        }
      }
    }
  })
  // **盯 document 而不是 document.documentElement** —— addInitScript 跑在页面
  // 载入之前，那时 documentElement 可能还不存在（observe 会抛参数错）。
  obs.observe(document, { childList: true, subtree: true })
})

const sleep = (ms) => page.waitForTimeout(ms)
const readToasts = async () => page.evaluate(() => window.__cwqToasts || [])

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill(viewer.username)
await page.locator('input').nth(1).fill(viewer.password)
await page.click('button:has-text("登录")')
await page.waitForURL(/#\/(books|login)/, { timeout: 20000 })
await sleep(1800)

await step('进入账套', async () => {
  const enter = page.locator('button:has-text("进入账套")')
  if (await enter.count()) {
    await enter.first().click()
    await page.waitForTimeout(1800)
  }
})

await step('打开流水对账页', async () => {
  await page.locator('.modules button:has-text("凭证处理")').first().click()
  await page.waitForTimeout(900)
  await page.locator('.sidebar li').filter({ hasText: '流水对账' }).first().click()
  await page.waitForTimeout(1800)
})

// ---------------------------------------------------------------- ① 流水明细
await step('只读账号打开「流水明细」：流水行显示，不弹假错误', async () => {
  // **顶栏的期间得先跟对账单对上**（顶栏默认停在 12 月，而对账单在 9 月）——
  // 走台账空态里那条「别的期间还有」的入口，跟产品给用户的是同一条路。
  const st = await gotoStatementPeriod(page)
  if (st.mode === 'none') throw new Error('这本账一份对账单都没有 —— 自查没法做')
  await selectStatement(page)

  const rows = await page.locator('.flow-rows-table tr.el-table__row').count()
  if (!rows) throw new Error('流水明细一行都没有 —— 页面其实没加载出来')

  const seen = await readToasts()
  const bad1 = seen.find((t) => t.includes('只读权限'))
  if (bad1) throw new Error(`弹了一条假的权限错误：「${bad1}」`)
  const bad2 = seen.find((t) => t.includes('加载流水失败'))
  if (bad2) throw new Error(`流水好好地显示着，却弹了「${bad2}」`)
})

await step('只读账号压根没发 plan-vouchers 那个请求', async () => {
  if (planCalls.length) throw new Error(`发了 ${planCalls.length} 次 plan-vouchers`)
})

await step('控制台没有 403', async () => {
  if (apiErrors.length || forbidden.length) {
    throw new Error([...forbidden, ...apiErrors].join(' | '))
  }
})

// ---------------------------------------------------------------- ② 凭证页
await step('只读账号打开凭证页：不吃 403，「前一张/后一张」可点', async () => {
  // **菜单项叫「填制凭证」**（不是「凭证列表」—— 那是页面的名字，菜单上的
  // 标题是「填制凭证」，见 router/nav.js）。
  await page.locator('.sidebar li').filter({ hasText: '填制凭证' }).first().click()
  await page.waitForTimeout(2500)

  // **凭证列表页本身也不许吃 403**（那里要拉「断号体检」—— 2026-10-28 修的那处）
  const listForbidden = forbidden.filter((u) => u.includes('/vouchers'))
  if (listForbidden.length) throw new Error(`凭证列表页吃了 403：${listForbidden.join(' | ')}`)

  const row = page.locator('.el-table:visible tr.el-table__row').first()
  if (!(await row.count())) throw new Error('凭证列表一行都没有')
  await row.dblclick()
  await page.waitForTimeout(3000)

  if (!page.url().includes('/vouchers/')) throw new Error(`没进凭证页：${page.url()}`)
  const seen = await readToasts()
  const bad1 = seen.find((t) => t.includes('只读权限'))
  if (bad1) throw new Error(`凭证页弹了一条假的权限错误：「${bad1}」`)

  // **翻凭证是「看」不是「改」** —— 这两颗按钮只读账号也该点得动。
  const prev = page.locator('button:has-text("前一张")').first()
  const next = page.locator('button:has-text("后一张")').first()
  const canPrev = await prev.isEnabled()
  const canNext = await next.isEnabled()
  if (!canPrev && !canNext) {
    throw new Error('「前一张/后一张」两颗都是灰的 —— 只读账号翻不动凭证')
  }
})

await step('凭证页没有 403', async () => {
  if (apiErrors.length || forbidden.length) {
    throw new Error([...forbidden, ...apiErrors].join(' | '))
  }
})

// ---------------------------------------------------------------- ③ 期间过滤
await step('存疑凭证跟着顶栏的会计期间走', async () => {
  await page.locator('.sidebar li').filter({ hasText: '流水对账' }).first().click()
  await page.waitForTimeout(2000)
  await page.click('.el-tabs__item:has-text("存疑凭证")')
  await page.waitForTimeout(2500)

  const monthText = async () => {
    const sel = page.locator('.ctx-select.ctx-year')
    return (await sel.nth(1).innerText()).trim()
  }
  const view = async () => {
    const rows = await page.locator('.flow-suspicious-table tr.el-table__row').count()
    if (rows) return { rows, text: '' }
    const empty = page.locator('.el-empty__description')
    return { rows: 0, text: (await empty.count()) ? (await empty.first().innerText()).trim() : '' }
  }

  const before = await view()
  const beforeMonth = await monthText()

  // **换顶栏的月份**（两个 .ctx-select.ctx-year 里，第二个是月份）
  const sel = page.locator('.ctx-select.ctx-year')
  await sel.nth(1).click()
  await page.waitForTimeout(700)
  const options = page.locator('.period-popper .el-select-dropdown__item:visible')
  const n = await options.count()
  let switched = null
  for (let i = 0; i < n; i++) {
    const label = (await options.nth(i).innerText()).trim()
    if (label !== beforeMonth) { switched = label; break }
  }
  if (!switched) throw new Error('月份下拉里没有别的月份可选')
  const idx = (await options.allInnerTexts()).map((s) => s.trim()).indexOf(switched)
  await options.nth(idx).click()
  await page.waitForTimeout(2200)

  const after = await view()
  const afterMonth = await monthText()
  if (afterMonth === beforeMonth) throw new Error('顶栏的月份没切过去')

  // **空态要说得清是哪个期间** —— 不说清楚，用户只会以为功能坏了。
  if (!after.rows && after.text && !after.text.includes(afterMonth.replace(/\s*月$/, ' 月'))) {
    // 文案形如「2026 年 12 月没有存疑凭证」；月份下拉写的是「12 月」。
    const want = afterMonth.match(/(\d+)\s*月/)
    if (!want || !after.text.includes(`${want[1]} 月`)) {
      throw new Error(`空态文案没说是哪个期间：「${after.text}」（顶栏是 ${afterMonth}）`)
    }
  }
  if (!after.rows && !after.text) throw new Error('空态连文案都没有')
})

await page.screenshot({ path: `${OUT}/readonly_period.png` })

console.log(errors.length ? `\n有问题：${errors.length}` : '\n没问题')
errors.forEach((e) => console.log('  - ' + e))
await browser.close()
unseed()
process.exit(errors.length ? 1 : 0)
