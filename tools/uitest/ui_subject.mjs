// 柠檬云式科目处理：凭证格整格可填写的科目选择面板 + 新增科目弹窗
//   node ui_subject.mjs
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:5173'
const OUT = fileURLToPath(new URL('../../.shots/', import.meta.url))
const fails = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1560, height: 980 } })
page.on('pageerror', e => fails.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') fails.push('console: ' + m.text()) })
const sleep = ms => page.waitForTimeout(ms)
async function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  console.log(ok ? '✓' : '✗', name, ok ? '' : `— 实际 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`)
  if (!ok) fails.push(`${name}: ${JSON.stringify(got)} != ${JSON.stringify(want)}`)
}

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
// 侧栏现在是 .sidebar（不再是 el-aside）—— 等一个已经删掉的类名，
// 脚本会一直超时，报错还长得像「登录失败」。同 ui_voucher.mjs。
await page.waitForSelector('.sidebar .sb-list li', { timeout: 15000 })
await sleep(1800)
await page.locator('.topbar .el-select').first().click(); await sleep(800)
await page.locator('.el-select-dropdown__item:visible', { hasText: '启明物业' }).first().click()
await sleep(1800)

// ---- 凭证里的科目格：整格就是一个输入框，里面没有「选择科目」按钮 ----
await page.goto(`${BASE}/#/vouchers/new`); await sleep(2200)
await check('格子里没有「选择科目」按钮', await page.locator('.td-subj .sp-trigger').count(), 0)
const subjCell = page.locator('.td-subj .sp-input').first()
const cellBox = await page.locator('.td-subj').first().boundingBox()
const inputBox = await subjCell.boundingBox()
// 框横向铺满整格，纵向只占内容那几行 —— 写满一行自己折成两行（最多两行）。
// 格子下半截（压着科目余额）空着，点那儿也算「点这一格」，见下面 onCellClick。
await check('框横向铺满整格', Math.abs(cellBox.width - inputBox.width) < 2, true)
await check('框纵向只占一行（写长了才长成两行）', inputBox.height < cellBox.height, true)
await subjCell.click(); await sleep(700)
await check('点一下就出面板', await page.locator('.sp-panel').isVisible(), true)
await check('面板里没有标题、没有搜索框',
  [await page.locator('.sp-title').count(), await page.locator('.sp-search-input').count()], [0, 0])
await check('分类页签（去掉了「常用」）', await page.locator('.sp-tab').allTextContents(),
  ['全部', '资产', '负债', '权益', '成本', '损益'])
await check('字是在格子里打的',
  await page.evaluate(() => document.activeElement?.classList.contains('sp-input')), true)
await page.screenshot({ path: OUT + 'ui-subject-panel.png' })

// 拼音首字母：直接在格子里打，面板跟着筛
await page.keyboard.type('kcxj');          // 库存现金
await sleep(700)
await check('拼音首字母 kcxj → 库存现金', (await page.locator('.sp-item .sp-name').first().textContent()), '库存现金')
await subjCell.fill(''); await sleep(300)
await subjCell.fill('yhck'); await sleep(700)   // 银行存款
await check('拼音首字母 yhck 搜得到银行存款类',
  (await page.locator('.sp-item .sp-name').allTextContents()).some(t => t.includes('银行存款')), true)

// 按分类筛
await subjCell.fill(''); await sleep(300)
await page.locator('.sp-tab', { hasText: '损益' }).click(); await sleep(500)
const cats = await page.locator('.sp-item .sp-name').allTextContents()
await check('切到「损益」只剩损益类', cats.length > 0 && !cats.includes('库存现金'), true)
await page.locator('.sp-tab', { hasText: '全部' }).click(); await sleep(400)

// 选一个科目
await subjCell.fill('库存现金'); await sleep(500)
await page.locator('.sp-item').first().click(); await sleep(500)
// 点面板里的科目选中时焦点还留在格子上 → 写的是编码（人接着打就是改科目）；
// 等焦点离开这一格，才换回一级一级拼出来的全名给读凭证的人看
await check('选中后写回格子（焦点还在这一格 → 写的是编码）',
  await subjCell.inputValue(), '1001')
await check('选完面板收起', await page.locator('.sp-panel').count(), 0)

