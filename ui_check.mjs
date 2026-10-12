import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const errors = []
const out = process.argv[2] || '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))

async function step(name, fn) {
  try { await fn(); console.log('✓', name) }
  catch (e) { console.log('✗', name, '—', e.message.split('\n')[0]); errors.push(name + ': ' + e.message.split('\n')[0]) }
}

await step('打开登录页', async () => {
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await page.waitForSelector('text=财务记账系统')
})
await page.screenshot({ path: `${out}/01-login.png` })

await step('登录', async () => {
  await page.click('button:has-text("登录")')
  await page.waitForSelector('.el-aside', { timeout: 15000 })
  await page.waitForTimeout(800)
})
await page.screenshot({ path: `${out}/02-books.png` })

await step('登录后落在账套管理', async () => {
  await page.waitForSelector('text=账套是独立的记账单元', { timeout: 10000 })
})

await step('顶栏账套选择器只显示账套名', async () => {
  await page.click('.topbar .el-select >> nth=0')
  await page.waitForTimeout(500)
  const labels = await page.locator('.el-select-dropdown__item').allInnerTexts()
  if (!labels.length) throw new Error('下拉为空')
  if (labels.some(l => l.includes('·'))) throw new Error('下拉里还有「单位 · 账套」的拼接: ' + labels[0])
  console.log('   下拉项:', labels.join(' / '))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)
})

