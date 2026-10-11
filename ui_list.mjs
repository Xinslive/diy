// 凭证列表页的验收脚本：工具栏精简 + 筛选按期间 / 金额区间。
// 全部只读，不写库。
//   node ui_list.mjs
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:5173'
const OUT = fileURLToPath(new URL('./.shots/', import.meta.url))
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
const btn = (name) => page.locator('.toolbar').getByRole('button', { name, exact: true })

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
await page.waitForSelector('.el-aside', { timeout: 15000 })
await sleep(1600)
await page.locator('.topbar .el-select').first().click(); await sleep(800)
await page.locator('.el-select-dropdown__item:visible', { hasText: '启明物业' }).first().click()
await sleep(1600)

// 尺子：挑一个「上一期已结账、期间里真有凭证」的月份，把顶栏停在这儿。
// 「上一期已结账」这条现在只剩确认框措辞在用（记账按钮撤了之后），
// 留着是为了跑完一遍之后还能拿同一个期间的状态做前后对照。
const period = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const book = books.find(b => b.name === '启明物业')
  const ps = await (await fetch(`/api/books/${book.id}/periods`, { headers: h })).json()
  let best = null
  for (const p of ps) {
    const prev = ps.find(x => x.ord === p.ord - 1)
    if (!prev || !prev.locked) continue
    const q = await (await fetch(
      `/api/books/${book.id}/vouchers?from_period_id=${p.id}&to_period_id=${p.id}&page_size=200`,
      { headers: h })).json()
    const items = (q.items || []).filter(v => v.period_id === p.id)
    if (!items.length) continue
    best = {
      id: p.id, year: p.year, code: p.code, count: items.length,
      unposted: items.filter(v => v.status !== 'POSTED').map(v => v.id),
      posted: items.filter(v => v.status === 'POSTED').map(v => v.id)
    }
  }
  return best
})
if (!period) throw new Error('账里没有可用于验收的期间（上一期已结账 + 期间内有凭证）')
console.log(`   （尺子：${period.code} 共 ${period.count} 张，未记账 ${period.unposted.length} / 已记账 ${period.posted.length}）`)
await page.evaluate(id => localStorage.setItem('cwq_period', String(id)), period.id)
await page.reload({ waitUntil: 'networkidle' }); await sleep(2000)

await page.goto(`${BASE}/#/vouchers`); await sleep(2000)

// ---- 工具栏精简了：起止/全部期间/状态/来源四个控件撤掉，多了金额区间 ----
const bar = () => page.locator('.toolbar')
await check('工具栏里没有下拉了', await bar().locator('.el-select').count(), 0)
await check('也没有「全部期间」按钮', await bar().locator('button:has-text("全部期间")').count(), 0)
await check('金额区间在那儿', await bar().locator('.el-input-number').count(), 2)
await check('凭证号/摘要还在', await bar().locator('input[placeholder="凭证号 / 摘要"]').count(), 1)

// ---- 表格：科目行数、来源两列撤了，日期不许折行 ----
const heads = (await page.locator('.el-table__header th .cell').allTextContents()).map(t => t.trim())
await check('没有「科目行数」列', heads.includes('科目行数'), false)
await check('没有「来源」列', heads.includes('来源'), false)
await check('日期/凭证号/摘要/借贷合计/状态/操作都还在',
  ['日期', '凭证号', '摘要', '借方合计', '贷方合计', '状态', '操作'].filter(h => !heads.includes(h)), [])
// 列序不写死下标：先从表头认出「日期」在第几格，再拿同一个下标去取表体。
// 这样列一挪位，下面那条「第二格就是日期列」会自己炸出来，而不是悄悄量错格子
const headCells = page.locator('.el-table__header th')
const dateIdx = (await headCells.allTextContents()).findIndex(t => t.trim() === '日期')
await check('表头里有日期列', dateIdx >= 0, true)
await check('日期在勾选列后面', dateIdx, 1)
const dateCell = page.locator('.el-table__body-wrapper tbody tr').first().locator('td').nth(dateIdx)
await check('第二格就是日期列', /^\d{4}-\d{2}-\d{2}$/.test((await dateCell.textContent()).trim()), true)
// 表头宽度就是列宽（两边同源），量表体那一格反而容易被 sticky 列干扰
const dateHeadW = (await headCells.nth(dateIdx).boundingBox()).width
// 一行文字是一个 client rect；折成两行就是两个 —— 直接看渲染结果，不猜
await check('日期一行显示，没折行',
  await dateCell.locator('.cell').evaluate(e => e.getClientRects().length), 1)