// 打了半天没选中：别让格子上写的和实际挂的科目说两套话
// 点已经聚焦的这一格 = 把光标放到点上（好改其中几位），整格重打得自己先全选
await subjCell.click(); await sleep(400)
await subjCell.press('ControlOrMeta+a')
await page.keyboard.type('查无此科目'); await sleep(500)
await page.locator('.td-sum textarea').first().click(); await sleep(400)
await check('认不出来的字回填成原来的', await subjCell.inputValue(), '库存现金')

// ---- 新增科目 ----
await subjCell.click(); await sleep(500)
await page.locator('.sp-add').click(); await sleep(700)
await check('新增科目弹窗打开', await page.locator('.el-dialog:has-text("新增科目")').isVisible(), true)
const code0 = await page.locator('.el-dialog:has-text("新增科目") input').nth(1).inputValue()
await check('没选上级时按一级接号', /^\d{4}$/.test(code0), true)
await check('编码规则提示', (await page.locator('.sf-note').textContent()).trim(), '注意：科目编码规则：4-2-2-2。')
await check('新增科目不做核算（没有数量/辅助/外币开关）', await page.locator('.el-checkbox').count(), 0)
const noteX = (await page.locator('.sf-note').boundingBox()).x
const formX = (await page.locator('.sf .el-form-item').first().boundingBox()).x
await check('编码规则提示贴着弹窗左下角', Math.abs(noteX - formX) < 2, true)

// 选上级科目 → 编码接号 + 类别锁死
await page.locator('.el-dialog:has-text("新增科目") .el-select__wrapper').first().click(); await sleep(700)
await page.locator('.el-select-dropdown__item:visible').filter({ hasText: '1002 银行存款' }).first().click(); await sleep(800)
await check('按上级自动接号', await page.locator('.el-dialog:has-text("新增科目") input').nth(1).inputValue(), '100204')
await check('类别跟着上级锁死', await page.locator('.el-dialog:has-text("新增科目") .el-select__wrapper').nth(1).getAttribute('class')
  .then(c => c.includes('is-disabled')), true)
await page.locator('.el-dialog:has-text("新增科目") input').nth(2).fill('UI 验收科目')
await page.screenshot({ path: OUT + 'ui-subject-create.png' })
await page.locator('.el-dialog:has-text("新增科目") button:has-text("确定")').click()
await sleep(1600)

const created = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const book = books.find(b => b.name === '启明物业')
  const tree = await (await fetch(`/api/books/${book.id}/subjects`, { headers: h })).json()
  const walk = (ns, out = []) => { ns.forEach(n => { out.push(n); walk(n.children || [], out) }); return out }
  const all = walk(tree)
  const hit = all.find(s => s.name === 'UI 验收科目')
  return hit ? { code: hit.code, parent: hit.parent_id, category: hit.category, qty: hit.qty_calc, aux: hit.aux_calc, level: hit.level } : null
})
await check('科目真的建出来了', created && created.code, '100204')
await check('类别继承上级', created?.category, 'ASSET')
await check('核算标记一律是关的', [created?.qty, created?.aux], [false, false])
await check('建完自动选中（银行存款底下的 UI 验收科目，名字一级一级拼）',
  await page.locator('.td-subj .sp-input').first().inputValue(), '银行存款/UI 验收科目')

// ---- 科目余额表：编码规则在，没有核算列 ----
await page.goto(`${BASE}/#/subjects`); await sleep(1800)
await check('表格显示编码规则', (await page.locator('.toolbar .muted').textContent()).trim(), '科目编码规则 4-2-2-2')
await check('科目表也没有核算列',
  await page.locator('.el-table__header th:has-text("核算")').count(), 0)
await page.screenshot({ path: OUT + 'ui-subject-tree.png' })

// 清理：把验收科目删掉，别留在账里
await page.evaluate(async () => {
  const h = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const book = books.find(b => b.name === '启明物业')
  const tree = await (await fetch(`/api/books/${book.id}/subjects`, { headers: h })).json()
  const walk = (ns, out = []) => { ns.forEach(n => { out.push(n); walk(n.children || [], out) }); return out }
  const hit = walk(tree).find(s => s.name === 'UI 验收科目')
  if (hit) await fetch(`/api/books/${book.id}/subjects/${hit.id}`, { method: 'DELETE', headers: h })
})

console.log(fails.length ? `\n${fails.length} 项没过：\n- ` + fails.join('\n- ') : '\n全部通过')
await browser.close()
process.exit(fails.length ? 1 : 0)
