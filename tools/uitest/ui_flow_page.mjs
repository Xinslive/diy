/**
 * 流水对账页的**界面自查**：上传一份夹具对账单，把「流水明细」页的两种
 * 形态都截下来 —— 摊开「对账信息」的样子、收起来的样子、对方科目
 * 编辑弹窗、改科目弹窗。
 *
 * **它验的是「看起来对不对」**：列的疏密、默认摊开的是不是「还有活要干的
 * 行」、弹窗里能不能把一笔流水拆到两个科目上 —— 这些单元测与后端用例都
 * 验不了。
 *
 * 用法：node tools/uitest/ui_flow_page.mjs
 */
import { chromium } from 'playwright'
import { gotoStatementPeriod, selectStatement } from './lib/statement_period.mjs'

const BASE = 'http://127.0.0.1:5173'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const FIXTURE = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/backend/tests/fixtures/statements/cmb_csv.xlsx'

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
  // **等路由而不是等某个 CSS 类**：登录成功后落在 #/books，
  // 而 .sidebar 要等布局渲染出来才在（早半秒等它就超时）。
  await page.waitForURL(/#\/(books|login)/, { timeout: 20000 })
  await sleep(1500)
}

// ---- 先进入账套，再进流水对账页 ----
await step('进入账套', async () => {
  const enter = page.locator('button:has-text("进入账套")')
  if (await enter.count()) {
    await enter.first().click()
    await page.waitForTimeout(1500)
  }
})

await step('打开流水对账页', async () => {
  // 点模块按钮而不是点 .module 容器 —— 容器不可点，click 会一直等到超时
  await page.locator('.modules button:has-text("凭证处理")').first().click()
  await page.waitForTimeout(900)
  await page.locator('.sidebar li').filter({ hasText: '流水对账' }).first().click()
  await page.waitForTimeout(1500)
})

// ---- 顶栏的期间要先对上对账单所属的期间 ----
//
// **2026-10-28 起台账按顶栏的会计期间过滤**（用户批注「根据这里选中的期间，
// 来显示对账单」）。顶栏默认停在「最靠前的一个未结账月份」（dev 库里是 12 月），
// 而对账单在 9 月 —— 不先切过去，下拉是空的、后面每一步都超时。
await step('台账跟着顶栏的期间走：别的期间的对账单能一键跳过去', async () => {
  const r = await gotoStatementPeriod(page)
  if (r.mode === 'none') {
    console.log('   这本账还没有对账单 —— 下面走上传')
    return
  }
  if (r.mode === 'switched') {
    console.log(`   空态写着「${r.emptyText.replace(/\n/g, ' | ')}」`)
    console.log(`   点了「${r.label}」→ 顶栏切过去了，列表里有 ${r.rows} 份`)
  } else {
    console.log('   顶栏已经停在有对账单的期间上')
  }
})

// ---- 上传一份夹具对账单（已经有了就跳过：同账户同期间只允许一份） ----
await step('准备一份对账单', async () => {
  const hasRows = await page.locator('.el-table:visible tr.el-table__row').count()
  if (hasRows > 0) { ok('已有一份对账单，跳过上传'); return }
  const subjectSelect = page.locator('.flow-upload-form .el-select').first()
  await subjectSelect.click()
  await page.waitForTimeout(400)
  const option = page.locator('.el-select-dropdown__item:visible').first()
  await option.click()
  await page.waitForTimeout(300)
  await page.setInputFiles('input[type=file]', FIXTURE)
  await page.click('button:has-text("上传并解析")')
  await page.waitForTimeout(2500)
})

// ---- 流水明细页：默认形态 ----
await step('切到流水明细并选中这份对账单', async () => {
  await selectStatement(page)
})
await page.screenshot({ path: `${OUT}/flow_compact_default.png` })

