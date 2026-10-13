// 凭证录入页（柠檬云式分录格）的验收脚本：只读 + 内存里的临时操作，不落库。
// 自动保存那条路会把写接口拦下来，记下请求再伪造一个回包 —— 照样能验「存了几次、
// 存的什么、存完走没走」，但不会往真账里塞凭证。
//   node ui_voucher.mjs
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:5173'
const OUT = fileURLToPath(new URL('../../.shots/', import.meta.url))
const FAKE_ID = 987654
const fails = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1560, height: 980 } })
page.on('pageerror', e => fails.push('pageerror: ' + e.message + '｜' + String(e.stack || '').split('\n')[1]))
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

/**
 * 往金额格里填数：**点一下 → 全选 → 退格 → 打字**。
 *
 *  不能用 Playwright 的 fill()：它靠「先全选、再聚焦、再插入」，
 * 而金额格一聚焦就把光标挪到元/角之间，那次全选就白做了 ——
 * 实测 fill('1234.56') 变成了往光标处插字，结果是 200.001234.56。
 */
/** 合计行里那一栏的金额：格子上是逐格落的数字，读回来拼成 '1000.00' 才好比 */
async function totalOf(side) {
  const t = await page.locator(`.vg-total .td-total.${side}`)
    .evaluate((el) => el.textContent.replace(/\s+/g, ''))
  if (!t) return ''
  const neg = t[0] === '\u2212' || t[0] === '-'
  const d = neg ? t.slice(1) : t
  return (neg ? '-' : '') + d.slice(0, -2) + '.' + d.slice(-2)
}

async function fillAmt(loc, text) {
  await loc.click(); await sleep(150)
  // 全选 + 直接打字覆盖。不用退格：点格子时光标按落格线摆，点到数字左边就落在 0 号位，
  // 那儿退格什么也删不掉；也不用 select()——Chrome 一 select 就把像数字的文本规整成两位小数
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

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
// 登录成功的标志是外壳渲染出来。侧栏现在是 .sidebar（不再是 el-aside）——
// 等一个已经删掉的类名，脚本会一直超时，报错还长得像「登录失败」。
await page.waitForSelector('.sidebar .sb-list li', { timeout: 15000 })
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
  await rows().nth(i).locator('.td-sum textarea').isDisabled(),
  await rows().nth(i).locator('.sp-input').isDisabled(),
  await rows().nth(i).locator('.td-amt.d .amt-input').isDisabled(),
  await rows().nth(i).locator('.td-amt.c .amt-input').isDisabled()
]
await fillAmt(rows().nth(0).locator('.td-amt.d .amt-input'), ''); await sleep(500)
await check('第一行少了金额，第二行四格全锁着', await lockedCells(1), [true, true, true, true])
await rows().nth(1).locator('.td-amt.d').click(); await sleep(600)
await check('点锁着的行会说清差在哪一行',
  (await page.locator('.el-message').last().textContent()).includes('第 1 行'), true)
// 锁的是「能不能写」，不是「长什么样」：变淡看着像这几格的数据坏了，
// 其实只是还没轮到你写。跟已记账凭证一个道理（见 readonly_styling）。
await check('锁着的行不褪色（跟能写的那几格一个样子）',
  await rows().nth(1).evaluate((el) => [...el.querySelectorAll('.is-blocked')]
    .map((n) => getComputedStyle(n.querySelector('textarea, .sp-input, .amt') || n).opacity)),
  ['1', '1', '1', '1'])
await fillAmt(rows().nth(0).locator('.td-amt.d .amt-input'), '1234.56'); await sleep(500)
await check('第一行补齐，第二行立刻开张', await lockedCells(1), [false, false, false, false])
await check('第一行自己始终没被锁', await lockedCells(0), [false, false, false, false])

await fillAmt(rows().nth(0).locator('.td-amt.d .amt-input'), ''); await sleep(300)
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
/** 点格子不再整格选中，所以「重新打一遍」得自己先把这一格全选掉再打 */
const retype = async (text) => {
  await cell.click(); await sleep(150)
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type(text); await sleep(350)
}

// 点格子 → 光标跟着鼠标走（不再整格选中）：点哪儿就是哪儿
await page.keyboard.type('1234.56')
const cellBox = await cell.boundingBox()
const clickAt = async (frac) => {
  await cell.click({ position: { x: cellBox.width * frac, y: cellBox.height / 2 } }); await sleep(300)
}
const pos = () => page.evaluate(() => document.querySelector('.td-amt.d .amt-input').selectionStart)
// 点的是「落格线」，不是透明输入框里那串靠左的字：
// 1234.56 的 1234 落在 千百十元 四格（缝 5-9），56 落在 角分（缝 9-11）
await clickAt(0.04)
await check('点最左边（数字还没到的缝）光标压在头一个数前', await pos(), 0)
await check('点同一格不再整格选中',
  await page.evaluate(() => !!document.querySelector('.td-amt.d .amt.is-focus .amt-sel')), false)
await clickAt(0.727)      // 第 8 条缝 = 十与元之间
await check('点十位那一格 → 光标在 123 之后', await pos(), 3)
await check('画出来的竖线光标也跟着落在那条缝上', await caretAt(), at(8))
await clickAt(0.818)      // 第 9 条缝 = 元/角之间
await check('点元/角之间 → 光标在小数点后头', await pos(), 4)
await clickAt(0.91)
await check('点角/分之间 → 光标在角分两位中间', await pos(), 6)

// 光标挪到元/角之间（小数点该在的地方）再敲：接着敲的就是角分
await clickAt(0.98)       // 先落到最末尾，再往左两格 = 元/角之间
await page.keyboard.press('ArrowLeft'); await page.keyboard.press('ArrowLeft'); await sleep(250)
await check('← 两次挪到元/角之间', await caretAt(), at(9))
await page.keyboard.type('99'); await sleep(350)
await check('在元/角之间敲两个数 = 改角分', await val(), '1234.99')
await check('光标跟着刚敲的数走到末尾', await caretAt(), at(11))

