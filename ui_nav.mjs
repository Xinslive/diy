// 列表 ⇄ 凭证之间的来回：默认范围、返回保留条件、前一张/后一张翻的是结果集。
// 全部只读，不写库。
//   node ui_nav.mjs
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:5173'
const OUT = fileURLToPath(new URL('./.shots/', import.meta.url))
const fails = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1560, height: 980 } })
page.on('pageerror', e => fails.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') fails.push('console: ' + m.text()) })
const bad = []
page.on('response', r => { if (r.status() >= 400) bad.push(r.status() + ' ' + r.url()) })
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
await page.waitForSelector('.el-aside', { timeout: 15000 })
await sleep(1600)
await page.locator('.topbar .el-select').first().click(); await sleep(800)
await page.locator('.el-select-dropdown__item:visible', { hasText: '启明物业' }).first().click()
await sleep(1600)

// 顶栏可能停在最新的那个年度（下一年的 12 期，提前就建出来了、里面一张凭证也没有）。
// 这里显式切到用户截图里的那个月，免得「无筛选只显示顶栏月」验在一个空月份上。
const bookId = await page.evaluate(() => localStorage.getItem('cwq_book'))
const wanted = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const ps = await (await fetch(`/api/books/${localStorage.getItem('cwq_book')}/periods`, { headers: h })).json()
  const p = ps.filter(x => x.vouchers > 0).sort((a, b) => b.vouchers - a.vouchers)[0]
  return p ? { id: p.id, code: p.code } : null
})
if (!wanted) throw new Error('这本账里一张凭证都没有，没法验收')
await page.evaluate(id => localStorage.setItem('cwq_period', String(id)), wanted.id)
await page.reload({ waitUntil: 'networkidle' }); await sleep(2000)
console.log(`   （顶栏定在 ${wanted.code}，该期 ${'有'}凭证）`)

const rows = () => page.locator('.el-table__body-wrapper tbody tr')
const periodCode = async () => {
  const y = (await page.locator('.topbar .el-select').nth(1).textContent()).trim()
  const m = (await page.locator('.topbar .el-select').nth(2).textContent()).trim()
  return `${y} ${m}`.trim()
}
const dateCol = async () => {
  // 等表格真的把行铺完再数 —— 刚导航完就数，拿到的是上一屏或半屏
  await rows().first().waitFor({ state: 'visible', timeout: 15000 }).catch(() => {})
  const heads = page.locator('.el-table__header th')
  const i = (await heads.allTextContents()).findIndex(t => t.trim() === '日期')
  for (let n = 0; n < 20; n++) {           // 行数连续两次一样才认，避开骨架屏→数据的抖动
    const a = await rows().evaluateAll((trs, idx) => trs.map(tr => {
      const td = tr.querySelectorAll('td')[idx]
      return td ? td.textContent.trim() : ''
    }), i)
    await sleep(300)
    const b = await rows().evaluateAll((trs, idx) => trs.map(tr => {
      const td = tr.querySelectorAll('td')[idx]
      return td ? td.textContent.trim() : ''
    }), i)
    if (a.length === b.length) return b
  }
  return []
}

// ---- 1) 什么都不填：只显示顶栏那个月 ----
await page.goto(`${BASE}/#/vouchers`); await sleep(1800)
const month = await periodCode()
const dates = await dateCol()
const prefix0 = wanted.code                     // '2026-12'，跟日期列头两位对齐
const crossMonth = dates.filter(d => d && !d.startsWith(prefix0))
console.log(`   （顶栏停在 ${month}，列表 ${dates.length} 行，跨月 ${crossMonth.length} 行）`)
await check('无筛选条件时只显示顶栏那个月的凭证', crossMonth, [])
await check('顶栏那个月确实有凭证', dates.length > 0, true)

// 换一个月，表也跟着换 —— 证明范围是跟着顶栏走的
const otherId = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const ps = await (await fetch(`/api/books/${localStorage.getItem('cwq_book')}/periods`, { headers: h })).json()
  const cur = localStorage.getItem('cwq_period')
  const p = ps.filter(x => String(x.id) !== cur && x.vouchers > 0)
    .sort((a, b) => (a.code < b.code ? 1 : -1))[0]
  return p ? { id: p.id, code: p.code } : null
})
if (otherId) {
  await page.evaluate(id => localStorage.setItem('cwq_period', String(id)), otherId.id)
  await page.reload({ waitUntil: 'networkidle' }); await sleep(2200)
  const d2 = await dateCol()
  console.log(`   （顶栏换到 ${otherId.code}，列表 ${d2.length} 行）`)
  await check(`顶栏换到 ${otherId.code} 后列表跟着换`,
    d2.filter(x => x && !x.startsWith(otherId.code)), [])
  await check('换月之后确实有数据', d2.length > 0, true)
  await page.evaluate(id => localStorage.setItem('cwq_period', String(id)), wanted.id)
  await page.reload({ waitUntil: 'networkidle' }); await sleep(2000)
}