await step('「方向/对方科目/状态」不是列（它们在展开行里）', async () => {
  // **默认摊开的是「还有活要干的行」**（2026-10-28 改）—— 摊开的是**展开
  // 行里的内容**，那三样**仍然不是表格列**：列一多表格就被撑宽，窄屏上
  // 只能横着拖着看。
  const headers = await page.locator('.flow-rows-table thead th').allInnerTexts()
  const text = headers.join('|')
  for (const label of ['方向', '对方科目', '状态']) {
    if (text.includes(label)) throw new Error(`「${label}」不该是列：${text}`)
  }
  for (const label of ['日期', '摘要', '用途', '对方户名', '收入', '支出', '余额']) {
    if (!text.includes(label)) throw new Error(`默认列少了「${label}」：${text}`)
  }
})

await step('明细行确实加载出来了', async () => {
  const rows = await page.locator('.flow-rows-table tbody tr').count()
  if (rows < 3) throw new Error(`明细行数不对：${rows}`)
  const height = await page.locator('.flow-rows-table tbody tr').first().evaluate(
    (el) => el.getBoundingClientRect().height)
  console.log(`   行高 ${height}px，共 ${rows} 行`)
})

/**
 * **没有横向滚动条** —— 这是用户提的要求：早先那版把方向/对方科目/状态做成
 * **往右加的列**，列一多表格就被撑宽，窄屏上只能横着拖着看。
 *
 * 判据不是「看起来没溢出」，而是**真的量一遍**：滚动容器的 scrollWidth
 * 超过 clientWidth 就说明有横向溢出。
 */
const measureOverflow = () => page.evaluate(() => {
  const wrap = document.querySelector('.flow-rows-table .el-scrollbar__wrap')
    || document.querySelector('.flow-rows-table .el-table__body-wrapper')
  if (!wrap) return null
  return { scrollWidth: wrap.scrollWidth, clientWidth: wrap.clientWidth }
})

await step('默认形态没有横向滚动条', async () => {
  const m = await measureOverflow()
  if (m && m.scrollWidth > m.clientWidth + 1) {
    throw new Error(`表格横向溢出了（要出左右滑动条了）：${JSON.stringify(m)}`)
  }
  if (m) console.log(`   内容宽 ${m.scrollWidth}px / 可视宽 ${m.clientWidth}px`)
})

// ---- 展开「对账信息」：**往下展开**，不是往右加列 ----
await step('行首一直有展开箭头（没有「对账信息」开关）', async () => {
  // 曾经过去是「勾上『对账信息』才出现箭头」—— 那样「不看对账信息的人」与
  // 「要改对账信息的人」看到的是**两张不同的表**。
  // 用户原话：「默认收起就行，去掉勾选对账信息的开关」。
  if (await page.locator('.flow-toggle').count()) {
    throw new Error('还有「对账信息」勾选开关')
  }
  const icons = await page.locator('.flow-rows-table .el-table__expand-icon').count()
  if (icons < 3) throw new Error(`行首没有展开箭头（${icons} 个）`)
})

await step('点行首箭头，内容在**行的下方**展开', async () => {
  // **默认摊开的是「还有活要干的行」**（2026-10-28 改）—— 所以第一行很可能
  // **本来就是展开的**。这时再点箭头是**收起**，脚本不能假设「点了就是展开」。
  // 展开内容在**紧跟数据行之后的另一个 <tr> 里**（Element Plus 的展开行是
  // 独立的一行），**不在数据行自己身上**。
  const row = page.locator('.flow-rows-table tbody tr.el-table__row').first()
  const icon = row.locator('.el-table__expand-icon').first()
  const wasExpanded = (await icon.getAttribute('aria-expanded')) === 'true'
  if (wasExpanded) {
    // 先收起，顺手验一下「收」这条路也通
    await icon.click()
    await page.waitForTimeout(600)
    const gone = row.locator('xpath=following-sibling::tr[1]')
    if (await gone.locator('.flow-extra').count() !== 0) {
      throw new Error('点了箭头没收起来')
    }
  }
  await icon.click()
  await page.waitForTimeout(700)
  const expanded = row.locator('xpath=following-sibling::tr[1]')
  if (await expanded.locator('.flow-extra').count() !== 1) {
    throw new Error('展开内容不在紧跟其后的那一行里')
  }
  const text = await expanded.locator('.flow-extra').innerText()
  for (const label of ['方向', '对方科目', '状态']) {
    if (!text.includes(label)) throw new Error(`展开行里少了「${label}」：${text}`)
  }
})
await page.screenshot({ path: `${OUT}/flow_compact_expanded.png` })

