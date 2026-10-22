/**
 * 查询条件在「切走再从标签栏切回来」之后还在不在。
 *
 * 一次性探测脚本（跟 tools/uitest/ 里其余脚本一个性质：留个记录，不进 CI）。
 * 正式回归在 frontend/tests/：nav_tabs.test.mjs 盯标签记的完整地址，
 * url_conditions.test.mjs 盯条件 ↔ URL 的读写。这一份盯**界面上的真话**。
 *
 * 跑法（必须从仓库根）：
 *   npm install && npx playwright install chromium
 *   ./dev.sh                    # 另开一个终端起后端 :8000 与前端 :5173
 *   node tools/uitest/ui_tab_conditions.mjs
 */
import { chromium } from 'playwright'

const OUT = new URL('../../.shots/', import.meta.url).pathname
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1539, height: 954 } })
const errs = []
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()) })
const sleep = ms => page.waitForTimeout(ms)
const hash = () => page.evaluate(() => location.hash)
const tabByText = (txt) => page.locator('.tb-tab').filter({ hasText: txt })

const results = []
function check(name, ok, detail) {
  results.push({ name, ok })
  console.log(`${ok ? 'OK  ' : 'FAIL'} ${name}${detail ? ' —— ' + detail : ''}`)
}
/** 从侧栏点进某个页面（这样标签栏才会多一条标签）。 */
async function gotoByMenu(moduleName, itemName) {
  await page.locator('.module').filter({ hasText: moduleName }).click()
  await sleep(900)
  const item = page.locator('.sidebar li').filter({ hasText: itemName }).first()
  if (await item.count()) { await item.click(); await sleep(2000) }
  else await sleep(2000)
}
/** 切去别的标签，再切回来 —— 这就是那个「离开页面」的动作。 */
async function roundTrip(awayText, backText) {
  await tabByText(awayText).first().click()
  await sleep(1600)
  await tabByText(backText).first().click()
  await sleep(2200)
}
const subText = () => page.evaluate(() => document.querySelector('.report-sub')?.innerText || '')
const periodTexts = () => page.evaluate(() => [...document.querySelectorAll('.ledger-period-select')]
  .map(s => s.querySelector('.el-select__placeholder span')?.innerText?.trim() || ''))

await page.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' })
if (await page.locator('input[type=password]').count()) {
  await page.locator('input').nth(0).fill('admin')
  await page.locator('input').nth(1).fill('123456')
  await page.click('button:has-text("登录")')
  await page.waitForSelector('.shell .sidebar', { timeout: 20000 })
  await sleep(1500)
}

// 先把「凭证」这条标签开出来 —— 后面的来回切都拿它当「别的页面」
await gotoByMenu('凭证处理', '凭证')
await sleep(1200)

// ── 明细账：科目、期间、含未记账 ─────────────────────────
await gotoByMenu('账簿报表', '明细账')
await sleep(1200)
await page.locator('.ledger-subject-picker textarea').click()
await sleep(800)
const subj = page.locator('.sp-panel .sp-item').filter({ hasText: '1002' }).first()
if (await subj.count()) { await subj.click(); await sleep(1600) }
const cb = page.locator('.ledger-toolbar .el-checkbox').first()
if (await cb.count()) { await cb.click(); await sleep(1600) }
const dl0 = { sub: await subText(), periods: await periodTexts(), hash: await hash() }
await roundTrip('凭证', '明细账')
const dl1 = { sub: await subText(), periods: await periodTexts(), hash: await hash() }
check('明细账：切走再切回来，科目/期间/含未记账还在',
  dl0.hash === dl1.hash && dl0.sub === dl1.sub, `${dl0.hash} → ${dl1.hash}`)

// ── 凭证：关键字 ─────────────────────────────────────────
await tabByText('凭证').first().click()
await sleep(1600)
const kw = page.locator('.toolbar .el-input__inner').first()
await kw.fill('记0')
await kw.press('Enter')
await sleep(1800)
const v0 = await hash()
await roundTrip('明细账', '凭证')
const kw2 = await page.locator('.toolbar .el-input__inner').first().inputValue()
check('凭证：切走再切回来，关键字还在', kw2 === '记0' && (await hash()) === v0,
  `${v0} → ${await hash()}`)

// ── 总账：科目与期间 ─────────────────────────────────────
await gotoByMenu('账簿报表', '总账')
await sleep(1400)
await page.locator('.ledger-subject-select .el-select__wrapper').first().click()
await sleep(700)
const glOpt = page.locator('.el-select-dropdown:visible .el-select-dropdown__item')
if (await glOpt.count() >= 2) { await glOpt.nth(1).click(); await sleep(1600) }
const gl0 = await hash()
await roundTrip('凭证', '总账')
check('总账：切走再切回来，科目与期间还在', (await hash()) === gl0, `${gl0} → ${await hash()}`)

// ── 科目余额表：范围与期间 ───────────────────────────────
await gotoByMenu('账簿报表', '科目余额表')
await sleep(1400)
await page.locator('.toolbar .el-radio-button', { hasText: '区间' }).first().click()
await sleep(1800)
const bs0 = await hash()
await roundTrip('凭证', '科目余额表')
check('科目余额表：切走再切回来，档位与期间还在', (await hash()) === bs0, `${bs0} → ${await hash()}`)

// ── 科目表：筛选关键字 ───────────────────────────────────
await gotoByMenu('基础资料', '科目表')
await sleep(1400)
await page.locator('input[placeholder="按编码或名称筛选"]').fill('管理')
await sleep(1200)
await roundTrip('凭证', '科目表')
const kw3 = await page.locator('input[placeholder="按编码或名称筛选"]').inputValue()
check('科目表：切走再切回来，筛选关键字还在', kw3 === '管理', `"${kw3}"`)

// ── 打印模板：范围与模板 ─────────────────────────────────
await gotoByMenu('基础资料', '打印模板')
await sleep(1800)
await page.locator('.sel .el-select__wrapper').first().click()
await sleep(700)
const scopeOpt = page.locator('.el-select-dropdown:visible .el-select-dropdown__item')
const texts = await scopeOpt.allInnerTexts()
const idx = texts.findIndex(t => t.includes('明细账'))
if (idx >= 0) { await scopeOpt.nth(idx).click(); await sleep(2000) }
const pt0 = await hash()
await roundTrip('凭证', '打印模板')
check('打印模板：切走再切回来，范围与模板还在', (await hash()) === pt0, `${pt0} → ${await hash()}`)

await page.screenshot({ path: OUT + 'tab_conditions.png' })
console.log('\n标签栏:', await page.locator('.tb-tab span').allInnerTexts())
console.log('错误:', errs.length ? errs : '无')
const failed = results.filter(r => !r.ok)
console.log(`\n通过 ${results.length - failed.length} / 失败 ${failed.length}`)
await browser.close()
process.exit(failed.length ? 1 : 0)