// 光标压在最高位左边 / 最低位右边时，方向键要**出格**：← 去左边那一格，→ 去右边那一格。
// 光标停在边界上浏览器只会把它按在原地，人按这一下想的就是「退回上一格」，
// 却什么都没发生 —— 整张凭证一路往右录下来，回头想改科目还得先 Tab 绕一圈
{
  const R0 = '.vg-row:not(.vg-head):not(.vg-total) >> nth=0'
  const focusedIs = async (sel) => page.locator(sel).evaluate(e => e === document.activeElement)
  const dInp = page.locator(`${R0} >> .td-amt.d .amt-input`)
  const dCaret = () => dInp.evaluate(e => [e.selectionStart, e.selectionEnd])
  const putCaret = async (p) => {
    await dInp.evaluate((e, p) => { e.focus(); e.setSelectionRange(p, p) }, p); await sleep(220)
  }

  await putCaret(0)
  await page.keyboard.press('ArrowLeft'); await sleep(500)
  await check('光标压在最高位左边 → ← 出格到会计科目', await focusedIs(`${R0} >> .sp-input`), true)

  await putCaret(1)
  await page.keyboard.press('ArrowLeft'); await sleep(350)
  await check('还没到最高位 → ← 只是在格内挪光标', await dCaret(), [0, 0])
  await check('没出格，焦点还留在借方这一格', await focusedIs(`${R0} >> .td-amt.d .amt-input`), true)

  await putCaret(999)
  await page.keyboard.press('ArrowRight'); await sleep(500)
  await check('光标压在最低位右边 → → 出格到贷方', await focusedIs(`${R0} >> .td-amt.c .amt-input`), true)

  await page.locator(`${R0} >> .td-amt.c .amt-input`)
    .evaluate(e => { e.focus(); e.setSelectionRange(0, 0) }); await sleep(220)
  await page.keyboard.press('ArrowLeft'); await sleep(500)
  await check('贷方 ← 回借方', await focusedIs(`${R0} >> .td-amt.d .amt-input`), true)

  await page.locator(`${R0} >> .td-amt.c .amt-input`)
    .evaluate(e => { e.focus(); e.setSelectionRange(e.value.length, e.value.length) }); await sleep(220)
  await page.keyboard.press('ArrowRight'); await sleep(400)
  await check('贷方最右 → 右边没格子了，停在本表里', await focusedIs(`${R0} >> .td-amt.c .amt-input`), true)

  // 整格选着的时候，方向键是「收选区」，不是「出格」—— 选区是人有意划的
  await dInp.evaluate(e => { e.focus(); e.select() }); await sleep(220)
  await page.keyboard.press('ArrowLeft'); await sleep(350)
  await check('整格选着按 ← → 先收选区，不出格', await dCaret(), [0, 0])
  await check('收完选区焦点还在借方', await focusedIs(`${R0} >> .td-amt.d .amt-input`), true)
}