await step('**展开后也没有横向滚动条**', async () => {
  const m = await measureOverflow()
  if (m && m.scrollWidth > m.clientWidth + 1) {
    throw new Error(`展开后表格横向溢出了：${JSON.stringify(m)}`)
  }
  if (m) console.log(`   内容宽 ${m.scrollWidth}px / 可视宽 ${m.clientWidth}px`)
})

// ---- 对方科目弹窗（从**展开行里**点） ----
await step('展开行里能打开对方科目弹窗', async () => {
  // **默认摊开的可能不止一行**（2026-10-28 改：还有活要干的行都摊开），
  // 所以不能直接拿「第一个 .flow-extra」—— 那一行可能是已入账的，
  // 它的「指定/调整」按钮是**灰的**，点了没反应。
  // 找**第一个按钮点得动**的展开行。
  const extras = page.locator('.flow-rows-table .flow-extra')
  const n = await extras.count()
  let clicked = false
  for (let i = 0; i < n; i++) {
    const btn = extras.nth(i).locator(
      'button:has-text("指定"), button:has-text("调整")').first()
    if (!(await btn.count())) continue
    if (await btn.isDisabled()) continue
    await btn.click()
    clicked = true
    break
  }
  if (!clicked) throw new Error('没有一行能打开对方科目弹窗')
  await page.waitForTimeout(700)
  await page.screenshot({ path: `${OUT}/flow_plan_dialog.png` })
  const rows = await page.locator('.flow-plan-row').count()
  if (rows < 1) throw new Error('弹窗里没有科目行')
})

await step('能再加一个科目（拆成两个）', async () => {
  await page.click('button:has-text("再加一个科目")')
  await page.waitForTimeout(400)
  const rows = await page.locator('.flow-plan-row').count()
  if (rows < 2) throw new Error(`加完之后只有 ${rows} 行`)
  await page.screenshot({ path: `${OUT}/flow_plan_dialog_two.png` })
})

// ---- 科目规则页：摘要模板的「占位符 / 示例 / 预览」 ----
//
// 用户说过这个功能「有点看不懂」—— 所以界面上必须三样齐全：
// 占位符能点、示例能套、预览能看到真东西长什么样。
await step('切到科目规则页', async () => {
  await page.keyboard.press('Escape')
  await page.waitForTimeout(500)
  // **页签叫「科目规则」，不是「流水科目规则」** —— 前面那个「流水」是废话，
  // 页签叫「流水对账」已经交代了上下文了。（脚本按老名字点会一直超时。）
  await page.click('.el-tabs__item:has-text("科目规则")')
  await page.waitForTimeout(900)
})

// **规则级模板**（2026-10-28：账套级模板已下线，那张卡片整个没了，
// 脚本不能再找它 —— 找不到就 fill 超时，后面几步全崩）。
const ruleTemplateInput = () =>
  page.locator('.el-form-item:has-text("摘要模板") input').first()

await step('页面上没有账套级模板卡片了（2026-10-28 下线）', async () => {
  // 钉住这次下线：账套级摘要模板一张卡片都不许再有。
  const accountLevel = await page.locator('.flow-config-card').count()
  if (accountLevel > 0) throw new Error(`还有 ${accountLevel} 张账套级模板卡片`)
  const body = await page.locator('body').innerText()
  if (body.includes('账套级')) throw new Error('界面上还有「账套级」的字样')
})

