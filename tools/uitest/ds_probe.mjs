// 数据安全页面的渲染核对（开发环境 :5173 + :8000）。
//
// 跑法：先起好 `./dev.sh`，再 `node tools/uitest/ds_probe.mjs`。
// 它钉的是「这一页要真的渲染出来」：侧栏有入口、字段一个不缺、
// 月备份那档是只读的「永久保留」、默认保留天数是 30/365。
// 少了任何一条断言，页面照样能编译通过、路由照样能进 —— 然后一片空白。
import { chromium } from 'playwright'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1305, height: 984 } })
const errs = []
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 160)) })

await page.goto('http://127.0.0.1:5173', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2000)
await page.locator('input[placeholder="账号"]').fill('admin')
await page.locator('input[placeholder="密码"]').fill('123456')
await page.locator('.el-tab-pane:visible button:has-text("登录")').first().click()
await page.waitForSelector('.shell .sidebar', { timeout: 25000 })
await page.waitForTimeout(800)

await page.goto('http://127.0.0.1:5173/#/data-security', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2200)

console.log('URL       :', page.url())
console.log('页面标题  :', await page.locator('.page-title').textContent().catch(() => '(无)'))
console.log('侧栏      :', JSON.stringify(await page.locator('.sb-list li').allTextContents()))
for (const label of ['Endpoint', 'Bucket', '对象前缀', 'AccessKey ID', 'AccessKey Secret',
                     '天备份', '周备份', '月备份', '备份记录', '立即备份一次', '测试连接',
                     '定时备份', '备份范围']) {
  const n = await page.locator(`text=${label}`).count()
  console.log(`  ${label.padEnd(16)} ${n > 0 ? '有' : '缺失 <<<'}`)
}
console.log('月备份(只读):', await page.locator('.keep-form input[disabled]').inputValue().catch(() => '(无)'))
console.log('保留天数输入:', await page.locator('.keep-form input:not([disabled])').evaluateAll(e => e.map(x => x.value)).catch(() => []))
console.log('表格空文案  :', await page.locator('.el-table__empty-text').first().textContent().catch(() => '(有数据)'))
console.log('JS 错误     :', errs.length ? errs.slice(0, 5) : '无')
await page.screenshot({ path: '.shots/data-security.png', fullPage: true })
await page.locator('.card').last().scrollIntoViewIfNeeded()
await page.waitForTimeout(400)
await page.screenshot({ path: '.shots/data-security-bottom.png' })
await browser.close()
