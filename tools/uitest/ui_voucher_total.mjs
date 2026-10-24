// 合计口径的浏览器验收：**按列求和、负数照减**（与账上同一口径）。
//
// 拿的是用户 2026-10-24 那张凭证的真实数字：借 −333，贷 2222 + 贷 3。
// 页面上的合计必须是「借 −333.00 / 贷 2225.00」——
// 以前这里是「红字算到对面去」：借 −333 被挪到贷方，贷方那栏就摆出 2,558.00，
// 页面合计与总账、报表、打印都对不上。
//
//   node ui_voucher_total.mjs
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

/** 合计行里那一栏的金额：格子上是逐格落的数字，读回来拼成 '1000.00' 才好比 */
async function totalOf(side) {
  const t = await page.locator(`.vg-total .td-total.${side}`)
    .evaluate((el) => el.textContent.replace(/\s+/g, ''))
  if (!t) return ''
  const neg = t[0] === '\u2212' || t[0] === '-'
  const d = neg ? t.slice(1) : t
  return (neg ? '-' : '') + d.slice(0, -2) + '.' + d.slice(-2)
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

/** 写接口一律拦下来：记一笔再回一个假的凭证 —— 不往真账里塞凭证 */
const writes = []
await page.route('**/api/books/*/vouchers**', async (route) => {
  const req = route.request()
  if (req.url().includes('/neighbour')) {
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ prev_id: null, next_id: null }) })
  }
  if (req.method() === 'GET') return route.continue()
  writes.push({ method: req.method(), body: JSON.parse(req.postData() || '{}') })
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

// ---- 用户那张凭证的数字：借 −333，贷 2222 + 贷 3 ----
await page.goto(`${BASE}/#/vouchers/new`); await sleep(2000)

// 第一行：借 −333
await pickSubject(0, 'kcxj')
await fillAmt(rows().nth(0).locator('.td-amt.d .amt-input'), '-333'); await sleep(300)
await check('第一行借方录成 −333', await rows().nth(0).locator('.td-amt.d .amt-input').inputValue(), '-333')

// 第二行：贷 2222
await pickSubject(1, 'kcxj')
await fillAmt(rows().nth(1).locator('.td-amt.c .amt-input'), '2222'); await sleep(300)

// 第三行：贷 3
await pickSubject(2, 'kcxj')
await fillAmt(rows().nth(2).locator('.td-amt.c .amt-input'), '3'); await sleep(400)

// 合计：按列求和、负数照减
await check('合计行是借 −333.00 / 贷 2225.00',
  [await totalOf('d'), await totalOf('c')], ['-333.00', '2225.00'])

// 页脚差额还是照常算：贷 2225 − 借 (−333) = 2558（差额在两种口径下恒等）
await check('页脚说清是贷方大 2,558.00',
  (await page.locator('.foot-right .tag-bad').textContent()).trim(), '贷方大 2,558.00')

// 借方那一格是负数 → 红字（红字冲销的老规矩）
await check('借方合计是红字',
  await page.evaluate(() => getComputedStyle(document.querySelector('.vg-total .td-total.d .amt-cell')).color),
  'rgb(217, 48, 37)')
await check('贷方合计不是红字',
  await page.evaluate(() => getComputedStyle(document.querySelector('.vg-total .td-total.c .amt-cell')).color),
  'rgb(48, 49, 51)')

// 证据照：合计摆着「借 −333.00 / 贷 2225.00」的那一刻
await page.screenshot({ path: OUT + 'ui-voucher-total.png' })

// 只填借方一栏时：贷方那一栏压根没有分录 → 留空；借方那栏还剩 −333，照实摆着。
// 清空要从**下往上**清：清掉上一行，下面那行立刻就锁了（正着清会清到一半卡住）。
await fillAmt(rows().nth(2).locator('.td-amt.c .amt-input'), ''); await sleep(300)
await fillAmt(rows().nth(1).locator('.td-amt.c .amt-input'), ''); await sleep(500)
await check('撤掉贷方两行 → 借方 −333.00，贷方留空',
  [await totalOf('d'), await totalOf('c')], ['-333.00', ''])

await browser.close()
console.log(fails.length ? `\n✗ 失败 ${fails.length} 项：\n` + fails.map(f => '  - ' + f).join('\n')
  : '\n✓ 全部通过')
process.exit(fails.length ? 1 : 0)
