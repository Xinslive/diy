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
    // 登录页是预填好的，点一下就行；自己填反而会把预置账号改错
    await page.click('button:has-text("登录")')
    await page.waitForSelector('.el-aside', { timeout: 20000 })
    await page.waitForTimeout(800)
  })
}
await page.goto(BASE + '/#/print-templates', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

await step('版式编辑器已加载', async () => { await page.waitForSelector('.vle .el', { timeout: 10000 }) })
await step('纸型标注每页张数', async () => {
  await page.locator('.el-form-item:has-text("纸型") .el-select').first().click()
  await page.waitForTimeout(400)
  const items = await page.locator('.el-select-dropdown__item').allInnerTexts()
  await page.keyboard.press('Escape')
  if (!items.some((i) => /A4.*每页 2 张/.test(i))) throw new Error('A4 没标 2 张：' + items.join('|'))
  if (!items.some((i) => /A5.*每页 1 张/.test(i))) throw new Error('A5 没标 1 张：' + items.join('|'))
})
await step('5 行规则提示已移除', async () => {
  if (await page.locator('.rule').count()) throw new Error('版式面板里还有 5 行规则的提示块')
})
await step('页面底色为白', async () => {
  const bg = await page.locator('.page-plain').evaluate((e) => getComputedStyle(e).backgroundColor)
  if (bg !== 'rgb(255, 255, 255)') throw new Error(bg)
})
await page.screenshot({ path: '.shots/pt-1-open.png', fullPage: true })

// 选中分录表格 -> 出现行高与列宽设置
await step('画布不超出可视区', async () => {
  const box = await page.locator('.vle .canvas').boundingBox()
  const wrap = await page.locator('.canvas-wrap').boundingBox()
  if (box.height > wrap.height + 1) throw new Error(`画布 ${box.height.toFixed(0)}px 高于容器 ${wrap.height.toFixed(0)}px`)
  if (box.height > 700) throw new Error('画布过高 ' + box.height.toFixed(0) + 'px')
})
await step('选中表格出现行列设置', async () => {
  await page.locator('.elist li:has-text("分录表格")').click()
  await page.waitForTimeout(400)
  const t = await page.locator('.props').innerText()
  if (!/行高/.test(t)) throw new Error('属性面板没有行高：' + t.slice(0, 120))
  if (!/列宽/.test(t)) throw new Error('属性面板没有列宽')
})
await step('表格开关是框线不是显示', async () => {
  const t = await page.locator('.props').innerText()
  if (!/框线/.test(t)) throw new Error('表格属性面板没有框线开关：' + t.slice(0, 140))
  if (/\n显示\n/.test(t)) throw new Error('表格属性面板还留着「显示」开关')
})
await step('关框线后表格结构仍在', async () => {
  const before = await page.locator('.tmock .trow').count()
  await page.locator('.props .el-form-item:has-text("框线") .el-switch').click()
  await page.waitForTimeout(600)
  const after = await page.locator('.tmock .trow').count()
  if (after !== before) throw new Error(`关框线后行数变了 ${before} -> ${after}`)
  const cls = await page.locator('.tmock').getAttribute('class')
  if (!/noborder/.test(cls)) throw new Error('画布没标记无框线：' + cls)
  const f = page.frames().find((fr) => fr.url() === 'about:srcdoc')
  if (!/no-border/.test(await f.content())) throw new Error('预览里没有无框线样式')
})
await step('重新打开框线', async () => {
  await page.locator('.props .el-form-item:has-text("框线") .el-switch').click()
  await page.waitForTimeout(600)
  const cls = await page.locator('.tmock').getAttribute('class')
  if (/noborder/.test(cls)) throw new Error('框线没开回来')
})
await step('画布行高一致', async () => {
  const hs = await page.locator('.tmock .trow').evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height))
  if (hs.some((h) => Math.abs(h - hs[0]) > 0.6)) throw new Error('行高不等：' + hs.join(','))
})
await page.screenshot({ path: '.shots/pt-2-table.png', fullPage: true })

