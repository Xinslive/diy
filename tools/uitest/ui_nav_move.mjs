import { chromium } from 'playwright'
const BASE = 'http://127.0.0.1:5173'
const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1543, height: 984 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
const step = async (n, f) => { try { await f(); console.log('✓', n) } catch (e) { console.log('✗', n, '—', e.message.split('\n')[0]); errors.push(n) } }

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
const modules = () => page.locator('.modules button').allInnerTexts()
const sidebar = async (mod) => {
  await page.locator(`.modules button:has-text("${mod}")`).click()
  await page.waitForTimeout(500)
  return page.locator('.sb-list li').allInnerTexts()
}

await step('顶部没有「期末与输出」', async () => {
  await page.goto(BASE + '/#/vouchers', { waitUntil: 'networkidle' })
  await page.waitForTimeout(1500)
  const ms = (await modules()).map((t) => t.trim())
  if (ms.some((t) => t.includes('期末与输出'))) throw new Error('还在：' + ms.join('|'))
  if (ms.length !== 4) throw new Error('顶部模块数不对：' + ms.join('|'))
})
await step('「期末结账」在凭证处理下', async () => {
  const items = (await sidebar('凭证处理')).map((t) => t.trim())
  if (!items.includes('期末结账')) throw new Error('凭证处理下没有：' + items.join('|'))
  if (items.some((t) => /期末处理/.test(t))) throw new Error('还叫期末处理：' + items.join('|'))
})
await step('「打印模板」在基础资料下', async () => {
  const items = (await sidebar('基础资料')).map((t) => t.trim())
  if (!items.includes('打印模板')) throw new Error('基础资料下没有：' + items.join('|'))
})
await step('点期末结账进的是结账页，标签名也跟着改', async () => {
  await sidebar('凭证处理')
  await page.locator('.sb-list li:has-text("期末结账")').click()
  await page.waitForTimeout(1500)
  if (!/#\/periods/.test(page.url())) throw new Error('路由不对：' + page.url())
  const tabs = await page.locator('.tabbar, .tabs').first().innerText().catch(() => '')
  const body = await page.locator('.page').first().innerText()
  if (/期末处理/.test(tabs + body)) throw new Error('页面上还写着「期末处理」')
})
await step('点打印模板归属基础资料', async () => {
  await page.goto(BASE + '/#/print-templates', { waitUntil: 'networkidle' })
  await page.waitForTimeout(2000)
  const head = (await page.locator('.sb-head').innerText()).trim()
  if (head !== '基础资料') throw new Error('侧栏头是 ' + head)
  const on = await page.locator('.sb-list li.active').innerText()
  if (on.trim() !== '打印模板') throw new Error('高亮的是 ' + on)
  const top = await page.locator('.modules button.active').innerText()
  if (top.trim() !== '基础资料') throw new Error('顶部亮的是 ' + top)
})
await step('从期初余额的文案里点得到结账页', async () => {
  await page.goto(BASE + '/#/openings', { waitUntil: 'networkidle' })
  await page.waitForTimeout(1500)
  const t = await page.locator('.page').first().innerText()
  if (/「期末处理」/.test(t)) throw new Error('期初余额页还写「期末处理」')
})
await page.screenshot({ path: '.shots/nav-master.png', fullPage: false })
await step('侧栏表头与顶栏同高，下边线连成一条', async () => {
  await page.goto(BASE + '/#/periods', { waitUntil: 'networkidle' })
  await page.waitForTimeout(1500)
  const measure = async () => {
    const head = await page.locator('.sidebar .sb-head').boundingBox()
    const bar = await page.locator('header.topbar').boundingBox()
    const sb = await page.locator('aside.sidebar').boundingBox()
    return { dy: Math.abs(head.y + head.height - bar.y - bar.height), dx: Math.abs(sb.x + sb.width - bar.x) }
  }
  const open = await measure()
  if (open.dy > 0.5) throw new Error('两条下边线差 ' + open.dy.toFixed(1) + 'px')
  if (open.dx > 0.5) throw new Error('侧栏右边与顶栏左边差 ' + open.dx.toFixed(1) + 'px')
  await page.locator('.topbar .icon-btn').click()      // 收起侧栏
  await page.waitForTimeout(600)
  const off = await measure()
  if (off.dy > 0.5) throw new Error('收起后两条下边线差 ' + off.dy.toFixed(1) + 'px')
  await page.locator('.topbar .icon-btn').click()      // 展开回来
  await page.waitForTimeout(600)
  const icon = await page.locator('.sidebar .sb-head .el-icon').boundingBox()
  const head = await page.locator('.sidebar .sb-head').boundingBox()
  const mid = icon.y + icon.height / 2 - (head.y + head.height / 2)
  if (Math.abs(mid) > 1) throw new Error('模块图标没在表头里垂直居中，偏 ' + mid.toFixed(1) + 'px')
})

await step('顶栏没有品牌徽标，折叠按钮后面直接是模块菜单', async () => {
  await page.goto(BASE + '/#/detail-ledger', { waitUntil: 'networkidle' })
  await page.waitForTimeout(1500)
  if (await page.locator('.topbar .brand, .topbar .logo, .topbar .brand .name').count())
    throw new Error('顶栏还有品牌区块')
  const bar = await page.locator('header.topbar').innerText()
  if (/CWQ|财务记账/.test(bar)) throw new Error('顶栏文字里还有品牌：' + bar.replace(/\n/g, '|'))
  const btn = await page.locator('.topbar .icon-btn').first().boundingBox()
  const mod = await page.locator('.modules button').first().boundingBox()
  const gap = mod.x - (btn.x + btn.width)
  if (gap < 8 || gap > 40) throw new Error('折叠按钮与第一个模块之间空隙 ' + gap.toFixed(1) + 'px，不像是紧挨着')
  const ms = (await modules()).map((t) => t.trim())
  if (ms.length !== 4) throw new Error('模块数量不对：' + ms.join('|'))
})
await step('侧栏不显示整理凭证和凭证导入', async () => {
  const items = (await sidebar('凭证处理')).map((t) => t.trim())
  const gone = items.filter((t) => /整理凭证|凭证导入/.test(t))
  if (gone.length) throw new Error('还在：' + gone.join('|'))
  if (!items.includes('凭证') || !items.includes('期末结账')) throw new Error('剩下的不对：' + items.join('|'))
  await page.goto(BASE + '/#/vouchers', { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
  const again = (await page.locator('.sb-list li').allInnerTexts()).map((t) => t.trim())
  if (again.some((t) => /整理凭证|凭证导入/.test(t))) throw new Error('在凭证页又冒出来了：' + again.join('|'))
})
await step('隐藏的页面仍能直接敲地址进，标签名也对', async () => {
  for (const [path, title] of [['#/vouchers/organize', '整理凭证'], ['#/vouchers/import', '凭证导入']]) {
    await page.goto(BASE + '/' + path, { waitUntil: 'networkidle' })
    await page.waitForTimeout(1500)
    const body = await page.locator('.page').first().innerText()
    if (body.length < 10) throw new Error(path + ' 没渲染出来')
    const tabbar = await page.locator('header + div, .tabbar').first().innerText().catch(() => '')
    if (!tabbar.includes(title)) throw new Error(path + ' 的标签名不是「' + title + '」：' + tabbar.slice(0, 80))
    if (await page.locator('.sb-list li').allInnerTexts().then((a) => a.some((t) => /整理凭证|凭证导入/.test(t))))
      throw new Error(path + ' 打开时侧栏又把隐藏项露出来了')
  }
})
await step('点凭证处理落到 #/vouchers', async () => {
  await page.goto(BASE + '/#/books', { waitUntil: 'networkidle' })
  await page.waitForTimeout(1500)
  await page.locator('.modules button:has-text("凭证处理")').click()
  await page.waitForTimeout(1500)
  if (!/#\/vouchers(\?|$)/.test(page.url())) throw new Error('到了 ' + page.url())
})
await step('点账簿报表落到 #/detail-ledger', async () => {
  await page.locator('.modules button:has-text("账簿报表")').click()
  await page.waitForTimeout(1500)
  if (!/#\/detail-ledger(\?|$)/.test(page.url())) throw new Error('到了 ' + page.url())
})
await step('点基础资料落到 #/books', async () => {
  await page.locator('.modules button:has-text("基础资料")').click()
  await page.waitForTimeout(1500)
  if (!/#\/books(\?|$)/.test(page.url())) throw new Error('到了 ' + page.url())
})
await step('系统管理落到第一项（操作日志）', async () => {
  await page.locator('.modules button:has-text("系统管理")').click()
  await page.waitForTimeout(1500)
  if (!/#\/audit(\?|$)/.test(page.url())) throw new Error('到了 ' + page.url())
})
await step('在模块内点模块不动页面', async () => {
  await page.goto(BASE + '/#/balance', { waitUntil: 'networkidle' })
  await page.waitForTimeout(1500)
  await page.locator('.modules button:has-text("账簿报表")').click()
  await page.waitForTimeout(900)
  if (!/#\/balance(\?|$)/.test(page.url())) throw new Error('被弹走了：' + page.url())
  const on = (await page.locator('.sb-list li.active').innerText()).trim()
  if (on !== '科目余额表') throw new Error('侧栏高亮的是 ' + on)
})
await page.screenshot({ path: '.shots/nav-voucher.png', fullPage: false })

console.log(errors.length ? '\n应用报错:\n' + errors.join('\n') : '\n无应用报错')
await browser.close()