await check('日期列加宽到放得下', dateHeadW >= 120, true)

// ---- 金额筛选：范围是全部期间，语义是「某一条分录的金额」 ----
// 界面上那个 20 万，是拿来问「哪张凭证里有一笔 20 万」，
// 不是「哪张凭证合计 20 万」—— 两者在借贷多行时结果差很远。
// 下面整段都在 API 层验，因为要拿到每一张凭证的分录明细才判得出来
const amountCheck = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const book = books.find(b => b.name === '启明物业')
  const get = (u) => fetch(u, { headers: h }).then(r => r.json())
  const ps = await get(`/api/books/${book.id}/periods`)
  const code = id => ps.find(p => p.id === id)?.code

  // page_size 上限 200（超了后端直接 422），这里老老实实翻页取全量
  const fetchAll = async (params = {}) => {
    const qs = new URLSearchParams(params)
    const out = []
    for (let page = 1; ; page++) {
      qs.set('page', page); qs.set('page_size', 200)
      const d = await get(`/api/books/${book.id}/vouchers?${qs}`)
      out.push(...(d.items || []))
      if (out.length >= (d.total || 0) || !(d.items || []).length) break
    }
    return out
  }

  const all = await fetchAll()
  // 先把账里所有分录的明细抓回来当底账，后面所有判断都基于它
  const detail = {}
  for (const v of all) {
    const d = await get(`/api/books/${book.id}/vouchers/${v.id}`)
    detail[v.id] = { total: Number(v.total_debit), period: code(v.period_id),
                     lines: (d.entries || []).map(e => Number(e.amount)) }
  }

  // 阈值不写死：现找一个「真能分辨单行 / 合计」的门槛出来。
  // 判据是账面自带 —— 存在合计 ≥ T 却没有一行 ≥ T 的凭证时，这个 T 才验得出东西。
  // 找不到就说明这本账里没有多行大额凭证，下面那条断言会空转（这里直接报出来）
  const trapsFor = (T) => Object.entries(detail)
    .filter(([, d]) => d.total >= T && !d.lines.some(a => a >= T)).map(([id]) => Number(id))
  const CANDIDATES = [150000, 200000, 120000, 100000, 80000, 250000, 300000]
  let T = 0, trap = []
  for (const c of CANDIDATES) { const t = trapsFor(c); if (t.length) { T = c; trap = t; break } }

  const hit = { items: await fetchAll({ min_amount: T }) }
  const hitIds = new Set(hit.items.map(v => v.id))

  // 一、筛出来的每一张，都必须真有至少一行 ≥ T（这就是「单行」语义）
  const bad = hit.items.filter(v => !detail[v.id].lines.some(a => a >= T)).map(v => v.id)
  // 二、反过来最狠的一条：合计够 T、但没有一行够 T 的凭证，一张都不该被捞出来。
  //    按「合计」语义写的话这些会全部混进结果里
  const leaked = trap.filter(id => hitIds.has(id))
  const sample = trap[0] ? `${trap[0]}：合计 ${detail[trap[0]].total}，最大行 ${Math.max(...detail[trap[0]].lines)}` : ''
  return {
    T, hitCount: hit.items.length, bad, leaked,
    trapTotal: trap.length, sample,
    span: [...new Set(hit.items.map(v => code(v.period_id)))].filter(Boolean),
  }
})
console.log(`   （阈值 ${amountCheck.T}：有 ${amountCheck.trapTotal} 张「合计够、没有一行够」的诱饵凭证` +
            (amountCheck.sample ? `，例如 ${amountCheck.sample}）` : '）'))
