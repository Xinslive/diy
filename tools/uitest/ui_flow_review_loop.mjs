/**
 * 流水对账页「**判「不是同一笔」判不完**」那个死循环的界面自查
 * （2026-10-30 用户在生产上报的）。
 *
 * **它验的是三件事**（单元测与后端用例都验不了「界面上那一下」）：
 *
 * 1. **判过的凭证不再冒出来** —— 不许回到原来那一行，也不许挪到旁边
 *    那一行（用户原话：「我点了这边的『不是同一笔』，那边的又冒出来」）；
 * 2. **点下去要真的停下来** —— 连点若干次之后不再有「待复核」，
 *    两行都回到「可制证」（判了等于没判是最气人的那种 bug）；
 * 3. **展开的那一块要写清是哪一行** —— 展开行与没展开的行之间要分得开，
 *    否则用户会把上一行的东西看成了下一行的。
 *
 * 用法：node tools/uitest/ui_flow_review_loop.mjs
 * （前置：./dev.sh 起着，且跑过 tools/uitest/setup_review_loop_case.py --seed）
 */
import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const STATEMENT = 'uitest-判否死循环'

const errors = []
const ok = (n) => console.log('✓', n)
const bad = (n, d) => { console.log('✗', n, '—', d); errors.push(`${n}: ${d}`) }
async function step(name, fn) {
  try { await fn(); ok(name) } catch (e) { bad(name, e.message.split('\n')[0]) }
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1539, height: 954 } })
page.on('pageerror', (e) => bad('JS error', e.message))
page.on('console', (m) => { if (m.type() === 'error') bad('console error', m.text().slice(0, 160)) })
const sleep = (ms) => page.waitForTimeout(ms)

