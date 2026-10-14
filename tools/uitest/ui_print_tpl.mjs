import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
page.setDefaultTimeout(8000)
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
const step = async (name, fn) => {
  try { await fn(); console.log('✓', name) }
  catch (e) { console.log('✗', name, '—', e.message); errors.push(name) }
}

await page.goto(BASE, { waitUntil: 'networkidle' })
if (await page.locator('input[type=password]').count()) {
  await step('登录', async () => {
    await page.click('button:has-text("登录")')
    await page.waitForSelector('.sidebar', { timeout: 20000 })
    await page.waitForTimeout(800)
  })
}
const sessionToken = await page.evaluate(() => localStorage.getItem('cwq_token'))
const testApi = { headers: { Authorization: `Bearer ${sessionToken}` } }
const testBooks = await (await page.request.get(`${BASE}/api/books`, testApi)).json()
const testBook = testBooks.find((b) => b.id === 59) || testBooks.find((b) => ['OWNER', 'ACCOUNTANT'].includes(b.role))
if (!testBook) throw new Error('需要可写账套')
const testPeriods = await (await page.request.get(`${BASE}/api/books/${testBook.id}/periods`, testApi)).json()
const testPeriod = testPeriods.find((p) => p.vouchers > 0) || testPeriods[0]
await page.evaluate(({ b, p }) => { localStorage.setItem('cwq_book', b); localStorage.setItem('cwq_period', p) },
  { b: String(testBook.id), p: String(testPeriod.id) })