await check('这个阈值真能分辨单行 / 合计（存在诱饵凭证）', amountCheck.trapTotal > 0, true)
await check(`按单行金额筛，捞出来的每一张真有行 ≥${amountCheck.T}`, amountCheck.bad, [])
await check('合计够但没行够的凭证一张都没混进来', amountCheck.leaked, [])
await check('确实筛出了一些', amountCheck.hitCount > 0, true)
console.log(`   （有行 ≥${amountCheck.T} 的凭证横跨：` + amountCheck.span.join('、') + '）')
await check('金额筛选的范围是全部期间', amountCheck.span.length > 1, true)

// 界面这边也要跟着动一下：填 20 万点查询，表格不能空
const total = await page.locator('.el-table__body-wrapper tbody tr').count()
await page.locator('.toolbar .el-input-number input').first().fill('200000')
await bar().getByRole('button', { name: '查询', exact: true }).click(); await sleep(1500)
const shown = await page.locator('.el-table__body-wrapper tbody tr').count()
await check('界面上按 20 万筛出了凭证', shown > 0, true)
await page.locator('.toolbar .el-input-number input').first().fill('')
await sleep(200)
await bar().getByRole('button', { name: '查询', exact: true }).click(); await sleep(1500)

// ---- 记账 / 反记账撤了（期末处理的结账自带），「更多」下拉整个撤掉 ----
// 结算按钮不该在两个地方各摆一份。真正要记账的地方是期末处理。
await check('工具栏里没有「记账」', await bar().getByRole('button', { name: '记账', exact: true }).count(), 0)
await check('工具栏里没有「反记账」', await bar().getByRole('button', { name: '反记账', exact: true }).count(), 0)
await check('也没有「更多」下拉', await bar().getByRole('button', { name: /更多/ }).count(), 0)
await check('也没了「按科目重排选中凭证的分录」', await page.locator('text=按科目重排选中凭证的分录').count(), 0)

// 腾出来的宽度给了这两个：常用操作不该埋在二级菜单里
await check('「整理凭证」摆到明面上了', await bar().getByRole('button', { name: '整理凭证', exact: true }).count(), 1)
await check('「导入凭证」也摆到明面上了', await bar().getByRole('button', { name: '导入凭证', exact: true }).count(), 1)
await check('不再叫「从 Excel 导入」', await bar().getByRole('button', { name: '从 Excel 导入', exact: true }).count(), 0)

// 结算走期末处理那边 —— 这里点一下「整理凭证」应该真的跳过去
await bar().getByRole('button', { name: '整理凭证', exact: true }).click(); await sleep(1200)
await check('「整理凭证」跳到整理页', /#\/vouchers\/organize/.test(page.url()), true)

// ---- 整理凭证页：方式就两个，「断号检查」不再是选项，「范围」也撤了 ----
await sleep(1600)
await check('没有「范围」选择了', await page.locator('.el-form-item:has-text("范围")').count(), 0)
await page.locator('.el-form-item:has-text("方式") .el-select').click(); await sleep(700)
const strat = await page.locator('.el-select-dropdown__item:visible').allTextContents()
await check('方式只剩两条', strat, ['按日期升序重排并编号', '按当前顺序重新编号'])
await check('「断号检查」不再是整理方式', strat.some(t => t.includes('断号检查')), false)
await check('没有「按日期降序」这条规则了', strat.some(t => t.includes('降序')), false)
// 时序不对的月份不给选「按当前顺序」：那等于把乱的号段固化成整齐的乱。
// 这一条要跟体检结果对齐，所以直接问后端要一份，别把结论写死在脚本里 ——
// 真账的数据天天在变，写死某个月「应该是禁的」，改一天数据就成了空转。
const renumDis = await page.locator('.el-select-dropdown__item:visible', { hasText: '按当前顺序重新编号' })
  .evaluate(e => e.className.includes('is-disabled'))
const oo = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const book = localStorage.getItem('cwq_book'), pid = localStorage.getItem('cwq_period')
  const r = await fetch(`/api/books/${book}/vouchers/period-check?period_id=${pid}`, { headers: h })
  return !!(await r.json()).time_out_of_order?.length
})
await check('「按当前顺序重新编号」的禁用状态跟着体检结果走', renumDis, oo)
await page.keyboard.press('Escape'); await sleep(400)

