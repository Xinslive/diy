/**
 * 流水对账「**跨期候选**」的界面自查。
 *
 * 跨期（凭证日期与流水日期差了十来天）的那条候选，界面上必须**标出日期差
 * 并让人确认** —— 这是「时间跨度过大的不藏起来，让人二次确认」那条口径的
 * 落地。单元测与后端用例都验不了「那句话真的显示在弹窗里」，所以这里真跑
 * 一遍界面。
 *
 * **它自己造数据、自己清理**（``seed_cross_period_case.py``）：dev 库里加一行
 * 带标记的流水 + 两张凭证，一张差 3 天（窗口内）、一张差 10 天（跨期）。
 * 两条摆在一起对比，才看得出「提示只出现在该出现的地方」。
 *
 * 用法：node tools/uitest/ui_flow_cross.mjs
 */
import { chromium } from 'playwright'
import { execFileSync } from 'node:child_process'
import { gotoStatementPeriod, selectStatement } from './lib/statement_period.mjs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '../..')
const PY = path.join(ROOT, 'backend/.venv/bin/python')
const SEED = path.join(HERE, 'seed_cross_period_case.py')
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
  // **先切到对账单所属的期间**（2026-10-28 起台账跟着顶栏的期间走），
  // 否则「选择对账单」下拉是空的 —— 后面每一步都在等一个不存在的元素。
  await step('把顶栏期间切到对账单所属的期间', async () => {
    const r = await gotoStatementPeriod(page)
    if (r.mode === 'none') throw new Error('这本账没有对账单 —— 造数据的脚本得先准备一份')
    if (r.mode === 'switched') console.log(`   点了「${r.label}」跳过去了`)
  })
  await step('切到流水明细并选中这份对账单', async () => {
    await selectStatement(page)
  })
  // **展开内容在紧跟数据行之后的另一个 <tr> 里**（Element Plus 的展开行是
  // 独立的一行），**不在数据行自己身上** —— 找错地方会以为按钮不见了。
  // （这个坑是 2026-10-28 改成展开行时留下的：那时脚本还在找数据行里的
  //  按钮，于是「可改科目」永远找不到。）
  let expanded = null
  await step('展开那一行（箭头一直都在，不用勾开关）', async () => {
    const row = page.locator('.flow-rows-table tbody tr.el-table__row')
      .filter({ hasText: '__uitest_cross__' })
    if (await row.count() !== 1) throw new Error(`没找到测试流水行（${await row.count()} 行）`)
    const icon = row.locator('.el-table__expand-icon').first()
    // **默认摊开的是「还有活要干的行」**（2026-10-28 改）—— 这一行还没入账、
    // 没被发票占用，所以它**很可能本来就是展开的**。这时再点箭头是**收起**。
    const already = (await icon.getAttribute('aria-expanded')) === 'true'
    if (!already) {
      await icon.click()
      await page.waitForTimeout(800)
    }
    expanded = row.locator('xpath=following-sibling::tr[1]')
    if (await expanded.locator('.flow-extra').count() !== 1) {
      throw new Error('展开内容不在紧跟其后的那一行里')
    }
  })

  await step('测试流水那行有「可改科目」入口', async () => {
    if (!expanded) throw new Error('上一步没展开成')
    const btn = expanded.locator('button:has-text("可改科目")')
    if (await btn.count() !== 1) throw new Error('展开行里没有「可改科目」按钮')
    await btn.click()
    await page.waitForTimeout(900)
  })

  await page.screenshot({ path: `${OUT}/flow_cross_period.png` })

  await step('弹窗里有两条候选（窗口内 + 跨期）', async () => {
    const items = page.locator('.flow-reclassify-item')
    const n = await items.count()
    if (n !== 2) throw new Error(`候选条数不对：${n}`)
  })

  await step('跨期那条标出日期差，窗口内那条不标', async () => {
    const items = page.locator('.flow-reclassify-item')
    const texts = await items.allInnerTexts()
    const withHint = texts.filter((t) => /日期相差/.test(t))
    if (withHint.length !== 1) {
      throw new Error(`应当只有一条带「日期相差」提示，实际 ${withHint.length} 条：${JSON.stringify(texts)}`)
    }
    if (!/10 天/.test(withHint[0])) {
      throw new Error(`提示里没有写出 10 天：${withHint[0]}`)
    }
    if (!/请确认是不是同一笔/.test(withHint[0])) {
      throw new Error(`提示没说要人工确认：${withHint[0]}`)
    }
    console.log('   窗口内那条：', texts.find((t) => !/日期相差/.test(t)).replace(/\n/g, ' | '))
    console.log('   跨期那条：', withHint[0].replace(/\n/g, ' | '))
  })

  await step('点跨期那条会弹二次确认', async () => {
    const items = page.locator('.flow-reclassify-item')
    let clicked = false
    for (let i = 0; i < (await items.count()); i++) {
      const t = await items.nth(i).innerText()
      if (/日期相差/.test(t)) {
        await items.nth(i).locator('button').click()
        clicked = true
        break
      }
    }
    if (!clicked) throw new Error('没找到跨期那条候选')
    await page.waitForTimeout(700)
    const box = page.locator('.el-message-box')
    if (await box.count() !== 1) throw new Error('没有弹出二次确认框')
    const text = await box.innerText()
    if (!/10 天/.test(text)) throw new Error(`确认框里没有写日期差：${text}`)
    if (!/同一笔/.test(text)) throw new Error(`确认框没说要确认同一笔：${text}`)
    await page.screenshot({ path: `${OUT}/flow_cross_confirm.png` })
    // 点「再想想」—— **别真的改**：这一跑只为验提示长什么样
    await page.locator('.el-message-box button:has-text("再想想")').click()
    await page.waitForTimeout(500)
  })
} finally {
  console.log('— 清理 —')
  try {
    execFileSync(PY, [SEED, '--clean'], { cwd: ROOT, stdio: 'inherit' })
  } catch (e) {
    bad('清理失败', String(e.message).split('\n')[0])
  }
  await browser.close()
}

console.log(errors.length ? `\n有问题：${errors.length}` : '\n没问题')
process.exit(errors.length ? 1 : 0)