await step('规则级模板：点一下占位符就插进去', async () => {
  const input = ruleTemplateInput()
  await input.fill('')
  await page.locator('.flow-placeholder-tag').filter({ hasText: '{用途}' }).first().click()
  await page.waitForTimeout(400)
  const v = await input.inputValue()
  if (!v.includes('{用途}')) throw new Error(`点占位符没插进去：${v}`)
})

await step('预览跟着模板实时变', async () => {
  const preview = page.locator('.flow-template-preview-text').first()
  const text = (await preview.innerText()).trim()
  if (!text) throw new Error('预览是空的')
  console.log(`   预览：${text}`)
  await page.screenshot({ path: `${OUT}/flow_template_preview.png` })
})

await step('套用示例模板，预览也变', async () => {
  const input = ruleTemplateInput()
  await input.fill('')
  // 挑一条**三个字段都有值**的示例 —— dev 库里第一行流水的「用途」与
  // 「对方户名」都是空的，套「用途 + 对方」会渲染成「-」（那是对的，
  // 但验不出东西）。「日期 + 摘要 + 金额」这三个字段都有值。
  await page.locator('.flow-placeholder-tag')
    .filter({ hasText: '日期 + 摘要 + 金额' }).first().click()
  await page.waitForTimeout(500)
  const v = await input.inputValue()
  if (!v.includes('{日期}') || !v.includes('{摘要}') || !v.includes('{金额}')) {
    throw new Error(`示例模板没套进去：${v}`)
  }
  const text = (await page.locator('.flow-template-preview-text').first().innerText()).trim()
  // 渲染出来必须**有实际内容**（不是空、也不是只剩分隔符）
  if (!text || !/[0-9A-Za-z一-龥]/.test(text)) {
    throw new Error(`预览没渲染出内容：${text}`)
  }
  console.log(`   预览：${text}`)
})