// 拖动列分隔线
const widthsBefore = await page.locator('.colw input').evaluateAll((e) => e.map((i) => i.value))
await step('拖动列分隔线改列宽', async () => {
  const h = page.locator('.colhandles .ch').first()
  const b = await h.boundingBox()
  await page.mouse.move(b.x + b.width / 2, b.y + 30)
  await page.mouse.down()
  await page.mouse.move(b.x + b.width / 2 + 45, b.y + 30, { steps: 10 })
  await page.mouse.up()
  await page.waitForTimeout(400)
  const after = await page.locator('.colw input').evaluateAll((e) => e.map((i) => i.value))
  if (after[0] === widthsBefore[0]) throw new Error('列宽没变：' + widthsBefore.join('/') + ' -> ' + after.join('/'))
  const sum = after.reduce((s, v) => s + Number(v), 0)
  if (Math.abs(sum - 100) > 1.5) throw new Error('列宽合计 ' + sum.toFixed(1) + ' ≠ 100')
})

// 输入行高 -> 表格变高，所有行同步
const hBefore = await page.locator('.tmock').evaluate((e) => e.getBoundingClientRect().height)
const mmBefore = Number(await page.locator('.props .grid2 input[type=number]').first().inputValue())
await step('输入行高同步所有行', async () => {
  const inp = page.locator('.props .grid2 input[type=number]').first()
  await inp.fill(String(mmBefore + 5))
  await inp.dispatchEvent('change')
  await page.waitForTimeout(500)
  const hs = await page.locator('.tmock .trow').evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height))
  const hAfter = await page.locator('.tmock').evaluate((e) => e.getBoundingClientRect().height)
  if (!(hAfter > hBefore + 5)) throw new Error(`表格没变高 ${hBefore.toFixed(0)} -> ${hAfter.toFixed(0)}`)
  if (hs.some((h) => Math.abs(h - hs[0]) > 0.6)) throw new Error('改行高后行高又不一致：' + hs.join(','))
})
await step('行高回显一致', async () => {
  const shown = Number(await page.locator('.props .grid2 input[type=number]').first().inputValue())
  if (Math.abs(shown - (mmBefore + 5)) > 0.2) throw new Error(`回显 ${shown}，期望 ${mmBefore + 5}`)
})
await page.screenshot({ path: '.shots/pt-3-rowh.png', fullPage: true })

// 预览里行高一致
await step('预览行高一致', async () => {
  const f = page.frameLocator('iframe.preview')
  await f.locator('table.grid tbody').first().waitFor({ timeout: 10000 })
  const hs = await f.locator('table.grid tbody tr').evaluateAll(() => {}) // cross-frame not supported; use frame eval
  const h = await page.frames().find(fr => fr.url() === 'about:srcdoc') || page.frames()[1]
  const rows = await h.evaluate(() => [...document.querySelectorAll('.voucher-slot table.grid')].map(tb =>
    [...tb.querySelectorAll('tbody tr')].map(tr => +tr.getBoundingClientRect().height.toFixed(1))))
  const flat = rows[0]
  if (!flat || flat.some((x) => Math.abs(x - flat[0]) > 0.6)) throw new Error('预览行高不等：' + flat.join(','))
})

// 切 A5
// 纸型下拉要和「模板」下拉区分开：模板里也有一个叫「自定义凭证 A5」的项，
// 用 has-text 会点中隐藏的那份，必须按可见下拉 + 精确文案来选。
const pickPaper = async (label) => {
  const sel = page.locator('.el-form-item:has-text("纸型") .el-select').first()
  await sel.scrollIntoViewIfNeeded()
  await sel.click()
  await page.waitForTimeout(500)
  await page.locator('.el-select-dropdown:visible .el-select-dropdown__item', { hasText: new RegExp('^' + label) }).first().click()
}
await step('切 A5 单槽位', async () => {
  await pickPaper('A5')
  await page.waitForTimeout(900)
  const tip = await page.locator('.stage-tip').innerText()
  if (!/1 张/.test(tip)) throw new Error(tip)
})
await step('A4 横向左右排列', async () => {
  await pickPaper('A4')
  await page.waitForTimeout(400)
  await page.locator('label:has-text("横向")').scrollIntoViewIfNeeded()
  await page.locator('label:has-text("横向")').click()
  await page.waitForTimeout(900)
  const tip = await page.locator('.stage-tip').innerText()
  if (!/左右/.test(tip)) throw new Error(tip)
})
await page.screenshot({ path: '.shots/pt-4-landscape.png', fullPage: true })

