import { chromium } from 'playwright'
const BASE = 'http://127.0.0.1:5173'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const errs = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } })
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
const sleep = ms => page.waitForTimeout(ms)
async function step(name, fn) {
  try { await fn(); console.log('✓', name) }
  catch (e) { console.log('✗', name, '—', e.message.split('\n')[0]); errs.push(name + ': ' + e.message.split('\n')[0]) }
}
const topbar = () => page.locator('.topbar .el-select')
async function pick(sel, text) {
  await sel.click(); await sleep(700)
  await page.locator('.el-select-dropdown__item:visible').filter({ hasText: text }).first().click()
  await sleep(1300)
}
async function gotoPeriod(year, month) {
  await pick(topbar().nth(1), String(year))
  await pick(topbar().nth(2), `${month} 月`)
  await sleep(800)
}
const btn = t => page.locator('.toolbar button').filter({ hasText: t }).first()

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
await page.waitForSelector('.el-aside', { timeout: 15000 })
await sleep(1500)

// 造一张 2026-12 的未记账临时凭证，用来验「回车加行」（已记账凭证是只读的）
const setup = await page.evaluate(async () => {
  const h = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const book = books.find(b => b.name === '启明物业')
  const subjects = await (await fetch(`/api/books/${book.id}/subjects`, { headers: h })).json()
  const sub = c => (subjects.items || subjects).find(s => s.code === c).id
  const made = await (await fetch(`/api/books/${book.id}/vouchers`, {
    method: 'POST', headers: h,
    body: JSON.stringify({
      voucher_date: '2026-12-05', remark: 'UI 验证用临时凭证', source: 'MANUAL',
      entries: [
        { subject_id: sub('1002'), direction: 'DEBIT', amount: '1.00', summary: '临时' },
        { subject_id: sub('6001'), direction: 'CREDIT', amount: '1.00', summary: '临时' },
      ],
    })
  })).json()
  return { bookId: book.id, id: made.id, no: made.voucher_no }
})
console.log('   临时凭证:', setup.no, '#' + setup.id)

await pick(topbar().nth(0), '启明物业')
await page.click('.el-menu-item:has-text("凭证")')
await sleep(1500)
await gotoPeriod(2026, 12)
await sleep(800)

await step('① 工具栏是四个按钮，没有「增加分录」', async () => {
  await page.goto(`${BASE}/#/vouchers/${setup.id}`, { waitUntil: 'networkidle' })
  await sleep(2000)
  const texts = await page.locator('.toolbar button').allInnerTexts()
  console.log('   工具栏按钮:', texts.map(t => t.trim()).join(' | '))
  for (const want of ['前一张', '后一张', '插入凭证', '增加凭证'])
    if (!texts.some(t => t.includes(want))) throw new Error('少了「' + want + '」按钮')
  if (texts.some(t => t.includes('增加分录'))) throw new Error('「增加分录」按钮还在')
})

await step('② 回车加一行分录', async () => {
  const before = await page.locator('.entries .el-table__body tr').count()
  const lastSummary = page.locator('.entries .el-table__body tr').last().locator('input').last()
  await lastSummary.click()
  await lastSummary.press('Enter')
  await sleep(900)
  const after = await page.locator('.entries .el-table__body tr').count()
  console.log(`   分录行数 ${before} → ${after}`)
  if (after !== before + 1) throw new Error(`回车没加行：${before} → ${after}`)
})

await step('③ 前一张 / 后一张能翻', async () => {
  await page.goto(`${BASE}/#/vouchers/${setup.id}`, { waitUntil: 'networkidle' })
  await sleep(2000)
  const urlOf = () => page.url().split('/').pop()
  const start = urlOf()
  const prevDis = await btn('前一张').isDisabled()
  const nextDis = await btn('后一张').isDisabled()
  console.log('   当前位置:', start, '| 前一张禁用:', prevDis, '| 后一张禁用:', nextDis)
  if (prevDis && nextDis) throw new Error('前后张都禁用，但这个期间明明有多张凭证')
  const go = await btn('后一张').isDisabled() ? btn('前一张') : btn('后一张')
  await go.click(); await sleep(1800)
  const now = urlOf()
  console.log('   翻到:', now)
  if (now === start || !/^\d+$/.test(now)) throw new Error(`没翻到别的凭证：${start} → ${now}`)
  // 再翻回去
  const back = now === String(setup.id) ? btn('前一张') : btn('后一张')
  await back.click(); await sleep(1500)
})