await step('凭证列表有数据', async () => {
  await page.click('.el-menu-item:has-text("凭证")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  const n = await page.locator('.el-table__row').count()
  if (n === 0) throw new Error('凭证列表为空')
  console.log('   凭证行数:', n)
})

await step('打开科目余额表', async () => {
  await page.click('.el-menu-item:has-text("科目余额表")')
  await page.waitForSelector('.ledger .el-table__row', { timeout: 10000 })
  await page.waitForTimeout(500)
  const ok = await page.locator('.balance-check.ok').count()
  if (!ok) throw new Error('试算未显示为平衡')
})
await page.screenshot({ path: `${out}/03-balance.png` })

await step('打开总账', async () => {
  await page.click('.el-menu-item:has-text("总账")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/04-general.png` })

await step('打开明细账', async () => {
  await page.click('.el-menu-item:has-text("明细账")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/05-detail.png` })

await step('打开资产负债表', async () => {
  await page.click('.el-menu-item:has-text("报表")')
  await page.waitForTimeout(500)
  await page.click('a:has-text("资产负债表")')
  await page.waitForSelector('.report-table tbody tr', { timeout: 10000 })
  await page.waitForTimeout(500)
  const err = await page.locator('.el-alert--error').count()
  if (err) throw new Error('报表有公式错误')
})
await page.screenshot({ path: `${out}/06-balance-report.png` })

await step('打开利润表', async () => {
  await page.goto(BASE + '/#/reports/2', { waitUntil: 'networkidle' })
  await page.waitForSelector('.report-table tbody tr', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/07-income-report.png` })

await step('期末处理面板', async () => {
  await page.click('.el-menu-item:has-text("期末结账")')
  await page.waitForSelector('.act-title', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/08-periods.png` })

await step('科目表', async () => {
  await page.click('.el-menu-item:has-text("科目表")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(500)
})
await page.screenshot({ path: `${out}/09-subjects.png` })

await step('操作日志', async () => {
  await page.click('.el-menu-item:has-text("操作日志")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/10-audit.png` })

await step('新增凭证页', async () => {
  await page.click('.el-menu-item:has-text("凭证")')
  await page.waitForTimeout(600)
  await page.click('button:has-text("新增凭证")')
  // 分录区的类名是 .vgrid（不是早先的 .entries），认错了会一直等到超时
  await page.waitForSelector('.vgrid', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/11-voucher-new.png` })

await step('整理凭证页', async () => {
  await page.goto(BASE + '/#/vouchers/organize', { waitUntil: 'networkidle' })
  await page.waitForSelector('.card-head', { timeout: 10000 })
  await page.click('button:has-text("查看方案")')
  await page.waitForSelector('.period-block', { timeout: 10000 })
  await page.waitForTimeout(600)
})
await page.screenshot({ path: `${out}/12-organize.png` })

await step('凭证导入页', async () => {
  await page.goto(BASE + '/#/vouchers/import', { waitUntil: 'networkidle' })
  await page.waitForSelector('.el-upload-dragger', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/13-import.png` })

await step('打印模板设计器（含实时预览）', async () => {
  await page.goto(BASE + '/#/print-templates', { waitUntil: 'networkidle' })
  await page.waitForSelector('iframe.preview', { timeout: 15000 })
  await page.waitForTimeout(2500)
  const frame = page.frameLocator('iframe.preview')
  await frame.locator('table.grid').first().waitFor({ timeout: 15000 })
  const rows = await frame.locator('table.grid tr').count()
  if (rows === 0) throw new Error('预览里没有表格内容')
  console.log('   预览表格行数:', rows)
})
await page.screenshot({ path: `${out}/14-print-designer.png` })

await step('改纸型后预览跟着变', async () => {
  await page.locator('.el-form-item').filter({ hasText: '纸型' }).locator('.el-select').first().click()
  await page.waitForTimeout(700)
  // 所有 el-select 的选项都会提前渲染进 DOM（合计 100+ 个隐藏项），必须先限 :visible
  await page.locator('.el-select-dropdown__item:visible').filter({ hasText: /^A5$/ }).first().click()
  await page.waitForTimeout(1800)
  const css = await page.frameLocator('iframe.preview').locator('style').first().textContent()
  if (!css.includes('A5')) throw new Error('预览仍是旧纸型，实际：' + css.slice(0, 80))
})
await page.screenshot({ path: `${out}/15-print-a5.png` })

await step('管理员能看到「用户与授权」', async () => {
  const n = await page.locator('.el-menu-item:has-text("用户与授权")').count()
  if (n !== 1) throw new Error('管理员菜单项数量异常: ' + n)
})

await step('用户与授权页', async () => {
  await page.goto(BASE + '/#/users', { waitUntil: 'networkidle' })
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(500)
})
await page.screenshot({ path: `${out}/16-users.png` })

// 上级科目那两行（银行存款 / 主营业务收入…）：
//   · 方向列要跟末级用同款标签，不能是一行灰字 —— 一眼扫过去像少了东西
//   · 金额列要显示它这一支的合计，不能空着（期初只能录在末级上，但
//     「银行存款这一行到底装了多少钱」恰恰是翻这张表最该得到答案的问题）
await step('期初余额页的上级科目做成小计行', async () => {
  // 先切到**有账的那一期**再验：跑到空月份上，上下级全是 0，
  // 「合计 = 下级相加」会拿 0 = 0 当通过，等于什么都没验
  const busy = await page.evaluate(async () => {
    const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token'),
                'Content-Type': 'application/json' }
    const books = await (await fetch('/api/books', { headers: h })).json()
    for (const b of books) {
      const ps = await (await fetch(`/api/books/${b.id}/periods`, { headers: h })).json()
      const p = ps.filter(x => x.vouchers > 0).pop()
      if (p) return { bookId: b.id, periodId: p.id, code: p.code }
    }
    return null
  })
  if (!busy) throw new Error('所有账套都没有任何期间的凭证，这条没法验')
  await page.evaluate(async (b) => {
    const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token'),
                'Content-Type': 'application/json' }
    localStorage.setItem('cwq_book', String(b.bookId))
    localStorage.setItem('cwq_period', String(b.periodId))
  }, busy)
  await page.reload({ waitUntil: 'networkidle' })
  await page.goto(BASE + '/#/openings', { waitUntil: 'networkidle' })
  await page.waitForSelector('table.grid tbody tr', { timeout: 10000 })
  await page.waitForTimeout(600)
  const r = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('table.grid tbody tr')]
    const get = (code) => rows.find(tr => tr.querySelector('td')?.textContent.trim() === code)
    const cell = (tr, i) => tr?.querySelectorAll('td')[i]
    const read = (code) => {
      const tr = get(code)
      if (!tr) return null
      const tag = cell(tr, 2).querySelector('.el-tag')
      const inp = cell(tr, 3).querySelector('input')
      const sub = cell(tr, 3).querySelector('.sub-total')
      return {
        hasTag: !!tag,
        clickable: !!tag?.classList.contains('flip'),
        side: cell(tr, 2).textContent.trim(),
        value: Number(inp?.value || 0),
        disabled: !!inp?.disabled,
        // 盒子长什么样：跟同一列的末级逐项比对
        box: (() => {
          const w = cell(tr, 3).querySelector('.el-input__wrapper')
          const cs = getComputedStyle(w), r = w.getBoundingClientRect()
          return { w: Math.round(r.width), h: Math.round(r.height),
                   bg: cs.backgroundColor, border: cs.boxShadow, radius: cs.borderRadius }
        })(),
        weight: getComputedStyle(inp).fontWeight
      }
    }
    const parent = rows.find(tr => tr.classList.contains('is-parent'))
      ?.querySelector('td')?.textContent.trim()
    if (!parent) return { skip: '这本账没有多级科目' }
    const parentRow = get(parent)
    const depth = Number(parentRow.querySelector('td').style.paddingLeft.replace('px', ''))
    const kids = rows.filter(tr => Number(tr.querySelector('td')?.style.paddingLeft?.replace('px', '') || 0) > depth)
    return {
      parent, parentRow: read(parent),
      kid: read(kids[0].querySelector('td').textContent.trim()),
      kids: kids.slice(0, 3).map(tr => read(tr.querySelector('td').textContent.trim()))
    }
  })
  if (r.skip) { console.log('   （' + r.skip + '，这两条不适用）'); return }
  if (!r.parentRow?.hasTag) throw new Error('上级科目没有方向标签')
  if (r.parentRow.clickable) throw new Error('上级科目的方向不该能点：它是下级合计出来的')
  const sum = r.kids.reduce((a, k) => a + k.value, 0)
  if (Math.abs(r.parentRow.value - sum) > 0.005) {
    throw new Error(`${r.parent} 的合计 ${r.parentRow.value} 不等于下级相加 ${sum}`)
  }
  if (!sum) throw new Error(`${r.parent} 上下级全是 0，这条等于没验（检查跑到空期间了）`)
  console.log(`   （${r.parent} 小计 ${r.parentRow.value.toLocaleString()} = 下级相加 ${sum.toLocaleString()}）`)
  if (!(r.parentRow.side === '借' || r.parentRow.side === '贷')) {
    throw new Error('上级科目的方向标签是空的')
  }
  if (!r.parentRow.disabled) throw new Error('上级科目那一行居然能改：它是下级合计出来的')
  // 样式统一：上级那格必须跟同列末级长得一模一样（同样宽高、同样底色和边框），
  // 只靠字重区分「这是小计」。自己拿 div 仿一个盒子，早晚差那么一两个像素
  for (const k of ['w', 'h', 'bg', 'border', 'radius']) {
    if (r.parentRow.box[k] !== r.kid.box[k]) {
      throw new Error(`上级科目的金额格跟末级对不齐：${k} ${r.parentRow.box[k]} vs ${r.kid.box[k]}`)
    }
  }
  if (r.parentRow.weight === r.kid.weight) {
    throw new Error('小计没有字重上的区分，跟普通行长得太像了')
  }
})

// 期初余额必须跟着顶栏那一期走：切到空月份就该能改，不能被
// 「账套里有过凭证」一句话判死（这个月到底有没有账，是看它自己，不是看整本账）
await step('期初余额跟着顶栏那一期', async () => {
  // 登录后的默认账套未必同时有「有凭证」和「没凭证」的月份，
  // 挨个账套找一个两样都齐的，找不到就明说，不硬跑一条空转的断言
  const pick = await page.evaluate(async () => {
    const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
    const books = await (await fetch('/api/books', { headers: h })).json()
    for (const b of books) {
      const ps = await (await fetch(`/api/books/${b.id}/periods`, { headers: h })).json()
      const busy = ps.filter(p => p.vouchers > 0).pop()
      const free = ps.filter(p => p.vouchers === 0).pop()
      if (busy && free) return { bookId: b.id, book: b.name,
                                  busy: busy.code, busyId: busy.id,
                                  free: free.code, freeId: free.id }
    }
    return null
  })
  if (!pick) throw new Error('没有一个账套同时含「有凭证」和「没凭证」的月份，这两条没法验')
  console.log(`   （尺子：${pick.book} ${pick.busy} 有凭证、${pick.free} 一张没有）`)
  await page.evaluate(p => {
    localStorage.setItem('cwq_book', String(p.bookId))
    localStorage.setItem('cwq_period', String(p.busyId))
  }, pick)

  const openAs = async (id) => {
    await page.evaluate(pid => localStorage.setItem('cwq_period', String(pid)), id)
    await page.goto(BASE + '/#/openings')
    // store 只在应用启动时读 localStorage，不 reload 的话页面拿的还是
    // 上一次那个期间 —— 断言会拿旧月份的数据判新月份，结论完全相反
    await page.reload({ waitUntil: 'networkidle' })
    await page.waitForTimeout(1500)
    return page.evaluate(() => ({
      head: document.querySelector('.page-head .muted')?.innerText || '',
      alert: !!document.querySelector('.el-alert'),
      editable: !!document.querySelector('.grid input:not([disabled])')
    }))
  }

  const onBusy = await openAs(pick.busyId)
  if (!onBusy.head.includes(pick.busy)) throw new Error('标题没跟着期间走：' + onBusy.head)
  if (!onBusy.alert || onBusy.editable) throw new Error(`${pick.busy} 明明有凭证，却还能改`)

  const onFree = await openAs(pick.freeId)
  if (!onFree.head.includes(pick.free)) throw new Error('标题没跟着期间走：' + onFree.head)
  if (onFree.alert || !onFree.editable) throw new Error(`${pick.free} 一张凭证没有，却不让改`)
})
await page.screenshot({ path: `${out}/17-openings.png` })

console.log('\n' + (errors.length ? '问题:\n' + errors.map(e => '  - ' + e).join('\n') : '无 JS 错误'))
await browser.close()
process.exit(errors.length ? 1 : 0)
