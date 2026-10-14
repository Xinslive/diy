/** 打印设计器回归：使用真实后端渲染，按屏幕坐标比较预览和覆盖层。 */
import { chromium } from 'playwright'
import { mkdir } from 'node:fs/promises'

const BASE = process.env.UI_BASE || 'http://127.0.0.1:5173'
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } })
const page = await context.newPage()
page.setDefaultTimeout(8000)
const failures = []
let checks = 0
const step = async (name, fn) => {
  try { await fn(); checks++; console.log('✓', name) }
  catch (e) { failures.push(name + ': ' + e.message); console.log('✗', failures.at(-1)); await page.screenshot({ path: `.shots/pt-new-fail-${checks}.png` }) }
}
page.on('pageerror', (e) => failures.push('页面错误: ' + e.message))
page.on('console', (m) => { if (m.type() === 'error') failures.push('控制台: ' + m.text()) })
await mkdir('.shots', { recursive: true })
const settle = () => page.waitForTimeout(1100)
const choose = async (selector, name) => {
  await page.locator(selector).click()
  await page.locator('.el-select-dropdown__item:visible').filter({ hasText: name }).first().click()
  await settle()
}
const pageTab = () => page.getByRole('tab', { name: '页面', exact: true }).click()
const selectTable = async () => {
  await page.getByRole('tab', { name: '元素', exact: true }).click()
  await page.locator('.elist li').filter({ hasText: '分录表格' }).first().click()
}
const table = () => page.locator('.slot > .el').filter({ hasText: '表格 · 分录' })
const drag = async (target, dx, dy, anchor = 'center') => {
  const r = await target.boundingBox()
  if (!r) throw new Error('拖拽目标不存在')
  const x = r.x + r.width / 2, y = anchor === 'top' ? r.y + 5 : r.y + r.height / 2
  await page.mouse.move(x, y)
  await page.mouse.down()
  await page.mouse.move(x + dx, y + dy, { steps: 10 })
  await page.mouse.up()
  await settle()
}

// 测试的是最终屏幕矩形，不能用 offsetLeft 忽略父容器偏移。
const geometry = () => page.evaluate(() => {
  const fr = document.querySelector('iframe.frame')
  const d = fr?.contentDocument
  const index = Number(document.querySelector('.page-no')?.textContent?.split('/')[0]) - 1
  const paper = d?.querySelectorAll('.page')[Math.max(0, index)]
  const slotIndex = [...document.querySelectorAll('.slot-switch button')].findIndex((e) => e.classList.contains('on'))
  const slot = paper?.querySelectorAll('.voucher-slot')[Math.max(0, slotIndex)]
  if (!fr || !slot) throw new Error('真实凭证槽位未渲染')
  const f = fr.getBoundingClientRect()
  const s = f.width / parseFloat(getComputedStyle(fr).width)
  const real = [...slot.querySelectorAll('.voucher-element')].map((e) => {
    const r = e.getBoundingClientRect()
    return { x: f.left + r.left * s, y: f.top + r.top * s, w: r.width * s, h: r.height * s }
  })
  const overlay = [...document.querySelectorAll('.slot > .el:not(.off)')].map((e) => {
    const r = e.getBoundingClientRect()
    return { x: r.left, y: r.top, w: r.width, h: r.height }
  })
  if (real.length !== overlay.length || !real.length) throw new Error(`控件数量 ${real.length}/${overlay.length}`)
  const worst = Math.max(...real.flatMap((r, i) => ['x', 'y', 'w', 'h'].map((k) => Math.abs(r[k] - overlay[i][k]))))
  return { worst, real, overlay, scale: s }
})
const assertAligned = async () => {
  const result = await geometry()
  if (result.worst > 1.5) throw new Error(`屏幕偏差 ${result.worst.toFixed(2)}px`)
  return result
}

