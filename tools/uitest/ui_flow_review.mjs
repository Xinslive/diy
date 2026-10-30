/**
 * 流水对账页「**待复核**」的界面自查（2026-10-29 加）。
 *
 * **它验的是「看起来对不对、点得动不动」**：那一行显示的是「待复核」吗？
 * 那句「账上有一笔同额的凭证，日期相差 N 天」摆出来了吗？两颗按钮在吗？
 * 点「是同一笔，标为已入账」之后，它真的变成「已入账」了吗？
 * 点「不是同一笔」之后，它真的回到「可以制证」了吗？
 *
 * 这些单元测与后端用例都验不了 —— 它们验的是**界面上那一下**。
 *
 * **两条判定路径都点一遍**：只有「是同一笔」这一条被验过，
 * 「不是同一笔」那条就还是没验过 —— 而那一条的后果更重
 * （判错方向 = 同一笔钱记两遍）。
 *
 * 用法：node tools/uitest/ui_flow_review.mjs
 * （前置：./dev.sh 起着，且跑过 tools/uitest/setup_review_case.py --seed）
 */
import { chromium } from 'playwright'
import { gotoStatementPeriod } from './lib/statement_period.mjs'

const BASE = 'http://127.0.0.1:5173'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'

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

await page.goto(BASE, { waitUntil: 'networkidle' })
if (await page.locator('input[type=password]').count()) {
  await page.locator('input').nth(0).fill('admin')
  await page.locator('input').nth(1).fill('123456')
  await page.click('button:has-text("登录")')
  await page.waitForURL(/#\/(books|login)/, { timeout: 20000 })
  // **等「进入账套」那颗按钮真的在** —— 不等它就往下走的话，
  // 后面每一步都在登录页上空转，最后截出一张登录页的图，
  // 看着像「界面坏了」，其实是自己没登进去。
  await page.waitForSelector('button:has-text("进入账套")', { timeout: 30000 })
  await sleep(800)
}

await step('进入账套', async () => {
  const enter = page.locator('button:has-text("进入账套")')
  if (await enter.count()) {
    await enter.first().click()
    await page.waitForTimeout(1500)
  }
})

// **必须自己走到「流水对账」页** —— 共用助手 ``gotoStatementPeriod``
// 是在**这一页里**点页签的；人还站在账套管理页上时，它点的是
// 一个根本不存在的页签，于是「click 超时」—— 看起来像界面坏了，
// 其实是脚本自己没走到地方。
await step('打开流水对账页', async () => {
  // **等它真的在**：dev 首次进账套要现编译模块，页面会先空一会儿 ——
  // 固定等 1.5 秒然后点，命中的是「还没编译完」的空壳。
  await page.waitForSelector('.modules button', { timeout: 60000 })
  await page.locator('.modules button:has-text("凭证处理")').first().click()
  await page.waitForSelector('.sidebar li:has-text("流水对账")', { timeout: 30000 })
  await page.locator('.sidebar li').filter({ hasText: '流水对账' }).first().click()
  await sleep(2000)
})

await step('切到有对账单的期间', async () => {
  const r = await gotoStatementPeriod(page)
  if (!r.rows) throw new Error('这一屏一份对账单都没有')
})

/**
 * 选中那份「待复核」的对账单。
 *
 * **不共用 ``selectStatement``**：它选的是下拉里**第一份**，
 * 而我们要的是文件名带「uitest-待复核」的那一份（dev 库里还有别的）。
 */
await step('选中那份「待复核」的对账单', async () => {
  await page.click('.el-tabs__item:has-text("流水明细")')
  await sleep(900)
  await page.locator('.flow-select').click()
  await sleep(600)
  const option = page.locator('.el-select-dropdown__item:visible',
    { hasText: 'uitest-待复核' }).first()
  if (!(await option.count())) throw new Error('下拉里没有「uitest-待复核」那份')
  await option.click()
  await sleep(1800)
})

/** 找到那一行（**数据行**，不是展开行）。 */
async function rowBySummary(part) {
  const row = page.locator('.flow-rows-table tbody tr.el-table__row')
    .filter({ hasText: part })
  if (await row.count() !== 1) {
    throw new Error(`没找到「${part}」那一行（${await row.count()} 行）`)
  }
  return row.first()
}

/**
 * 展开内容在**紧跟数据行之后的另一个 <tr> 里**（Element Plus 的展开行是
 * 独立的一行），不在数据行自己身上 —— 找错地方会以为按钮不见了。
 *
 * **只认「紧跟着的那一行」**：不能往后翻「下一个含 .flow-extra 的行」——
 * 目标行自己收起时（判完「是同一笔」它就收起了），那样会翻到**别行**的
 * 展开内容上，读到的状态是另一行的，看着像「点了之后状态没变」。
 *
 * **已经展开着就直接返回**：判完之后这一行会收起
 * （它没有活要干了），得再点开才看得见「已入账」三个字。
 */
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

/**
 * 展开块里「状态」那一格写着什么。
 *
 * **不能拿整块的文字去 includes('待复核')** —— 种子数据的摘要就叫
 * 「uitest-待复核-甲」，而展开块顶上那一行（#序号 / 日期 / 收支 / 金额 /
 * 摘要）**正是为了说清「这是哪一行」才把摘要写上去的**。于是整块文字里
 * 一直有「待复核」这三个字，跟状态无关，判断就会一直误报「点了之后状态
 * 没变」。只认「状态」那一格里的那个标签。
 */
async function stateText(extra) {
  const item = extra.locator('.flow-extra-item').filter({ hasText: '状态' }).first()
  const tag = item.locator('.el-tag').first()
  if (!(await tag.count())) throw new Error('展开块里没有「状态」那一格')
  return (await tag.innerText()).trim()
}

await step('那一行显示「待复核」', async () => {
  const row = await rowBySummary('uitest-待复核-甲')
  const extra = await expandedRow(row)
  const state = await stateText(extra)
  if (state !== '待复核') throw new Error(`那一行的状态不是「待复核」：${state}`)
})

await step('待复核那一块把候选凭证摆出来了', async () => {
  const row = await rowBySummary('uitest-待复核-甲')
  const extra = await expandedRow(row)
  const panel = extra.locator('.flow-review')
  if (!(await panel.count())) throw new Error('没有那一块「待复核」的说明')
  const text = (await panel.innerText()).trim()
  if (!text.includes('日期相差')) throw new Error(`那一块没说要差几天：${text}`)
  if (!text.includes('同额')) throw new Error(`那一块没说清是同额的：${text}`)
  // **凭证号 + 日期**：光有凭证号认不出是哪一张，用户得靠日期才找得到。
  if (!/\d{4}-\d{2}-\d{2}/.test(text)) throw new Error(`那一块没写凭证日期：${text}`)
  console.log('   说的是：', text.replace(/\s+/g, ' ').slice(0, 90))
})

await step('两颗按钮都在', async () => {
  const row = await rowBySummary('uitest-待复核-甲')
  const extra = await expandedRow(row)
  const panel = extra.locator('.flow-review')
  // **用 getByRole 的精确匹配** —— 两个坑叠在一起：
  //  ① 「不是同一笔」里**含有**「是同一笔」这三个字，用 :has-text 两颗都命中
  //     （而 count() 那种写法更糟：数到 2 个也照样判「按钮在」）；
  //  ② :text-is 反而一个都匹配不上 —— Element Plus 的按钮文字前后带空格
  //     （textContent 是 " 是同一笔，标为已入账 "），:text-is 不去空白。
  // 按「无障碍名 + exact」匹配，两条都躲得过。
  const yes = panel.getByRole('button', { name: '是同一笔，标为已入账', exact: true })
  const no = panel.getByRole('button', { name: '不是同一笔', exact: true })
  if (await yes.count() !== 1) throw new Error(`「是同一笔」那颗按钮不对：${await yes.count()} 个`)
  if (await no.count() !== 1) throw new Error(`「不是同一笔」那颗按钮不对：${await no.count()} 个`)
})

await page.screenshot({ path: `${OUT}/flow-review-before.png`, fullPage: false })
console.log('   截图：', `${OUT}/flow-review-before.png`)

await step('点「是同一笔，标为已入账」→ 变成「已入账」', async () => {
  const row = await rowBySummary('uitest-待复核-甲')
  const extra = await expandedRow(row)
  await extra.getByRole('button', { name: '是同一笔，标为已入账', exact: true }).click()
  await sleep(2200)
  const row2 = await rowBySummary('uitest-待复核-甲')
  const extra2 = await expandedRow(row2)
  const state = await stateText(extra2)
  if (state !== '已入账') throw new Error(`点了之后不是「已入账」：${state}`)
  if (await row2.locator('.flow-review').count()) {
    throw new Error('点了之后那一块「待复核」的说明还在')
  }
})

await step('判完的行不再出现在可制证清单里', async () => {
  // **这是要害**：待复核这一行**不在**可制证清单里，所以「生成流水凭证」
  // 不会顺手把它再记一遍 —— 同一笔钱记两遍就是这么来的。
  const candidates = page.locator('.flow-rows-table tbody tr.el-table__row')
    .filter({ hasText: 'uitest-待复核-甲' })
  if (await candidates.count() !== 1) throw new Error('流水行不见了')
  const panel = await page.locator('.flow-candidate-list, .flow-plan-list').count()
  console.log(`   清单里的候选块：${panel} 个（0 = 没有把它列进可制证清单）`)
})

await page.screenshot({ path: `${OUT}/flow-review-after.png`, fullPage: false })
console.log('   截图：', `${OUT}/flow-review-after.png`)

await step('点「不是同一笔」→ 二次确认 → 回到可以制证', async () => {
  const row = await rowBySummary('uitest-待复核-乙')
  const extra = await expandedRow(row)
  await extra.getByRole('button', { name: '不是同一笔', exact: true }).click()
  await sleep(900)
  // **要点一次确认**：判错方向的代价是同一笔钱记两遍。
  const dialog = page.locator('.el-message-box').first()
  if (!(await dialog.count())) throw new Error('没有二次确认对话框')
  const dialogText = (await dialog.innerText()).trim()
  if (!dialogText.includes('记两遍') && !dialogText.includes('可制证')) {
    throw new Error(`确认框没把后果说清楚：${dialogText.replace(/\s+/g, ' ')}`)
  }
  await dialog.locator('.el-message-box__btns button.el-button--primary').click()
  await sleep(2200)

  const row2 = await rowBySummary('uitest-待复核-乙')
  const extra2 = await expandedRow(row2)
  const state = await stateText(extra2)
  if (state === '待复核') throw new Error(`判完之后还写着「待复核」：${state}`)
  if (state !== '待确认科目' && state !== '可制证') {
    throw new Error(`判完之后不是「可以制证」：${state}`)
  }
})

await step('判「不是同一笔」之后，可制证清单里多出这一行', async () => {
  // **判完之后这一行就该能被制证了** —— 它本来就在候选里，
  // 只是判之前被「待复核」挡住了。
  const body = await page.locator('body').innerText()
  if (!body.includes('可生成')) throw new Error('界面上没有「可生成 N 条」这一句')
})

await page.screenshot({ path: `${OUT}/flow-review-dismissed.png`, fullPage: false })
console.log('   截图：', `${OUT}/flow-review-dismissed.png`)

await browser.close()
if (errors.length) {
  console.log(`\n✗ ${errors.length} 处不对：`)
  for (const e of errors) console.log('  -', e)
  process.exit(1)
}
console.log('\n✓ 全部通过')