// 对照表：表头和内容都居中，「手工」改叫「人工填制」
await page.click('button:has-text("查看方案")'); await sleep(2200)
const orgHeads = await page.locator('.centered th .cell').allTextContents()
await check('对照表第一列是「原凭证号」（之前这一列是空的）', orgHeads[0], '原凭证号')
await check('表头居中', await page.locator('.centered th .cell').first()
  .evaluate(e => getComputedStyle(e).textAlign), 'center')
await check('内容也居中', await page.locator('.centered td .cell').first()
  .evaluate(e => getComputedStyle(e).textAlign), 'center')
const firstNo = (await page.locator('.centered tbody tr').first().locator('td .cell').first().textContent()).trim()
await check('原凭证号那一列真的填上了号', /^\S+\d+$/.test(firstNo), true)
const sources = await page.locator('.centered tbody tr td:nth-child(5) .cell').allTextContents()
await check('不再叫「手工」，改叫「人工填制」',
  [sources.some(t => t.trim() === '手工'), sources.includes('人工填制')], [false, true])
// 结转损益必须排在号段末尾 —— 但只有这个月**确实有**结转凭证时才谈得上这一条。
// 有没有结转凭证取决于账跑到哪一步了，写死「最后一行必须是结转损益」，
// 换个干净的月份就成了一条空转的断言。
const carryIdx = sources.map(t => t.trim()).lastIndexOf('结转损益')
if (carryIdx >= 0) {
  await check('结转损益排在号段末尾', sources.slice(carryIdx).every(t => t.trim() === '结转损益'), true)
  await check('结转损益确实落在最后一行', carryIdx, sources.length - 1)
} else {
  console.log('   （这个月还没有结转凭证，「结转损益垫底」这两条不适用）')
}
await page.goto(`${BASE}/#/vouchers`); await sleep(1800)
await bar().getByRole('button', { name: '导入凭证', exact: true }).click(); await sleep(1200)
await check('「导入凭证」跳到导入页', /#\/vouchers\/import/.test(page.url()), true)
await page.goto(`${BASE}/#/vouchers`); await sleep(1800)

// ---- 没勾选凭证时删除仍然禁着（这条跟记账撤不撤无关） ----
await check('没勾选时删除还是禁着的', await btn('删除').isDisabled(), true)
await page.locator('.el-table__body-wrapper tbody tr').first().locator('.el-checkbox').click()
await sleep(600)
await check('勾选后有提示条', await page.locator('.el-alert:has-text("已选")').count(), 1)
await check('勾选后删除可点', await btn('删除').isDisabled(), false)
await page.locator('.el-table__body-wrapper tbody tr').first().locator('.el-checkbox').click()
await sleep(600)

// 真账里的凭证状态一张都不该被动过 —— 这一遍全程只读
const after = await page.evaluate(async (pid) => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const book = books.find(b => b.name === '启明物业')
  const q = await (await fetch(
    `/api/books/${book.id}/vouchers?from_period_id=${pid}&to_period_id=${pid}&page_size=200`,
    { headers: h })).json()
  const items = (q.items || []).filter(v => v.period_id === pid)
  return { unposted: items.filter(v => v.status !== 'POSTED').length,
           posted: items.filter(v => v.status === 'POSTED').length }
}, period.id)
await check('真账里的凭证状态一张没动',
  after, { unposted: period.unposted.length, posted: period.posted.length })

await page.screenshot({ path: OUT + 'ui-voucher-list.png' })
console.log(fails.length ? `\n${fails.length} 项没过：\n- ` + fails.join('\n- ') : '\n全部通过')
await browser.close()
process.exit(fails.length ? 1 : 0)
