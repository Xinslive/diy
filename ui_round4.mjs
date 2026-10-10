import { chromium } from 'playwright'
const BASE = 'http://127.0.0.1:5173'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const errs = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1560, height: 950 } })
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
const sleep = ms => page.waitForTimeout(ms)
async function step(name, fn) {
  try { await fn(); console.log('✓', name) }
  catch (e) { console.log('✗', name, '—', e.message.split('\n')[0]); errs.push(name + ': ' + e.message.split('\n')[0]) }
}

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
await page.waitForSelector('.el-aside', { timeout: 15000 })
await sleep(1800)

// 造一张 2026-12 的未记账凭证（该期间未结账，可编辑）
const setup = await page.evaluate(async () => {
  const h = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const book = books.find(b => b.name === '启明物业')
  const tree = await (await fetch(`/api/books/${book.id}/subjects`, { headers: h })).json()
  const flat = [...tree, ...tree.flatMap(x => x.children || [])]
  const sub = c => flat.find(s => s.code === c && s.is_leaf).id
  const r = await fetch(`/api/books/${book.id}/vouchers`, {
    method: 'POST', headers: h,
    body: JSON.stringify({
      voucher_date: '2026-12-05', remark: 'UI 验证', source: 'MANUAL',
      entries: [
        { subject_id: sub('100201'), direction: 'DEBIT', amount: '100', summary: '测试' },
        { subject_id: sub('600101'), direction: 'CREDIT', amount: '100', summary: '测试' },
      ],
    })
  })
  const v = await r.json()
  return { bookId: book.id, id: v.id, bank: sub('100201'), rev: sub('600101') }
})
console.log('   临时凭证 id =', setup.id)

await page.locator('.topbar .el-select').first().click(); await sleep(800)
await page.locator('.el-select-dropdown__item:visible', { hasText: '启明物业' }).first().click()
await sleep(1800)
await page.goto(`${BASE}/#/vouchers/${setup.id}`); await sleep(2500)

const row = (i) => page.locator('.entries .el-table__body tr').nth(i)
const amtIn = (i, which) => row(i).locator('.amt-input').nth(which)

await step('① 借方/贷方是两个独立列，不再有方向单选钮', async () => {
  const heads = (await page.locator('.entries .el-table__header th .cell').allInnerTexts()).map(t => t.trim())
  console.log('   列:', heads.join(' | '))
  if (!heads.includes('借方金额') || !heads.includes('贷方金额')) throw new Error('没有借方/贷方两列')
  if (heads.some(h => h.includes('方向'))) throw new Error('还有「方向」单选列')
  const radios = await page.locator('.entries .el-radio-group').count()
  if (radios) throw new Error('还有 ' + radios + ' 组方向单选钮')
  const order = heads.indexOf('摘要') < heads.indexOf('会计科目')
  if (!order) throw new Error('摘要应该在会计科目前面')
})

await step('② 金额式分栏表头 = 亿千百十万千百十元角分', async () => {
  const units = (await page.locator('.entries .amt-unit').allInnerTexts()).map(t => t.trim())
  const want = ['亿','千','百','十','万','千','百','十','元','角','分']
  console.log('   表头:', units.join(' '), '| 出现组数:', units.length / 11)
  if (units.slice(0, 11).join('') !== want.join('')) throw new Error('金额式表头不对: ' + units.join(''))
  if (units.slice(11).join('') !== want.join('')) throw new Error('贷方金额式表头不对: ' + units.slice(11).join(''))
  if (units.length !== 22) throw new Error('金额式表头应该只有首行各印一遍(借+贷共22)，实际 ' + units.length)
})

await step('③ 数字按金额式落格', async () => {
  // 借、贷两列各有 9 个数字格，得只取借方这一列（第 4 个 td）
  const drCell = row(0).locator('td').nth(3)
  const digits = (await drCell.locator('.amt-digit').allInnerTexts()).map(t => t.trim()).join('')
  const yuan = (await drCell.locator('.amt-yuan').innerText()).trim()
  const crYuan = (await row(0).locator('td').nth(4).locator('.amt-yuan').innerText()).trim()
  if (crYuan !== '0.00') throw new Error('贷方列应该是空的: ' + crYuan)
  console.log('   借方 亿~十 =', digits, '| 元角分 =', yuan)
  if (digits !== '000000100') throw new Error('借方数字没落对格: ' + digits)
  if (yuan !== '100.00') throw new Error('元角分不对: ' + yuan)
})

