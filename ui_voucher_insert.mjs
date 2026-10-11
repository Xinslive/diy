// 凭证录入页这四条新口径的验收脚本：只读 + 内存里的临时操作，不落库。
//   1. 摘要格点一下整格选中
//   2. 金额格 ↑/↓ 在上下行之间跳
//   3. 金额格敲「=」自动配平；负数红字；红字算到对面去的合计口径
//   4. 插入凭证：拿参照的号、说明条、保存时带 insert_after
// 写接口一律拦下来（记一笔再伪造回包），所以不会往真账里塞凭证。
//   node ui_voucher_insert.mjs
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:5173'
const OUT = fileURLToPath(new URL('./.shots/', import.meta.url))
const FAKE_ID = 987655
const fails = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1560, height: 980 } })
page.on('pageerror', e => fails.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') fails.push('console: ' + m.text()) })
const sleep = ms => page.waitForTimeout(ms)
const rows = () => page.locator('.vg-row:not(.vg-head):not(.vg-total)')
const val = () => page.evaluate(() => document.activeElement?.value ?? '')
/** 第 i 行某一侧是不是当前焦点所在 */
const at = (i, side) => rows().nth(i).evaluate(
  (el, sd) => document.activeElement === el.querySelector(`.td-amt.${sd} .amt-input`), side)
/** 焦点格里的选中范围（跳过去就是选中） */
const selection = () => page.evaluate(() => {
  const el = document.activeElement
  return [el.selectionStart, el.selectionEnd, el.value]
})
async function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  console.log(ok ? '✓' : '✗', name, ok ? '' : `— 实际 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`)
  if (!ok) fails.push(`${name}: ${JSON.stringify(got)} != ${JSON.stringify(want)}`)
}

/** 写接口拦下来：记一笔再回一个假的凭证 */
const writes = []
let insertPreview = null
await page.route('**/api/books/*/vouchers**', async (route) => {
  const req = route.request()
  const url = req.url()
  if (url.includes('/neighbour')) {
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ prev_id: null, next_id: null }) })
  }
  if (url.includes('/insert-preview')) {
    // 这个口是只读的（只算不改），放行真的后端，好验顺位那句话对不对
    return route.continue()
  }
  if (req.method() === 'GET') return route.continue()
  writes.push({ method: req.method(), body: JSON.parse(req.postData() || '{}') })
  await route.fulfill({
    status: req.method() === 'POST' ? 201 : 200,
    contentType: 'application/json',
    body: JSON.stringify({
      id: FAKE_ID, voucher_no: '记006', voucher_date: '2026-12-11', status: 'UNPOSTED',
      remark: '', attachments: 0, period_id: 0, total_debit: '0', total_credit: '0',
      entries: [{ id: 1, line_no: 1, subject_id: 1, subject_code: '', subject_name: '',
        direction: 'DEBIT', amount: '0', summary: '' }]
    })
  })
})

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

// ==================================================== 1. 摘要：点一下整格选中
await page.goto(`${BASE}/#/vouchers/new`); await sleep(2000)
await rows().nth(0).locator('.td-sum input').click()
await page.keyboard.type('房租收入'); await sleep(300)
// 点在文字中间：光标不该停在那儿，整句都该选中
const box = await rows().nth(0).locator('.td-sum input').boundingBox()
await page.mouse.click(box.x + box.width * 0.5, box.y + box.height / 2); await sleep(250)
await check('点摘要格整格选中', await page.evaluate(() => {
  const el = document.activeElement
  return [el.selectionStart, el.selectionEnd, el.value]
}), [0, 4, '房租收入'])
await page.keyboard.press('Backspace'); await sleep(250)
await check('一键就删空了', await rows().nth(0).locator('.td-sum input').inputValue(), '')
await rows().nth(0).locator('.td-sum input').fill('房租收入'); await sleep(200)