// 光标紧贴着小数点时按退格：抹掉的得是角分，不是那个点本身
// （只删小数点的话 200.00 就成了 20000 —— 金额凭空大一百倍，还会被自动保存存进账）
await retype('200.00')
await clickAt(0.98)
await page.keyboard.press('ArrowLeft'); await page.keyboard.press('ArrowLeft'); await sleep(250)
await check('挪到元/角之间（紧贴小数点后头）', await caretAt(), at(9))
await page.keyboard.press('Backspace'); await sleep(350)
await check('退格删的是角分，小数点不能单独没', await val(), '200')
await page.keyboard.press('Delete'); await sleep(350)
await check('再按删除也还是 200', await val(), '200')
await retype('1234.56')
await page.locator('.toolbar input').first().click(); await sleep(300)
await check('金额落格 亿千百十万千百十元角分',
  (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
  ' | | | | |1|2|3|4|5|6')

// **打字的时候**数字就得在格里往前走，不能等失焦才落格 ——
// 一边敲一边看着数字往元位挤，才对得上「这一格是多少钱」
// 光标也得跟着格子走：方向键挪一下，竖线要挪到对应的那条缝上
await retype('1234.56')
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
await cell.click()
// 选中层只剩「按住拖选 / 双击 / 键盘选中」这几条路会画，单纯点一下不画
await page.evaluate(() => document.querySelector('.td-amt.d .amt-input').select()); await sleep(300)
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
await retype('2332')
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
await retype('1234.56')
await check('拨回 1234.56，别把后面的配平测试带歪',
  (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
  ' | | | | |1|2|3|4|5|6')

// 负号是**整格的属性**：光标在哪一位数上都一样，按一下整格翻负，再按一下翻回来
{
  const cell = rows().first().locator('.td-amt.d .amt-input')
  await fillAmt(cell, '303000'); await sleep(400)
  await check('先录成正数', await cell.inputValue(), '303000')
  // 光标刻意摆在中间（万位那条缝上），不是末尾 —— 要验的是「在哪一位上都翻整格」
  await cell.evaluate((e) => e.setSelectionRange(3, 3)); await sleep(200)
  await page.keyboard.press('-'); await sleep(400)
  await check('光标停在中间按「-」：整格翻负',
    await cell.inputValue(), '-303000')
  await check('负号在数的前头，不在光标那儿',
    (await rows().first().locator('.td-amt.d .amt-cell').allTextContents()).join('|'),
    ' | |−|3|0|3|0|0|0|0|0')
  await check('光标跟着挪一位，还压在原来那个数字上',
    await page.evaluate(() => document.querySelector('.vg-row:not(.vg-head) .td-amt.d .amt-input').selectionStart), 4)
  // 红是落在**数字**上的（.is-red .amt-cell），不是落在外层那圈落格线上
  await check('落格是红字（红字冲销）', await page.evaluate(() =>
    getComputedStyle(document.querySelector('.vg-row:not(.vg-head) .td-amt.d .amt-cell')).color),
    'rgb(217, 48, 37)')
  await page.keyboard.press('-'); await sleep(400)
  await check('再按一次「-」：翻回正数',
    await cell.inputValue(), '303000')
  await check('翻回来就不再是红字了', await page.evaluate(() =>
    getComputedStyle(document.querySelector('.vg-row:not(.vg-head) .td-amt.d .amt-cell')).color),
    'rgb(48, 49, 51)')
  // 空格子：先按负号摆好方向，接着敲的数直接就是红的
  await fillAmt(cell, ''); await sleep(300)
  await page.keyboard.press('-'); await sleep(300)
  await check('空格按一下「-」：先摆出负号', await cell.inputValue(), '-')
  await page.keyboard.type('500'); await sleep(400)
  await check('接着敲 500，直接成 -500', await cell.inputValue(), '-500')
  // 拨回 1234.56：后面的自动保存拿的是这一行的借方，留个红字在那儿就永远配不平
  await fillAmt(cell, '1234.56'); await sleep(400)
  await check('拨回 1234.56，别把后面的配平测试带歪', await cell.inputValue(), '1234.56')
}

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
await check('搜明细码回车就选中', await rows().nth(0).locator('.sp-input').inputValue(), '银行存款/工商银行')
// 科目名是**一级一级拼**出来的：库里 600101 存的是「主营业务收入-租金」，
// 而同一个上级底下的 600103 只存「其他」—— 后者名字里一个横杠都没有，
// 光把横杠换成斜杠的话格里就只剩光秃秃的「其他」，看不出挂在 6001 底下
await pickSubject(0, '600103')
await check('名字里没横杠的子科目，也要带出它上面那一级',
  await rows().nth(0).locator('.sp-input').inputValue(), '主营业务收入/其他')
await pickSubject(0, '600101')
await check('名字里抄了上级的那一段不能重复拼',
  await rows().nth(0).locator('.sp-input').inputValue(), '主营业务收入/租金')
await pickSubject(0, 'kcxj')
await rows().nth(0).locator('.sp-input').fill('kcxj'); await sleep(700)
await page.keyboard.press('Enter'); await sleep(400)
await check('搜末级名（拼音）也能直接选中', await rows().nth(0).locator('.sp-input').inputValue(), '库存现金')
// 回车选完，焦点已经去了借方金额格 —— 这一格这时候该换回给人读的全名
await check('没在编辑时，格子换回一级一级拼出来的全名',
  await rows().nth(0).locator('.sp-input').inputValue(), '库存现金')
await check('没在编辑时不留编码（那串是给人对科目表改的）',
  await rows().nth(0).locator('.sp-input').evaluate(el => el.selectionStart === el.selectionEnd && el === document.activeElement), false)

// 科目面板就贴在「会计科目」这一列下面，多宽都跟这一格一样宽
await rows().nth(0).locator('.sp-input').click(); await sleep(800)
const subjCell = await rows().nth(0).locator('.td-subj').boundingBox()
const panelBox = await page.locator('.sp-panel').boundingBox()
await check('面板宽度跟会计科目列一样宽', Math.abs(subjCell.width - panelBox.width) <= 1, true)
await check('面板左边跟这一列对齐', Math.abs(subjCell.x - panelBox.x) <= 1, true)
await check('页签一行铺开不换行', await page.locator('.sp-panel .sp-tab').count(), 6)
await page.keyboard.press('Escape'); await sleep(300)

const R0SUB = '.vg-row:not(.vg-head):not(.vg-total) >> nth=0'
const subjEl = page.locator(`${R0SUB} >> .sp-input`)
const subjSel = () => subjEl.evaluate(el => [el.selectionStart, el.selectionEnd])

// 编辑状态写的是**科目编码**：录的时候人手里拿着科目表，对的是编码；
// 「主营业务收入/其他」那一长串是给读凭证的人看的，挡在编码前面只会让人
// 先把它整个划掉、再数一遍自己想去哪个。歇着的时候才写回全名。
// 焦点先挪去摘要格再点科目格 —— 这一下 click 才是「进这一格」。
await page.locator(`${R0SUB} >> .td-sum textarea`).click(); await sleep(300)
await subjEl.click(); await sleep(500)
await check('一进这一格就换成科目编码', await subjEl.inputValue(), '1001')
await check('刚进这一格 = 整格选中，接着打就是重打一遍', await subjSel(), [0, 4])

// 整格选中**不是锁**。编码就 4 位，人十有八九不是要重打，是要改其中几位
// （6001 改 6002、把中间两位挑出来改）。全选要是方向键按不掉、鼠标也点不掉，
// 这一格就只能整段盖着改 —— 那是添堵，不是省事。
await page.keyboard.press('ArrowLeft'); await sleep(350)
await check('整格选中的时候按 ←：先收选区，光标到最前（不是甩到隔壁格）', await subjSel(), [0, 0])
await page.keyboard.type('9'); await sleep(500)
await check('收完选区再打字 = 插进去，不是盖掉整格', await subjEl.inputValue(), '91001')
await subjEl.press('ControlOrMeta+a'); await page.keyboard.type('1001'); await sleep(500)
await check('改回来', await subjEl.inputValue(), '1001')
await page.keyboard.press('Escape'); await sleep(300)

// 鼠标也一样：人已经在这格里待过了，再点一下是要改其中几个字，
// 不是又要重打 —— 别再替他全选，光标落到点上就对了。
await subjEl.click(); await sleep(400)
const c1 = await subjSel()
await check('再点一下已聚焦的格子 → 选区收掉（能局部改）', c1[0] === c1[1], true)
await check('再点一下不会把编码改掉', await subjEl.inputValue(), '1001')
// 点在框下面的空白上（框只有内容那么高）没有位置可落，就落到末尾 ——
// 跟单行输入框里点到右边空白是一个意思
const spBox = await page.locator(`${R0SUB} >> .td-subj .sp`).boundingBox()
await page.mouse.click(spBox.x + 20, spBox.y + spBox.height - 6); await sleep(400)
await check('点格子空白处 → 光标落到末尾', await subjSel(), [4, 4])

// 摘要格的方向键也一样：整行是「摘要 → 科目 → 借方 → 贷方」，
// 到摘要末尾再按一下 → 却只能把光标按在原地 —— 明明右边就摆着下一格。
// 这块先有一句摘要才试得动：空框「整格选中」也选不出个名堂来。
{
  const sumTa = page.locator(`${R0SUB} >> .td-sum textarea`)
  const sumSel = () => sumTa.evaluate(e => [e.selectionStart, e.selectionEnd, e.value.length])
  const orig = await sumTa.inputValue()
  // 把手挪到某一格上：先点一下，再把光标挪过去（跟人手一样）
  const putSum = async (p) => {
    await sumTa.click(); await sleep(300)
    await sumTa.evaluate((e, p) => e.setSelectionRange(p, p), p); await sleep(250)
  }
  if (!orig) {
    await sumTa.click(); await sleep(300)
    await page.keyboard.type('测试摘要'); await sleep(500)
  }
  const len = (await sumTa.inputValue()).length
  await check('这句摘要写得进去', await sumTa.inputValue(), orig || '测试摘要')

  await putSum(len)
  await page.keyboard.press('ArrowRight'); await sleep(550)
  await check('摘要末尾按 → 走到会计科目格',
    await subjEl.evaluate(e => e === document.activeElement), true)
  await check('进科目格就是进编辑态，写的是编码', await subjEl.inputValue(), '1001')

  await putSum(len - 2)
  await page.keyboard.press('ArrowRight'); await sleep(350)
  await check('没到末尾 → → 只在格内挪光标', await sumSel(), [len - 1, len - 1, len])

  await putSum(0)
  await page.keyboard.press('ArrowLeft'); await sleep(400)
  await check('← 到头没地方去（左边是序号），停在这一格',
    await sumTa.evaluate(e => e === document.activeElement), true)

  // 光标进摘要格 = 整格选中：摘要多半是同一句话抄好几遍，改就整句重打
  await subjEl.click(); await sleep(300)
  await sumTa.click(); await sleep(400)
  await check('点没聚焦的摘要格 = 整格选中（改整句最省事）',
    (await sumSel())[0] !== (await sumSel())[1], true)
  await page.keyboard.press('ArrowRight'); await sleep(350)
  await check('选着按 → 先收选区，不急着出格', await sumSel(), [len, len, len])
  await page.keyboard.press('ArrowRight'); await sleep(550)
  await check('收完选区再按 → 才出格到科目',
    await subjEl.evaluate(e => e === document.activeElement), true)

  // 摘要改的是一整句，不是编码：局部改也得做得到。
  // 留一句原来的样子给后面的断言，别把这一行改成别的样子。
  await sumTa.click(); await sleep(300)
  await sumTa.press('ControlOrMeta+a')
  if (orig) await page.keyboard.type(orig); else await page.keyboard.press('Backspace')
  await sleep(500)
  await page.keyboard.press('Escape'); await sleep(300)
  await check('摘要改回原样', await sumTa.inputValue(), orig)
}

// ← → 是「换格子」，不是「操作下面那块面板」：被换走的是人正录的那一格，
// 不是列表。展开/收起交给 ▸ 和 ↑↓ 走到那一行再回车。
// 刚进来整格选中的那一下先收选区（见上面），再按才轮到换格子 ——
// 人自己把选区收掉、光标停在哪，就是他想去哪一格。
await page.keyboard.press('Home'); await sleep(200)
await page.keyboard.press('ArrowLeft'); await sleep(550)
await check('← 从科目格回到摘要格',
  await page.locator(`${R0SUB} >> .td-sum textarea`).evaluate(e => e === document.activeElement), true)
await check('换走了，科目格换回全名',
  await rows().nth(0).locator('.sp-input').inputValue(), '库存现金')
await rows().nth(0).locator('.sp-input').click(); await sleep(500)
await page.keyboard.press('ArrowDown'); await sleep(350)
await check('↓ 还是在面板里走光标（这一条没改）',
  await page.locator('.sp-item.is-cursor').count(), 1)
await page.keyboard.press('End'); await sleep(200)      // 选中着先收掉，光标到末尾
await page.keyboard.press('ArrowRight'); await sleep(550)
await check('→ 从科目格走到借方金额格',
  await page.locator(`${R0SUB} >> .td-amt.d .amt-input`).evaluate(e => e === document.activeElement), true)
await check('换走了，面板顺手收起', await page.locator('.sp-panel').count(), 0)

// ---- 这一行填了科目，面板就别再摊一整本科目表出来 ----
await check('没有「是上级科目，点▸展开」那种提示条', await page.locator('.sp-parent').count(), 0)
await pickSubject(0, '100202')          // 银行存款-建设银行：压在 1002 底下
await rows().nth(0).locator('.sp-input').click(); await sleep(700)
// 1001 库存现金跟 1002 银行存款是**平级**的一级科目，别混进来
await check('已填科目 → 只摆它上面那一整条路 + 它同一层',
  (await treeRows()).map(r => r.code), ['1002', '100201', '100202', '100203'])
await check('这一支里就它一条是选中态',
  await page.locator('.sp-item.is-on').count(), 1)
// 上下键那条淡蓝光标，打开面板就得**停在已填的那个科目上** ——
// 人重开面板是来改它的，从第一条按 ↑↓ 挪过去得按三下
await check('重开面板：光标直接落在已填科目那一行',
  await page.evaluate(() => {
    const items = [...document.querySelectorAll('.sp-item')]
    const i = items.findIndex(el => el.classList.contains('is-cursor'))
    return i >= 0 ? items[i].querySelector('.sp-code').textContent : null
  }), '100202')
await check('光标跟选中态是同一行（不是旁边那条父科目）',
  await page.evaluate(() => {
    const c = document.querySelector('.sp-item.is-cursor')
    return !!c && c.classList.contains('is-on')
  }), true)
await page.keyboard.press('Escape'); await sleep(300)

// 一级科目也得落在自己身上（它没有上级可展开，是另一条路径）
await rows().nth(0).locator('.sp-input').click(); await sleep(400)
await page.keyboard.press('ControlOrMeta+a'); await page.keyboard.type('1001'); await sleep(650)
await page.keyboard.press('Enter'); await sleep(400)
await rows().nth(0).locator('.td-sum textarea').click(); await sleep(250)
await rows().nth(0).locator('.sp-input').click(); await sleep(700)
await check('一级科目：光标也落在自己身上',
  await page.evaluate(() => {
    const items = [...document.querySelectorAll('.sp-item')]
    const i = items.findIndex(el => el.classList.contains('is-cursor'))
    return i >= 0 ? items[i].querySelector('.sp-code').textContent : null
  }), '1001')
await page.keyboard.press('Escape'); await sleep(300)
// 收回 100202。Esc 之后焦点还在这一格里，这时再点是「局部编辑」（不整格选中，
// 人要点哪儿就在哪儿落笔），直接打字会接在 1001 后面 —— 所以先自己全选一遍
await rows().nth(0).locator('.sp-input').click(); await sleep(300)
await page.keyboard.press('ControlOrMeta+a'); await sleep(200)
await page.keyboard.type('100202'); await sleep(650)
await page.keyboard.press('Enter'); await sleep(400)
await check('收回 100202', await rows().nth(0).locator('.sp-input').inputValue(), '银行存款/建设银行')

// 反过来：这一行空着是在「逛科目表」，一级目录就得全摊开
await rows().nth(0).locator('.sp-input').click(); await sleep(400)
await page.keyboard.press('ControlOrMeta+a'); await page.keyboard.press('Backspace'); await sleep(700)
await check('这一行空着 → 整个一级目录照旧摊开', (await treeRows()).length > 30, true)
await page.keyboard.press('Escape'); await sleep(300)
await pickSubject(0, 'kcxj')           // 收回 1001，后面的配平还得靠它
await check('收回原来那个科目', await rows().nth(0).locator('.sp-input').inputValue(), '库存现金')

// ---- 自动保存：借贷一平就落库，但人还留在这一页 ----
await pickSubject(1, '600101')      // 6001 是上级，只能展开不能选；要明细就写明细码
await fillAmt(rows().nth(1).locator('.td-amt.c .amt-input'), '1234.56')
await sleep(2200)
await check('借贷平了自动落库', writes.length, 1)
await check('自动保存用 POST 新建一张', writes[0]?.method, 'POST')
await check('自动保存带的是两条分录', writes[0]?.body.entries?.length, 2)
await check('自动保存不把人踢回列表', await page.locator('.vgrid').count(), 1)
await check('URL 换成刚落库的这张', page.url().endsWith(`/#/vouchers/${FAKE_ID}`), true)
await check('页脚报一声平安', (await page.locator('.foot-right .saved-tag').textContent()).includes('已自动保存'), true)
await rows().nth(0).locator('.td-sum textarea').fill('换个摘要'); await sleep(2000)
await check('改完再平又存一次（走 PUT）', writes.map(w => w.method), ['POST', 'PUT'])
await check('页面上一个字都没丢', await rows().nth(0).locator('.td-sum textarea').inputValue(), '换个摘要')

// ---- 摘要写满一行 → 自动折成两行；两行也不许顶破格子、压到下一行 ----
{
  const row0 = rows().nth(0)
  const shortH = Math.round((await row0.locator('.td-sum textarea').boundingBox()).height)
  await row0.locator('.td-sum textarea').fill('这是一句特别长的摘要'.repeat(8)); await sleep(500)
  const longH = Math.round((await row0.locator('.td-sum textarea').boundingBox()).height)
  await check('摘要写满一行 → 自己长成两行', longH / shortH, 2)
  const gap = await row0.evaluate((el) => Math.round(el.nextElementSibling.getBoundingClientRect().top - el.getBoundingClientRect().bottom))
  await check('两行也没顶破格子、没压到下一行', gap, 0)
  await row0.locator('.td-sum textarea').fill('换个摘要'); await sleep(400)
  await check('改短了又收回一行', Math.round((await row0.locator('.td-sum textarea').boundingBox()).height), shortH)

  // 科目格跟摘要格是同一套折行口径：科目名也写满一行折两行。
  // 账上真正用得上的科目名没那么长（一格放得下 25 个字），
  // 得靠一长串假名字才逼得出第二行来
  const sub = row0.locator('.td-subj .sp-input')
  const sumTa = row0.locator('.td-sum textarea')
  const subShortH = Math.round((await sub.boundingBox()).height)
  await check('科目名只有一行时，跟同一行的摘要落在同一条基线上',
    Math.round((await sub.boundingBox()).y - (await sumTa.boundingBox()).y), 0)
  const LONG = '主营业务收入/管理费用明细科目超长名称测试折行一二三四五六七八九十'
  await sub.click(); await sleep(400)
  await sub.fill(LONG); await sleep(600)
  const subLongH = Math.round((await sub.boundingBox()).height)
  await check('科目名写满一行 → 跟摘要一样自己长成两行', subLongH / subShortH, 2)
  const subjBox = await row0.locator('.td-subj').boundingBox()
  await check('两行也没顶破这一格', subLongH <= Math.round(subjBox.height), true)
  if (await row0.locator('.td-subj .subj-balance').count()) {
    const balY = (await row0.locator('.td-subj .subj-balance').boundingBox()).y
    await check('两行贴上沿，没压到左下角的科目余额',
      Math.round((await sub.boundingBox()).y + subLongH) <= Math.round(balY), true)
  }
  // 框只占内容那几行，格子下半截（压着科目余额）空着 —— 点那儿也得算「点这一格」
  await page.mouse.click(subjBox.x + subjBox.width / 2, subjBox.y + subjBox.height - 8)
  await sleep(450)
  await check('点科目格下半截 → 焦点还是落在这一格',
    await sub.evaluate(e => e === document.activeElement), true)
  // 人已经在这格里待过了（刚打完字），这一下是要接着改，不是要重打：
  // 框只占内容那几行，点在框下面没有位置可落，光标落到末尾 ——
  // 跟单行输入框里点到右边空白是一个意思
  await check('点科目格下半截 → 光标落到末尾（不再替他整格选中）',
    await sub.evaluate(e => [e.selectionStart, e.selectionEnd]), [LONG.length, LONG.length])
  await check('点下半截不会把刚打的字退回成原来的科目', await sub.inputValue(), LONG)
  await page.keyboard.press('Escape'); await sleep(300)
  await sub.fill('库存现金'); await sleep(500)
  await check('科目名改短了又收回一行', Math.round((await sub.boundingBox()).height), subShortH)
}

// ---- 回车一路往下写：从上到下、从左到右，一格一格挪，不跳行 ----
const BASE_ROWS = await rows().count()
const focusIn = (sel) => page.locator(sel).evaluate(e => e === document.activeElement)
await check('只摆四行时不显示删除按钮', await rows().locator('.row-del').count(), 0)

await rows().nth(2).locator('.td-sum textarea').click(); await sleep(300)
await page.keyboard.press('Enter'); await sleep(500)
await check('摘要回车 → 落在同一行的会计科目格', await focusIn('.vg-row:not(.vg-head):not(.vg-total) >> nth=2 >> .sp-input'), true)
await check('不会平白多长出一行', await rows().count(), BASE_ROWS)
await check('也没有一头蹦到最后一行（第 5 行还不存在）', await page.locator('.td-sum textarea').count(), BASE_ROWS)
await page.keyboard.press('Escape'); await sleep(250)

await rows().nth(2).locator('.td-sum textarea').fill('备用金'); await sleep(200)
await rows().nth(2).locator('.td-sum textarea').click(); await sleep(300)
await page.keyboard.press('Enter'); await sleep(400)
// 1002 是上级：回车是**展开它**，不是选中它。一路回车往下钻到末级为止
await page.keyboard.type('1002'); await sleep(600)
await page.keyboard.press('Enter'); await sleep(400)
await check('停在上级上按回车 = 展开，面板不收（选中才会收）',
  await page.locator('.sp-panel').count(), 1)
await page.keyboard.press('ArrowDown'); await sleep(250)
await page.keyboard.press('Enter'); await sleep(500)
await check('钻到末级才选中', await rows().nth(2).locator('.sp-input').inputValue(), '银行存款/工商银行')
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
await fillAmt(rows().nth(2).locator('.td-amt.d .amt-input'), '50'); await sleep(300)
await rows().nth(2).locator('.td-amt.c .amt-input').click(); await sleep(300)
await page.keyboard.press('Enter'); await sleep(600)
await check('一行到头 → 下一行的摘要', await focusIn(R(3, '.td-sum textarea')), true)
await check('上一行的摘要自动带过来了', await rows().nth(3).locator('.td-sum textarea').inputValue(), '备用金')
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
await fillAmt(rows().nth(3).locator('.td-amt.d .amt-input'), '50'); await sleep(200)
await rows().nth(3).locator('.td-amt.c .amt-input').click(); await sleep(300)
await page.keyboard.press('Enter'); await sleep(800)
await check('科目+金额齐了才长出新行', await rows().count(), BASE_ROWS + 1)
await check('新行的光标落在摘要上', await focusIn('.vg-row:not(.vg-head):not(.vg-total) >> nth=4 >> .td-sum textarea'), true)

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
await check('新行落在序号 2，光标就在新行摘要上', await focusIn(R(1, '.td-sum textarea')), true)
await check('原来那一行被顶到序号 3，摘要没丢',
  await rows().nth(2).locator('.td-sum textarea').inputValue(), '备用金')

// 插出来的是个空行：它自己没写完，后面的行就都先不开张
await check('插出来的空行把后面挡住了', (await lockedCells(2))[0], true)
await check('它自己先抄了本格子的摘要', await rows().nth(1).locator('.td-sum textarea').inputValue(), '备用金')
await rows().nth(1).locator('.td-sum textarea').fill('备用金出账'); await sleep(200)
await pickSubject(1, '100201')
await fillAmt(rows().nth(1).locator('.td-amt.c .amt-input'), '50'); await sleep(400)
await check('它写完了，后面的行才开张', (await lockedCells(2)).slice(0, 4), [false, false, false, false])

// 往后再插：末行也得先成条
const last = (await rows().count()) - 1
await pickSubject(last, '100202')
await fillAmt(rows().nth(last).locator('.td-amt.c .amt-input'), '1'); await sleep(300)
await rows().nth(last).hover(); await sleep(250)
await rows().nth(last).locator('.g-add').click(); await sleep(600)
await check('下面的按钮在本行后面插一行', await rows().count(), BASE_ROWS + 2)
await check('它插在末行之下，新行就是末行', await focusIn(R(BASE_ROWS + 1, '.td-sum textarea')), true)

// 合计格只写「合计」两个字、靠右 + 借贷平衡提示
// 从下往上清：清空一行的金额，下面那行立刻就锁了，正着清会清到一半卡住
for (let i = (await rows().count()) - 1; i >= 0; i--) {
  for (const w of ['c', 'd']) {
    const el = rows().nth(i).locator(`.td-amt.${w} .amt-input`)
    if (await el.count() && await el.isEnabled()) { await fillAmt(el, ''); await sleep(60) }
  }
}
await sleep(600)
await check('清空后合计格只剩「合计」', (await page.locator('.vg-total .td-sumcell').textContent()).trim(), '合计')
// 量文字真身的位置，不看 text-align：格子是 flex 容器，摆位置的是
// justify-content，text-align 在这儿写着 right 字也照样杵在左边
await check('合计靠右', await page.locator('.vg-total .td-sumcell').evaluate((el) => {
  const rg = document.createRange(); rg.selectNodeContents(el)
  const t = rg.getBoundingClientRect()
  const pad = Math.round(el.getBoundingClientRect().right - t.right)
  return { pad, 贴右边: pad <= 20, 离左边: Math.round(t.left - el.getBoundingClientRect().left) > 200 }
}), { pad: 16, 贴右边: true, 离左边: true })
await check('一分钱没录时不报红字差额',
  (await page.locator('.foot-right').textContent()).includes('还没录金额'), true)
await fillAmt(rows().nth(0).locator('.td-amt.d .amt-input'), '1000'); await sleep(400)
await check('只填借方 → 报差额',
  (await page.locator('.foot-right .tag-bad').textContent()).trim(), '借方大 1,000.00')

// 借 1000 跟借 −1000 摆在一张凭证上：红字把 −1000 搬到对面去了，两栏都是 1000。
// 两栏一样大就是平的 —— 早先拿「带符号那一对是不是 0」再判一道，摆出来的却是
// 一句自相矛盾的红字「差额 0.00」，人只会以为程序算错了（2026-10-12 用户纠正）。
await fillAmt(rows().nth(1).locator('.td-amt.d .amt-input'), '-1000'); await sleep(400)
await check('借贷正负相抵 → 按平处理', (await page.locator('.foot-right .tag-ok').textContent()).trim(), '借贷平衡 ✓')
await check('这一状态下不报红字', await page.locator('.foot-right .tag-bad').count(), 0)
await check('合计行摆的也是 1000 / 1000',
  [await totalOf('d'), await totalOf('c')], ['1000.00', '1000.00'])
await fillAmt(rows().nth(1).locator('.td-amt.d .amt-input'), ''); await sleep(400)
await check('撤掉红字 → 回到「只填借方」的差额',
  (await page.locator('.foot-right .tag-bad').textContent()).trim(), '借方大 1,000.00')

await fillAmt(rows().nth(1).locator('.td-amt.c .amt-input'), '1200'); await sleep(400)
await check('贷方大的时候要说清是哪一边大（200 这个数不该凭空冒出来）',
  (await page.locator('.foot-right .tag-bad').textContent()).trim(), '贷方大 200.00')
await check('已自动保存那句要看得见：13px 加粗', await page.locator('.foot-right .saved-tag')
  .evaluate((el) => [getComputedStyle(el).fontSize, getComputedStyle(el).fontWeight]),
  ['13px', '700'])
await fillAmt(rows().nth(1).locator('.td-amt.c .amt-input'), '1000'); await sleep(400)
await check('借贷相等 → 显示平衡',
  (await page.locator('.foot-right .tag-ok').textContent()).trim(), '借贷平衡 ✓')
await check('录了钱也不回填大写金额', (await page.locator('.vg-total .td-sumcell').textContent()).trim(), '合计')

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
  // 期望值得跟应用一样顺着 parent_id 一级一级拼，不能只把名字里的横杠换成斜杠 ——
  // 「其他」这种名字里根本没有横杠的，光换横杠就拼不出「主营业务收入/其他」
  // /subjects 给的是**嵌套树**，先摊平、把 parent_id 补回来再拼名字
  const tree = await (await fetch(`/api/books/${book.id}/subjects`, { headers: h })).json()
  const subs = []
  const walk = (nodes, parent) => {
    for (const n of nodes) { subs.push({ ...n, parent_id: parent }); walk(n.children || [], n.id) }
  }
  walk(tree, null)
  const byId = new Map(subs.map(x => [x.id, x]))
  const fullName = (n) => {
    const chain = []
    const seen = new Set()
    for (let cur = n; cur && !seen.has(cur.id); ) {
      seen.add(cur.id); chain.unshift(cur)
      cur = cur.parent_id != null ? byId.get(cur.parent_id) : null
    }
    return chain.map((node, i) => {
      let name = String(node.name ?? '')
      const up = i > 0 ? String(chain[i - 1].name ?? '') : ''
      for (const sep of ['-', '/', '－']) {
        if (up && name.startsWith(up + sep)) { name = name.slice(up.length + sep.length); break }
      }
      return name.replace(/[-－]/g, '/')
    }).filter(Boolean).join('/')
  }
  return {
    id: v.id, entries: v.entries.length, posted: posted?.id ?? null,
    subs: v.entries.map(e => e.subject_id ? fullName(byId.get(e.subject_id)) : null)
  }
})
await page.goto(`${BASE}/#/vouchers/${old.id}`); await sleep(2000)
await check('老凭证补足 4 行', await rows().count(), Math.max(4, old.entries))
await check('补出来的空行不带摘要',
  (await page.locator('.td-sum textarea').evaluateAll(els => els.map(e => e.value)))
    .slice(old.entries).every(v => v === ''), true)