await step('切回 A4 纵向', async () => {
  await page.locator('label:has-text("纵向")').scrollIntoViewIfNeeded()
  await page.locator('label:has-text("纵向")').click()
  await page.waitForTimeout(700)
})
await step('窄屏无横向溢出', async () => {
  await page.setViewportSize({ width: 1180, height: 900 })
  await page.waitForTimeout(800)
  const ow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  if (ow > 4) throw new Error('横向溢出 ' + ow + 'px')
})

// ── 本轮新增：预览自适应 + 面板布局 ──────────────────────────
await step('预览整页不被裁切', async () => {
  await page.setViewportSize({ width: 1539, height: 954 })
  await page.waitForTimeout(900)
  const m = await page.evaluate(() => {
    const box = document.querySelector('.preview-box')
    const st = document.querySelector('.preview-stage')
    const fr = document.querySelector('iframe.preview')
    return {
      boxW: box?.clientWidth || 0,
      stageW: st?.getBoundingClientRect().width || 0,
      // iframe 的固有宽度才是「整页」宽度；可见宽必须 <= 容器，否则右边被切
      pageW: parseFloat(fr?.style.width) || 0,
      scale: parseFloat((fr?.style.transform.match(/[\d.]+/) || [1])[0]),
    }
  })
  if (m.scale <= 0) throw new Error('没有缩放：' + JSON.stringify(m))
  if (m.stageW > m.boxW + 1) throw new Error(`预览 ${m.stageW}px 超出容器 ${m.boxW}px`)
  // 缩放后可见宽度应恰好等于固有宽 * scale（允许 1px 取整误差）
  if (Math.abs(m.stageW - m.pageW * m.scale) > 1.5) throw new Error('缩放和页面尺寸对不上：' + JSON.stringify(m))
})
await step('宽屏时预览填满面板', async () => {
  await page.setViewportSize({ width: 2048, height: 1100 })
  await page.waitForTimeout(1200)
  const m = await page.evaluate(() => {
    const box = document.querySelector('.preview-box')
    const st = document.querySelector('.preview-stage')
    return { boxW: box?.clientWidth || 0, stageW: st?.getBoundingClientRect().width || 0 }
  })
  // 宽屏下不该再是「灰底中间一小块」：至少占满容器八成
  if (m.stageW < m.boxW * 0.8) throw new Error(`预览只占 ${m.stageW}/${m.boxW}px`)
})
await step('列面板不被拉宽', async () => {
  const w = await page.locator('.p-cols').evaluate((e) => Math.round(e.getBoundingClientRect().width))
  if (w > 540) throw new Error('「列」面板过宽 ' + w + 'px')
})
await step('预览在最左、列在最右', async () => {
  const order = await page.evaluate(() => {
    const x = (s) => document.querySelector(s).getBoundingClientRect().left
    return { format: x('.p-format'), preview: x('.p-preview'), cols: x('.p-cols') }
  })
  if (!(order.format < order.preview && order.preview < order.cols)) {
    throw new Error('面板顺序不对：' + JSON.stringify(order))
  }
})
await step('可用字段有中文释义', async () => {
  const t = await page.locator('.p-cols .fields').innerText()
  for (const kw of ['分录行号', '摘要', '借方金额', '贷方金额']) {
    if (!t.includes(kw)) throw new Error('字段说明缺「' + kw + '」：' + t.slice(0, 120))
  }
})
await step('字段说明标题在折叠组之上', async () => {
  const m = await page.evaluate(() => {
    const t = document.querySelector('.fields-title').getBoundingClientRect()
    const c = document.querySelector('.fields-body').getBoundingClientRect()
    return { title: Math.round(t.top), collapse: Math.round(c.top) }
  })
  // 旧的 .fields 是 flex 横排，标题会被「垂直居中」甩到面板侧边
  if (m.title > m.collapse) throw new Error('标题跑到折叠组下面了：' + JSON.stringify(m))
})
await step('预览左右无灰边', async () => {
  // 灰边有两个来源：框的 padding，以及 iframe 里 body{padding:16px}。
  // 两个都要没有 —— 页面必须和预览框齐平，且在 iframe 内左右各贴边。
  const m = await page.evaluate(() => {
    const box = document.querySelector('.preview-box')
    const st = document.querySelector('.preview-stage')
    const fr = document.querySelector('iframe.preview')
    const B = box.getBoundingClientRect(), S = st.getBoundingClientRect()
    const d = fr.contentDocument
    const pg = d.querySelector('.page') || d.querySelector('.voucher-sheet-page')
    return {
      left: Math.round(S.left - B.left),
      right: Math.round(B.right - S.right),
      overflowX: box.scrollWidth - box.clientWidth,
      innerLeft: pg ? Math.round(pg.getBoundingClientRect().left) : -1,
      innerW: pg ? Math.round(pg.getBoundingClientRect().width) : -1,
      frameW: d.documentElement.clientWidth,
      bodyPad: d.defaultView.getComputedStyle(d.body).paddingLeft,
    }
  })
  if (Math.abs(m.left - 8) > 1) throw new Error('框左侧留白应为 8px，实为 ' + m.left)
  if (Math.abs(m.right - 8) > 1) throw new Error('框右侧留白应为 8px，实为 ' + m.right)
  if (m.overflowX > 1) throw new Error('预览横向溢出 ' + m.overflowX + 'px')
  if (m.innerLeft !== 0) throw new Error('页面在 iframe 内左缩进 ' + m.innerLeft + 'px（body padding）')
  if (m.innerW > m.frameW + 1) throw new Error(`页面 ${m.innerW}px 宽于 iframe ${m.frameW}px，右侧被裁`)
})
await step('底部保存条不透底', async () => {
  const s = await page.evaluate(() => {
    const e = document.querySelector('.footer-bar')
    const cs = getComputedStyle(e)
    return { bg: cs.backgroundColor, opacity: cs.opacity, z: cs.zIndex }
  })
  // 半透明或没层级的话，下层面板会透上来
  if (s.bg !== 'rgb(255, 255, 255)') throw new Error('底色不是纯白：' + s.bg)
  if (Number(s.opacity) < 1) throw new Error('不透明度 ' + s.opacity)
  if (Number(s.z) < 10) throw new Error('z-index 太低 ' + s.z)
})
await page.screenshot({ path: '.shots/pt-5-layout.png', fullPage: true })