// ==================================================== 2. 金额格 ↑/↓ 换行
await pickSubject(0, 'kcxj')
await rows().nth(0).locator('.td-amt.d .amt').click(); await sleep(250)
await page.keyboard.type('100'); await sleep(250)
await pickSubject(1, 'kcxj')
await rows().nth(1).locator('.td-amt.d .amt').click(); await sleep(250)
await page.keyboard.type('200'); await sleep(250)
await pickSubject(2, '600102')
await rows().nth(2).locator('.td-amt.c .amt').click(); await sleep(300)
await check('光标在第 3 行贷方格', await at(2, 'c'), true)

await page.keyboard.press('ArrowUp'); await sleep(350)
await check('↑ 跳到上一行的贷方格（同一侧）', await at(1, 'c'), true)
await page.keyboard.press('ArrowUp'); await sleep(350)
await check('再 ↑ 到第 1 行，仍是贷方', await at(0, 'c'), true)
await check('跳过去就是整格选中', await selection(), [0, 0, ''])
await page.keyboard.press('ArrowDown'); await sleep(350)
await check('↓ 回到第 2 行且仍是贷方', await at(1, 'c'), true)
await page.keyboard.press('ArrowDown'); await sleep(350)
await check('再 ↓ 回第 3 行', await at(2, 'c'), true)
await page.keyboard.press('ArrowUp'); await page.keyboard.press('ArrowUp'); await sleep(350)
await check('到第一行再 ↑ 就停在原地', await at(0, 'c'), true)

// 跳过去之后是选中：敲一下即重打（拿第 3 行试，改完再改回来）
await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowDown'); await sleep(300)
await page.keyboard.type('9'); await sleep(300)
await check('跳过去就是选中，敲一下即重打', await rows().nth(2).locator('.td-amt.c .amt-input').inputValue(), '9')
await page.keyboard.press('Backspace'); await sleep(250)

// ==================================================== 3. 「=」自动配平 + 红字
const foot = async () => (await page.locator('.vfoot').innerText()).replace(/\s+/g, ' ')
// 借 100 / 借 200，第三行（空行）往贷方敲 = → 补 300
await rows().nth(2).locator('.td-amt.c .amt').click(); await sleep(300)
await page.keyboard.type('='); await sleep(600)
await check('空行往贷方敲「=」补上差额 300', await rows().nth(2).locator('.td-amt.c .amt-input').inputValue(), '300')
await check('补完就平了', (await foot()).includes('借贷平衡'), true)
await check('合计中文也补上了', await page.locator('.td-sumcell').innerText(), '合计：叁佰元整')

// 已经补对了还敲 =：数字不变，也不啰嗦
await page.keyboard.press('='); await sleep(700)
await check('已补对还敲「=」不乱写', await rows().nth(2).locator('.td-amt.c .amt-input').inputValue(), '300')
await check('也不再多嘴报一遍',
  (await page.locator('.el-message').allTextContents()).some(t => t.includes('已按差额补上 300')), true)

// ---- 方向会翻的那一格：第三行借方已经有 200，往贷方敲 = 应补 300 而不是 500
await rows().nth(2).locator('.td-amt.c .amt').click(); await sleep(250)
await page.keyboard.type('Backspace'); await sleep(300)
await rows().nth(2).locator('.td-amt.d .amt').click(); await sleep(250)
await page.keyboard.type('200'); await sleep(500)
await check('第三行现在借方 200', await foot().then(t => t.includes('借方合计 500.00')), true)
await rows().nth(2).locator('.td-amt.c .amt').click(); await sleep(250)
await page.keyboard.type('='); await sleep(700)
await check('往贷方敲「=」补 300（这一行从借 200 变成贷 300）',
  await rows().nth(2).locator('.td-amt.c .amt-input').inputValue(), '300')
await check('这一行的 200 没有被顶掉：借方合计还是 300', (await foot()).includes('借方合计 300.00'), true)
await check('补完正好平', (await foot()).includes('借贷平衡'), true)
await page.screenshot({ path: OUT + 'voucher-balance-flip.png' })

// ---- 补一行（有科目、没金额）+ 已平 → 这一格该空着，说一声已经平了
await pickSubject(3, '600102')
await rows().nth(3).locator('.td-amt.c .amt').click(); await sleep(300)
await page.keyboard.type('='); await sleep(800)
await check('空行 + 已平：这一格空着', await rows().nth(3).locator('.td-amt.c .amt-input').inputValue(), '')
await check('并说一声已经平了',
  (await page.locator('.el-message').allTextContents()).some(t => t.includes('已经平了')), true)