await step('④ 在借方填数 → 落 DEBIT；改到贷方 → 落 CREDIT', async () => {
  await amtIn(0, 0).fill('888.88'); await sleep(500)
  await amtIn(0, 1).fill(''); await sleep(300)
  await amtIn(0, 1).fill('888.88'); await sleep(500)
  const dr = (await row(0).locator('.amt-yuan').nth(0).innerText()).trim()
  const cr = (await row(0).locator('.amt-yuan').nth(1).innerText()).trim()
  console.log('   同一行：借方 =', dr, '| 贷方 =', cr)
  if (dr !== '0.00') throw new Error('改到贷方后借方没清空: ' + dr)
  if (cr !== '888.88') throw new Error('贷方没写上: ' + cr)
})

await step('⑤ 当前行淡绿高亮 + 金额格绿框', async () => {
  await amtIn(1, 1).click(); await sleep(600)
  const active = await page.locator('.entries .el-table__row.row-active').count()
  const boxed = await page.locator('.entries .amt.is-focus .amt-yuan.is-active').count()
  console.log('   高亮行数 =', active, '| 绿框金额格 =', boxed)
  if (active !== 1) throw new Error('当前行没高亮，行数 ' + active)
  if (!boxed) throw new Error('金额格没有聚焦绿框')
})

await step('⑥ 回车加一行 / ⊕ 在当前行下面插入', async () => {
  const before = await page.locator('.entries .el-table__body tr').count()
  const sum = row(1).locator('input').first()
  await sum.click(); await sum.press('Enter'); await sleep(800)
  const afterEnter = await page.locator('.entries .el-table__body tr').count()
  console.log(`   回车：${before} → ${afterEnter}`)
  if (afterEnter !== before + 1) throw new Error('回车没加行')
  await page.locator('.entry-gutter .g-ins').click(); await sleep(800)
  const afterIns = await page.locator('.entries .el-table__body tr').count()
  console.log(`   ⊕ 插入：${afterEnter} → ${afterIns}`)
  if (afterIns !== afterEnter + 1) throw new Error('⊕ 没插入行')
})

await step('⑦ 清空金额 → 合计写中文大写，并保留借贷平衡校验', async () => {
  const n = await page.locator('.entries .el-table__body tr').count()
  for (let i = 0; i < n; i++) {
    for (const w of [0, 1]) {
      const el = amtIn(i, w)
      if (await el.count()) { await el.fill(''); await sleep(120) }
    }
  }
  await sleep(400)
  const sum = (await page.locator('.sum-cn').innerText()).trim()
  const bal = (await page.locator('.totals .tag-ok, .totals .tag-bad').first().innerText()).trim()
  console.log('   合计:', sum, '| 校验:', bal)
  if (!/^合计：零元整$/.test(sum)) throw new Error('合计中文不对: ' + sum)
  if (!bal.includes('差额')) throw new Error('借贷不等时没有提示差额: ' + bal)
})

await step('⑧ 借贷平了才显示平衡', async () => {
  await amtIn(0, 0).fill('1000'); await sleep(500)
  let bal = (await page.locator('.totals .tag-ok, .totals .tag-bad').first().innerText()).trim()
  console.log('   只填借方 1000 →', bal)
  if (bal.includes('平衡')) throw new Error('只有借方 1000 居然显示平衡')
  await amtIn(1, 1).fill('1000'); await sleep(500)
  bal = (await page.locator('.totals .tag-ok, .totals .tag-bad').first().innerText()).trim()
  const sum = (await page.locator('.sum-cn').innerText()).trim()
  console.log('   补上贷方 1000 →', bal, '| 合计:', sum)
  if (!bal.includes('平衡')) throw new Error('借贷相等却没显示平衡: ' + bal)
  if (!sum.includes('壹仟元整')) throw new Error('合计中文不对: ' + sum)
})

await page.screenshot({ path: `${OUT}/r4-entry.png` })

// 回滚
const code = await page.evaluate(async (s) => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  return (await fetch(`/api/books/${s.bookId}/vouchers/${s.id}`, { method: 'DELETE', headers: h })).status
}, setup)
console.log('   临时凭证已删除:', code)
if (code !== 200) errs.push('临时凭证没删干净: ' + code)

await browser.close()
if (errs.length) { console.log('\n❌ 有问题:\n' + errs.join('\n')); process.exit(1) }
console.log('\n✅ 本轮 8 项全部通过')