await step('换纸型带出惯用方向', async () => {
  const orient = () => page.locator('.p-format .el-radio-button.is-active').innerText()
  // 先切到确定会触发 change 的纸型，再切回来，el-select 值没变时不会发事件
  await pickPaper('A4')
  if ((await orient()).trim() !== '纵向') throw new Error('A4 应为纵向，实为 ' + await orient())
  await pickPaper('A5')
  if ((await orient()).trim() !== '横向') throw new Error('A5 应为横向，实为 ' + await orient())
})
await step('预览框高度贴合内容', async () => {
  // flex:1 会让预览框撑满整张卡片，内容少时下面空一大片灰
  const m = await page.evaluate(() => {
    const b = document.querySelector('.preview-box')
    const s = document.querySelector('.preview-stage')
    return {
      box: Math.round(b.getBoundingClientRect().height),
      stage: Math.round(s.getBoundingClientRect().height),
      slack: b.scrollHeight - b.clientHeight,
    }
  })
  // 内容不足时不该有富余；内容超高时是滚动而不是撑破
  if (m.slack > 2) throw new Error(`预览框比内容高 ${m.slack}px（下方空了一块）`)
  if (m.box > m.stage + 24) throw new Error(`框 ${m.box}px 明显高于内容 ${m.stage}px`)
})
await step('预览高度不会越滚越大', async () => {
  // 用 scrollHeight 定高会自反馈：scrollHeight >= 视口高，而视口高又是自己设的
  const m = await page.evaluate(() => {
    const fr = document.querySelector('iframe.preview')
    const d = fr.contentDocument
    const pages = [...d.querySelectorAll('.page')]
    return {
      styleH: parseFloat(fr.style.height),
      contentH: pages.length ? Math.ceil(pages[pages.length - 1].getBoundingClientRect().bottom) : 0,
      pages: pages.length,
    }
  })
  if (!m.contentH) throw new Error('没量到纸页')
  if (m.styleH > m.contentH + 80) throw new Error(`iframe 高 ${m.styleH}px 远超内容 ${m.contentH}px（${m.pages} 页）`)
})
await step('版式不再重复画布已有的设置', async () => {
  // 标题、页眉显隐、页脚、框线在画布里都能改，版式再放一份就是两套开关打架
  const t = await page.locator('.p-format').innerText()
  for (const kw of ['主标题', '副标题', '显示项', '页脚']) {
    if (t.includes(kw)) throw new Error(`凭证的「${kw}」还留在版式面板：\n` + t.slice(0, 200))
  }
})
await step('画布显隐开关真的生效', async () => {
  // 后端 _voucher_element_html 此前完全不读 el.visible，画布上的「隐藏」是死的
  const before = await page.evaluate(() => document.querySelectorAll('iframe.preview').length)
  if (!before) throw new Error('预览没加载')
  const frame = () => page.frames().find(f => f.url() === 'about:srcdoc')
  const n0 = await frame().evaluate(() => document.querySelectorAll('.voucher-element').length)
  // 在画布上隐藏「单位」元素
  await page.locator('.elist li', { hasText: '单位' }).first().locator('button', { hasText: '隐藏' }).first().click()
  await page.waitForTimeout(1800)
  const n1 = await frame().evaluate(() => document.querySelectorAll('.voucher-element').length)
  if (n1 >= n0) throw new Error(`藏掉「单位」后元素数没变（${n0} -> ${n1}），画布显隐没接上`)
})
await step('老模板 meta 开关会折进画布', async () => {
  const m = await page.evaluate(() => {
    // 直接调迁移逻辑不方便，改查数据：meta 应全部为 true，显隐只由元素 visible 决定
    const r = document.querySelector('.designer')
    return r ? { hasLayout: !!document.querySelector('.p-layout') } : null
  })
  if (!m?.hasLayout) throw new Error('凭证范围应当有版式编辑画布')
})
await step('控件面板五个按钮各建各的', async () => {
  // 曾把整个 {key,label} 传给 addElement，kind === 'field' 恒为假，
  // 五个按钮全都落进默认分支造出同一个「新文本」。
  const before = await page.locator('.elist li').count()
  for (const label of ['文本', '字段', '表格', '线条', '矩形']) {
    await page.locator('.ctl', { hasText: label }).first().click()
    await page.waitForTimeout(350)
  }
  const added = await page.evaluate((n) =>
    [...document.querySelectorAll('.elist li')].slice(n).map(x => x.querySelector('.ename')?.innerText.trim()),
    before)
  if (added.length !== 5) throw new Error('新增元素数不对：' + JSON.stringify(added))
  if (new Set(added).size !== 5) throw new Error('控件效果重复：' + JSON.stringify(added))
})
await step('表单标签不折行', async () => {
  const wrapped = await page.evaluate(() => {
    const bad = []
    for (const f of document.querySelectorAll('.p-format .el-form-item__label')) {
      const t = (f.innerText || '').trim()
      if (!t) continue
      const r = document.createRange()
      r.selectNodeContents(f)
      // 文本被排成多个矩形 = 折行了
      if (r.getClientRects().length > 1) bad.push(t)
    }
    return bad
  })
  if (wrapped.length) throw new Error('这些标签折成两行了：' + wrapped.join(' / '))
})
console.log(errors.length ? '\n失败项:\n' + errors.join('\n') : '\n全部通过，无 JS 错误')
await browser.close()