await rows().nth(3).locator('.td-sum input').fill(''); await sleep(300)

// ---- 改成红字负数：合计按「负数算到对面」的口径
await rows().nth(2).locator('.td-amt.c .amt').click(); await sleep(250)
await page.keyboard.type('-300'); await sleep(700)
await check('负数落在贷方格里', await rows().nth(2).locator('.td-amt.c .amt-input').inputValue(), '-300')
await check('红字是红的', await page.evaluate(() => getComputedStyle(
  document.querySelectorAll('.vg-row')[3].querySelector('.td-amt.c .amt-cell')).color),
  'rgb(217, 48, 37)')
await check('借 100+200、贷 -300 → 借方合计 600', (await foot()).includes('借方合计 600.00'), true)
await check('贷方合计 0', (await foot()).includes('贷方合计 0.00'), true)
await check('差额 600', (await foot()).includes('差额 600.00'), true)
await check('合计行不再是一片红（负数已经算到对面）',
  await page.evaluate(() => document.querySelector('.vg-total .td-total.c .amt-cells')?.classList.contains('is-red')), false)
await page.screenshot({ path: OUT + 'voucher-red-negative.png' })

// 红字之后敲「=」：同侧重填，贷方变成 +300，平
await page.keyboard.press('='); await sleep(700)
await check('红字之后敲「=」同侧重填成 +300', await rows().nth(2).locator('.td-amt.c .amt-input').inputValue(), '300')
await check('红字之后敲「=」也能平', (await foot()).includes('借贷平衡'), true)

// ==================================================== 4. 插入凭证
await page.goto(`${BASE}/#/vouchers`); await sleep(2500)
const firstRow = page.locator('.el-table__body tr').first()
await firstRow.dblclick(); await sleep(2500)
const url = page.url()
const anchorId = Number(url.split('/').pop())
await check('打开了一张已存在的凭证', anchorId > 0, true)
const anchorNo = await page.locator('.toolbar input').first().inputValue()
const anchorDate = await page.locator('.toolbar input').nth(1).inputValue()

await page.click('button:has-text("插入凭证")'); await sleep(2500)
await check('跳到新凭证页且带着参照凭证', page.url().includes(`after=${anchorId}`), true)
await check('新页拿的是参照凭证的号', await page.locator('.toolbar input').first().inputValue(), anchorNo)
await check('日期沿用参照凭证', await page.locator('.toolbar input').nth(1).inputValue(), anchorDate)
// 页面上不摆任何说明条：插入这件事靠「号已经是插入位那个号」自己说话
await check('插入页不出现说明条', await page.locator('.insert-note').count(), 0)
await check('凭证号格只剩「留空按插入位发号」这一句提示',
  await page.locator('.toolbar input').first().getAttribute('placeholder'), '留空按插入位发号')

// 填平它 → 自动保存带上 insert_after
const writesBefore = writes.length
await rows().nth(0).locator('.td-amt.d .amt').click(); await sleep(250)
await page.keyboard.type('10'); await sleep(250)
await pickSubject(0, 'kcxj')
await pickSubject(1, '600102')
await rows().nth(1).locator('.td-amt.c .amt').click(); await sleep(250)
await page.keyboard.type('10'); await sleep(2000)
const created = writes.slice(writesBefore).find(w => w.method === 'POST')
await check('保存时带了 insert_after', created?.body?.insert_after, anchorId)
await check('凭证号就是插入位', created?.body?.voucher_no, anchorNo)

// 中途没填完就离开：不写库，别人的号原封不动
await page.goto(`${BASE}/#/vouchers/new`); await sleep(2000)
const before = writes.length
await page.goto(`${BASE}/#/vouchers`); await sleep(1500)
await check('空着退出不发任何写请求', writes.length, before)

await browser.close()
console.log('\n' + (fails.length ? `✗ ${fails.length} 项没过：\n  - ` + fails.join('\n  - ') : '✓ 全部通过'))
process.exit(fails.length ? 1 : 0)
