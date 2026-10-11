// 凭证录入页（柠檬云式分录格）的验收脚本：只读 + 内存里的临时操作，不落库。
// 自动保存那条路会把写接口拦下来，记下请求再伪造一个回包 —— 照样能验「存了几次、
// 存的什么、存完走没走」，但不会往真账里塞凭证。
//   node ui_voucher.mjs
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:5173'
const OUT = fileURLToPath(new URL('./.shots/', import.meta.url))
const FAKE_ID = 987654
const fails = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1560, height: 980 } })
page.on('pageerror', e => fails.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') fails.push('console: ' + m.text()) })
const sleep = ms => page.waitForTimeout(ms)
const rows = () => page.locator('.vg-row:not(.vg-head):not(.vg-total)')
const val = () => page.evaluate(() => document.activeElement?.value ?? '')
async function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  console.log(ok ? '✓' : '✗', name, ok ? '' : `— 实际 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`)
  if (!ok) fails.push(`${name}: ${JSON.stringify(got)} != ${JSON.stringify(want)}`)
}

/** 写接口一律拦下来：记一笔再回一个假的凭证 */
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
      id: FAKE_ID, voucher_no: '记-0001', voucher_date: '2026-12-11', status: 'UNPOSTED',
      remark: '', attachments: 0, period_id: 0, total_debit: '0', total_credit: '0', entries: []
    })
  })
})

/** 在某一行选科目：打字 → 回车。panel 里筛出来的第一条就是它 */
async function pickSubject(i, kw) {
  await rows().nth(i).locator('.sp-input').click(); await sleep(400)
  await page.keyboard.type(kw); await sleep(650)
  await page.keyboard.press('Enter'); await sleep(350)
}

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
await page.waitForSelector('.el-aside', { timeout: 15000 })
await sleep(1800)
await page.locator('.topbar .el-select').first().click(); await sleep(800)
await page.locator('.el-select-dropdown__item:visible', { hasText: '启明物业' }).first().click()
await sleep(1800)

// ---- 新凭证 ----
await page.goto(`${BASE}/#/vouchers/new`); await sleep(2000)
await check('新凭证至少 4 行', await rows().count(), 4)
await check('行高不小于 60px', (await page.locator('.td-idx').first().boundingBox()).height >= 60, true)
await check('没有凭证级备注框', await page.locator('input[placeholder="备注"]').count(), 0)
await check('工具栏没有保存按钮了', await page.locator('.toolbar button:has-text("保存")').count(), 0)

// ---- 一行之内：先填科目还是先填金额都认 ----
await check('没选科目也能填金额', await page.locator('.td-amt.d .amt-input').first().isDisabled(), false)
await page.locator('.td-amt.d .amt').first().click(); await sleep(300)
await page.keyboard.type('55'); await sleep(200)
await check('先填金额也收下了', await rows().nth(0).locator('.td-amt.d .amt-input').inputValue(), '55')

await pickSubject(0, 'kcxj')                                   // 1001 库存现金
// 整格删空 = 这一行不要科目了。清掉科目**不**该把已经填好的金额一起带走：
// 那等于又给这一行内的先后顺序设了关，与「先填哪个都行」相矛盾
await rows().nth(0).locator('.sp-input').fill(''); await sleep(600)
await check('整格删空 = 不要这个科目了', await rows().nth(0).locator('.sp-input').inputValue(), '')
await check('清掉科目不会把金额带走', await rows().nth(0).locator('.td-amt.d .amt-input').inputValue(), '55')
await pickSubject(0, 'kcxj')

// ---- 锁的是「行」：上一行没写完，下一行先不开张 ----
const lockedCells = async (i) => [
  await rows().nth(i).locator('.td-sum input').isDisabled(),
  await rows().nth(i).locator('.sp-input').isDisabled(),
  await rows().nth(i).locator('.td-amt.d .amt-input').isDisabled(),
  await rows().nth(i).locator('.td-amt.c .amt-input').isDisabled()
]
await rows().nth(0).locator('.td-amt.d .amt-input').fill(''); await sleep(500)
await check('第一行少了金额，第二行四格全锁着', await lockedCells(1), [true, true, true, true])
await rows().nth(1).locator('.td-amt.d').click(); await sleep(600)
await check('点锁着的行会说清差在哪一行',
  (await page.locator('.el-message').last().textContent()).includes('第 1 行'), true)
