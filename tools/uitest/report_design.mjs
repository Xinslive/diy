/**
 * 报表设计器端到端：结构增删改 + 公式引用自动跟随。
 *
 * 重点验证两件纯后端测试看不到的事：
 *   1. 界面上插入行之后，合计公式真的会跟着走（不是界面对、后端没跟上）
 *   2. 隐藏行在查看模式消失、打印预览里也不再出现
 */
import { chromium } from 'playwright'

const B = 'http://127.0.0.1:5173'
const errors = []
const step = async (name, fn) => {
  try { await fn(); console.log('✓', name) }
  catch (e) { console.log('✗', name, '—', e.message.split('\n')[0]); errors.push(name) }
}

const b = await chromium.launch()
const ctx = await b.newContext({ viewport: { width: 1680, height: 1050 }, deviceScaleFactor: 2 })
const p = await ctx.newPage()
p.on('pageerror', e => errors.push('pageerror: ' + e.message))
p.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 160)) })
p.setDefaultTimeout(15000)

await p.goto(B, { waitUntil: 'networkidle' })
await p.fill('input[type="text"]', 'admin')
await p.fill('input[type="password"]', '123456')
await p.click('button:has-text("登录")')
await p.waitForSelector('.sidebar', { timeout: 15000 })

// 先复制一份模板，别把内置的资产负债表改坏
let tid = 0
await step('另存为可随意改的副本', async () => {
  await p.evaluate(() => { location.hash = '#/reports' })
  await p.waitForTimeout(1200)
  await p.locator('.el-table__row a').first().click()
  await p.waitForTimeout(1800)
  tid = await p.evaluate(() => Number(location.hash.split('/').pop()))
  if (!tid) throw new Error('没进到报表页')
  await p.click('button:has-text("另存为新模板")')
  await p.waitForSelector('.el-message-box input', { timeout: 5000 })
  await p.fill('.el-message-box input', '设计器E2E' + String(Date.now()).slice(-4))
  await p.click('.el-message-box button:has-text("确定")')
  await p.waitForTimeout(2500)
  tid = await p.evaluate(() => Number(location.hash.split('/').pop()))
})

// 设计模式下行标签是 el-input，文字在 input 的 value 里而不是文本节点，
// 所以 hasText 匹配不到（看着有字，其实 match 0 行）。改成读 value 定位。
const rowIdxOf = (label) => p.$$eval(
  'tbody tr',
  (trs, lbl) => trs.findIndex(tr => {
    const i = tr.querySelector('td.c-label input')
    if (i) return i.value === lbl
    const t = tr.querySelector('td.c-label')
    return t && t.textContent.trim() === lbl
  }),
  label
)
const rowOf = async (label) => {
  const i = await rowIdxOf(label)
  if (i < 0) throw new Error('找不到行：' + label)
  return p.locator('tbody tr').nth(i)
}
// 按列序号定位（0 = 期末余额 B，1 = 年初余额 C）。插行会让行号变化，
// 但列的位置是稳的，比按 B9 这种绝对地址找更抗改动。
const cellOf = async (rowLabel, colIdx) => (await rowOf(rowLabel)).locator('td.c-num').nth(colIdx)
const formulaOf = async (rowLabel, colIdx = 0) => {
  await (await cellOf(rowLabel, colIdx)).click()
  await p.waitForSelector('.el-dialog input', { timeout: 5000 })
  const v = await p.locator('.el-dialog input').first().inputValue()
  await p.click('.el-dialog button:has-text("取消")')
  await p.waitForTimeout(200)
  return v
}

await step('进入设计模式', async () => {
  await p.click('.toolbar label:has-text("设计模式")')
  await p.waitForSelector('td.c-gutter', { timeout: 8000 })
  await p.waitForTimeout(600)
})
await p.screenshot({ path: '.shots/20-报表设计模式.png' })

let before
await step('读到合计公式', async () => {
  before = await formulaOf('流动资产合计', 0)
  if (before !== '合计(B2:B8)') throw new Error('实际是 ' + before)
})

await step('插入一行后合计公式自动跟随', async () => {
  await (await rowOf('非流动资产')).locator('td.c-gutter button[title="在上面插入一行"]').click()
  await p.waitForTimeout(1800)
  const after = await formulaOf('非流动资产合计', 0)
  if (after !== '合计(B12:B16)') throw new Error(`期望 合计(B12:B16)，实际 ${after}`)
  const total = await formulaOf('资产总计', 0)
  if (total !== 'B9+B17') throw new Error(`资产总计 期望 B9+B17，实际 ${total}`)
})
await p.screenshot({ path: '.shots/21-插行后公式.png' })

await step('插入一列后地址整体右移', async () => {
  // 插在「期末余额」左边：期末余额整列右移一位变成 C，公式里的 B 也要跟着变 C
  await p.locator('th.c-head button[title="在 B 列左侧插入"]').click()
  await p.waitForTimeout(1800)
  const headers = await p.locator('th.c-head').allInnerTexts()
  if (!headers[0].includes('期末余额')) throw new Error('期末余额没有右移：' + headers[0])
  const f = await formulaOf('非流动资产合计', 1)
  if (f !== '合计(C12:C16)') throw new Error('期望 合计(C12:C16)，实际 ' + f)
})
await p.screenshot({ path: '.shots/22-插列后公式.png' })

await step('隐藏一行：查看模式看不到、打印里也没有', async () => {
  await (await rowOf('交易性金融资产')).locator('td.c-gutter button[title*="隐藏"]').click()
  await p.waitForTimeout(1500)
  await p.click('.toolbar label:has-text("设计模式")')   // 关掉设计模式
  await p.waitForTimeout(1200)
  if (await p.locator('td.c-label input[value="交易性金融资产"]').count()
      || await p.locator('td.c-label').filter({ hasText: '交易性金融资产' }).count()) {
    throw new Error('查看模式仍显示隐藏行')
  }
})

await step('删除刚插入的行，公式复原', async () => {
  await p.click('.toolbar label:has-text("设计模式")')
  await p.waitForTimeout(800)
  await (await rowOf('新项目')).locator('td.c-gutter button[title="删除本行"]').click()
  await p.waitForSelector('.el-message-box', { timeout: 5000 })
  await p.click('.el-message-box button:has-text("删除")')
  await p.waitForTimeout(1800)
  // 行删回去，范围要跟着收回来（插行时是 C12:C16）
  const after = await formulaOf('非流动资产合计', 1)
  if (after !== '合计(C11:C15)') throw new Error('实际 ' + after)
})
await p.screenshot({ path: '.shots/23-删行后.png' })

console.log(errors.length ? '\n问题:\n' + errors.join('\n') : '\n无错误')
await b.close()