// ---- 2) 填了条件：范围放宽到全部期间 ----
await page.locator('.toolbar input[placeholder="凭证号 / 摘要"]').fill('16')
await page.locator('.toolbar').getByRole('button', { name: '查询', exact: true }).click()
await sleep(1800)
const kwDates = await dateCol()
const kwMonths = [...new Set(kwDates.map(d => d.slice(0, 7)))]
console.log('   （凭证号/摘要含「16」跨了：' + kwMonths.join('、') + '）')
await check('填了条件就跨到全部期间', kwMonths.length > 1, true)

// 走的是哪一张，看 URL 里的 id —— 不能看凭证号：筛「凭证号含 16」筛出来的
// 全都叫 记0016，拿号码比对等于什么都没比
const curId = async () => (page.url().match(/#\/vouchers\/(\d+)/) || [])[1] || ''
const neighbour = async (which) => {
  const before = await curId()
  const b = page.locator('.toolbar').getByRole('button', { name: which, exact: true })
  if (await b.isDisabled()) return { moved: false, before, disabled: true }
  await b.click(); await sleep(1800)
  const after = await curId()
  return { moved: before !== after, before, after, url: page.url() }
}

// ---- 3) 进凭证 → 前一张/后一张翻的是结果集 ----
// 结果集是时间正序：列表第 2 行是 12 月，它的「前一张」应该是 11 月那张
const openRow = async (i) => {
  await rows().nth(i).getByRole('button', { name: /查看/ }).click()
  await sleep(2000)
}
await openRow(1)
const entryUrl = page.url()
console.log(`   （进入凭证 #${await curId()} ${(await page.locator('.toolbar input').first().inputValue()).trim()}，${entryUrl}）`)
await check('进凭证时把条件带上了', /kw=16/.test(entryUrl), true)

const back1 = await neighbour('前一张')
console.log(`   （前一张：#${back1.before} → #${back1.after}）`)
await check('前一张跳到了另一张凭证', back1.moved, true)
await check('前一张之后条件还在', /kw=16/.test(back1.url || ''), true)
const fwd1 = await neighbour('后一张')
console.log(`   （后一张：#${fwd1.before} → #${fwd1.after}）`)
await check('后一张能跳回来', fwd1.after, back1.before)

// 结果集最早那张：前一张该禁用
await page.goto(`${BASE}/#/vouchers?kw=16`); await sleep(1800)
await openRow((await rows().count()) - 1)
const firstNo = '#' + await curId()
const prevDisabled = await page.locator('.toolbar').getByRole('button', { name: '前一张', exact: true }).isDisabled()
console.log(`   （结果集最早那张是 ${firstNo}，前一张禁用：${prevDisabled}）`)
await check('结果集第一张的「前一张」禁用', prevDisabled, true)
await check('结果集第一张的「后一张」能走', (await neighbour('后一张')).moved, true)

// ---- 4) 返回：回列表页，且条件没丢 ----
await page.goto(`${BASE}/#/vouchers?kw=16`); await sleep(1800)
await openRow(0)
const no = '#' + await curId()
await page.locator('.toolbar').getByRole('button', { name: /返回/ }).click()
await sleep(2000)
console.log(`   （从 ${no} 点返回 → ${page.url()}）`)
await check('返回到了凭证列表页', /#\/vouchers(\?|$)/.test(page.url()), true)
await check('返回后筛选条件还在输入框里',
  await page.locator('.toolbar input[placeholder="凭证号 / 摘要"]').inputValue(), '16')
await check('返回后还是那批凭证', (await dateCol()).length, kwDates.length)

// 从别的入口直接打开的凭证，返回也必须回列表，而不是 history 上一页。
// id 现取：账里的凭证是本人天天在增删的，写死一个号迟早有一天 404
const anyId = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const d = await (await fetch(
    `/api/books/${localStorage.getItem('cwq_book')}/vouchers?page_size=1`, { headers: h })).json()
  return (d.items || [])[0]?.id || null
})
if (!anyId) throw new Error('账里一张凭证都没有')
await page.goto(`${BASE}/#/vouchers/${anyId}`); await sleep(2000)
await page.locator('.toolbar').getByRole('button', { name: /返回/ }).click()
await sleep(2000)
await check('直接打开的凭证，返回也回列表页', /#\/vouchers(\?|$)/.test(page.url()), true)

// ---- 5) 顶栏换月 → 条件栏清空，重新按那个月取 ----
await page.goto(`${BASE}/#/vouchers`); await sleep(1800)
const monthBox = () => page.locator('.topbar .el-select').nth(2)
await page.locator('.toolbar input[placeholder="凭证号 / 摘要"]').fill('16')
await page.locator('.toolbar .el-input-number input').first().fill('150000')
await page.locator('.toolbar').getByRole('button', { name: '查询', exact: true }).click()
await sleep(1800)
const before = (await dateCol()).length
const kwBefore = await page.locator('.toolbar input[placeholder="凭证号 / 摘要"]').inputValue()
await check('先确认条件是生效的', kwBefore, '16')

// 换个和当前不同的月份。选项要先**把下拉开着**再读 —— 关着的时候
// `.el-select-dropdown__item:visible` 一个都匹配不到，读到空数组，
// 后面 `hasText: undefined` 等于「包含空串」＝匹配全部，`.first()` 就随便点了第一个。
const curMonth = (await monthBox().textContent()).trim()
await monthBox().click(); await sleep(800)
const opts = (await page.locator('.el-select-dropdown__item:visible').allTextContents())
  .map(t => t.trim()).filter(Boolean)
const target = opts.find(t => t !== curMonth && !t.includes('已结账')) || opts.find(t => t !== curMonth)
if (!target) throw new Error(`期间下拉里除了当前 ${curMonth} 没有第二个可选项`)
await page.locator('.el-select-dropdown__item:visible', { hasText: target }).first().click()
await sleep(2000)
console.log(`   （顶栏 ${curMonth} → ${target}，切换前列表 ${before} 行）`)
await check('换月后凭证号条件清空了',
  await page.locator('.toolbar input[placeholder="凭证号 / 摘要"]').inputValue(), '')
await check('换月后金额条件也清空了',
  await page.locator('.toolbar .el-input-number input').first().inputValue(), '')
const afterDates = await dateCol()
const prefixNew = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const ps = await (await fetch(`/api/books/${localStorage.getItem('cwq_book')}/periods`, { headers: h })).json()
  return (ps.find(p => String(p.id) === localStorage.getItem('cwq_period')) || {}).code || ''
})
await check(`换月后只显示 ${prefixNew} 的凭证`,
  afterDates.filter(x => x && !x.startsWith(prefixNew)), [])

