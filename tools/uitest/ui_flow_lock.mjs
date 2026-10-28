/**
 * 流水对账「**已入账不许再指定科目 / 忽略之后还要点得回来**」的界面自查。
 *
 * 这两条都是用户在实际使用里撞出来的：
 *
 * 1. 「流水明细点击忽略之后怎么没办法点击恢复？」—— 点完忽略，按钮自己把自己
 *    禁用了（文案写着「恢复」，却永远点不动）。
 * 2. 「状态是已入账的流水，设置成不可以再指定科目了！」—— 已入账的流水，
 *    科目**由凭证定死**：只改流水行、不改凭证，两边就对不上，而且界面上看
 *    不出来。所以按钮要灰，并写明出路。
 *
 * **它自己造数据、自己清理**（``seed_flow_lock_case.py``）：dev 库里加两条带
 * 标记的流水 —— 一条 OPEN（验忽略/恢复）、一条 POSTED（验对方科目按钮）。
 * 自然数据里「已入账的行」一条都没有，不造的话这项检查就**空转即绿**。
 *
 * 用法：node tools/uitest/ui_flow_lock.mjs
 */
import { chromium } from 'playwright'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '../..')
const PY = path.join(ROOT, 'backend/.venv/bin/python')
const SEED = path.join(HERE, 'seed_flow_lock_case.py')
const BASE = 'http://127.0.0.1:5173'
const OUT = path.join(ROOT, '.shots')

const errors = []
const ok = (n) => console.log('✓', n)
const bad = (n, d) => { console.log('✗', n, '—', d); errors.push(`${n}: ${d}`) }
async function step(name, fn) {
  try { await fn(); ok(name) } catch (e) { bad(name, e.message.split('\n')[0]) }
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1539, height: 954 } })
page.on('pageerror', (e) => bad('JS error', e.message))
const sleep = (ms) => page.waitForTimeout(ms)

/** 找到那一行（数据行，不是展开行）。 */
async function rowByText(summaryPart) {
  const row = page.locator('.flow-rows-table tbody tr.el-table__row')
    .filter({ hasText: summaryPart })
  if (await row.count() !== 1) {
    throw new Error(`没找到「${summaryPart}」那一行（${await row.count()} 行）`)
  }
  return row.first()
}

/**
 * 展开那一行，返回**展开内容**所在的那一行。
 *
 * **展开内容在紧跟数据行之后的另一个 <tr> 里**（Element Plus 的展开行是
 * 独立的一行），不在数据行自己身上 —— 找错地方会以为按钮不见了。
 *
 * **已经展开着就直接返回**：点「忽略」会重新拉一遍数据、重建表格，
 * 展开状态随之丢失（有的情况下又还在），所以每一步都要重新确认一次，
 * 不能假设它开着或关着。
 */
async function expandRow(summaryPart) {
  const row = await rowByText(summaryPart)
  // **只认「里面含 .flow-extra」的那一行** —— 别把下一个数据行当成展开行。
  const expanded = row.locator(
    'xpath=following-sibling::tr[.//*[contains(@class,"flow-extra")]][1]')
  if (await expanded.locator('.flow-extra').count() === 1) return expanded
  const arrow = row.locator('.el-table__expand-icon').first()
  if (!(await arrow.count())) throw new Error(`「${summaryPart}」那一行没有展开箭头`)
  await arrow.click()
  await sleep(1200)
  if (await expanded.locator('.flow-extra').count() !== 1) {
    throw new Error('展开内容不在紧跟其后的那一行里')
  }
  return expanded
}

/** 读一颗按钮的文案与禁用状态。 */
const buttonState = (locator) => locator.evaluate((b) => ({
  text: (b.innerText || '').trim(),
  disabled: b.disabled === true || b.getAttribute('disabled') !== null
    || b.classList.contains('is-disabled'),
}))

