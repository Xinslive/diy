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
    // 口令不再预填（见 LoginView.vue：生产登录页预填演示口令那次修复），
    // 只点「登录」会停在登录页，.sidebar 永远等不到。
    await page.fill('input[type=password]', process.env.CWQ_PASSWORD || '123456')
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
  // A4 横向 + 每页一张 —— 这正是老 A5 横向模板归一化之后的样子（0024）。
  // 故意用显式的 slots_per_page 而不是留个纸型：纸型已经不再表达「一页几张」，
  // 这里写纸型等于把一条已经作废的口径又用回测试夹具里。
  ...testApi, data: {
    scope: 'VOUCHER', name: testName,
    content: { paper: 'A4', orientation: 'landscape', voucher: { slots_per_page: 1 } },
  },
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
const pickScope = async (name) => {
  await page.locator('.pt-bar .sel:not(.sel-tpl)').click()
  await page.waitForTimeout(300)
  await visibleOptions().filter({ hasText: name }).first().click()
  await settle()
}

await step('更多菜单有重命名动作', async () => {
  const items = await openMore()
  if (await items.filter({ hasText: '重命名' }).count() !== 1) throw new Error('没有重命名菜单项')
  await page.keyboard.press('Escape')
})

// 断言的是**规则**不是**数据**：这两份基线模板是用户自己建的（名字会变），
// 脚本不去依赖任何一个具体名字，也不拿用户的模板当夹具。
await step('凭证模板不再提供内置默认，且默认标记唯一', async () => {
  const opts = await openTemplateOptions()
  const names = (await opts.allTextContents()).map((s) => s.trim()).filter(Boolean)
  if (names.some((n) => n.includes('内置'))) throw new Error(`凭证模板里还有内置默认：${names.join('/')}`)
  if (!names.length) throw new Error('凭证模板列表是空的')
  const marked = names.filter((n) => n.includes('（默认）'))
  if (marked.length !== 1) throw new Error(`默认标记不唯一（${marked.length} 个）：${names.join('/')}`)
  await page.keyboard.press('Escape')
})