// ---- 6) 期间下拉里不再挂「未记账 N」 ----
await monthBox().click(); await sleep(700)
const optTexts = (await page.locator('.el-select-dropdown__item:visible').allTextContents())
  .map(t => t.trim())
const unpostedTags = optTexts.filter(t => t.includes('未记账'))
console.log('   （期间下拉选项：' + optTexts.slice(0, 4).join(' / ') + ' …）')
await check('期间下拉不再显示「未记账 N」', unpostedTags, [])
await check('已结账的标记还留着', optTexts.some(t => t.includes('已结账')), true)
await page.keyboard.press('Escape'); await sleep(500)

if (bad.length) console.log('   （异常响应：' + bad.join(' | ') + '）')
// ---- 7) 新开一张：凭证号和日期先替人填好 ----
// 期望值全部现从接口取：账里的凭证是本人天天在增删的，写死号迟早过期
const want = await page.evaluate(async (pid) => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const book = localStorage.getItem('cwq_book')
  const n = await (await fetch(`/api/books/${book}/vouchers/next?period_id=${pid}`, { headers: h })).json()
  return n
}, wanted.id)
// 顶栏先拨回那个**开着**的月份再点「新增凭证」：预填的号和日期都是按顶栏期间算的，
// 上一节把顶栏切到了 1 月（已结账），不拨回来预填出来的就是 1 月的号 ——
// 那是应用算对了、测试的尺子摆错了。
await page.evaluate(id => localStorage.setItem('cwq_period', String(id)), wanted.id)
await page.goto(`${BASE}/#/vouchers`)
await page.reload({ waitUntil: 'networkidle' })
await sleep(2200)
await page.locator('.toolbar').getByRole('button', { name: '新增凭证', exact: true }).click()
await sleep(2200)
const noField = page.locator('.toolbar input').first()
const dateField = page.locator('.toolbar .el-date-editor input').first()
console.log(`   （预填：凭证号 ${await noField.inputValue()}，日期 ${await dateField.inputValue()}；接口给的是 ${want.voucher_no} / ${want.voucher_date}）`)
await check('新开一张自动填了下一个凭证号', await noField.inputValue(), want.voucher_no)
await check('日期 = 这个期间最后一张凭证的日期', await dateField.inputValue(), want.voucher_date)

