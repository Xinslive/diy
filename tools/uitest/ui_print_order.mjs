/**
 * 打印顺序开关的界面探针（用户 2026-10-30 批注）。
 *
 * 盯这几件事：
 *   1. 一页一张的模板只露「倒序打印」，一页两张的模板两个都露；
 *   2. 默认值：错位配对开、倒序打印关；
 *   3. 真的把 print_order 与 reverse 送进请求了；
 *   4. 端到端：勾 6 张凭证，预览第一页确实是错位配对的一对。
 *
 * 一次性探针，不进 CI（见 README「tools/uitest」一节）。
 * 用法：node tools/uitest/ui_print_order.mjs
 */
import { chromium } from 'playwright'

const BASE = process.env.CWQ_BASE || 'http://127.0.0.1:5173'
const SHOT_DIR = process.env.SHOT_DIR || '.shots'
const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1543, height: 984 } })
page.setDefaultTimeout(8000)
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))

const step = async (name, fn) => {
  try { const r = await fn(); console.log('✓', name, r === undefined ? '' : r); return r }
  catch (e) { console.log('✗', name, '—', e.message); errors.push(name); return null }
}

/** 记下每次凭证打印请求上的 print_order 与 reverse。 */
const orderCalls = []
page.on('request', (req) => {
  const url = req.url()
  if (url.includes('/print/vouchers.html')) {
    const u = new URL(url)
    orderCalls.push({
      order: u.searchParams.get('print_order'),
      reverse: u.searchParams.get('reverse'),
    })
  }
})

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)
// vite 的依赖预构建缓存可能过期（504 Outdated Optimize Dep）：重载一次让它重算。
if (!(await page.locator('input[type=password]').count())) {
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(2500)
}
if (await page.locator('input[type=password]').count()) {
  await step('登录', async () => {
    await page.fill('input[type=password]', process.env.CWQ_PASSWORD || '123456')
    await page.click('button:has-text("登录")')
    // 登录页的表单消失即登录成功（外壳的类名各版本不一样，不拿它当判据）。
    await page.waitForSelector('input[type=password]', { state: 'detached', timeout: 20000 })
    await page.waitForTimeout(1200)
  })
}
const token = await page.evaluate(() => localStorage.getItem('cwq_token'))
const api = { headers: { Authorization: `Bearer ${token}` } }
const books = await (await page.request.get(`${BASE}/api/books`, api)).json()
const book = books.find((b) => ['OWNER', 'ACCOUNTANT'].includes(b.role))
if (!book) throw new Error('需要可写账套')
const periods = await (await page.request.get(`${BASE}/api/books/${book.id}/periods`, api)).json()
const period = periods.find((p) => p.vouchers > 0) || periods[0]
await page.evaluate(({ b, p }) => {
  localStorage.setItem('cwq_book', String(b))
  localStorage.setItem('cwq_period', String(p))
}, { b: book.id, p: period.id })

