// Reuses the repository's plain Playwright probe pattern, against a temporary DB only.
import { chromium } from 'playwright'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
const [phase, stateFile] = process.argv.slice(2)
const state = JSON.parse(await fs.readFile(stateFile, 'utf8'))
const browser = await chromium.launch({ headless: true })
const output = new URL('../../output/playwright/', import.meta.url)
await fs.mkdir(output, { recursive: true })
const requests = [], errors = []
async function login(username) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const page = await context.newPage()
  page.on('request', r => { if (r.url().includes('/api/plugins/')) requests.push({ url: r.url(), auth: !!r.headers().authorization }) })
  page.on('pageerror', e => errors.push(e.message))
  const document = await page.goto(state.url + '/#/login')
  await page.getByPlaceholder('账号', { exact: true }).fill(username)
  await page.getByPlaceholder('密码', { exact: true }).fill(state.password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL('**/#/books')
  await page.locator('aside.sidebar').waitFor()
  return { context, page, document }
}
async function manager(page) { await page.goto(state.url + '/#/plugins'); await page.getByRole('heading', { name: '扩展管理' }).waitFor() }
function row(page, id) { return page.getByRole('row').filter({ hasText: id }) }
async function grant(page, id, enabled) {
  await row(page, id).getByRole('button', { name: '管理用户' }).click()
  const dialog = page.getByRole('dialog').filter({ hasText: '使用授权' })
  await dialog.getByRole('textbox', { name: '搜索可授权用户' }).fill('plugin-staff')
  await dialog.getByRole('button', { name: '搜索', exact: true }).click()
  const box = dialog.getByRole('checkbox', { name: '授权 plugin-staff', exact: true })
  if (await box.isChecked() !== enabled) await box.locator('xpath=ancestor::label').click()
  assert.equal(await box.isChecked(), enabled)
  const saved = page.waitForResponse(r => r.url().endsWith(`/api/system/plugins/${id}/users`) && r.request().method() === 'PUT')
  await dialog.getByRole('button', { name: '保存授权名单' }).click()
  assert.equal((await saved).status(), 200)
  await dialog.waitFor({ state: 'hidden' })
}
try {
  const admin = await login('plugin-admin')
  await manager(admin.page)
  if (phase === 'install') {
    for (const bundle of state.packages) {
      const response = admin.page.waitForResponse(r => r.url().endsWith('/api/system/plugins/install'))
      await admin.page.locator('input[type=file]').setInputFiles(bundle.path)
      const installed = await response
      assert.equal(installed.status(), 200, await installed.text())
      await row(admin.page, bundle.id).waitFor()
      assert.match(await row(admin.page, bundle.id).innerText(), /等待重启/)
      assert.match(await row(admin.page, bundle.id).innerText(), /0 人/)
      await grant(admin.page, bundle.id, true)
    }
    await admin.page.screenshot({ path: fileURLToPath(new URL('plugin-install.png', output)), fullPage: true })
    console.log('PASS UI install, pending restart, zero initial grants, user search/picker/save')
  } else {
    const normal = state.packages[0], bad = state.packages[1]
    const adminToken = await admin.page.evaluate(() => localStorage.getItem('cwq_token'))
    const adminManifest = await admin.context.request.get(state.url + '/api/plugins/manifest', { headers: { Authorization: `Bearer ${adminToken}` } })
    assert.deepEqual(await adminManifest.json(), { plugins: [] })
    const staff = await login('plugin-staff')
    const token = await staff.page.evaluate(() => localStorage.getItem('cwq_token'))
    const manifest = await staff.context.request.get(state.url + '/api/plugins/manifest', { headers: { Authorization: `Bearer ${token}` } })
    assert.match(manifest.headers()['cache-control'], /(?:^|,\s*)no-store(?:,|$)/)
    const granted = (await manifest.json()).plugins
    assert.equal(granted.length, 3)
    const assets = granted.find(p => p.id === normal.id)
    // **资产 URL 里带一张短期票据**（backend/app/plugins/security.py::asset_token），
    // 长这样：/api/plugins/assets/<id>/<digest>/frontend/_t/<到期秒>.<签名>/index.js
    //
    // 票据的到期时间是**整秒**（int(time.time()) + 3600），所以**两次 manifest
    // 响应拿到的 URL 会差一个时间戳段** —— 测试自己取的那份 manifest 与页面
    // 取的那份不在同一秒时，URL 就对不上。
    //
    // 2026-10-21 的 CI 就是这么红的：同一行断言连续三轮时绿时红（跨秒就红）。
    // 断言真正要守的性质是「**浏览器以不带 Authorization 的方式**加载插件
    // 代码」（插件代码走 import 静态路径不带令牌，插件数据走 fetch 带令牌），
    // 所以这里把比较基准缩到**与票据无关**的那部分：
    //   前缀 = /api/plugins/assets/<id>/，文件名 = index.js
    const assetPrefix = '/api/plugins/assets/' + assets.id + '/'
    const entryFile = assets.entry.split('/').pop()
    const publicAsset = await browser.newContext()
    const source = await publicAsset.request.get(state.url + assets.entry)
    assert.equal(source.status(), 200)
    assert.match(source.headers()['cache-control'], /immutable/)
    assert.equal((await publicAsset.request.get(state.url + `/api/plugins/${normal.id}/notes?book_id=${state.bookId}`)).status(), 401)
    const main = staff.document
    // **在导航之前就把等待挂上**（2026-10-21 修）。
    //
    // 插件入口是用**动态 import()** 加载的（frontend/src/plugins/runtime.js），
    // 那条请求由浏览器发起、不带 Authorization —— 这正是这条断言要守的东西：
    // 插件的**代码**走 <script>/import 静态路径（不携带令牌），
    // 插件的**数据**走 fetch（带令牌）。一旦有人把入口改成用带令牌的
    // fetch 去拉，令牌就会进到资源请求里。
    //
    // 原来是在后面用 ``requests.some(...)`` 去翻**已经记过的数组**：
    // 那条 import 请求到底有没有被记下来，取决于它相对断言的时序 ——
    // 于是同一份代码上一轮 CI 是绿的、下一轮就红了（连续两轮红在
    // plugin_e2e.mjs:84）。改成「先挂等待、再触发」，就不再靠运气。
    // catch 立刻挂上：否则超时时的 rejection 在 await 之前就成了「未处理的
    // promise 拒绝」，而 Node 默认会让进程带着它退出。
    const entryRequest = staff.page.waitForRequest(
      r => new URL(r.url()).pathname.startsWith(assetPrefix) && r.url().endsWith(entryFile)
        && !r.headers().authorization,
      { timeout: 20000 }).catch(() => null)
    await staff.page.goto(state.url + `/#/plugin/${normal.id}`)
    assert.match(main.headers()['content-security-policy'], /script-src 'self';/)
    assert.ok(!main.headers()['content-security-policy'].includes('unsafe-eval'))
    await staff.page.getByTestId('plugin-note').waitFor()
    await staff.page.getByRole('textbox', { name: '工作笔记' }).fill('浏览器签名插件往返验证')
    const created = staff.page.waitForResponse(r => r.url().endsWith(`/api/plugins/${normal.id}/notes`) && r.request().method() === 'POST')
    await staff.page.getByRole('button', { name: '保存笔记' }).click()
    assert.equal((await created).status(), 200)
    await staff.page.getByRole('listitem').filter({ hasText: '浏览器签名插件往返验证' }).waitFor()
    await staff.page.screenshot({ path: fileURLToPath(new URL('plugin-authorized.png', output)), fullPage: true })
    const entryReq = await entryRequest
    assert.ok(entryReq,
      `插件入口资源应当由浏览器以「不带 Authorization」的方式加载；` +
      `实际记录到的请求：${JSON.stringify(requests.map(r => ({ url: r.url, auth: r.auth })))}`)
    assert.ok(requests.some(r => r.url.includes(`/api/plugins/${normal.id}/notes`) && r.auth))
    assert.ok(requests.every(r => !/[?&](token|access_token)=/i.test(r.url)))
    const csv = state.packages[2]
    await staff.page.goto(state.url + `/#/plugin/${csv.id}`)
    await staff.page.getByTestId('csv-voucher').waitFor()
    const styled = () => staff.page.waitForFunction(() => {
      const root = document.querySelector('[data-testid="csv-voucher"]')
      return root && getComputedStyle(root).paddingTop === '24px'
    })
    await styled()
    const target = staff.page.getByRole('combobox', { name: '目标账套', exact: true })
    await target.selectOption(String(state.bookId))
    const debit = staff.page.getByRole('combobox', { name: '借方科目', exact: true })
    await debit.locator('option[value="1001"]').waitFor({ state: 'attached' })
    await debit.selectOption('1001')
    await staff.page.getByRole('combobox', { name: '贷方科目', exact: true }).selectOption({ index: 2 })
    const configSaved = staff.page.waitForResponse(r => r.url().includes('/_host/config/') && r.request().method() === 'PUT')
    await staff.page.getByRole('button', { name: '保存账套配置', exact: true }).click()
    assert.equal((await configSaved).status(), 200)
    await staff.page.getByLabel('上传 CSV 文件', { exact: true }).setInputFiles({ name: 'browser.csv', mimeType: 'text/csv',
      buffer: Buffer.from('date,amount,summary\n2026-03-02,12.50,浏览器第一笔\n2026-03-03,7.25,浏览器第二笔\n') })
    await staff.page.getByRole('button', { name: '上传文件', exact: true }).click()
    await staff.page.getByTestId('csv-state').filter({ hasText: 'UPLOADED' }).waitFor()
    await staff.page.getByRole('button', { name: '解析并生成预览', exact: true }).click()
    await staff.page.getByTestId('csv-state').filter({ hasText: 'PREVIEW_READY' }).waitFor()
    const batchText = await staff.page.getByTestId('csv-state').innerText()
    const batchId = batchText.match(/批次 ([a-f0-9]+)/)[1]
    await staff.page.getByRole('textbox', { name: '金额 1', exact: true }).fill('19.50')
    // 顶部切换到另一账套后，目标和未保存的调整仍留在插件页面。
    await staff.page.locator('.ctx-select').first().click()
    await staff.page.locator('li[role="option"]').filter({ hasText: /^切换演练账套$/ }).click()
    assert.equal(await target.inputValue(), String(state.bookId))
    assert.equal(await staff.page.getByRole('textbox', { name: '金额 1', exact: true }).inputValue(), '19.50')
    const edited = staff.page.waitForResponse(r => r.url().endsWith(`/imports/${batchId}/preview`) && r.request().method() === 'PUT')
    await staff.page.getByRole('button', { name: '保存调整后的预览', exact: true }).click()
    assert.equal((await edited).status(), 200)
    await staff.page.reload()
    await styled()
    await target.selectOption(String(state.bookId))
    await staff.page.getByRole('combobox', { name: '恢复批次', exact: true }).locator(`option[value="${batchId}"]`).waitFor({ state: 'attached' })
    await staff.page.getByRole('combobox', { name: '恢复批次', exact: true }).selectOption(batchId)
    await staff.page.getByTestId('csv-state').filter({ hasText: 'EDITING' }).waitFor()
    assert.equal(await staff.page.getByRole('textbox', { name: '金额 1', exact: true }).inputValue(), '19.50')
    await staff.page.getByRole('button', { name: '确认当前预览', exact: true }).click()
    await staff.page.getByTestId('csv-state').filter({ hasText: 'CONFIRMED' }).waitFor()
    const submitted = staff.page.waitForResponse(r => r.url().endsWith(`/imports/${batchId}/submit`))
    await staff.page.getByRole('button', { name: '整批提交', exact: true }).click()
    const committed = await submitted
    assert.equal(committed.status(), 200, await committed.text())
    const savedBatch = await committed.json()
    assert.equal(savedBatch.book_id, state.bookId)
    assert.equal(savedBatch.result.length, 2)
    await staff.page.getByTestId('csv-result').filter({ hasText: '记0001' }).waitFor()
    await styled()
    await staff.page.screenshot({ path: fileURLToPath(new URL('plugin-csv-submitted.png', output)), fullPage: true })
    const duplicate = await staff.context.request.post(state.url + `/api/plugins/${csv.id}/_host/imports/${batchId}/submit`, {
      headers: { Authorization: `Bearer ${token}` }, data: { revision: savedBatch.revision } })
    assert.deepEqual((await duplicate.json()).result, savedBatch.result)
    // 后续笔记恢复演练继续在原账套操作。
    await staff.page.locator('.ctx-select').first().click()
    await staff.page.locator('li[role="option"]').filter({ hasText: /^插件隔离演练账套$/ }).click()
    console.log('PASS CSV book config, real multipart upload, preview edit, global book switch preserves draft, refresh recovery, manual confirm, atomic submit and idempotent replay')
    await staff.page.goto(state.url + `/#/plugin/${normal.id}`)
    await staff.page.getByTestId('plugin-note').waitFor()
    const outsider = await login('plugin-other')
    await outsider.page.goto(state.url + `/#/plugin/${normal.id}`)
    await outsider.page.waitForURL('**/#/books')
    assert.equal(await outsider.page.getByTestId('plugin-note').count(), 0)
    const otherToken = await outsider.page.evaluate(() => localStorage.getItem('cwq_token'))
    const refused = await outsider.context.request.get(state.url + `/api/plugins/${normal.id}/notes?book_id=${state.bookId}`, { headers: { Authorization: `Bearer ${otherToken}` } })
    assert.equal(refused.status(), 404)
    assert.equal((await refused.json()).detail.code, 'PLUGIN_NOT_FOUND_OR_UNAVAILABLE')
    await grant(admin.page, normal.id, false)
    const oldToken = await staff.context.request.get(state.url + `/api/plugins/${normal.id}/notes?book_id=${state.bookId}`, { headers: { Authorization: `Bearer ${token}` } })
    assert.equal(oldToken.status(), 404)
    await staff.page.evaluate(() => window.dispatchEvent(new Event('focus')))
    await staff.page.waitForURL('**/#/books')
    assert.equal(await staff.page.locator(`link[href*="${normal.id}"]`).count(), 0)
    assert.equal(await staff.page.getByTestId('plugin-note').count(), 0)
    const tabs = await staff.page.evaluate(() => localStorage.getItem('cwq_tabs'))
    assert.ok(!tabs?.includes(`plugin:${normal.id}:`))
    await grant(admin.page, normal.id, true)
    await staff.page.goto(state.url + `/#/plugin/${normal.id}`)
    await staff.page.getByRole('listitem').filter({ hasText: '浏览器签名插件往返验证' }).waitFor()
    await staff.page.goto(state.url + `/#/plugin/${bad.id}`)
    await staff.page.getByRole('heading', { name: '扩展暂不可用' }).waitFor()
    await staff.page.screenshot({ path: fileURLToPath(new URL('plugin-fallback.png', output)), fullPage: true })
    await staff.page.goto(state.url + '/#/books')
    await staff.page.locator('aside.sidebar').waitFor()
    assert.ok(errors.every(message => message.includes('expected frontend failure')), errors.join('\n'))
    console.log('PASS native import without Bearer, protected API with Bearer, same-origin CSP, book write/read, unauthorized admin/other, live revoke/regrant, route/tab/style cleanup, failed module fallback')
  }
} catch (error) {
  for (const [index, context] of browser.contexts().entries()) {
    for (const [pageIndex, page] of context.pages().entries()) {
      await page.screenshot({ path: fileURLToPath(new URL(`plugin-failure-${phase}-${index}-${pageIndex}.png`, output)), fullPage: true }).catch(() => {})
    }
  }
  console.error('Browser errors:', errors)
  throw error
} finally { await browser.close() }