const testName = `__ui_print_${Date.now()}`
const response = await page.request.post(`${BASE}/api/books/${testBook.id}/print/templates`, {
  ...testApi, data: { scope: 'VOUCHER', name: testName, content: { paper: 'A5', orientation: 'landscape' } },
})
if (!response.ok()) throw new Error('临时模板创建失败')
const testTemplate = await response.json()
try {
await page.reload({ waitUntil: 'networkidle' })
await page.goto(BASE + '/#/print-templates', { waitUntil: 'networkidle' })
await page.waitForTimeout(2800)
await page.locator('.pt-bar .sel-tpl').click()
await page.locator('.el-select-dropdown__item:visible').filter({ hasText: testName }).click()
await page.waitForTimeout(1000)

const templateSelect = () => page.locator('.pt-bar .sel-tpl')
const visibleOptions = () => page.locator('.el-select-dropdown__item:visible')
const tableRow = () => page.locator('.elist li').filter({ hasText: '分录表格' }).first()
const tableField = (label) => page.locator('.ti-elements .fl').filter({ hasText: label }).last().locator('input').first()
const previewHtml = () => page.evaluate(() => document.querySelector('iframe.frame')?.contentDocument?.documentElement?.outerHTML || '')
const previewText = () => page.evaluate(() => document.querySelector('iframe.frame')?.contentDocument?.body?.innerText || '')
const settle = () => page.waitForTimeout(1800)

await page.getByRole('tab', { name: '元素', exact: true }).click()
await tableRow().click()
await page.waitForTimeout(400)

await step('预览纸面可读且不再重复缩放', async () => {
  const m = await page.evaluate(() => {
    const paper = document.querySelector('.paper')?.getBoundingClientRect()
    const frame = document.querySelector('iframe.frame')?.getBoundingClientRect()
    const scale = Number((document.querySelector('.zoom-label')?.innerText || '').replace('%', ''))
    return { paper, frame, scale }
  })
  if (!m.paper || m.paper.width < 500) throw new Error(`纸面过小：${m.paper?.width}`)
  if (!m.frame || Math.abs(m.frame.width - m.paper.width) > 2) throw new Error('iframe 与纸面宽度不一致')
  if (!(m.scale > 50)) throw new Error(`缩放仍像缩略图：${m.scale}%`)
})

await step('属性面板有表头设置', async () => {
  const t = await page.locator('.ti-elements').innerText()
  if (!/表头/.test(t) || !/显示表头/.test(t) || !/列头高/.test(t)) throw new Error(t.slice(0, 240))
})

await step('属性面板有合计文字', async () => {
  const t = await page.locator('.ti-elements').innerText()
  if (!/合计文字/.test(t) || await tableField('合计文字').count() !== 1) throw new Error('合计文字控件不存在')
})

await step('默认预览有表头行', async () => {
  if (!/<thead[ >]/.test(await previewHtml())) throw new Error('预览表头行不见了')
})

const setShowHeader = async (checked) => {
  const label = page.locator('.ti-elements .fl').filter({ hasText: '显示表头' }).last().locator('.el-checkbox')
  const current = await label.locator('input').isChecked()
  if (current !== checked) await label.click()
}

await setShowHeader(false)
await settle()
await step('关表头后预览没有 thead', async () => {
  if (/<thead[ >]/.test(await previewHtml())) throw new Error('预览还在印表头')
})
await page.screenshot({ path: '.shots/pt-1-nohead.png', fullPage: true })

await setShowHeader(true)
await settle()
await step('重新打开后预览表头回来了', async () => {
  if (!/<thead[ >]/.test(await previewHtml())) throw new Error('预览没有 thead')
})

await step('列头高度可改且预览行高同步', async () => {
  const before = await page.evaluate(() => {
    const d = document.querySelector('iframe.frame')?.contentDocument
    return d?.querySelector('.voucher-element.table thead')?.getBoundingClientRect().height || 0
  })
  const input = page.locator('.ti-elements .fl').filter({ hasText: '列头高' }).last().locator('input').first()
  await input.fill('12')
  await input.press('Tab')
  await settle()
  const after = await page.evaluate(() => {
    const d = document.querySelector('iframe.frame')?.contentDocument
    return d?.querySelector('.voucher-element.table thead')?.getBoundingClientRect().height || 0
  })
  if (Math.abs(after - before) < 1) throw new Error(`列头高度没变 ${before} -> ${after}`)
})

await step('合计文字可改且预览同步', async () => {
  const input = tableField('合计文字')
  await input.fill('本页合计')
  await input.press('Enter')
  await settle()
  if (!(await previewText()).includes('本页合计')) throw new Error('预览合计行没变')
})
await page.screenshot({ path: '.shots/pt-2-label.png', fullPage: true })

await step('合计文字可改回空', async () => {
  const input = tableField('合计文字')
  await input.fill('')
  await input.press('Enter')
  await settle()
  if ((await previewText()).includes('本页合计')) throw new Error('预览合计文字未清空')
})

await step('版式面板有分页编号和格式示例', async () => {
  await page.getByRole('tab', { name: '页面', exact: true }).click()
  const t = (await page.locator('.ti-page').innerText()).replace(/\n+/g, ' | ')
  if (!/分页编号/.test(t) || !/001\/003/.test(t)) throw new Error(t.slice(-300))
})

await step('分页编号示例随格式串变化', async () => {
  const input = page.locator('.ti-page .fl').filter({ hasText: '凭证字号格式' }).last().locator('input').first()
  await input.fill('{voucher_no} - {first}/{lines}')
  await input.press('Enter')
  await page.waitForTimeout(300)
  const changed = (await page.locator('.ti-page').innerText()).replace(/\n+/g, ' | ')
  if (!/001\/012/.test(changed)) throw new Error(changed.slice(-300))
  await input.fill('{voucher_no} - {page}/{pages}')
  await input.press('Enter')
  await page.waitForTimeout(300)
})

// 下面两项直接验后端打印输出，避免只验证前端提示而漏掉正式打印。
await step('正式打印：凭证号带分页号、日期是中文写法', async () => {
  const token = await page.evaluate(() => localStorage.getItem('cwq_token'))
  const api = { headers: { Authorization: `Bearer ${token}` } }
  const books = await (await page.request.get(`${BASE}/api/books`, api)).json()
  const book = books.find((b) => b.id === 59) || books[0]
  if (!book) throw new Error('没有账套')
  const tmp = await (await page.request.post(`${BASE}/api/books/${book.id}/print/templates`, {
    ...api, data: { scope: 'VOUCHER', name: '__ui_check_tmp__', content: {} },
  })).json()
  try {
    const periods = await (await page.request.get(`${BASE}/api/books/${book.id}/periods`, api)).json()
    let hit = null
    for (const p of (periods || []).filter((x) => x.vouchers > 0)) {
      const r = await page.request.get(
        `${BASE}/api/books/${book.id}/print/vouchers.html?period_id=${p.id}&template_id=${tmp.id}`, api)
      const html = await r.text()
      if (/凭证字号：记\d+/.test(html)) { hit = html; break }
    }
    if (!hit) throw new Error('没有可验的凭证')
    const no = hit.match(/凭证字号：([^<]*)/)[1].trim()
    if (!/^记\d+ - \d{3}\/\d{3}$/.test(no)) throw new Error('凭证号格式不对：' + no)
    const date = hit.match(/日期：([^<]*)/)[1].trim()
    if (!/^\d{4}年\d{2}月\d{2}日$/.test(date)) throw new Error('日期不是中文写法：' + date)
    if (/\d{4}-\d{2}-\d{2}/.test(hit)) throw new Error('输出里还残留 ISO 日期')
  } finally {
    await page.request.delete(`${BASE}/api/books/${book.id}/print/templates/${tmp.id}`, api)
  }
})

await step('拆分页的合计是累计的', async () => {
  const token = await page.evaluate(() => localStorage.getItem('cwq_token'))
  const api = { headers: { Authorization: `Bearer ${token}` } }
  const books = await (await page.request.get(`${BASE}/api/books`, api)).json()
  const book = books.find((b) => b.id === 59) || books[0]
  const tmp = await (await page.request.post(`${BASE}/api/books/${book.id}/print/templates`, {
    ...api, data: { scope: 'VOUCHER', name: '__ui_check_tmp__', content: { voucher: { rows_per_page: 2 } } },
  })).json()
  try {
    const periods = await (await page.request.get(`${BASE}/api/books/${book.id}/periods`, api)).json()
    for (const p of (periods || []).filter((x) => x.vouchers > 0)) {
      const html = await (await page.request.get(
        `${BASE}/api/books/${book.id}/print/vouchers.html?period_id=${p.id}&template_id=${tmp.id}`, api)).text()
      const slots = html.split('<div class="voucher-slot">').slice(1)
      const rows = slots.map((slot) => {
        const no = slot.match(/凭证字号：记\d+ - (\d{3})\/(\d{3})/)
        const nums = (slot.match(/<tr class="total">[\s\S]*?<\/tr>/) || [''])[0]
          .match(/<td class="num">([\d,.]+)<\/td><td class="num">([\d,.]+)<\/td>/)
        return { voucher: slot.match(/凭证字号：记(\d+)/)?.[1], page: no?.[1], pages: no?.[2],
          dr: nums ? Number(nums[1].replace(/,/g, '')) : null,
          cr: nums ? Number(nums[2].replace(/,/g, '')) : null }
      })
      const first = rows[0]
      const same = rows.filter((x) => x.voucher === first?.voucher)
      if (!first || first.pages === '001' || same.length < 2) continue
      if (same[0].dr >= same.at(-1).dr) throw new Error('第 1 页不是部分合计')
      if (same.at(-1).dr !== same.at(-1).cr) throw new Error('最后一张没有累计到整张')
      return
    }
    throw new Error('没找到可验的拆分凭证')
  } finally {
    await page.request.delete(`${BASE}/api/books/${book.id}/print/templates/${tmp.id}`, api)
  }
})

// ---- 模板改名：新页面把低频动作收进「更多」菜单 ----
const pickedName = async () => (await templateSelect().innerText()).trim()
const openTemplateOptions = async () => {
  await templateSelect().click()
  await page.waitForTimeout(300)
  return visibleOptions()
}
const openMore = async () => {
  await page.locator('.pt-bar .el-dropdown').click()
  await page.waitForTimeout(250)
  return page.locator('.el-dropdown-menu:visible .el-dropdown-menu__item')
}

await step('更多菜单有重命名动作', async () => {
  const items = await openMore()
  if (await items.filter({ hasText: '重命名' }).count() !== 1) throw new Error('没有重命名菜单项')
  await page.keyboard.press('Escape')
})

await step('内置默认模板不能改名', async () => {
  const opts = await openTemplateOptions()
  await opts.filter({ hasText: '（内置默认）' }).first().click()
  const items = await openMore()
  if (await items.filter({ hasText: '重命名' }).isEnabled()) throw new Error('内置默认模板仍可改名')
  await page.keyboard.press('Escape')
})

await step('重命名后下拉里是新名字且不改变版式状态', async () => {
  const token = await page.evaluate(() => localStorage.getItem('cwq_token'))
  const api = { headers: { Authorization: 'Bearer ' + token } }
  const books = await (await page.request.get(`${BASE}/api/books`, api)).json()
  const book = books.find((b) => b.id === 59) || books[0]
  const list = await (await page.request.get(`${BASE}/api/books/${book.id}/print/templates`, api)).json()
  const tpl = list.find((t) => t.id === testTemplate.id)
  if (!tpl) throw new Error('没有可改名模板')
  const oldName = tpl.name
  const undo = () => page.request.put(`${BASE}/api/books/${book.id}/print/templates/${tpl.id}`,
    { ...api, data: { name: oldName } })
  try {
    const opts = await openTemplateOptions()
    await opts.filter({ hasText: oldName }).first().click()
    await page.waitForTimeout(700)
    const before = await page.locator('.save-bar').innerText()
    const items = await openMore()
    await items.filter({ hasText: '重命名' }).click()
    await page.waitForSelector('.el-message-box input', { timeout: 8000 })
    const want = oldName + '·改名'
    await page.fill('.el-message-box input', want)
    await page.click('.el-message-box__btns button.el-button--primary')
    await page.waitForTimeout(900)
    if (!(await pickedName()).includes(want)) throw new Error('下拉里还是旧名字')
    if ((await page.locator('.save-bar').innerText()) !== before) throw new Error('改名动了版式状态')
  } finally {
    await undo()
  }
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(2200)
  if ((await pickedName()).includes('·改名')) throw new Error('测试名字没有回滚')
})

} catch (e) {
  errors.push(e.message)
} finally {
  const deleted = await page.request.delete(`${BASE}/api/books/${testBook.id}/print/templates/${testTemplate.id}`, testApi)
  if (!deleted.ok()) errors.push('临时模板没有清理')
  await browser.close()
}
console.log(errors.length ? '\n应用报错:\n' + errors.join('\n') : '\n无应用报错')
process.exitCode = errors.length ? 1 : 0