// 老数据的 subject_id 一直都在，格子空着是显示的问题：打开就该看见科目名
const shown = await page.locator('.sp-input').evaluateAll(els => els.map(e => e.value.trim()))
await check('老凭证的会计科目写在格子里', old.subs.every((s, i) => (s ? shown[i] === s : shown[i] === '')), true)
await page.screenshot({ path: OUT + 'ui-voucher-edit.png' })

if (old.posted) {
  await page.goto(`${BASE}/#/vouchers/${old.posted}`); await sleep(2000)
  await check('已记账凭证只读', await page.locator('.amt-input').first().isDisabled(), true)
  await page.screenshot({ path: OUT + 'ui-voucher-posted.png' })
}

// ==================================================== 标记科目时的余额 / 新增科目预填
// 这两段跑在一张全新凭证上：前面那几百行断言的状态别让它们碰坏
await page.goto(`${BASE}/#/vouchers/new`); await sleep(2200)

/** 第一行科目格左下角那个余额（没有就是空串） */
const subjBalance = () => page.evaluate(() =>
  document.querySelector('.vg-row:not(.vg-head):not(.vg-total) .subj-balance')?.textContent.trim() ?? '')

// ---- 标记科目的时候，角上那个余额先收起来 ----
// 人敲了一半（'10020'）时，角上那个数是**上一个**科目的余额：
// 摆在还没对上号的字旁边，人会照着它填金额 —— 数字本身没错，
// 错的是它挂在谁名下，等录完才发现基数挑错了科目，比一开始不显示还费事
{
  const cell = rows().nth(0).locator('.sp-input')
  await cell.click(); await sleep(400)
  await page.keyboard.press('ControlOrMeta+a'); await sleep(150)
  await page.keyboard.type('100201'); await sleep(650)
  await page.keyboard.press('Enter'); await sleep(500)
  await page.locator('.td-sum textarea').first().click(); await sleep(400)
  await check('先挂一个有余额的科目，余额摆出来了', (await subjBalance()) !== '', true)
  await cell.click(); await sleep(450)
  await check('点进来还没动手：这一格早选定过，余额照旧摆着',
    (await subjBalance()) !== '', true)
  await page.keyboard.press('ControlOrMeta+a'); await sleep(150)
  await page.keyboard.type('10020'); await sleep(600)
  await check('敲了一半、还没对上任何科目：余额收起来', await subjBalance(), '')
  await page.keyboard.type('1'); await sleep(500)
  await check('敲完整、跟已选的那个科目对上了：余额回来',
    (await subjBalance()) !== '', true)
  await page.keyboard.press('Escape'); await sleep(450)
  await check('Esc 取消编辑：余额回到这一格该有的那个数',
    (await subjBalance()) !== '', true)
  await cell.click(); await sleep(400)
  await page.keyboard.press('ControlOrMeta+a'); await sleep(150)
  await page.keyboard.type('kcxj'); await sleep(650)
  await page.keyboard.press('Enter'); await sleep(500)
  await check('收回原来的科目', await cell.inputValue(), '库存现金')
  await page.locator('.td-sum textarea').first().click(); await sleep(300)
}