const cleanup = []
let api, bookId
try {
  await page.goto(BASE, { waitUntil: 'networkidle' })
  if (await page.locator('input[type=password]').count()) {
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForSelector('.sidebar', { timeout: 20000 })
  }
  const token = await page.evaluate(() => localStorage.getItem('cwq_token'))
  api = { headers: { Authorization: `Bearer ${token}` } }
  const books = await (await page.request.get(`${BASE}/api/books`, api)).json()
  const candidates = [...books].sort((a, b) => (b.id === 59 ? 1 : 0) - (a.id === 59 ? 1 : 0))
  let period
  for (const book of candidates) {
    if (!['OWNER', 'ACCOUNTANT'].includes(book.role)) continue
    const periods = await (await page.request.get(`${BASE}/api/books/${book.id}/periods`, api)).json()
    period = periods.filter((p) => p.vouchers >= 3).sort((a, b) => b.vouchers - a.vouchers)[0]
    if (period) { bookId = book.id; break }
  }
  if (!period) throw new Error('需要一个至少有 3 张凭证的可写账套来验证翻页与双凭证')
  await page.evaluate(({ b, p }) => {
    localStorage.setItem('cwq_book', b)
    localStorage.setItem('cwq_period', p)
  }, { b: String(bookId), p: String(period.id) })
  const name = `__pt_geometry_${Date.now()}`
  const text = { type: 'text', visible: true, font_size: 9, align: 'left', last_page_only: false }
  const fixture = {
    paper: 'A5', orientation: 'landscape', margins: { top: 10, right: 8, bottom: 10, left: 8 },
    elements: [
      { ...text, id: 'title', bind: 'title', text: '记 账 凭 证', x: 32, y: 3, w: 36, h: 8, font_size: 18, align: 'center' },
      { ...text, id: 'company', bind: 'company', text: '单位：{company}', x: 3, y: 12, w: 32, h: 5 },
      { ...text, id: 'voucher_no', bind: 'voucher_no', text: '凭证字号：{voucher_no}', x: 66, y: 12, w: 31, h: 10 },
      { id: 'table', type: 'table', bind: 'entries', x: 3, y: 24, w: 94, h: 55, font_size: 9, visible: true },
      { ...text, id: 'footer', bind: 'custom', text: '制单：{accountant}', x: 3, y: 90, w: 32, h: 5 },
    ],
  }
  const created = await page.request.post(`${BASE}/api/books/${bookId}/print/templates`, {
    ...api, data: { scope: 'VOUCHER', name, content: fixture },
  })
  if (!created.ok()) throw new Error('创建临时模板失败: ' + await created.text())
  const tmp = await created.json(); cleanup.push(tmp.id)
  await page.reload({ waitUntil: 'networkidle' })
  await page.goto(BASE + '/#/print-templates', { waitUntil: 'networkidle' })
  await settle()
  await choose('.pt-bar .sel-tpl', name)

  await step('A5：实际表格与编辑框屏幕对齐', async () => {
    const result = await assertAligned()
    console.log(`  ${result.real.length} 个控件，最大屏幕偏差 ${result.worst.toFixed(2)}px`)
    const paper = await page.locator('.paper').boundingBox()
    if (paper.width < 550) throw new Error('A5 预览过小')
  })
  await page.screenshot({ path: '.shots/pt-new-a5.png' })

  await step('纸面选中表格能直接打开对应属性', async () => {
    await table().click()
    if (!(await page.locator('.ti-elements:visible').count())) throw new Error('没有显示元素检查器')
    if (!(await page.locator('.elist li.on').innerText()).includes('分录表格')) throw new Error('选中的元素不对应')
  })
  await step('拖拽位移和缩放换算正确', async () => {
    const before = await geometry()
    await drag(table(), 0, -26)
    const after = await assertAligned()
    const delta = after.real[3].y - before.real[3].y
    if (Math.abs(delta + 26) > 2) throw new Error(`实际移动 ${delta.toFixed(1)}px，预期 -26px`)
    if (!(await page.locator('.save-bar.dirty').count())) throw new Error('未提示未保存')
  })
  await step('放大、缩小和恢复适应后仍对齐', async () => {
    for (const title of ['放大', '放大', '缩小']) {
      await page.locator(`.stage-tools button[title="${title}"]`).click(); await assertAligned()
    }
    await page.locator('.stage-tools button').filter({ hasText: '适应' }).click(); await assertAligned()
  })
  await step('四角缩放：左上角保持右下位置', async () => {
    const before = await table().boundingBox()
    await drag(table().locator('.grip.nw'), 18, 12)
    const after = await table().boundingBox()
    if (Math.abs((before.x + before.width) - (after.x + after.width)) > 2 ||
        Math.abs((before.y + before.height) - (after.y + after.height)) > 2) throw new Error(`右下角随左上拖动而偏移 ${JSON.stringify({ before, after })}`)
    await assertAligned()
  })
  await step('列分隔线与真实表格列宽一起移动', async () => {
    const boundary = () => page.evaluate(() => {
      const f = document.querySelector('iframe.frame'), r = f.getBoundingClientRect()
      const th = f.contentDocument.querySelector('.voucher-element.table th').getBoundingClientRect()
      return r.left + th.right * r.width / parseFloat(getComputedStyle(f).width)
    })
    const before = await boundary()
    await drag(table().locator('.colgrip').first(), 12, 0)
    if (Math.abs((await boundary()) - before - 12) > 2) throw new Error('列宽拖动距离不匹配')
    await assertAligned()
  })
  await step('检查器可以收起并再次展开', async () => {
    const before = await page.locator('.paper').boundingBox()
    await page.locator('.rail-toggle').click(); await page.waitForTimeout(250)
    const after = await page.locator('.paper').boundingBox()
    if (after.width < before.width + 20) throw new Error('收起后预览没有扩大')
    await assertAligned()
    await page.locator('.rail-toggle').click()
    await page.locator('.rail').waitFor({ state: 'visible' })
    await assertAligned()
  })
  await step('放弃改动还原模板', async () => {
    await page.locator('.save-bar button').filter({ hasText: '放弃改动' }).click(); await settle()
    if (await page.locator('.save-bar.dirty').count()) throw new Error('放弃后仍为未保存')
    await assertAligned()
  })
  await step('多页导航：真实凭证内容和覆盖层同时切换', async () => {
    const old = await page.locator('.page-no').innerText()
    if (Number(old.split('/')[1]) < 2) throw new Error('没有实际多页数据')
    await page.locator('.stage-tools .tool-group').first().locator('button').last().click()
    if (!(await page.locator('.page-no').innerText()).startsWith('2 /')) throw new Error('没有切到第 2 页')
    await assertAligned()
    await table().click(); await drag(table(), 0, -12)
    if (!(await page.locator('.page-no').innerText()).startsWith('2 /')) throw new Error('编辑第 2 页后跳回第 1 页')
    await assertAligned()
    await page.locator('.save-bar button').filter({ hasText: '放弃改动' }).click(); await settle()
  })
  await step('A4 纵向两个槽位、第二页及横向槽位全部对齐', async () => {
    await pageTab(); await choose('.ti-page .el-select', 'A4')
    for (const slot of [0, 1]) {
      await page.locator('.slot-switch button').nth(slot).click(); await assertAligned()
    }
    await page.locator('.slot-switch button').first().click()
    await page.locator('.stage-tools .tool-group').first().locator('button').last().click()
    await assertAligned()
    await page.locator('.ti-page .el-radio-button').filter({ hasText: '横向' }).click()
    await settle()
    for (const slot of [0, 1]) {
      await page.locator('.slot-switch button').nth(slot).click(); await assertAligned()
    }
    await page.screenshot({ path: '.shots/pt-new-a4.png' })
    await choose('.ti-page .el-select', 'A5')
    await assertAligned()
  })
  await step('页边距范围与后端一致，拖动距离换算正确', async () => {
    await pageTab()
    const top = page.locator('.ti-page input[type=number]').first()
    await top.fill('50'); await top.press('Enter'); await settle()
    if (await top.inputValue() !== '50') throw new Error('页边距被限制在 40mm')
    await top.fill('10'); await top.press('Enter'); await settle()
    await page.locator('.pt-bar button').filter({ hasText: '边距' }).click()
    const s = (await geometry()).scale
    await drag(page.locator('.guides .edge.top'), 0, 20, 'top')
    const expected = 10 + 20 / s / (96 / 25.4)
    if (Math.abs(Number(await top.inputValue()) - expected) > 0.2) throw new Error('边距位移换算错误')
    await assertAligned()
    await page.locator('.pt-bar button').filter({ hasText: '边距' }).click()
    await page.locator('.save-bar button').filter({ hasText: '放弃改动' }).click(); await settle()
  })
  await step('双击文本：Esc 取消、快捷键与失焦保存', async () => {
    const title = () => page.locator('.slot > .el').first()
    await title().dblclick()
    await page.locator('.inline-edit textarea').fill('应当取消')
    await page.locator('.inline-edit textarea').press('Escape'); await settle()
    if (await page.locator('.save-bar.dirty').count()) throw new Error('Esc 取消仍保存了文字')
    await title().dblclick()
    await page.locator('.inline-edit textarea').fill('测试标题')
    await page.locator('.inline-edit textarea').press('Control+Enter'); await settle()
    const htmlText = await page.locator('iframe.frame').evaluate((f) => f.contentDocument.body.innerText)
    if (!htmlText.includes('测试标题')) throw new Error('快捷键没有保存文字')
    await title().dblclick()
    await page.locator('.inline-edit textarea').fill('失焦保存')
    await page.locator('.pt-title').click(); await settle()
    if (!(await page.locator('iframe.frame').evaluate((f) => f.contentDocument.body.innerText)).includes('失焦保存')) throw new Error('失焦没有保存')
    await page.locator('.save-bar button').filter({ hasText: '放弃改动' }).click(); await settle()
  })
  await step('删除选中的元素后清除选中态', async () => {
    await page.getByRole('tab', { name: '元素', exact: true }).click()
    await page.locator('.pal-btn').filter({ hasText: '文本' }).click()
    const count = await page.locator('.slot > .el').count()
    await page.locator('.elist li.on button[title="删除"]').click(); await settle()
    if (await page.locator('.slot > .el').count() !== count - 1 || await page.locator('.slot > .el.sel').count()) throw new Error('删除后仍有残留选中态')
    await assertAligned()
    if (await page.locator('.save-bar.dirty').count()) {
      await page.locator('.save-bar button').filter({ hasText: '放弃改动' }).click(); await settle()
    }
  })
  await step('线条和矩形的编辑框与实际渲染边界一致', async () => {
    await page.getByRole('tab', { name: '元素', exact: true }).click()
    for (const label of ['线条', '矩形']) {
      await page.locator('.pal-btn').filter({ hasText: label }).click(); await settle()
      await assertAligned()
    }
    await page.locator('.save-bar button').filter({ hasText: '放弃改动' }).click(); await settle()
  })
  await step('新建、拖动、保存、重新载入的往返一致', async () => {
    const nextName = name + '_roundtrip'
    await page.locator('.pt-bar button').filter({ hasText: '新建' }).click()
    await page.locator('.el-message-box input').fill(nextName)
    await page.locator('.el-message-box__btns button.el-button--primary').click(); await settle()
    const list = await (await page.request.get(`${BASE}/api/books/${bookId}/print/templates`, api)).json()
    const created = list.find((t) => t.name === nextName)
    if (!created) throw new Error('新建模板没有写入')
    cleanup.push(created.id)
    await table().click(); await drag(table(), 0, -18)
    const wanted = (await geometry()).real[3]
    await page.locator('.save-bar button').filter({ hasText: '保存模板' }).click(); await settle()
    if (await page.locator('.save-bar.dirty').count()) throw new Error('保存后仍然未保存')
    await page.reload({ waitUntil: 'networkidle' }); await settle()
    await choose('.pt-bar .sel-tpl', nextName)
    const actual = (await assertAligned()).real[3]
    if (Math.abs(wanted.y - actual.y) > 1.5) throw new Error('重载后位置改变')
  })
  await step('打印入口优先使用当前已保存模板与期间', async () => {
    const selected = name + '_roundtrip'
    const requests = []
    const capture = (r) => { if (r.url().includes('/print/vouchers.html')) requests.push(r.url()) }
    page.on('request', capture)
    await page.locator('.pt-bar button').filter({ hasText: '打印' }).click()
    await page.locator('.print-dialog .side .el-select').first().waitFor({ state: 'visible' })
    await settle()
    if (!(await page.locator('.print-dialog .side .el-select').first().innerText()).includes(selected)) throw new Error('打印窗口没有选择当前模板')
    const request = new URL(requests.at(-1))
    if (Number(request.searchParams.get('template_id')) !== cleanup.at(-1) ||
        Number(request.searchParams.get('period_id')) !== period.id) throw new Error('打印模板或期间与编辑页面不同')
    await page.locator('.print-dialog button').filter({ hasText: '取消' }).click()
    await choose('.pt-bar .sel-tpl', '（内置默认）')
    await page.locator('.pt-bar button').filter({ hasText: '打印' }).click()
    await page.locator('.print-dialog .side .el-select').first().waitFor({ state: 'visible' })
    await settle()
    if (!(await page.locator('.print-dialog .side .el-select').first().innerText()).includes('内置默认')) throw new Error('内置版式被其它模板替换')
    if (new URL(requests.at(-1)).searchParams.get('template_id') !== '0') throw new Error('打印请求没有明确选用内置版式')
    await page.locator('.print-dialog button').filter({ hasText: '取消' }).click()
    page.off('request', capture)
  })
  await step('总账长表被切成真实 A4，热点仍可定位', async () => {
    await choose('.pt-bar .sel:not(.sel-tpl)', '总账')
    if (await page.locator('.slot > .el').count()) throw new Error('总账仍有凭证控件')
    for (const key of ['title', 'subtitle', 'meta', 'table', 'footer']) {
      await page.locator(`.hotspot.${key}`).click(); await page.waitForTimeout(250)
      if (key !== 'meta' && !(await page.evaluate((k) => document.querySelector(`.ti [data-field="${k}"]`)?.contains(document.activeElement), key))) throw new Error(key + ' 没有定位')
    }
    // 后端把总账所有行进同一个 .page，.page 只有 min-height 没有上限，
    // 一行一个科目就能撑成好几倍 A4。预览必须把它切回一张张真纸。
    const geo = await page.evaluate(() => {
      const d = document.querySelector('iframe.frame').contentDocument
      const pages = [...d.querySelectorAll('.page')]
      const a4 = 297 * 96 / 25.4
      return {
        count: pages.length,
        heights: pages.map((p) => p.getBoundingClientRect().height),
        a4,
        paper: parseFloat(document.querySelector('.paper').style.height),
        rowsPerPage: pages.map((p) => p.querySelectorAll('tbody tr').length),
        // 每张纸的「底部余量」= 纸底 - 内容底（页脚）。尾页本来就该空着，不算。
        gaps: pages.map((p) => {
          const r = p.getBoundingClientRect()
          const foot = p.querySelector('.doc-footer')?.getBoundingClientRect()
          const bottom = foot ? foot.bottom : (p.lastElementChild?.getBoundingClientRect().bottom || r.top)
          return Math.round(r.height - (bottom - r.top))
        }),
      }
    })
    if (geo.count < 2) throw new Error(`总账长表没有被切页（只有 ${geo.count} 张）`)
    const tall = geo.heights.filter((h) => h > geo.a4 * 1.04)
    if (tall.length) throw new Error(`有 ${tall.length} 张纸超过 A4：${geo.heights.map(Math.round)}`)
    if (geo.rowsPerPage.some((n) => n === 0)) throw new Error('切出了空白页')
    // 除尾页外，每张都要排满。回归过一次：量高时把 .page 的内边距扣了两次，
    // 每页白扔 8~9 行，纸下半截永远空着（修复前首张底部余 302px / 27%）。
    // 阈值取 15%：正常剩的是「下边距 + 不足一行的零头」，离阈值很远。
    const loose = geo.gaps.slice(0, -1).map((g, i) => [i + 1, g, Math.round(g / geo.heights[i] * 100)])
      .filter(([, g]) => g > geo.heights[0] * 0.15)
    if (loose.length) throw new Error(`第 ${loose.map(([n, g, p]) => `${n}张余${g}px(${p}%)`).join('、')} 没排满`)
    console.log(`   ${geo.count} 张 A4，每张 ${geo.rowsPerPage.join('/')} 行，底部余 ${geo.gaps.join('/')}px，纸高 ${Math.round(geo.paper)}px（A4=${Math.round(geo.a4)}px）`)
    // 页码按切出来的张数重算，不能每张都写「第 1 页 共 1 页」
    const footers = await page.evaluate(() => [...document.querySelector('iframe.frame').contentDocument
      .querySelectorAll('.page')].map((p) => p.querySelector('.doc-footer')?.innerText.trim() || ''))
    if (footers.length && footers.some((f) => /共 1 页/.test(f))) throw new Error('页脚总页数没有重算：' + JSON.stringify(footers))
    if (geo.paper > geo.a4 * 1.04) throw new Error(`纸还是太长：${Math.round(geo.paper)}px`)
    await page.locator('.pt-bar button').filter({ hasText: '边距' }).click()
    const edge = await page.locator('.guides .edge.top').boundingBox()
    if (edge.y < 150) throw new Error('进入边距模式时没有回到纸面顶部')
  })
  await step('明细账、科目余额表和报表均能预览', async () => {
    for (const scope of ['明细账', '科目余额表', '报表']) {
      await choose('.pt-bar .sel:not(.sel-tpl)', scope)
      if (await page.locator('.stage-err').count()) throw new Error(scope + ' 预览失败')
      if (!(await page.locator('iframe.frame').evaluate((f) => f.contentDocument.querySelector('table.grid')?.rows?.length))) throw new Error(scope + ' 未生成表格')
      if (scope !== '科目余额表' && !(await page.locator('.stage-opts .el-select').count())) throw new Error(scope + ' 缺少范围选择')
    }
  })
  await step('1280px 视口下保存、打印和检查器仍可操作', async () => {
    await page.setViewportSize({ width: 1280, height: 800 }); await page.waitForTimeout(300)
    for (const text of ['打印', '保存']) {
      const r = await page.locator('.pt-bar button').filter({ hasText: text }).last().boundingBox()
      if (!r || r.right > 1280 || r.x + r.width > 1280) throw new Error(text + ' 超出视口')
    }
    await page.setViewportSize({ width: 1600, height: 1000 })
  })
  await step('VIEWER 权限：所有写控件禁用，预览与热点仍可用', async () => {
    await context.route('**/api/books', async (route) => {
      const response = await route.fetch()
      const data = await response.json()
      await route.fulfill({ response, json: data.map((b) => ({ ...b, role: 'VIEWER' })) })
    })
    await page.reload({ waitUntil: 'networkidle' }); await settle()
    if (!(await page.locator('.ro-note').count()) || await page.locator('.slot > .el').count()) throw new Error('只读模式仍可拖动凭证')
    for (const text of ['新建', '保存', '边距']) {
      if (await page.locator('.pt-bar button').filter({ hasText: text }).last().isEnabled()) throw new Error(text + ' 仍可操作')
    }
    await page.locator('.pt-bar .el-dropdown').click()
    for (const text of ['重命名', '复制一份', '删除']) {
      if (await page.locator('.el-dropdown-menu__item:visible').filter({ hasText: text }).isEnabled()) throw new Error(text + ' 仍可操作')
    }
    await page.keyboard.press('Escape')
    await choose('.pt-bar .sel:not(.sel-tpl)', '总账')
    await page.locator('.hotspot.title').click()
    if (await page.locator('.ti-page input:not(:disabled)').count()) throw new Error('只读检查器存在可写输入框')
    if (!(await page.locator('.hotspot').count())) throw new Error('只读账簿无法查看热点')
    await context.unroute('**/api/books')
  })
} catch (e) {
  failures.push('测试初始化/运行: ' + e.message)
} finally {
  for (const id of cleanup.reverse()) {
    const r = await page.request.delete(`${BASE}/api/books/${bookId}/print/templates/${id}`, api)
    if (!r.ok()) failures.push('临时模板未清理: ' + id)
  }
  await browser.close()
}
console.log(`\n${checks} 项通过；${failures.length} 项失败`)
if (failures.length) console.log(failures.join('\n'))
process.exitCode = failures.length ? 1 : 0