// **别用 networkidle** —— vite 的 HMR websocket 一直挂着，
// 「网络静默」这件事在开发服务器上永远不成立，于是每次都在 goto 上
// 卡满 30 秒。改成「DOM 出来了」+ 真的等到界面上那件东西出现。
await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 })
await page.waitForSelector('#app', { timeout: 30000 })
// **等「登录表单」或「已经进账套了」二者之一出现**，再决定要不要登录。
// domcontentloaded 只保证 DOM 出来了，Vue 还一个字都没渲染 —— 那时去数
// input[type=password] 永远是 0，于是「已经登录过」和「还没渲染」被
// 混成一件事，整段登录被静默跳过，后面每一步都在登录页上空转。
await Promise.race([
  page.waitForSelector('input[type=password]', { timeout: 30000 }).catch(() => {}),
  page.waitForSelector('.modules button', { timeout: 30000 }).catch(() => {}),
])
if (await page.locator('input[type=password]').count()) {
  await page.locator('input').nth(0).fill('admin')
  await page.locator('input').nth(1).fill('123456')
  await page.click('button:has-text("登录")')
  await page.waitForURL(/#\/(books|login)/, { timeout: 20000 })
  await page.waitForSelector('button:has-text("进入账套")', { timeout: 30000 })
  await sleep(800)
  const enter = page.locator('button:has-text("进入账套")')
  if (await enter.count()) { await enter.first().click(); await sleep(1500) }
}

await step('打开流水对账页', async () => {
  await page.waitForSelector('.modules button', { timeout: 60000 })
  await page.locator('.modules button:has-text("凭证处理")').first().click()
  await page.waitForSelector('.sidebar li:has-text("流水对账")', { timeout: 30000 })
  await page.locator('.sidebar li').filter({ hasText: '流水对账' }).first().click()
  await sleep(2000)
})

await step('切到有对账单的期间', async () => {
  // 顶栏默认停在**最早的未结账月份**，而对账单在 12 月 —— 对不上就看不见。
  const period = page.locator('.el-select').first()
  await period.click()
  await sleep(700)
  const opt = page.locator('.el-select-dropdown__item:visible').filter({ hasText: '12 月' }).first()
  if (await opt.count()) { await opt.click(); await sleep(1200) }
})

await step('选中那份对账单', async () => {
  await page.click('.el-tabs__item:has-text("流水明细")')
  await sleep(900)
  await page.locator('.flow-select').click()
  await sleep(700)
  const option = page.locator('.el-select-dropdown__item:visible',
    { hasText: STATEMENT }).first()
  if (!(await option.count())) throw new Error(`下拉里没有「${STATEMENT}」那份`)
  await option.click()
  await sleep(2000)
})

/** 数据行（不是展开行）。 */
function dataRows() {
  return page.locator('.flow-rows-table tbody tr.el-table__row')
}

/** 展开内容在**紧跟数据行之后的另一个 <tr> 里**。 */
async function expandedRow(row) {
  const expanded = row.locator('xpath=following-sibling::tr[1]')
  if (await expanded.locator('.flow-extra').count() === 1) return expanded
  const arrow = row.locator('.el-table__expand-icon').first()
  if (!(await arrow.count())) throw new Error('这一行没有展开箭头')
  await arrow.click()
  await sleep(1200)
  if (await expanded.locator('.flow-extra').count() !== 1) {
    throw new Error('展开内容不在紧跟其后的那一行里')
  }
  return expanded
}

/** 界面上所有待复核的行 —— 每一行它对着哪张凭证（凭证号）。 */
async function pending() {
  const out = []
  const rows = dataRows()
  const count = await rows.count()
  for (let i = 0; i < count; i += 1) {
    const row = rows.nth(i)
    const panel = row.locator('xpath=following-sibling::tr[1]').locator('.flow-review')
    if (!(await panel.count())) continue
    const text = (await panel.innerText()).trim()
    const m = text.match(/记\d+/)
    out.push({ index: i, voucher: m ? m[0] : text.slice(0, 40) })
  }
  return out
}

await step('两行都在「待复核」', async () => {
  const p = await pending()
  if (p.length !== 2) throw new Error(`待复核的行有 ${p.length} 行（要 2 行）`)
  console.log('   待复核：', JSON.stringify(p))
})

await step('展开的那一块写清是哪一行', async () => {
  const row = dataRows().first()
  const extra = await expandedRow(row)
  const head = extra.locator('.flow-extra-head')
  if (!(await head.count())) throw new Error('展开块里没有「这一行是谁」的那一行')
  const text = (await head.innerText()).trim()
  if (!text.includes('#')) throw new Error(`身份行里没有序号：${text}`)
  if (!/\d{4}-\d{2}-\d{2}/.test(text)) throw new Error(`身份行里没有日期：${text}`)
  console.log('   身份行：', text.replace(/\s+/g, ' '))
})

await page.screenshot({ path: `${OUT}/flow-review-loop-before.png`, fullPage: false })
console.log('   截图：', `${OUT}/flow-review-loop-before.png`)

/**
 * 点一次「不是同一笔」（含二次确认），**返回这次判掉的是哪一张凭证**。
 *
 * **必须返回「判掉的那一张」，不能把界面上所有待复核的都算成判过了**：
 * 判掉一张之后，同一行会接着问下一张（那是对的 —— 一行可以问好几张），
 * 把没判过的那几张也算进「已判」集合，下一轮就会误报「又冒出来了」。
 */
async function dismissOne() {
  const rows = dataRows()
  const count = await rows.count()
  for (let i = 0; i < count; i += 1) {
    const row = rows.nth(i)
    const expanded = row.locator('xpath=following-sibling::tr[1]')
    if (!(await expanded.locator('.flow-review').count())) continue
    if (!(await expanded.locator('.flow-extra').count())) {
      await expandedRow(row)
    }
    const panel = expanded.locator('.flow-review')
    const text = (await panel.innerText()).trim()
    const m = text.match(/记\d+/)
    const voucher = m ? m[0] : null
    const btn = expanded.getByRole('button', { name: '不是同一笔', exact: true })
    if (!(await btn.count())) continue
    await btn.click()
    await sleep(800)
    const dialog = page.locator('.el-message-box').first()
    if (await dialog.count()) {
      await dialog.locator('.el-message-box__btns button.el-button--primary').click()
    }
    await sleep(2200)
    return voucher
  }
  return null
}

const judged = new Set()
await step('连点「不是同一笔」直到停下来（不许死循环）', async () => {
  for (let round = 0; round < 12; round += 1) {
    const before = await pending()
    if (!before.length) break
    // **每一轮开始前先记下界面上在问哪几张** —— 判过的不许再问。
    for (const p of before) {
      if (judged.has(p.voucher)) {
        throw new Error(`判过的「${p.voucher}」又冒出来了（第 ${p.index + 1} 行）`)
      }
    }
    const dismissed = await dismissOne()
    if (!dismissed) break
    // **只记这一张** —— 判掉它之后，同一行接着问下一张是正常的。
    judged.add(dismissed)
  }
  const left = await pending()
  if (left.length) {
    throw new Error(`点了 12 次还没停下来，还剩 ${left.length} 行待复核：${JSON.stringify(left)}`)
  }
  console.log(`   判掉了 ${judged.size} 张：`, [...judged].join('、'))
})

await step('判完之后两行都回到「可制证」', async () => {
  const rows = dataRows()
  const count = await rows.count()
  if (count < 2) throw new Error(`流水行不见了（只剩 ${count} 行）`)
  for (let i = 0; i < count; i += 1) {
    const row = rows.nth(i)
    const extra = await expandedRow(row)
    const text = (await extra.innerText()).trim()
    if (text.includes('待复核')) throw new Error(`第 ${i + 1} 行还写着「待复核」：${text}`)
  }
})

await page.screenshot({ path: `${OUT}/flow-review-loop-after.png`, fullPage: false })
console.log('   截图：', `${OUT}/flow-review-loop-after.png`)

await browser.close()
if (errors.length) {
  console.log(`\n✗ ${errors.length} 处不对：`)
  for (const e of errors) console.log('  -', e)
  process.exit(1)
}
console.log('\n✓ 全部通过')