/** 弹窗里某一栏读回来的值（下拉读选中的标签，文本框读 value） */
const dlgField = (label) => page.evaluate((label) => {
  const item = [...document.querySelectorAll('.el-dialog .el-form-item')]
    .find(i => i.querySelector('.el-form-item__label')?.textContent.trim() === label)
  if (!item) return null
  const sel = [...item.querySelectorAll('.el-select__selected-item')]
    .map(e => e.textContent.trim()).filter(Boolean).join('')
  if (sel) return sel
  const input = item.querySelector('input')
  return input ? input.value : null
}, label)
const dlgDirection = () => page.evaluate(() =>
  document.querySelector('.el-dialog .el-radio.is-checked')?.textContent.trim() ?? null)
async function openAddWith(kw, row = 0) {
  const cell = rows().nth(row).locator('.sp-input')
  await cell.click(); await sleep(400)
  await page.keyboard.press('ControlOrMeta+a'); await sleep(150)
  await page.keyboard.type(kw); await sleep(700)
  await page.locator('.sp-add').click(); await sleep(800)
}
async function closeAdd() {
  await page.locator('.el-dialog button:has-text("取消")').click(); await sleep(600)
}

// ---- 填了个查不到的编码再点「+ 点击添加」：整份表单照它铺 ----
// 人敲的是编码，上级就该顺着编码判出来（100204 → 1002 银行存款），
// 类别、余额方向再跟着上级走 —— 都是能推出来的，不该在弹窗里让人再敲一遍
await openAddWith('100204')
await check('编码留的是人敲的那一串', await dlgField('科目编码'), '100204')
await check('上级按编码判出来（100204 → 1002 银行存款）', await dlgField('上级科目'), '1002 银行存款')
await check('类别跟着上级走', await dlgField('科目类别'), '资产')
await check('余额方向跟着类别走', await dlgDirection(), '借')
await check('名称空着等人来起（没瞎填一个）', await dlgField('科目名称'), '')
await closeAdd()