// 「增加凭证」/「插入凭证」走同一个页面，规则一样。
// 这两个按钮是 `!canAdd` 时禁用的（已记账 / 期间结账），所以得挑一张**没记账**的
// —— 直接拿列表第一行点进去，十有八九是已记账的，点了没反应，测试还会当成失败
const draft = await page.evaluate(async (pid) => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const book = localStorage.getItem('cwq_book')
  const d = await (await fetch(
    `/api/books/${book}/vouchers?status=UNPOSTED&from_period_id=${pid}&to_period_id=${pid}&page_size=1`,
    { headers: h })).json()
  return (d.items || [])[0]?.id || null
}, wanted.id)
// 这一段验的是「增加凭证 / 插入凭证」两个按钮 —— 它们 `!canAdd` 时是禁的，
// 而 canAdd 看的是**顶栏期间有没有结账**。账要是全部结完了，这条就没法验，
// 但那是账的状态，不是这两行的代码坏了。所以老实跳过，别把整套验收带崩。
if (!draft) {
  console.log('   （这本账里已经没有未结账、未记账的凭证了，「增加凭证 / 插入凭证」这两项跳过）')
} else {
// 顶栏先拨回那个**开着**的月份：`canAdd` 看的是 `store.currentPeriod.locked`，
// 不是这张凭证本身。上一步刚把顶栏切到 1 月（已结账），不拨回来两个按钮全是灰的，
// 点下去没反应，测试却会当成「按钮坏了」。
// 从「新增凭证」那张页直接换到草稿凭证；hash 变了但**不是整页刷新**，
// store 只在启动时读一次 localStorage，所以中途要 reload 才认新期间。
await page.goto(`${BASE}/#/vouchers/${draft}`)
await page.reload({ waitUntil: 'networkidle' })
await sleep(2200)
const addBtn = page.locator('.toolbar').getByRole('button', { name: '增加凭证', exact: true })
await check('未记账凭证上「增加凭证」是可点的', await addBtn.isDisabled(), false)
await addBtn.click()
await sleep(2200)
await check('「增加凭证」也自动填了凭证号', await noField.inputValue(), want.voucher_no)
await check('「增加凭证」的日期规则一样', await dateField.inputValue(), want.voucher_date)

// 「插入凭证」沿用当前那张的日期，但号码照样预填
await page.goto(`${BASE}/#/vouchers/${draft}`); await sleep(2200)
const curDate = await dateField.inputValue()
await check('（前置）这张凭证确实能开新凭证',
  await page.locator('.toolbar').getByRole('button', { name: '插入凭证', exact: true }).isDisabled(), false)
await page.locator('.toolbar').getByRole('button', { name: '插入凭证', exact: true }).click()
await sleep(2200)
await check('「插入凭证」沿用当前凭证的日期', await dateField.inputValue(), curDate)
await check('「插入凭证」也预填了凭证号', await noField.inputValue(), want.voucher_no)
}

// ---- 8) 凭证号三位 ----
await page.goto(`${BASE}/#/vouchers`); await sleep(1800)
const nos = (await page.locator('.el-table__body-wrapper tbody tr').evaluateAll(trs =>
  trs.map(tr => tr.querySelectorAll('td')[2].textContent.trim()))).filter(Boolean)
const four = nos.filter(n => /^\D\d{4,}$/.test(n))
console.log('   （本月凭证号：' + nos.slice(0, 5).join('、') + ' …）')
await check('新发的凭证号是三位', four, [])
await check('凭证号不为空', nos.every(n => n.length > 1), true)

await page.screenshot({ path: OUT + 'shot-nav.png' })
await browser.close()
console.log(fails.length ? `\n${fails.length} 项没过：\n- ` + fails.join('\n- ') : '\n全部通过')
process.exit(fails.length ? 1 : 0)
