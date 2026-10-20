/**
 * 无账套时的落点（2026-10-20 随「去掉空状态提示」一起做的回归）。
 *
 * 两个场景各验一遍，缺一不可：
 *
 *  1. **一个账套都没有的人**：账套管理页上不再有那段长提示（只剩「0 个账套」
 *     和右上角那颗「新建账套」），而点任何账套相关的页面都会被送回账套管理。
 *  2. **有账套的人**：点账套相关的页面**必须留在原地**。
 *     场景 1 的判定建立在「账套列表问过了」这个事实上 ——
 *     判错一次，所有有账套的用户在打开深链接时都会被踢回账套管理。
 *     只验场景 1 的话，这个回归根本测不出来。
 *
 * 用法：先起本地环境（./dev.sh），再 node tools/uitest/ui_nobook.mjs
 */
import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1539, height: 954 } })
const errs = []
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()) })
const sleep = ms => page.waitForTimeout(ms)

let fail = 0
const check = (name, ok, detail = '') => {
  console.log((ok ? '  OK   ' : '  FAIL ') + name + (detail ? ' —— ' + detail : ''))
  if (!ok) fail++
}

/**
 * 备好一个「一个账套都没有」的账号。
 *
 * 走后台的建号接口（POST /api/system/users，books 留空），
 * 所以这个脚本自己就能跑，不依赖库里事先有人。
 * 账号已存在时把它的账套授权清空 —— 测试的前提是「一个都没有」。
 */
async function ensureNoBookUser() {
  const loginRes = await fetch(BASE + '/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'admin', password: '123456' })
  })
  if (!loginRes.ok) throw new Error('连本地后端都登不上，dev.sh 起了吗？')
  const { access_token: token } = await loginRes.json()
  const auth = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token }

  const users = await (await fetch(BASE + '/api/system/users', { headers: auth })).json()
  const found = users.find(u => u.username === 'nobook01')
  if (!found) {
    const res = await fetch(BASE + '/api/system/users', {
      method: 'POST', headers: auth,
      body: JSON.stringify({ username: 'nobook01', password: '123456', real_name: '无账套测试', books: [] })
    })
    if (!res.ok) throw new Error('建无账套账号失败：' + res.status)
    return
  }
  // 已存在：把账套授权清空，保证它真的是「一个账套都没有」
  // 这条接口收的是**一份列表本身**（不是 {books:[...]}），整份替换
  await fetch(BASE + `/api/system/users/${found.id}/books`, {
    method: 'PUT', headers: auth, body: JSON.stringify([])
  })
}

async function login(username) {
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await page.evaluate(() => localStorage.clear())
  await page.goto(BASE, { waitUntil: 'networkidle' })
  if (await page.locator('input[type=password]').count()) {
    await page.locator('input').nth(0).fill(username)
    await page.locator('input').nth(1).fill('123456')
    await page.click('button:has-text("登录")')
    try {
      // 侧栏是应用自己的 <aside class="sidebar">，不是 Element Plus 的 el-aside
      await page.waitForSelector('.sidebar', { timeout: 20000 })
    } catch {
      await page.screenshot({ path: OUT + `/login_failed_${username}.png` })
      throw new Error(`登录没进主界面：${username}，停在 ${page.url()}`)
    }
    await sleep(1200)
  }
}

console.log('== 准备：一个账套都没有的账号 ==')
await ensureNoBookUser()

console.log('')
console.log('== 场景一：一个账套都没有的人（nobook01） ==')
await login('nobook01')

check('落在账套管理页', page.url().endsWith('#/books'), page.url())
const bodyText = await page.locator('body').innerText()
check('页面上没有那句「还没有账套…」', !bodyText.includes('还没有账套'))
check('页面上没有「用户与授权」那句引导', !bodyText.includes('用户与授权'))
check('仍然显示「0 个账套」', bodyText.includes('0 个账套'))
check('右上角「新建账套」还在', bodyText.includes('新建账套'))
check('没有残留的空状态占位框', (await page.locator('.el-empty').count()) === 0)
await page.screenshot({ path: OUT + '/nobook_books.png' })

// 点侧栏里的「科目表」：以前它会渲染一张「暂无数据」的空表，看不出所以然
await page.click('.sidebar li:has-text("科目表")')
await sleep(1500)
check('点科目表被送回账套管理', page.url().endsWith('#/books'), page.url())

// 换一个账套相关的页面再验一次（走的是另一条路由）
await page.click('.sidebar li:has-text("打印模板")')
await sleep(1500)
check('点打印模板也回账套管理', page.url().endsWith('#/books'), page.url())

console.log('')
console.log('== 场景二：有账套的人（admin） ==')
await login('admin')

await page.goto(BASE + '/#/subjects', { waitUntil: 'networkidle' })
await sleep(2000)
check('打开深链接不会被踢回账套管理', page.url().endsWith('#/subjects'), page.url())
const subjectText = await page.locator('body').innerText()
check('科目表真的渲染出来了', subjectText.includes('科目编码规则'))
check('不是一张空表', !subjectText.includes('暂无数据'))
await page.screenshot({ path: OUT + '/admin_subjects.png' })

// 再走一次侧栏点击，确认不是只有深链接这条路是好的
await page.click('.sidebar li:has-text("账套管理")')
await sleep(1200)
await page.click('.sidebar li:has-text("科目表")')
await sleep(1500)
check('侧栏点科目表也留在科目表', page.url().endsWith('#/subjects'), page.url())

console.log('')
console.log('控制台错误：', errs.length ? errs : '无')
await browser.close()
process.exit(fail ? 1 : 0)