await rows().nth(0).locator('.td-amt.d .amt-input').fill('1234.56'); await sleep(500)
await check('第一行补齐，第二行立刻开张', await lockedCells(1), [false, false, false, false])
await check('第一行自己始终没被锁', await lockedCells(0), [false, false, false, false])

await rows().nth(0).locator('.td-amt.d .amt-input').fill(''); await sleep(300)
// 金额 = 一个格子：聚焦时绿框套住整格，但**不翻格** ——
// 落格线、底色、已落的数字全留着，只多一根会眨的光标。
// 聚焦就换成一块大白底大字，等于把自画的那 11 根线全作废。
const cell = page.locator('.td-amt.d .amt').first()
await cell.click(); await sleep(300)
const box = await cell.boundingBox()
const head = await page.locator('.th-units.d').boundingBox()
await check('绿框套的是整个金额格', Math.abs(box.width - head.width) < 2, true)
await check('聚焦不铺白底（行底色透上来）',
  await page.evaluate(() => getComputedStyle(document.querySelector('.td-amt.d .amt.is-focus')).backgroundColor),
  'rgba(0, 0, 0, 0)')
await check('聚焦不藏落格层',
  await page.evaluate(() => getComputedStyle(document.querySelector('.td-amt.d .amt.is-focus .amt-cells')).visibility),
  'visible')
await check('聚焦不显示第二个大字', await page.evaluate(() => {
  const el = document.querySelector('.td-amt.d .amt.is-focus .amt-input')
  return [getComputedStyle(el).color, getComputedStyle(el).caretColor]
}), ['rgba(0, 0, 0, 0)', 'rgba(0, 0, 0, 0)'])
// 空格子聚焦：没有数可落格，光标就落在第一个数字该去的地方 —— 元位（第 9 条缝）
const at = (k) => Number((k / 11 * 100).toFixed(2))          // 第 k 条格缝 = 百分之几
const caretAt = () => page.evaluate(() => {
  const s = document.querySelector('.td-amt.d .amt.is-focus .amt-caret')?.style.left
  return s ? Number(parseFloat(s).toFixed(2)) : null
})
await check('空格子聚焦光标落在元位', await caretAt(), at(9))

