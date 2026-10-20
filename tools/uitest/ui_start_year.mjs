/**
 * 新建账套的「起始年度」（2026-10-20）。
 *
 * 用户报的是：「起始年度能选的范围太少了，我想要想选哪年就选哪年」。
 * 以前那个下拉只有 6 个写死的候选（去年到四年后），想建一本 1998 年起步的账，
 * 界面上根本选不出来 —— 剩下的路只有去改库。
 *
 * 这里钉的是**真实界面**上的四件事：
 *   ① 候选年份铺满整个可用区间（1900 年到今年往后 5 年），顶头停在近年；
 *   ② 输一个年份能搜到它，**按回车就选上**（不写 default-first-option 的话，
 *      Element Plus 这个属性的默认值是 false，回车会按原值收场）；
 *   ③ 很早的年份（1998、1900）同样搜得到、选得上；
 *   ④ 选中的年份发给后端时是**数字**，且就是用户选的那个。
 *
 * 跑法：先起好 ./dev.sh，再从仓库根 `node tools/uitest/ui_start_year.mjs`。
 */
import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const errors = []

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
// 最后一步会主动 abort 掉创建请求（不想在开发库里留下删不掉的账套），
// 那条 ERR_FAILED 是我们自己造成的，不算页面的问题。
page.on('console', (m) => {
  if (m.type() === 'error' && !m.text().includes('ERR_FAILED')) errors.push('console: ' + m.text())
})
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))

async function step(name, fn) {
  try { await fn(); console.log('✓', name) }
  catch (e) {
    console.log('✗', name, '—', e.message.split('\n')[0])
    errors.push(name + ': ' + e.message.split('\n')[0])
  }
}

/** 新建账套弹窗里「起始年度」那一栏的下拉 */
const yearItem = page.locator('.el-dialog:visible .el-form-item')
  .filter({ has: page.locator('.el-form-item__label', { hasText: '起始年度' }) })
const yearSelect = yearItem.locator('.el-select').first()
const yearInput = yearSelect.locator('input').first()

/** 下拉里当前可见的选项文字 */
const optionTexts = () => page.evaluate(() =>
  [...document.querySelectorAll('.el-select-dropdown__item')]
    .filter(el => el.offsetParent !== null)
    .map(el => el.textContent.trim()))

/** 下拉框当前显示的值（Element Plus 把选中项渲染在 selected-item 里） */
const shownValue = () => page.evaluate(() => {
  // 注意：`:visible` 是 Playwright 的伪类，原生 querySelector 不认
  const dlg = [...document.querySelectorAll('.el-dialog')].find(e => e.offsetParent !== null)
  const sel = dlg?.querySelector('.el-select')
  if (!sel) return ''
  return [...sel.querySelectorAll('.el-select__selected-item')]
    .map(el => el.textContent.trim()).filter(Boolean).join('')
})

/** 输了年份、回车，看它有没有落到那个年份上 */
const pickByTyping = async (year) => {
  await yearInput.click()
  await page.waitForTimeout(350)
  await yearInput.fill('')
  await page.keyboard.type(String(year), { delay: 50 })
  await page.waitForTimeout(450)
  const visible = await optionTexts()
  await page.keyboard.press('Enter')
  await page.waitForTimeout(600)
  return visible
}

await step('登录并进账套管理', async () => {
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await page.fill('input[type=password]', process.env.CWQ_PASSWORD || '123456')
  await page.click('button:has-text("登录")')
  await page.waitForSelector('.sidebar', { timeout: 15000 })
  await page.goto(BASE + '/#/books', { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
})

await step('打开「新建账套」弹窗', async () => {
  await page.click('.create-book-button')
  await page.waitForSelector('.el-dialog:visible:has-text("新建账套")', { timeout: 8000 })
  await page.waitForTimeout(600)
})

await step('起始年度的候选铺满了整个区间', async () => {
  await yearInput.click()
  await page.waitForTimeout(600)
  const texts = await optionTexts()
  if (texts.length < 100) throw new Error(`候选只有 ${texts.length} 个，还是选不出想选的年份`)
  console.log(`   （候选 ${texts.length} 个：${texts[0]} … ${texts[texts.length - 1]}）`)
  for (const want of ['2026 年', '1998 年', '1900 年']) {
    if (!texts.includes(want)) throw new Error(`候选里没有「${want}」`)
  }
  await page.screenshot({ path: `${OUT}/start-year-dropdown.png` })
})

await step('输入 1998 回车就选上了', async () => {
  const visible = await pickByTyping(1998)
  if (visible.length !== 1 || !visible[0].includes('1998')) {
    throw new Error(`搜「1998」出来的不是它：${visible.join('/')}`)
  }
  const shown = await shownValue()
  if (!shown.includes('1998')) throw new Error(`没选中，显示的是「${shown}」`)
})

await step('输入 1900 回车也选得上', async () => {
  await pickByTyping(1900)
  const shown = await shownValue()
  if (!shown.includes('1900')) throw new Error(`没选中，显示的是「${shown}」`)
})

await step('点一个很早的年份也选得上', async () => {
  await yearInput.click()
  await page.waitForTimeout(400)
  await yearInput.fill('')
  await page.keyboard.type('1975', { delay: 50 })
  await page.waitForTimeout(450)
  await page.locator('.el-select-dropdown__item:visible', { hasText: '1975' }).first().click()
  await page.waitForTimeout(600)
  const shown = await shownValue()
  if (!shown.includes('1975')) throw new Error(`没选中，显示的是「${shown}」`)
})

await step('发给后端的 start_year 是选中的那个数字', async () => {
  // 拦下这个 POST 看一眼请求体，**不放行** —— 账套在产品上删不掉，
  // 验证脚本不该在开发库里留下一本删不掉的账。
  let payload = null
  await page.route('**/api/books', async (route) => {
    if (route.request().method() === 'POST') {
      payload = route.request().postDataJSON()
      await route.abort()
    } else {
      await route.continue()
    }
  })
  await page.fill('.el-dialog:visible .el-form-item:has(.el-form-item__label:text-is("公司名称")) input', '起始年度验证')
  await page.fill('.el-dialog:visible .el-form-item:has(.el-form-item__label:text-is("账套名称")) input', '起始年度验证')
  await page.click('.el-dialog:visible button:has-text("创建账套")')
  await page.waitForTimeout(1200)
  await page.unroute('**/api/books')
  if (!payload) throw new Error('没抓到创建账套的请求')
  if (payload.start_year !== 1975) throw new Error(`发出去的是 ${JSON.stringify(payload.start_year)}，不是 1975`)
  if (typeof payload.start_year !== 'number') throw new Error('start_year 不是数字')
})

await page.screenshot({ path: `${OUT}/start-year-picker.png` })
console.log('\n' + (errors.length ? '问题:\n' + errors.map(e => '  - ' + e).join('\n') : '无 JS 错误'))
await browser.close()
process.exit(errors.length ? 1 : 0)