await step('④ 插入凭证：日期沿用当前凭证', async () => {
  await page.goto(`${BASE}/#/vouchers/${setup.id}`, { waitUntil: 'networkidle' })
  await sleep(2000)
  const srcDate = (await page.locator('.toolbar input').nth(1).inputValue())
  await btn('插入凭证').click(); await sleep(2000)
  if (!page.url().includes('/vouchers/new')) throw new Error('没跳到新增页: ' + page.url())
  const rows = await page.locator('.entries .el-table__body tr').count()
  console.log('   新增页: 凭证号空、日期 =', (await page.locator('.toolbar input').nth(1).inputValue()), '| 初始分录行 =', rows)
  if ((await page.locator('.toolbar input').nth(1).inputValue()) !== srcDate)
    throw new Error('插入凭证的日期没沿用当前凭证')
  if (rows !== 2) throw new Error('新增页应有 2 行空分录，实际 ' + rows)
  await page.screenshot({ path: `${OUT}/r3-new-voucher.png` })
})

await step('⑤ 增加凭证：另起一张', async () => {
  await page.goto(`${BASE}/#/vouchers/${setup.id}`, { waitUntil: 'networkidle' })
  await sleep(2000)
  await btn('增加凭证').click(); await sleep(2000)
  if (!page.url().includes('/vouchers/new')) throw new Error('没跳到新增页: ' + page.url())
  console.log('   新增页日期:', (await page.locator('.toolbar input').nth(1).inputValue()))
})

await step('⑥ 月份下拉里「已结账」与月份文字水平对齐', async () => {
  await page.click('.el-menu-item:has-text("凭证")'); await sleep(1500)
  await topbar().nth(2).click(); await sleep(900)
  // 注意：页面上同时存在好几个 select 的 popper，只能挑「月份」这个下拉的选项，
  // 不然会量到账套列表年度下拉里的同名标记。
  const boxes = await page.evaluate(() => {
    const out = []
    for (const opt of document.querySelectorAll('.el-select-dropdown__item')) {
      const wrap = opt.querySelector('.period-opt')
      const txt = opt.querySelector('.period-opt > span')
      const tag = opt.querySelector('.el-tag')
      if (!wrap || !txt || !tag) continue
      const a = txt.getBoundingClientRect(), b = tag.getBoundingClientRect()
      out.push({
        text: txt.textContent.trim(),
        dy: Math.abs((a.top + a.height / 2) - (b.top + b.height / 2)),
        gapToRight: Math.round(opt.getBoundingClientRect().right - b.right)
      })
    }
    return out
  })
  console.log('   带标记的选项:', boxes.map(b => `${b.text} 上下差 ${b.dy.toFixed(2)}px`).join(', '))
  console.log('   标签右边缘到选项右边缘:', boxes[0]?.gapToRight, 'px（应远大于 0，说明是贴右而非浮着）')
  if (!boxes.length) throw new Error('下拉里没有带「已结账」标记的选项')
  for (const b of boxes) if (b.dy > 1.5) throw new Error(`「${b.text}」的标记上下错开了 ${b.dy.toFixed(1)}px`)
  await page.screenshot({ path: `${OUT}/r3-period-tags.png` })
  await page.keyboard.press('Escape'); await sleep(400)
})

// 回滚
const cleanup = await page.evaluate(async (s) => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const r = await fetch(`/api/books/${s.bookId}/vouchers/${s.id}`, { method: 'DELETE', headers: h })
  return r.status
}, setup)
console.log('   临时凭证已删除:', cleanup)
if (cleanup !== 200) errs.push('临时凭证没删干净: ' + cleanup)

await browser.close()
if (errs.length) { console.log('\n❌ 有问题:\n' + errs.join('\n')); process.exit(1) }
console.log('\n✅ 本轮 6 项全部通过')