// 连点同一格 → 整格选中，重打即覆盖
await page.keyboard.type('1234.56')
await cell.click(); await page.keyboard.type('99')
await check('再点同一格整格重打', await val(), '99')
await cell.click(); await page.keyboard.type('1234.56')
await page.locator('.toolbar input').first().click(); await sleep(300)
await check('金额落格 亿千百十万千百十元角分',
  (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
  ' | | | | |1|2|3|4|5|6')

// **打字的时候**数字就得在格里往前走，不能等失焦才落格 ——
// 一边敲一边看着数字往元位挤，才对得上「这一格是多少钱」
// 光标也得跟着格子走：方向键挪一下，竖线要挪到对应的那条缝上
await cell.click(); await page.keyboard.type('1234.56'); await sleep(300)
await check('打字时数字实时落格',
  (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
  ' | | | | |1|2|3|4|5|6')
await check('打完整格，光标在最末（分之后）', await caretAt(), at(11))
for (let i = 0; i < 4; i++) { await page.keyboard.press('ArrowLeft'); await sleep(80) }
await check('光标往左 4 格', await caretAt(), at(8))
await page.keyboard.press('Backspace'); await sleep(250)
await check('退格后数字跟着退',
  (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
  ' | | | | | |1|2|4|5|6')

// 整格选中时，那层选中底色必须画在数字**底下** ——
// 铺在数字上头就是一片蓝把数全吃掉，最该看清「要重打的是这几个数」时反而一个都看不见
await cell.click(); await sleep(300)
await check('聚焦选中时底下那层还在（提示可以重打）',
  await page.evaluate(() => !!document.querySelector('.td-amt.d .amt.is-focus .amt-sel')), true)
await check('选中底色是半透明的（数字压在上头还读得清）',
  await page.evaluate(() => getComputedStyle(document.querySelector('.amt-sel')).backgroundColor),
  'rgba(64, 158, 255, 0.2)')
await check('选中的数字照样看得见',
  (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
  ' | | | | | |1|2|4|5|6')
await check('DOM 顺序：选中层排在数字前头',
  await page.evaluate(() => {
    const box = document.querySelector('.td-amt.d .amt.is-focus .amt-cells')
    return [...box.children].findIndex(el => el.classList.contains('amt-sel'))
      < [...box.children].findIndex(el => el.classList.contains('amt-cell'))
  }), true)
await page.keyboard.type('1234.56'); await sleep(300)
await check('重新打一遍，回到 1234.56',
  (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
  ' | | | | |1|2|3|4|5|6')

// 只敲整数时，角分要自己补成 00 —— 凭证上没有「二千三百三十二块」这种写法，
// 而合计行早就按两位小数落格了；分录行空着角分，同一张纸上两行写法对不上
await cell.click(); await page.keyboard.type('2332'); await sleep(350)
await check('只敲整数时角分自动补 00',
  (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
  ' | | | | |2|3|3|2|0|0')
await check('补出来的零在光标右边（不是人敲的）', await caretAt(), at(9))
await page.keyboard.type('.5'); await sleep(350)
await check('接着敲小数点，角位接上、没打上的那位补 0',
  (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
  ' | | | | |2|3|3|2|5|0')
// 拨回 1234.56：下面自动保存那几项拿的是这一行的借方，
// 留个 2332.50 在这儿，后面的 1234.56 就永远配不平了
await cell.click(); await page.keyboard.type('1234.56'); await sleep(300)
await check('拨回 1234.56，别把后面的配平测试带歪',
  (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
  ' | | | | |1|2|3|4|5|6')

// ---- 科目面板是一棵树：一级一级点开，末级才选得了 ----
await rows().nth(0).locator('.sp-input').click(); await sleep(700)
const treeRows = () => page.evaluate(() => [...document.querySelectorAll('.sp-item')].map(el => ({
  code: el.querySelector('.sp-code').textContent,
  caret: el.querySelector('.sp-caret').textContent,
  depth: Math.round((parseInt(el.style.paddingLeft) - 12) / 16)
})))
const flat = await treeRows()
await check('打开面板只摆一级（下级收着）', [...new Set(flat.map(r => r.depth))], [0])
await check('有下级的科目带展开箭头', flat.find(r => r.code === '1002')?.caret, '▸')
await check('末级科目不带箭头', flat.find(r => r.code === '1001')?.caret, '')

// 点上级只展开，不选中：格子里原来选的那个不能被换掉
const before = await rows().nth(0).locator('.sp-input').inputValue()
await page.locator('.sp-item').filter({ hasText: '1002' }).first().click(); await sleep(400)
await check('点上级只是展开，不改这一行选的科目',
  await rows().nth(0).locator('.sp-input').inputValue(), before)
const opened = await treeRows()
await check('展开后下级 indented 一级', opened.find(r => r.code === '100201')?.depth, 1)
await check('展开后箭头翻成收起', opened.find(r => r.code === '1002')?.caret, '▾')

// 搜上级码：整条路连着出，光标停在第一条真命中的上
await rows().nth(0).locator('.sp-input').fill('1002'); await sleep(700)
const found = await treeRows()
await check('搜上级码时整条路都出来', found.map(r => r.code), ['1002', '100201', '100202', '100203'])
await check('光标停在真命中的那一行，不是垫在前面的路', await page.evaluate(() =>
  document.querySelectorAll('.sp-item')[0].classList.contains('is-cursor')), true)
await page.keyboard.press('Escape'); await sleep(300)

// 搜明细码：直接回车就选中明细
await rows().nth(0).locator('.sp-input').fill('100201'); await sleep(700)
await page.keyboard.press('Enter'); await sleep(400)
await check('搜明细码回车就选中', await rows().nth(0).locator('.sp-input').inputValue(), '100201 银行存款-工商银行')
await rows().nth(0).locator('.sp-input').fill('kcxj'); await sleep(700)
await page.keyboard.press('Enter'); await sleep(400)
await check('搜末级名（拼音）也能直接选中', await rows().nth(0).locator('.sp-input').inputValue(), '1001 库存现金')

// 科目面板就贴在「会计科目」这一列下面，多宽都跟这一格一样宽
await rows().nth(0).locator('.sp-input').click(); await sleep(800)
const subjCell = await rows().nth(0).locator('.td-subj').boundingBox()
const panelBox = await page.locator('.sp-panel').boundingBox()
await check('面板宽度跟会计科目列一样宽', Math.abs(subjCell.width - panelBox.width) <= 1, true)
await check('面板左边跟这一列对齐', Math.abs(subjCell.x - panelBox.x) <= 1, true)
await check('页签一行铺开不换行', await page.locator('.sp-panel .sp-tab').count(), 6)
await page.keyboard.press('Escape'); await sleep(300)

// ---- 自动保存：借贷一平就落库，但人还留在这一页 ----
await pickSubject(1, '600101')      // 6001 是上级，只能展开不能选；要明细就写明细码
await rows().nth(1).locator('.td-amt.c .amt-input').fill('1234.56')
await sleep(2200)
await check('借贷平了自动落库', writes.length, 1)
await check('自动保存用 POST 新建一张', writes[0]?.method, 'POST')
await check('自动保存带的是两条分录', writes[0]?.body.entries?.length, 2)
await check('自动保存不把人踢回列表', await page.locator('.vgrid').count(), 1)
await check('URL 换成刚落库的这张', page.url().endsWith(`/#/vouchers/${FAKE_ID}`), true)
await check('页脚报一声平安', (await page.locator('.foot-right .saved-tag').textContent()).includes('已自动保存'), true)
await rows().nth(0).locator('.td-sum input').fill('换个摘要'); await sleep(2000)
await check('改完再平又存一次（走 PUT）', writes.map(w => w.method), ['POST', 'PUT'])
await check('页面上一个字都没丢', await rows().nth(0).locator('.td-sum input').inputValue(), '换个摘要')

// ---- 回车一路往下写：从上到下、从左到右，一格一格挪，不跳行 ----
const BASE_ROWS = await rows().count()
const focusIn = (sel) => page.locator(sel).evaluate(e => e === document.activeElement)
await check('只摆四行时不显示删除按钮', await rows().locator('.row-del').count(), 0)

await rows().nth(2).locator('.td-sum input').click(); await sleep(300)
await page.keyboard.press('Enter'); await sleep(500)
await check('摘要回车 → 落在同一行的会计科目格', await focusIn('.vg-row:not(.vg-head):not(.vg-total) >> nth=2 >> .sp-input'), true)
await check('不会平白多长出一行', await rows().count(), BASE_ROWS)
await check('也没有一头蹦到最后一行（第 5 行还不存在）', await page.locator('.td-sum input').count(), BASE_ROWS)
await page.keyboard.press('Escape'); await sleep(250)

await rows().nth(2).locator('.td-sum input').fill('备用金'); await sleep(200)
await rows().nth(2).locator('.td-sum input').click(); await sleep(300)
await page.keyboard.press('Enter'); await sleep(400)
// 1002 是上级：回车是**展开它**，不是选中它。一路回车往下钻到末级为止
await page.keyboard.type('1002'); await sleep(600)
await page.keyboard.press('Enter'); await sleep(400)
await check('停在上级上按回车 = 展开，面板不收（选中才会收）',
  await page.locator('.sp-panel').count(), 1)
await page.keyboard.press('ArrowDown'); await sleep(250)
await page.keyboard.press('Enter'); await sleep(500)
await check('钻到末级才选中', await rows().nth(2).locator('.sp-input').inputValue(), '100201 银行存款-工商银行')
const R = (i, sel) => `.vg-row:not(.vg-head):not(.vg-total) >> nth=${i} >> ${sel}`
await check('回车选中科目 → 直接落在借方金额格', await focusIn(R(2, '.td-amt.d .amt-input')), true)

// 只有科目、没有金额：回车不许落到下一行（也别让人以为加行了）
await rows().nth(2).locator('.td-amt.c .amt-input').click(); await sleep(300)
await page.keyboard.press('Enter'); await sleep(700)
await check('中间行没金额，回车不许往下走',
  [await focusIn(R(2, '.td-amt.c .amt-input')), await rows().count()], [true, BASE_ROWS])
await check('并且说清该补金额',
  (await page.locator('.el-message').last().textContent()).includes('金额'), true)
await check('这一行没写完，下一行整行锁着', await lockedCells(3), [true, true, true, true])

// 补上金额才走得下去；凭证还没平，往下走的是同一件事，摘要跟着上一行走
await rows().nth(2).locator('.td-amt.d .amt-input').fill('50'); await sleep(300)
await rows().nth(2).locator('.td-amt.c .amt-input').click(); await sleep(300)
await page.keyboard.press('Enter'); await sleep(600)
await check('一行到头 → 下一行的摘要', await focusIn(R(3, '.td-sum input')), true)
await check('上一行的摘要自动带过来了', await rows().nth(3).locator('.td-sum input').inputValue(), '备用金')
await check('中间 2、3、4 行挨个走过，没有被跳过', await rows().count(), BASE_ROWS)

// ---- 走到最后一行最后一个格子才谈得上加行，而且这一行得先成条 ----
await rows().nth(3).locator('.sp-input').click(); await sleep(300)
await page.keyboard.type('kcxj'); await sleep(700)   // 末级科目：一个回车就选中
await page.keyboard.press('Enter'); await sleep(400)
await rows().nth(3).locator('.td-amt.c .amt-input').click(); await sleep(300)
await page.keyboard.press('Enter'); await sleep(700)
await check('末行没金额就不加行', await rows().count(), BASE_ROWS)
await check('并且说清该补金额',
  (await page.locator('.el-message').last().textContent()).includes('金额'), true)
await rows().nth(3).locator('.td-amt.d .amt-input').fill('50'); await sleep(200)
await rows().nth(3).locator('.td-amt.c .amt-input').click(); await sleep(300)
await page.keyboard.press('Enter'); await sleep(800)
await check('科目+金额齐了才长出新行', await rows().count(), BASE_ROWS + 1)
await check('新行的光标落在摘要上', await focusIn('.vg-row:not(.vg-head):not(.vg-total) >> nth=4 >> .td-sum input'), true)

// ---- 删除列没了：⊗ 挂在表格右边外面，只跟着绿行走 ----
await check('表格里没有删除列了', await page.locator('.th-del, .td-del').count(), 0)
await rows().nth(1).hover(); await sleep(300)
const delBox = await rows().nth(1).locator('.row-del').boundingBox()
const gridBox = await page.locator('.vgrid').boundingBox()
await check('⊗ 挂在表格右边外面', delBox.x >= gridBox.x + gridBox.width - 1, true)
await check('只有一行同时挂着三个 ⊗', await page.locator('.vg-del .row-del:visible').count(), 1)

// ---- 删除：出现第五行之前不给删，出现之后哪一行都能删 ----
await check('凑到第五行后每一行都带着删除按钮', await rows().locator('.row-del').count(), BASE_ROWS + 1)
await rows().nth(0).hover(); await sleep(300)
await check('绿着的第一行能删', await rows().nth(0).locator('.row-del').isVisible(), true)
await rows().nth(0).locator('.row-del').click(); await sleep(600)
await check('第一行也删得掉（不是「前四行保底」）', await rows().count(), BASE_ROWS)
await check('删回四行又把删除按钮收起来', await rows().locator('.row-del').count(), 0)

// 哪一行变绿：鼠标扫到哪一行就是哪一行（扫别人时刚才那行让位，不会两行一起绿）
await rows().nth(1).hover(); await sleep(300)
await check('鼠标扫过整行变绿',
  await rows().nth(1).locator('.td-sum').evaluate(e => getComputedStyle(e).backgroundColor),
  'rgb(242, 250, 242)')
await check('别的行还是白的',
  await rows().nth(0).locator('.td-sum').evaluate(e => getComputedStyle(e).backgroundColor), 'rgb(255, 255, 255)')
await check('绿行左边露出两个按钮', [
  await rows().nth(1).locator('.g-ins').isVisible(),
  await rows().nth(1).locator('.g-add').isVisible()], [true, true])
await check('没绿的行不露按钮', await rows().nth(0).locator('.g-ins').isVisible(), false)
const up = await rows().nth(1).locator('.g-ins').boundingBox()
const down = await rows().nth(1).locator('.g-add').boundingBox()
const idx = await rows().nth(1).locator('.td-idx').boundingBox()
// 绿行是「鼠标扫到哪一行，哪一行就绿」——扫的是**整条**，不只是格子。
// 早先左右那两条留白带子用 visibility:hidden 整个藏起来，带子跟着不接鼠标：
// 光标停在行左侧的空地上时命中的是外层容器，hoverRow 置空，绿行退回上一行不动，
// 看着就是「鼠标压着第二行，第一行绿着」。这里把整条都扫一遍。
{
  // 关键：先把「当前录入行」挪到第 4 行。不挪的话，就算悬停整个失灵，
  // 绿行也会退回「上一次录入的那行」照样是绿的，这条断言就成了空转。
  await rows().nth(3).locator('.td-amt.d .amt-input').click(); await sleep(200)
  await check('先把当前录入行挪到第 4 行',
    await rows().nth(3).locator('.td-sum').evaluate(e => getComputedStyle(e).backgroundColor),
    'rgb(242, 250, 242)')

  const b2 = await rows().nth(1).boundingBox()
  const g2 = await page.locator('.vgrid').boundingBox()
  const xs = [
    [g2.x - 40, '左侧留白带'],
    [g2.x - 6, '贴着表格左沿'],
    [g2.x + 20, '序号格里'],
    [g2.x + g2.width / 2, '格子中间'],
    [g2.x + g2.width - 20, '贴着表格右沿'],
    [g2.x + g2.width + 20, '右侧留白带']
  ]
  const got = []
  for (const [x, name] of xs) {
    await page.mouse.move(x, b2.y + b2.height / 2); await sleep(90)
    got.push(await rows().nth(1).locator('.td-sum').evaluate(e => getComputedStyle(e).backgroundColor))
  }
  await check(`扫到本行任意位置都亮（含${xs[0][1]}…${xs[5][1]}）`,
    got, Array(xs.length).fill('rgb(242, 250, 242)'))

  // 行与行之间那条横线也不能是「谁都不亮」的死像素
  const seam = []
  for (let i = 0; i < 3; i++) {
    const bb = await rows().nth(i).boundingBox()
    await page.mouse.move(g2.x + g2.width / 2, bb.y + bb.height - 1); await sleep(90)
    const on = await page.evaluate(() => {
      const rs = [...document.querySelectorAll('.vg-row:not(.vg-head):not(.vg-total)')]
      return rs.findIndex(r => r.classList.contains('is-on'))
    })
    seam.push(on === i)
  }
  await check('行与行之间没有「谁都不亮」的死像素', seam, [true, true, true])
}

await check('两个按钮落在表格左边、跟本行同高',
  up.x + up.width <= idx.x && down.x + down.width <= idx.x
  && Math.abs(up.y + up.height / 2 - (idx.y + idx.height / 2)) < 26, true)

// 按钮的位置就是它插的方向：上面那个往前插，下面那个往后插。
// 一个记不住上下文的图标比没有图标更糟，所以方向得写在脸上。
await check('上面那个按钮确实在上面', up.y + up.height <= down.y, true)
await check('往前的按钮写明插在上面',
  (await rows().nth(1).locator('.g-ins').getAttribute('title')).includes('上面'), true)
await check('往后的按钮写明插在下面',
  (await rows().nth(1).locator('.g-add').getAttribute('title')).includes('下面'), true)

// 往前插：绿着的这一行（备用金）是成条的，在它上面插一行
await rows().nth(1).hover(); await sleep(250)
await rows().nth(1).locator('.g-ins').click(); await sleep(600)
await check('上面的按钮在本行前面插一行', await rows().count(), BASE_ROWS + 1)
await check('新行落在序号 2，光标就在新行摘要上', await focusIn(R(1, '.td-sum input')), true)
await check('原来那一行被顶到序号 3，摘要没丢',
  await rows().nth(2).locator('.td-sum input').inputValue(), '备用金')

// 插出来的是个空行：它自己没写完，后面的行就都先不开张
await check('插出来的空行把后面挡住了', (await lockedCells(2))[0], true)
await check('它自己先抄了本格子的摘要', await rows().nth(1).locator('.td-sum input').inputValue(), '备用金')
await rows().nth(1).locator('.td-sum input').fill('备用金出账'); await sleep(200)
await pickSubject(1, '100201')
await rows().nth(1).locator('.td-amt.c .amt-input').fill('50'); await sleep(400)
await check('它写完了，后面的行才开张', (await lockedCells(2)).slice(0, 4), [false, false, false, false])

// 往后再插：末行也得先成条
const last = (await rows().count()) - 1
await pickSubject(last, '100202')
await rows().nth(last).locator('.td-amt.c .amt-input').fill('1'); await sleep(300)
await rows().nth(last).hover(); await sleep(250)
await rows().nth(last).locator('.g-add').click(); await sleep(600)
await check('下面的按钮在本行后面插一行', await rows().count(), BASE_ROWS + 2)
await check('它插在末行之下，新行就是末行', await focusIn(R(BASE_ROWS + 1, '.td-sum input')), true)

// 合计写中文大写 + 借贷平衡提示
// 从下往上清：清空一行的金额，下面那行立刻就锁了，正着清会清到一半卡住
for (let i = (await rows().count()) - 1; i >= 0; i--) {
  for (const w of ['c', 'd']) {
    const el = rows().nth(i).locator(`.td-amt.${w} .amt-input`)
    if (await el.count() && await el.isEnabled()) { await el.fill(''); await sleep(60) }
  }
}
await sleep(600)
await check('清空后合计写零元整', (await page.locator('.vg-total .td-sumcell').textContent()).trim(), '合计：零元整')
await check('一分钱没录时不报红字差额',
  (await page.locator('.foot-right').textContent()).includes('还没录金额'), true)
await rows().nth(0).locator('.td-amt.d .amt-input').fill('1000'); await sleep(400)
await check('只填借方 → 报差额',
  (await page.locator('.foot-right .tag-bad').textContent()).includes('差额'), true)
await rows().nth(1).locator('.td-amt.c .amt-input').fill('1000'); await sleep(400)
await check('借贷相等 → 显示平衡',
  (await page.locator('.foot-right .tag-ok').textContent()).trim(), '借贷平衡 ✓')
await check('合计写壹仟元整', (await page.locator('.vg-total .td-sumcell').textContent()).trim(), '合计：壹仟元整')

await page.screenshot({ path: OUT + 'ui-voucher-new.png' })

// ---- 老凭证 ----
const old = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const book = books.find(b => b.name === '启明物业')
  const list = await (await fetch(`/api/books/${book.id}/vouchers`, { headers: h })).json()
  const rows = Array.isArray(list) ? list : (list.items || list.data || [])
  const v = rows.find(x => x.status !== 'POSTED') || rows[0]
  const posted = rows.find(x => x.status === 'POSTED')
  return {
    id: v.id, entries: v.entries.length, posted: posted?.id ?? null,
    subs: v.entries.map(e => e.subject_id ? `${e.subject_code} ${e.subject_name}` : null)
  }
})
await page.goto(`${BASE}/#/vouchers/${old.id}`); await sleep(2000)
await check('老凭证补足 4 行', await rows().count(), Math.max(4, old.entries))
await check('补出来的空行不带摘要',
  (await page.locator('.td-sum input').evaluateAll(els => els.map(e => e.value)))
    .slice(old.entries).every(v => v === ''), true)
// 老数据的 subject_id 一直都在，格子空着是显示的问题：打开就该看见「编码 名称」
const shown = await page.locator('.sp-input').evaluateAll(els => els.map(e => e.value.trim()))
await check('老凭证的会计科目写在格子里', old.subs.every((s, i) => (s ? shown[i] === s : shown[i] === '')), true)
await page.screenshot({ path: OUT + 'ui-voucher-edit.png' })

if (old.posted) {
  await page.goto(`${BASE}/#/vouchers/${old.posted}`); await sleep(2000)
  await check('已记账凭证只读', await page.locator('.amt-input').first().isDisabled(), true)
  await page.screenshot({ path: OUT + 'ui-voucher-posted.png' })
}

console.log(fails.length ? `\n${fails.length} 项没过：\n- ` + fails.join('\n- ') : '\n全部通过')
await browser.close()
process.exit(fails.length ? 1 : 0)