try {
  console.log('— 造数据 —')
  execFileSync(PY, [SEED, '--seed'], { cwd: ROOT, stdio: 'inherit' })

  await page.goto(BASE, { waitUntil: 'networkidle' })
  if (await page.locator('input[type=password]').count()) {
    await page.locator('input').nth(0).fill('admin')
    await page.locator('input').nth(1).fill('123456')
    await page.click('button:has-text("登录")')
    await page.waitForURL(/#\/(books|login)/, { timeout: 20000 })
    await sleep(1500)
  }

  await step('进入账套', async () => {
    const enter = page.locator('button:has-text("进入账套")')
    if (await enter.count()) { await enter.first().click(); await page.waitForTimeout(1500) }
  })
  await step('打开流水对账页', async () => {
    await page.locator('.modules button:has-text("凭证处理")').first().click()
    await page.waitForTimeout(900)
    await page.locator('.sidebar li').filter({ hasText: '流水对账' }).first().click()
    await page.waitForTimeout(1500)
  })
  await step('切到流水明细并选中对账单', async () => {
    await page.click('.el-tabs__item:has-text("流水明细")')
    await page.waitForTimeout(800)
    await page.locator('.flow-select').click()
    await page.waitForTimeout(500)
    await page.locator('.el-select-dropdown__item:visible').first().click()
    await page.waitForTimeout(1500)
  })

  // ---- 1) 忽略之后，「恢复」必须点得动 ----
  await step('OPEN 的行：忽略按钮可点', async () => {
    const extra = await expandRow('可忽略的流水')
    const btn = extra.locator('button:has-text("忽略")')
    if (await btn.count() !== 1) throw new Error('没找到「忽略」按钮')
    const st = await buttonState(btn)
    if (st.disabled) throw new Error('「忽略」按钮是灰的')
  })

  await step('点完忽略，按钮变成「恢复」且**可点**', async () => {
    const extra = await expandRow('可忽略的流水')
    await extra.locator('button:has-text("忽略")').first().click()
    await sleep(1500)
    const after = await expandRow('可忽略的流水')
    const btn = after.locator('button:has-text("恢复")')
    if (await btn.count() !== 1) {
      const texts = await after.locator('button').allInnerTexts()
      throw new Error(`点完忽略没有「恢复」按钮，按钮是：${JSON.stringify(texts)}`)
    }
    const st = await buttonState(btn)
    if (st.disabled) {
      throw new Error('「恢复」按钮是灰的 —— 点完忽略就再也点不回来（用户原话：'
        + '「点击忽略之后怎么没办法点击恢复？」）')
    }
  })

  await step('点「恢复」真的恢复回来了', async () => {
    const extra = await expandRow('可忽略的流水')
    await extra.locator('button:has-text("恢复")').first().click()
    await sleep(1500)
    const after = await expandRow('可忽略的流水')
    const btn = after.locator('button:has-text("忽略")')
    if (await btn.count() !== 1) throw new Error('恢复之后按钮没变回「忽略」')
    const st = await buttonState(btn)
    if (st.disabled) throw new Error('恢复之后「忽略」按钮是灰的')
  })
  await page.screenshot({ path: `${OUT}/flow_lock_restore.png` })

  // ---- 2) 已入账的行：对方科目不许再指定 ----
  await step('已入账的行：对方科目那颗按钮是灰的', async () => {
    const extra = await expandRow('已入账的流水')
    const text = (await extra.textContent()) || ''
    if (!text.includes('已入账')) throw new Error(`这一行不是已入账：${text}`)
    const btn = extra.locator('button:has-text("指定"), button:has-text("调整")')
    if (await btn.count() !== 1) throw new Error('没找到「指定/调整」按钮')
    const st = await buttonState(btn)
    if (!st.disabled) {
      throw new Error('「指定/调整」按钮还能点 —— 已入账的行改了流水行却不改凭证，'
        + '两边就对不上且看不出来')
    }
  })

  await step('旁边写着为什么不能改、以及出路', async () => {
    const extra = await expandRow('已入账的流水')
    const text = (await extra.textContent()) || ''
    if (!text.includes('已入账，科目由凭证定死，要改请改凭证')) {
      throw new Error(`没写明为什么不能改：${text}`)
    }
  })
  await page.screenshot({ path: `${OUT}/flow_lock_posted.png` })

  // ---- 3) 展开箭头一直都在（没有「对账信息」开关） ----
  await step('没有「对账信息」勾选开关，展开箭头一直都在', async () => {
    const toggle = await page.locator('.flow-toggle').count()
    if (toggle) throw new Error('还有「对账信息」勾选开关')
    const icons = await page.locator('.flow-rows-table .el-table__expand-icon').count()
    if (icons < 1) throw new Error(`行首没有展开箭头（${icons} 个）`)
  })
} finally {
  console.log('— 清数据 —')
  try {
    execFileSync(PY, [SEED, '--clean'], { cwd: ROOT, stdio: 'inherit' })
  } catch (e) {
    bad('清理造的数据', e.message.split('\n')[0])
  }
  await browser.close()
}

console.log(errors.length ? `\n有问题：${errors.length}` : '\n没问题')
process.exit(errors.length ? 1 : 0)