await step('内置默认模板不能改名', async () => {
  // 内置版式只在「该范围一份保存的模板都没有」时才出现在列表里（凭证范围已有基线），
  // 所以这道改名门禁放到总账上验 —— 内置版式是后端 DEFAULT_TEMPLATES 的兜底，
  // 不是一行可编辑的模板记录。
  await pickScope('总账')
  const opts = await openTemplateOptions()
  await opts.filter({ hasText: '（内置默认）' }).first().click()
  await settle()
  const items = await openMore()
  if (await items.filter({ hasText: '重命名' }).isEnabled()) throw new Error('内置默认模板仍可改名')
  await page.keyboard.press('Escape')
  await pickScope('记账凭证')
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

// 「用户也能改，改的只是他自己那一份」—— 整套写时复制只在这一步里被看见。
// 断言放在 API 上（浏览器这一侧根本看不见「基线有没有被动过」），
// UI 上只钉住两件用户能感知的事：改完之后列表里那份已经切过去了，
// 以及删掉它之后基线原样回来。
await step('改系统基线会落成一份只有改动者看得见的版式，基线原封不动', async () => {
  const token = await page.evaluate(() => localStorage.getItem('cwq_token'))
  const api = { headers: { Authorization: 'Bearer ' + token } }
  const books = await (await page.request.get(`${BASE}/api/books`, api)).json()
  const book = books.find((b) => b.id === 59) || books[0]
  const tplUrl = `${BASE}/api/books/${book.id}/print/templates`
  const list = async () => await (await page.request.get(tplUrl, api)).json()
  const baseline = (await list()).find((t) => t.is_baseline && t.scope === 'VOUCHER')
  if (!baseline) throw new Error('没有凭证范围的系统基线模板')
  const want = `${baseline.name}·我的版本`

  let mine = null
  try {
    // 从基线出发改名：这一步会触发后端的写时复制
    const opts = await openTemplateOptions()
    await opts.filter({ hasText: baseline.name }).first().click()
    await settle()
    const items = await openMore()
    await items.filter({ hasText: '重命名' }).click()
    await page.waitForSelector('.el-message-box input', { timeout: 8000 })
    await page.fill('.el-message-box input', want)
    await page.click('.el-message-box__btns button.el-button--primary')
    await page.waitForTimeout(1200)

    const after = await list()
    mine = after.find((t) => t.is_private && t.forked_from === baseline.id)
    if (!mine) throw new Error('没有生成私有副本')
    if (mine.name !== want) throw new Error(`副本名不该是 ${mine.name}`)
    // 基线在**我的**列表里让位给副本（两份同名摆着，用户得自己猜哪份在用）。
    // 基线本身有没有被动过，浏览器这一侧看不见 —— 那条由后端用例
    // test_改基线落成我的私有副本_基线一个字节不动 在库里断言。
    if (after.some((t) => t.id === baseline.id)) {
      throw new Error('我的列表里基线与副本同时出现')
    }
    // 副本要**整份**带走基线：默认标记跟着走，改名不动版式。
    if (mine.is_default !== baseline.is_default) throw new Error('副本没继承基线的默认标记')
    if (JSON.stringify(mine.content) !== JSON.stringify(baseline.content)) {
      throw new Error('副本内容与基线不一致（改名不该动版式）')
    }
    if ((await pickedName()).includes('·我的版本') === false) {
      throw new Error('下拉里没有切到我的版本')
    }
    // 下拉里**不该**再冒出「（我的）」这类标记。写时复制是实现细节：用户改两下、
    // 存一下，看到的仍是那份熟悉的模板名。私有性在 API 上验（上面的 is_private
    // 与 forked_from），界面上刻意不提醒 —— 这一条是防止它被「顺手加回来」。
    const opts2 = await openTemplateOptions()
    const labels = (await opts2.allTextContents()).map((s) => s.trim())
    const mineLabel = labels.find((l) => l.includes('我的版本'))
    if (!mineLabel) throw new Error(`下拉里找不到我的版本：${labels.join('/')}`)
    // **只认标签括号里那几段。** 模板名字本身就叫「…·我的版本」，拿
    // `includes('我的')` 去扫整条 label 必然命中自己 —— 那条断言会一直绿，
    // 哪怕「（我的）」标记又被人加回来了。只比括号里的内容才是在测这件事。
    const tagOf = (l) => {
      const m = l.match(/[（(]([^（()）]*)[）)]\s*$/)
      return m ? m[1].split('·').map((x) => x.trim()).filter(Boolean) : []
    }
    const tags = tagOf(mineLabel)
    if (tags.includes('我的')) {
      throw new Error(`私有副本又被标了「我的」：${mineLabel}`)
    }
    // 「默认」那个标记必须还在 —— 那是真会印出来的那一份，不能一起删掉。
    if (!tags.includes('默认')) {
      throw new Error(`副本丢了「默认」标记：${mineLabel}`)
    }
    await page.keyboard.press('Escape')
  } finally {
    // 删掉副本就回到基线了 —— 留着就是往用户的库里塞垃圾。
    if (mine) await page.request.delete(`${tplUrl}/${mine.id}`, api)
  }
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(2200)
  const restored = (await list()).find((t) => t.id === baseline.id)
  if (!restored || restored.is_baseline !== true) throw new Error('副本删掉后基线没回来')
})

await step('系统基线不能删除', async () => {
  const token = await page.evaluate(() => localStorage.getItem('cwq_token'))
  const api = { headers: { Authorization: 'Bearer ' + token } }
  const books = await (await page.request.get(`${BASE}/api/books`, api)).json()
  const book = books.find((b) => b.id === 59) || books[0]
  const list = await (await page.request.get(`${BASE}/api/books/${book.id}/print/templates`, api)).json()
  const baseline = list.find((t) => t.is_baseline && t.scope === 'VOUCHER')
  const r = await page.request.delete(`${BASE}/api/books/${book.id}/print/templates/${baseline.id}`, api)
  if (r.status() !== 403) throw new Error(`基线删除返回 ${r.status()}，应为 403`)
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