// ---- 规则表格里**不摆输入框**，改走「编辑」（2026-10-28 用户批注） ----
//
// 用户原话：「又不能改，不要用输入框，不然还以为能在这里输入内容呢」。
// 那个行内输入框绑的是 @change —— **失焦就写库**，写没写成功只有一条
// toast 才知道；它看起来能改、其实是个暗改入口。
await step('规则表格里的摘要模板是纯文本，改走「编辑」', async () => {
  const KEYWORD = '__uitest_rule__'
  const TPL = '{摘要}-{金额}'
  await page.click('.el-tabs__item:has-text("科目规则")')
  await page.waitForTimeout(1200)
  const form = page.locator('.flow-rule-form')

  // ---- 0) 先清掉上一轮可能留下的同名规则 ----
  //
  // **上一轮失败时可能没跑到清理** —— 那条同名规则还在，新建就会撞
  // 「关键词重复」（400），于是脚本看的是**残留那条**（还带着上一轮
  // 塞进去的模板），验的就不是这一轮做出来的东西了。
  const stale = page.locator('.el-table:visible tr.el-table__row').filter({ hasText: KEYWORD })
  if (await stale.count()) {
    await stale.locator('button:has-text("删除")').click()
    await page.waitForTimeout(1800)
    if (await page.locator('.el-table:visible tr.el-table__row').filter({ hasText: KEYWORD }).count()) {
      throw new Error('上一轮留下的同名规则删不掉')
    }
  }

  // ---- 1) 新增一条（模板留空） ----
  await form.locator('.el-form-item:has-text("关键词") input').fill(KEYWORD)
  await form.locator('.el-form-item:has-text("对方科目") input').click()
  await page.waitForTimeout(600)
  await page.locator('.el-select-dropdown__item:visible').first().click()
  await page.waitForTimeout(300)
  await form.locator('.el-form-item:has-text("摘要模板") input').fill('')
  await form.locator('button:has-text("新增规则")').click()
  await page.waitForTimeout(1800)

  const row = page.locator('.el-table:visible tr.el-table__row').filter({ hasText: KEYWORD })
  if (await row.count() !== 1) throw new Error(`规则表里没找到刚建的那条（${await row.count()} 行）`)

  // ---- 2) 摘要模板那一格不许有输入框；没设模板就写「默认规则」 ----
  const cell = row.locator('td:nth-child(4)')
  if (await cell.locator('input').count()) throw new Error('摘要模板那一格还有输入框')
  const cellText = (await cell.innerText()).trim()
  if (cellText !== '默认规则') throw new Error(`空模板该显示「默认规则」，实际「${cellText}」`)

  // ---- 3) 点「编辑」→ 表单被填上、按钮变成「保存修改」 ----
  await row.locator('button:has-text("编辑")').click()
  await page.waitForTimeout(900)
  const kw = await form.locator('.el-form-item:has-text("关键词") input').inputValue()
  if (kw !== KEYWORD) throw new Error(`点「编辑」后表单没被填上：${kw}`)
  if (!(await form.locator('button:has-text("保存修改")').count())) {
    throw new Error('按钮没变成「保存修改」')
  }
  if (!(await page.locator('.flow-rule-editing').count())) {
    throw new Error('没有「正在修改哪一条」的提示')
  }

  // ---- 4) 在编辑态里给个模板 → 保存 → 那一格显示模板本身 ----
  await form.locator('.el-form-item:has-text("摘要模板") input').fill(TPL)
  await form.locator('button:has-text("保存修改")').click()
  await page.waitForTimeout(2200)
  const row2 = page.locator('.el-table:visible tr.el-table__row').filter({ hasText: KEYWORD })
  if (await row2.count() !== 1) throw new Error('保存之后规则表里找不到那条了')
  const cell2 = row2.locator('td:nth-child(4)')
  if (await cell2.locator('input').count()) throw new Error('保存后又冒出输入框了')
  const text2 = (await cell2.innerText()).trim()
  if (text2 !== TPL) throw new Error(`保存后那一格显示的不是模板本身：「${text2}」`)

  // ---- 5) 清理：把刚建的那条删掉，别在 dev 库里留垃圾 ----
  await row2.locator('button:has-text("删除")').click()
  await page.waitForTimeout(1800)
  if (await page.locator('.el-table:visible tr.el-table__row').filter({ hasText: KEYWORD }).count()) {
    throw new Error('刚建的那条规则没删掉')
  }
})

// ---- 页签记在地址栏：跳去改凭证再回来，还在同一页 ----
//
// 用户批注：「点击跳转后，再回到流水对账页面就回到默认页面「对账单」了，
// 一旦遇到大量需要修改的，就要不听得切换页面来「存疑凭证」」。
await step('跳去改凭证再回来，还在「存疑凭证」那一页', async () => {
  await page.click('.el-tabs__item:has-text("存疑凭证")')
  await page.waitForTimeout(2500)
  const url = page.url()
  if (!url.includes('tab=suspicious')) throw new Error(`地址栏里没记下页签：${url}`)

  const jump = page.locator('.flow-suspicious-table button:has-text("修改凭证")').first()
  if (!(await jump.count())) throw new Error('存疑凭证那一页没有可跳的按钮')
  await jump.click()
  await page.waitForTimeout(2500)
  if (page.url().includes('reconciliation')) throw new Error('根本没跳走')

  await page.goBack()
  await page.waitForTimeout(3000)
  if (!page.url().includes('tab=suspicious')) {
    throw new Error(`回来之后地址栏丢了页签：${page.url()}`)
  }
  const active = (await page.locator('.el-tabs__item.is-active').innerText()).trim()
  if (!active.includes('存疑凭证')) throw new Error(`回来后不在「存疑凭证」页签：${active}`)
  console.log(`   回来后地址栏：${page.url()}`)
})

console.log(errors.length ? `\n有问题：${errors.length}` : '\n没问题')
await browser.close()
process.exit(errors.length ? 1 : 0)