// 判不出上级时，也要认得出来它跟谁是一挂的：601099 跟 6001 主营业务收入同为损益
await openAddWith('601099')
await check('判不出上级时就留空，不硬套一个不相干的',
  await dlgField('上级科目'), '请选择（留空则新建一级科目）')
await check('编码还是人敲的那一串', await dlgField('科目编码'), '601099')
await check('类别按编码开头认出来（60… 是损益）', await dlgField('科目类别'), '损益')
await check('余额方向跟着类别走（损益是贷）', await dlgDirection(), '贷')
await closeAdd()

// 敲的是名字不是编码：名字进名称框，编码自动接一个
await openAddWith('交通银行')
await check('敲的是名字 → 落到名称框', await dlgField('科目名称'), '交通银行')
await check('编码自动接一个（这里一级科目下一个是 0102）', await dlgField('科目编码'), '0102')
await closeAdd()

// 换上级时，人自己敲的编码不能被自动接号悄悄顶掉
await openAddWith('100204')
await page.locator('.el-dialog .el-select').first().click(); await sleep(500)
await page.locator('.el-tree-node:visible', { hasText: '1001 库存现金' }).first().click()
await sleep(600)
await check('换上级：人敲的编码留着，不被自动接号顶掉', await dlgField('科目编码'), '100204')
await page.locator('.el-dialog .el-select').first().click(); await sleep(600)   // 选完下拉会收起来，再挑一个得重新拉开
await page.locator('.el-tree-node:visible', { hasText: '1002 银行存款' }).first().click()
await sleep(600)
await check('再换一个上级：还是那串', await dlgField('科目编码'), '100204')
await closeAdd()

console.log(fails.length ? `\n${fails.length} 项没过：\n- ` + fails.join('\n- ') : '\n全部通过')
await browser.close()
process.exit(fails.length ? 1 : 0)