await page.goto(BASE + '/#/vouchers', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

/** 打开「打印记账凭证」弹窗。
 *
 * 工具栏上的「打印」按钮**勾了凭证才冒出来**（用户 2026-10-26 批注），
 * 所以先勾一行，再点「打印所选（N）」。
 */
const openDialog = async () => {
  await page.waitForSelector('.el-table__body-wrapper .el-checkbox', { timeout: 20000 })
  await page.locator('.el-table__body-wrapper .el-checkbox').first().click()
  await page.waitForTimeout(600)
  const btn = page.locator('button:has-text("打印所选")').first()
  await btn.waitFor({ timeout: 10000 })
  await btn.click()
  await page.waitForSelector('.print-dialog', { timeout: 15000 })
  await page.waitForTimeout(1500)
}

/** 切到指定模板（按名字前缀）。 */
const pickTemplate = async (namePrefix) => {
  await page.locator('.print-dialog .el-form-item:has-text("打印模板") .el-select').click()
  await page.waitForTimeout(400)
  await page.locator('.el-select-dropdown__item:visible').filter({ hasText: namePrefix }).first().click()
  await page.waitForTimeout(1800)
}

/** 读出某个开关：说明文字 + 当前状态。找不到返回 null。 */
const readSwitch = async (label) => page.evaluate((label) => {
  const item = [...document.querySelectorAll('.print-dialog .el-form-item')]
    .find((el) => (el.querySelector('.el-form-item__label')?.textContent || '').trim() === label)
  if (!item) return null
  return {
    hint: (item.querySelector('.hint')?.textContent || '').trim(),
    on: !!item.querySelector('.el-switch.is-checked'),
  }
}, label)

/** 点某个开关。 */
const toggleSwitch = async (label) => {
  await page.locator(`.print-dialog .el-form-item:has-text("${label}") .el-switch`).first().click()
  await page.waitForTimeout(1500)
}

/** 最近一次凭证打印请求上的口径。 */
const lastOrder = async () => {
  await page.waitForTimeout(1200)
  const last = orderCalls[orderCalls.length - 1]
  if (!last) throw new Error('没看到凭证打印请求')
  return last
}

await step('打开打印记账凭证弹窗', async () => {
  await openDialog()
  await page.screenshot({ path: `${SHOT_DIR}/print_order_dialog_默认.png` })
})

await step('一页一张：只露「倒序打印」，默认关', async () => {
  const interleave = await readSwitch('打印顺序优化')
  if (interleave !== null) throw new Error('一页一张时不该露「打印顺序优化」')
  const s = await readSwitch('倒序打印')
  if (!s) throw new Error('弹窗里没有「倒序打印」开关')
  if (s.on) throw new Error('默认是打开的')
  await page.screenshot({ path: `${SHOT_DIR}/print_order_一张.png` })
  return s.hint
})

await step('一页一张时请求带 print_order=normal', async () => {
  const last = await lastOrder()
  if (last.order !== 'normal') throw new Error('请求上是 ' + last.order)
})

await step('打开倒序打印后请求带 reverse=true', async () => {
  await toggleSwitch('倒序打印')
  const last = await lastOrder()
  if (last.reverse !== 'true') throw new Error('请求上是 ' + last.reverse)
  await toggleSwitch('倒序打印')
})

await step('一页两张：两个开关都露，错位配对默认开、倒序默认关', async () => {
  await pickTemplate('基础模板')
  const interleave = await readSwitch('打印顺序优化')
  if (!interleave) throw new Error('一页两张时没有「打印顺序优化」开关')
  if (!interleave.on) throw new Error('错位配对默认没有打开')
  const rev = await readSwitch('倒序打印')
  if (!rev) throw new Error('一页两张时没有「倒序打印」开关')
  if (rev.on) throw new Error('倒序打印默认是打开的')
  await page.screenshot({ path: `${SHOT_DIR}/print_order_两张.png` })
  return interleave.hint
})

await step('一页两张时请求带 print_order=optimized', async () => {
  const last = await lastOrder()
  if (last.order !== 'optimized') throw new Error('请求上是 ' + last.order)
})

await step('关掉错位配对后请求带 print_order=normal', async () => {
  await toggleSwitch('打印顺序优化')
  const last = await lastOrder()
  if (last.order !== 'normal') throw new Error('请求上是 ' + last.order)
  await toggleSwitch('打印顺序优化')
})

await step('打开倒序打印后请求带 reverse=true', async () => {
  await toggleSwitch('倒序打印')
  const last = await lastOrder()
  if (last.reverse !== 'true') throw new Error('请求上是 ' + last.reverse)
})

// 端到端：勾 6 张凭证，看预览第一页是不是 1+4 那样错位配对的一对。
await step('预览第一页就是错位配对的一对', async () => {
  await page.keyboard.press('Escape')
  await page.waitForTimeout(600)
  // 回到列表：reload 比 goto 稳（hash 路由再 goto 一次有时等不到 networkidle）。
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  const boxes = page.locator('.el-table__body-wrapper .el-checkbox')
  const n = Math.min(6, await boxes.count())
  for (let i = 0; i < n; i++) await boxes.nth(i).click()
  await page.waitForTimeout(800)
  await page.locator('button:has-text("打印所选")').first().click()
  await page.waitForSelector('.print-dialog', { timeout: 15000 })
  await page.waitForTimeout(2500)
  await pickTemplate('基础模板')
  await page.waitForTimeout(2500)
  const pages = await page.evaluate(() => {
    const doc = document.querySelector('iframe.frame')?.contentDocument
    if (!doc) return []
    return [...doc.querySelectorAll('.page')].map((pg) =>
      [...pg.querySelectorAll('.voucher-slot')].map((sl) => {
        const m = (sl.textContent || '').match(/记\d{4}/)
        return m ? m[0] : ''
      }))
  })
  if (!pages.length) throw new Error('预览里没读到页面')
  await page.screenshot({ path: `${SHOT_DIR}/print_order_预览.png` })
  if (pages[0].length !== 2) throw new Error('第一页不是两格：' + JSON.stringify(pages[0]))
  // 错位配对：第一页的第一格不是页面上最早的那张（否则就是连续配对）。
  const first = pages[0][0]
  const lastSlot = pages[pages.length - 1][pages[pages.length - 1].length - 1]
  if (first >= lastSlot) throw new Error('第一页不是配对错开的：' + JSON.stringify(pages))
  return JSON.stringify(pages)
})

await browser.close()
console.log(errors.length ? '\n有错：\n' + errors.join('\n') : '\n没有报错')
process.exit(errors.length ? 1 : 0)
