import { chromium } from 'playwright'
const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1400, height: 900 } })
// 每次跑都用不重名的账套：账套建了删不掉（没有删除接口），
// 重名会被后端挡回来，看着像「建账失败」，其实是上次残留
const STAMP = Date.now().toString(36).slice(-6)
const NEW_BOOK = 'UI验证账套-' + STAMP
const errs = []
await p.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' })
await p.locator('input').nth(0).fill('acct_ui')
await p.locator('input').nth(1).fill('acct12345')
const btn = p.locator('button:has-text("登录")')
if (await btn.count()) { await btn.click(); await p.waitForTimeout(2500) }
else { await p.click('button:has-text("注册")'); await p.waitForTimeout(2000)
  await p.locator('input').nth(0).fill('acct_ui'); await p.locator('input').nth(1).fill('acct12345')
  await p.locator('input').nth(2).fill('测试会计').catch(()=>{})
  await p.click('button:has-text("注册并登录"), button:has-text("登录")'); await p.waitForTimeout(2500) }
const items = (await p.locator('.el-menu .el-menu-item').allInnerTexts()).map(s => s.trim())
console.log('普通用户菜单:', items.join(' | '))
if (items.some(t => t.includes('操作日志'))) errs.push('普通用户仍能看到操作日志')
if (items.some(t => t.includes('用户与授权'))) errs.push('普通用户仍能看到用户与授权')
await p.goto('http://127.0.0.1:5173/#/audit'); await p.waitForTimeout(1500)
const url = p.url()
console.log('直接敲 /#/audit 之后:', url)
if (url.includes('/audit')) errs.push('普通用户手敲地址栏进了操作日志')
await p.goto('http://127.0.0.1:5173/#/users'); await p.waitForTimeout(1500)
console.log('直接敲 /#/users 之后:', p.url())
if (p.url().includes('/users')) errs.push('普通用户手敲地址栏进了用户与授权')
await p.screenshot({ path: '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots/v-nonadmin.png' })

// ---- 普通用户能不能自己建账套 ----
await p.goto('http://127.0.0.1:5173/#/books'); await p.waitForTimeout(1800)
const newBtn = p.locator('button:has-text("新建账套")')
console.log('普通用户看到「新建账套」按钮:', await newBtn.count() > 0)
if (!(await newBtn.count())) errs.push('普通用户看不到「新建账套」按钮')
let createdId = null
if (await newBtn.count()) {
  await newBtn.first().click(); await p.waitForTimeout(900)
  const dlg = p.locator('.el-dialog:visible')
  const inputs = dlg.locator('input')
  await inputs.nth(0).fill('UI验证公司-' + STAMP)
  await inputs.nth(1).fill(NEW_BOOK)
  await dlg.locator('button:has-text("创建账套")').click()
  await p.waitForTimeout(2500)
  const ok = await dlg.locator('.el-alert').count()
  const body = await p.locator('.el-table__body').innerText()
  const made = body.includes(NEW_BOOK)
  console.log('   账套出现在列表里:', made, '| 成功提示:', ok > 0)
  if (!made) errs.push('普通用户建的账套没出现在列表里')
  if (!ok) errs.push('建账后没出现「下一步录期初余额」的提示')
  await p.screenshot({ path: '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots/v-nonadmin-created.png' })
  // 建第一个账套时 bookId 0→新账套，页面会整个重挂载；提示必须接得回来，
  // 关掉之后也不该再弹
  await p.locator('.el-dialog:visible button:has-text("稍后再说")').click()
  await p.waitForTimeout(1200)
  if (await p.locator('.el-dialog:visible').count()) errs.push('点「稍后再说」后弹窗没关')
  // NEW_BOOK 是 node 侧的变量，page.evaluate 跑在浏览器里，必须当参数传进去
  createdId = await p.evaluate(async (name) => {
    const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
    const books = await (await fetch('/api/books', { headers: h })).json()
    const hit = books.find(b => b.name === name)
    return hit ? hit.id : null
  }, NEW_BOOK)
  console.log('   新账套 id =', createdId)
  if (!createdId) errs.push('接口里查不到刚建的账套')
  // 建完能进得去（说明 OWNER 权限确实发下来了）
  if (createdId) {
    // 科目表是跟着顶栏当前账套走的（路由是 /subjects，不是 /books/:id/subjects）
    await p.locator('.topbar .el-select').first().click(); await p.waitForTimeout(800)
    await p.locator('.el-select-dropdown__item:visible', { hasText: NEW_BOOK }).first().click()
    await p.waitForTimeout(1800)
    await p.goto('http://127.0.0.1:5173/#/subjects')
    await p.waitForTimeout(2000)
    const rows = await p.locator('.el-table__body tr').count()
    console.log('   进新账套的科目表，行数 =', rows)
    if (rows < 10) errs.push('新账套的科目表是空的，行数 ' + rows)
  }
  await p.screenshot({ path: '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots/v-nonadmin-newbook.png' })
}
if (errs.length) console.log('❌ ' + errs.join('\n'))
else console.log('✅ 普通用户看不到操作日志/用户与授权，但可以自己建账套')
console.log('CLEANUP_ID=' + createdId)
await b.close()
