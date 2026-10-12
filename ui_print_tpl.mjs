import { chromium } from 'playwright'
const BASE = 'http://127.0.0.1:5173'
const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
const step = async (n, f) => { try { await f(); console.log('✓', n) } catch (e) { console.log('✗', n, '—', e.message.split('\n')[0]); errors.push(n) } }

await page.goto(BASE, { waitUntil: 'networkidle' })
if (await page.locator('input[type=password]').count()) {
  await step('登录', async () => {
    await page.click('button:has-text("登录")')
    await page.waitForSelector('.sidebar', { timeout: 20000 })
    await page.waitForTimeout(800)
  })
}
await page.goto(BASE + '/#/print-templates', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
await page.locator('.elist li:has-text("分录表格")').click()
await page.waitForTimeout(500)

const table = () => page.locator('.vle .tmock').first()
const previewHas = (s) => page.evaluate((needle) => {
  const d = document.querySelector('iframe.preview')?.contentDocument
  return !!d && d.body.innerHTML.includes(needle)
}, s)
const settle = async () => { await page.waitForTimeout(1600) }

await step('属性面板有表头设置', async () => {
  const t = await page.locator('.props').innerText()
  if (!/表头/.test(t)) throw new Error('没有表头设置：' + t.slice(0, 200))
  if (!/显示表头/.test(t)) throw new Error('没有表头显隐开关')
  if (!/列头高/.test(t)) throw new Error('没有列头高度')
})
await step('属性面板有合计文字', async () => {
  const t = await page.locator('.props').innerText()
  if (!/合计文字/.test(t)) throw new Error('没有合计文字设置：' + t.slice(0, 200))
  // 当前载入的可能是用户自己存过的模板，默认值由后端测试兜，这里只查控件在
  if (await page.locator('.props label:has-text("合计文字") input').count() !== 1)
    throw new Error('合计文字没有输入框')
})
await step('默认画布有表头行', async () => {
  if (await table().locator('.thead').count() !== 1) throw new Error('画布表头行不见了')
})

// 关掉表头
await page.locator('.props label:has-text("显示表头") input[type=checkbox]').uncheck()
await page.waitForTimeout(400)
await settle()
await step('关表头后画布没有表头行', async () => {
  if (await table().locator('.thead').count() !== 0) throw new Error('画布还留着表头')
  const rows = await table().locator('.trow').count()
  if (rows !== 6) throw new Error('行数不对：' + rows)
})
await step('关表头后预览也没有 thead', async () => {
  if (await previewHas('<thead>')) throw new Error('预览还在印表头')
})
await page.screenshot({ path: '.shots/pt-1-nohead.png', fullPage: true })

// 重新打开
await page.locator('.props label:has-text("显示表头") input[type=checkbox]').check()
await page.waitForTimeout(400)
await settle()
await step('重新打开后表头回来了', async () => {
  if (await table().locator('.thead').count() !== 1) throw new Error('表头没回来')
  if (!(await previewHas('<thead>'))) throw new Error('预览没有 thead')
})

// 列头高度
await step('列头高度可改且行高同步', async () => {
  const before = await page.evaluate(() => {
    const t = document.querySelector('.vle .tmock')
    return getComputedStyle(t.querySelector('.thead')).height
  })
  const input = page.locator('.props label:has-text("列头高") input')
  await input.fill('12')
  await input.press('Enter')
  await page.waitForTimeout(400)
  await settle()
  const after = await page.evaluate(() => {
    const t = document.querySelector('.vle .tmock')
    const head = getComputedStyle(t.querySelector('.thead')).height
    const row = getComputedStyle(t.querySelector('.trow')).height
    return { head, row }
  })
  if (Math.abs(parseFloat(after.head) - parseFloat(before)) < 1)
    throw new Error(`列头高度没变 ${before} -> ${after.head}`)
  // 表头 + 6 行仍然占满
  const sum = parseFloat(after.head) + parseFloat(after.row) * 6
  // 容器自带 1px 边框，内容区比外框少 2px
  const box = await table().boundingBox()
  if (Math.abs(sum - (box.height - 2)) > 2)
    throw new Error(`表头+行没占满 ${sum.toFixed(1)} vs ${(box.height - 2).toFixed(1)}`)
})

// 合计文字
await step('合计文字可改且画布与预览同步', async () => {
  const input = page.locator('.props label:has-text("合计文字") input')
  await input.fill('本页合计')
  await input.press('Enter')
  await page.waitForTimeout(400)
  await settle()
  const onCanvas = await table().innerText()
  if (!onCanvas.includes('本页合计')) throw new Error('画布合计行没变：' + onCanvas.slice(-40))
  if (!(await previewHas('本页合计'))) throw new Error('预览合计行没变')
})
await page.screenshot({ path: '.shots/pt-2-label.png', fullPage: true })

// 复原
await step('合计文字可改回空', async () => {
  const input = page.locator('.props label:has-text("合计文字") input')
  await input.fill('')
  await input.press('Enter')
  await page.waitForTimeout(400)
  await settle()
  const t = await table().innerText()
  if (/合/.test(t)) throw new Error('画布合计行还有文字：' + t.slice(-40))
})

// 分页编号
await step('版式面板有分页编号', async () => {
  const t = (await page.locator('.p-format').innerText()).replace(/\n+/g, ' | ')
  if (!/分页编号/.test(t)) throw new Error('版式面板没有分页编号：' + t.slice(0, 300))
  if (!/001\/003/.test(t)) throw new Error('没有给出格式示例：' + t.slice(-260))
})
await step('分页编号示例随格式串变化', async () => {
  const input = page.locator('.p-format .el-form-item:has-text("分页编号") input')
  await input.fill('{voucher_no} - {first}/{lines}')
  await input.press('Enter')
  await page.waitForTimeout(1600)
  const t = (await page.locator('.p-format').innerText()).replace(/\n+/g, ' | ')
  if (!/001\/012/.test(t)) throw new Error('示例没跟着变：' + t.slice(-260))
  await input.fill('{voucher_no} - {page}/{pages}')
  await input.press('Enter')
  await page.waitForTimeout(1600)
})
await step('画布示例凭证号带分页号', async () => {
  const t = await page.locator('.vle .canvas').innerText()
  if (!/记001号 - 001\/001/.test(t)) throw new Error('画布示例没有分页号：' + t.slice(0, 120))
})
await step('画布示例日期是中文写法', async () => {
  const t = await page.locator('.vle .canvas').innerText()
  if (!/2026年01月05日/.test(t)) throw new Error('画布日期不是中文写法：' + t.slice(0, 200))
  if (/2026-01-05/.test(t)) throw new Error('画布还印着 ISO 日期')
})
// 版式是当前账套**存着的**那份，页面正被人改过，所以自带一份干净模板来验，
// 验完删掉 —— 否则一次失败就把检查人的模板留在那儿。
await step('正式打印：凭证号带分页号、日期是中文写法', async () => {
  const token = await page.evaluate(() => localStorage.getItem('cwq_token'))
  const api = { headers: { Authorization: `Bearer ${token}` } }
  const books = await (await page.request.get(`${BASE}/api/books`, api)).json()
  if (!books?.length) throw new Error('没有账套：' + JSON.stringify(books).slice(0, 120))
  const book = books.find((b) => b.id === 59) || books[0]
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
  const readSlots = (html) => html.split('<div class="voucher-slot">').slice(1).map((slot) => {
    const no = slot.match(/凭证字号：记\d+ - (\d{3})\/(\d{3})/)?.[0]?.match(/记(\d+)/)
    const nums = (slot.match(/<tr class="total">[\s\S]*?<\/tr>/) || [''])[0]
      .match(/<td class="num">([\d,.]+)<\/td><td class="num">([\d,.]+)<\/td>/)
    return {
      voucher: no?.[1],
      page: slot.match(/凭证字号：记\d+ - (\d{3})\/(\d{3})/)?.[1],
      pages: slot.match(/凭证字号：记\d+ - (\d{3})\/(\d{3})/)?.[2],
      dr: nums ? Number(nums[1].replace(/,/g, '')) : null,
      cr: nums ? Number(nums[2].replace(/,/g, '')) : null,
    }
  })
  try {
    const periods = await (await page.request.get(`${BASE}/api/books/${book.id}/periods`, api)).json()
    for (const p of (periods || []).filter((x) => x.vouchers > 0)) {
      const html = await (await page.request.get(
        `${BASE}/api/books/${book.id}/print/vouchers.html?period_id=${p.id}&template_id=${tmp.id}`, api)).text()
      const slots = readSlots(html)
      // 找一张被拆开的凭证：同一凭证号出现了多张
      const first = slots[0]
      const same = slots.filter((x) => x.voucher === first.voucher)
      if (first.pages === '001' || same.length < 2) continue
      // 第 1 页合计是部分数；最后一张 = 整张凭证，借贷必须平
      if (same[0].dr >= same[same.length - 1].dr)
        throw new Error(`第 1 页合计 ${same[0].dr} 已经不小于整张 ${same[same.length - 1].dr}，不是累计`)
      if (same[same.length - 1].dr !== same[same.length - 1].cr)
        throw new Error(`最后一张借 ${same[same.length - 1].dr} 贷 ${same[same.length - 1].cr}，没累计到整张`)
      return
    }
    throw new Error('没找到可验的拆分凭证')
  } finally {
    await page.request.delete(`${BASE}/api/books/${book.id}/print/templates/${tmp.id}`, api)
  }
})

// ---- 模板改名 ----
// 选中项的文字在 .el-select__placeholder span 里，不是 input 的 value。
// 任何一步失败都要把名字改回去 —— 检查脚本不该把用户的模板留在「改名测试-xxx」上。
const tplSelect = () => page.locator('.toolbar .el-form-item:has-text("模板") .el-select__wrapper')
const renameBtn = () => page.locator('.toolbar button:has-text("重命名")')
const pickedName = async () =>
  (await page.locator('.toolbar .el-form-item:has-text("模板") .el-select__placeholder span').innerText()).trim()
const openOptions = async () => {
  await tplSelect().click()
  await page.waitForTimeout(500)
  return page.locator('.el-select-dropdown__item:visible')
}
/** 通过界面改名，再回原名；无论中间哪一步炸了都收尾。 */
async function withRename(fn) {
  const token = await page.evaluate(() => localStorage.getItem('cwq_token'))
  const api = { headers: { Authorization: 'Bearer ' + token } }
  const list = await (await page.request.get(`${BASE}/api/books/59/print/templates`, api)).json()
  const mine = (list || []).filter((t) => t.scope === 'VOUCHER')
  const tpl = mine.find((t) => t.is_default) || mine[0]
  const oldName = tpl?.name
  const undo = () => page.request.put(`${BASE}/api/books/59/print/templates/${tpl.id}`,
    { ...api, data: { name: oldName } })
  try {
    await fn(tpl, oldName)
  } finally {
    await undo()
  }
}

await step('工具栏有重命名按钮', async () => {
  if (await renameBtn().count() !== 1) throw new Error('没有重命名按钮')
})
await step('内置默认模板不能改名', async () => {
  const opts = await openOptions()
  await opts.filter({ hasText: '（内置默认）' }).first().click()
  await page.waitForTimeout(1500)
  if (!(await renameBtn().isDisabled())) throw new Error('内置默认模板的重命名按钮没禁用')
})
await step('重命名后下拉里是新名字', async () => {
  await withRename(async (tpl, oldName) => {
    const opts = await openOptions()
    await opts.filter({ hasText: oldName }).first().click()
    await page.waitForTimeout(1500)
    if (await renameBtn().isDisabled()) throw new Error('自定义模板的重命名按钮被禁用了')
    if ((await pickedName()) !== oldName) throw new Error('没选中目标模板')
    const footerBefore = await page.locator('.footer-bar').innerText()
    const want = oldName + '·改名'
    await renameBtn().click()
    await page.waitForSelector('.el-message-box input', { timeout: 8000 })
    await page.fill('.el-message-box input', want)
    await page.click('.el-message-box__btns button.el-button--primary')
    await page.waitForTimeout(1000)
    if ((await pickedName()) !== want) throw new Error('下拉里还是 ' + await pickedName())
    // 改名只是改名字，不该顺手把版式标成「未保存」
    if ((await page.locator('.footer-bar').innerText()) !== footerBefore)
      throw new Error('改名动了版式')
  })
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(2500)
})
await step('改名已回滚，模板名没被检查脚本留下', async () => {
  if ((await pickedName()).includes('·改名')) throw new Error('模板名没改回去：' + await pickedName())
})

console.log(errors.length ? '\n应用报错:\n' + errors.join('\n') : '\n无应用报错')
await browser.close()
