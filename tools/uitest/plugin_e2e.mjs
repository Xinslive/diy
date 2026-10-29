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
async function grant(page, id, enabled, username = 'plugin-staff') {
  await row(page, id).getByRole('button', { name: '管理用户' }).click()
  const dialog = page.getByRole('dialog').filter({ hasText: '使用授权' })
  await dialog.getByRole('textbox', { name: '搜索可授权用户' }).fill(username)
  await dialog.getByRole('button', { name: '搜索', exact: true }).click()
  const box = dialog.getByRole('checkbox', { name: `授权 ${username}`, exact: true })
  if (await box.isChecked() !== enabled) await box.locator('xpath=ancestor::label').click()
  assert.equal(await box.isChecked(), enabled)
  const saved = page.waitForResponse(r => r.url().endsWith(`/api/system/plugins/${id}/users`) && r.request().method() === 'PUT')
  await dialog.getByRole('button', { name: '保存授权名单' }).click()
  assert.equal((await saved).status(), 200)
  await dialog.waitFor({ state: 'hidden' })
}
// 共享卡场景：单独成一个函数，调用处才好**大声**跳过。
//
// **插件源码不在 git 里**（/plugins/ 是 gitignore 的，见 .gitignore 第 104 行）：
// 干净 checkout 里没有它 —— 那时这一段整个不跑。悄悄少跑一段比红着更坏
// （红着会有人来看），所以调用处要打一行 SKIP 而不是无声跳过。
async function sharedcardScenario({ admin, staff, state, sharedcard }) {
    // ───────────────────────────────────────── 共享卡对账：一张卡、两个账套、一份对账单
    //
    // 跑的是**仓库里那个真的插件包**（plugins/com.cwq.sharedcard），不是替身。
    // 它守的是共享卡这条主线在真实浏览器里成立：
    // 建卡 → 两本账各认领（同一张卡、科目编码不同）→ 上传**一份**对账单 →
    // **人工指定归属** → 卡级余额核对（哪本账少记了多少）。
    //
    // **插件不制证**（1.1.0 起）—— 所以这里没有「生成凭证」那一步。
    // **插件也没有归属规则**（1.2.0 起）—— 每一行都人工指定，所以这里
    // 没有「加规则」「按规则归属」那两步。

    // ① 管理员建卡 —— 卡是**全局表**，宿主只放行管理员写全局表。
    await admin.page.goto(state.url + `/#/plugin/${sharedcard.id}`)
    const adminSc = admin.page.getByTestId('sharedcard')
    await adminSc.waitFor()
    await adminSc.getByRole('textbox', { name: '卡名', exact: true }).fill('浏览器演练共享卡')
    await adminSc.getByRole('button', { name: '创建', exact: true }).click()
    await adminSc.locator('.sc-card-click').filter({ hasText: '浏览器演练共享卡' }).waitFor()

    // ② 会计账号做日常：建卡按钮**置灰**并说清为什么（不是点一下再报错）。
    await staff.page.goto(state.url + `/#/plugin/${sharedcard.id}`)
    const sc = staff.page.getByTestId('sharedcard')
    await sc.waitFor()
    await staff.page.getByTestId('sc-cards').waitFor()
    assert.equal(await sc.getByRole('button', { name: '创建', exact: true }).isDisabled(), true)
    await sc.locator('.sc-hint').filter({ hasText: '只有系统管理员能新建' }).waitFor()

    // ③ 两本账各认领这张卡 —— 同一张卡，科目编码不同。
    //    第一本账记在 1002 银行存款，第二本账记在 **1001 库存现金**：
    //    宿主那条流水对账线只认 1002，共享卡这条线**两个族都认**（插件的独门）。
    //    现金科目下拉**只列一级科目是 1001/1002 的** —— 1012 其他货币资金
    //    之类不该出现在选项里。
    await sc.locator('.sc-card-click').filter({ hasText: '浏览器演练共享卡' }).click()
    const detail = staff.page.getByTestId('sc-card-detail')
    await detail.waitFor()
    for (const [bookId, code] of [[state.sharedcardBookId, '1002'], [state.sharedcardOtherBookId, '1001']]) {
      await detail.getByRole('combobox', { name: '账套', exact: true }).selectOption(String(bookId))
      const subject = detail.getByRole('combobox', { name: '现金科目', exact: true })
      await subject.locator(`option[value="${code}"]`).waitFor({ state: 'attached' })
      // **下拉里不该有非现金族**：1012 其他货币资金的一级科目不是 1001/1002。
      assert.equal(await subject.locator('option[value="1012"]').count(), 0)
      await subject.selectOption(code)
      await detail.getByRole('button', { name: '添加', exact: true }).click()
      await detail.locator('.sc-table').filter({ hasText: code }).waitFor()
    }

    // ④ **没有归属规则**（1.2.0 起）：页签里不该有它，卡详情只有三个页签。
    //    规则是「按对方户名/摘要去猜」，猜错就是把钱记到别的账上 —— 插件不猜。
    assert.equal(await detail.getByRole('button', { name: '归属规则', exact: true }).count(), 0)
    assert.equal(await detail.locator('.sc-tabs .sc-tab').count(), 3)

    // ⑤ 上传**唯一一份**对账单（真实民生银行 Excel，5 行）
    //    **不为它选账套** —— 它是「为这张共享卡上传的」，那个下拉不该在。
    await detail.getByRole('button', { name: '对账单', exact: true }).click()
    assert.equal(await detail.getByRole('combobox', { name: '替哪本账上传', exact: true }).count(), 0)
    await detail.locator('input.sc-file-input').setInputFiles(state.statementFixture)
    await detail.locator('.sc-table').filter({ hasText: 'cmbc_xlsx.xlsx' }).waitFor()

    // ⑥ 打开对账单：**5 行都还没归属**（1.2.0 起没有规则自动判定了）
    await detail.getByRole('button', { name: '打开', exact: true }).click()
    const stmtPane = staff.page.getByTestId('sc-statement')
    await stmtPane.waitFor()
    const waitMetric = async (label, expected) => {
      await stmtPane.locator('.sc-metric').filter({ hasText: label + expected }).waitFor()
      const item = stmtPane.locator('.sc-metric').filter({ hasText: label })
      return (await item.innerText()).replace(/\s+/g, '')
    }
    assert.equal(await waitMetric('行数', '5'), '行数5')
    // **行列表是另一次请求**：指标先到、行后到。断言行之前必须等行刷新完 ——
    // 不等就会测到上一屏的数据（分配前看到 5 行未归属，分配后还是 5 行）。
    const waitUnassignedRows = async expected => {
      await staff.page.waitForFunction((want) => {
        const cells = [...document.querySelectorAll('[data-testid="sc-statement"] .sc-tr')]
          .filter(row => !row.classList.contains('sc-th'))
        return cells.filter(row => row.textContent.includes('未归属')).length === want
      }, expected)
    }
    assert.equal(await waitMetric('已归属', '0'), '已归属0')
    assert.equal(await waitMetric('待复核', '5'), '待复核5')
    await waitUnassignedRows(5)

    // ⑥-0 **一行都还没归属时**，核对页不能说「对得上、没有少记」——
    //    没比过的数不能说成比过了。两处文案都要带限定：合计卡上的提示，
    //    和「谁少记了」那块空态。
    await stmtPane.getByRole('button', { name: '返回', exact: true }).click()
    await staff.page.getByTestId('sc-cards').waitFor()
    await sc.locator('.sc-card-click').filter({ hasText: '浏览器演练共享卡' }).click()
    await detail.getByRole('button', { name: '余额核对', exact: true }).click()
    await detail.getByRole('button', { name: '核对余额', exact: true }).click()
    // 合计卡：说清「少记/多记」现在还不可靠。
    await detail.locator('.sc-hint').filter({ hasText: '还没归属的 5 行没算进' }).waitFor()
    // 「谁少记了」：没有已归属的少记，但**不能**说「对得上、没有少记」。
    const emptyShort = detail.locator('.sc-card').filter({ hasText: '谁少记了' }).first()
    await emptyShort.waitFor()
    const emptyShortText = (await emptyShort.innerText()).replace(/\s+/g, '')
    assert.match(emptyShortText, /还有5行没归属/)
    assert.doesNotMatch(emptyShortText, /对得上/)

    // 回到对账单继续指定归属。
    await detail.getByRole('button', { name: '对账单', exact: true }).click()
    await detail.getByRole('button', { name: '打开', exact: true }).click()
    await staff.page.getByTestId('sc-statement').waitFor()
    assert.equal(await waitMetric('行数', '5'), '行数5')
    assert.equal(await waitMetric('已归属', '0'), '已归属0')
    assert.equal(await waitMetric('待复核', '5'), '待复核5')
    await waitUnassignedRows(5)

    // ⑥-1 「丁物业管理」那一笔归到**第二本账**（它就是那本账的往来款）。
    const dingRow = stmtPane.locator('.sc-tr').filter({ hasText: '丁物业管理' })
    assert.equal(await dingRow.count(), 1)
    await dingRow.locator('input[type=checkbox]').check()
    await stmtPane.getByRole('combobox', { name: '人工归到', exact: true }).selectOption(String(state.sharedcardOtherBookId))
    await stmtPane.getByRole('button', { name: '指定归属', exact: true }).click()
    assert.equal(await waitMetric('已归属', '1'), '已归属1')
    assert.equal(await waitMetric('待复核', '4'), '待复核4')
    await waitUnassignedRows(4)

    // ⑥-2 其余 4 行归到第一本账
    const pending = stmtPane.locator('.sc-tr').filter({ hasText: '未归属' })
    const pendingCount = await pending.count()
    assert.equal(pendingCount, 4)
    for (let index = 0; index < pendingCount; index++) {
      await pending.nth(index).locator('input[type=checkbox]').check()
    }
    await stmtPane.locator('.sc-toolbar-inset').filter({ hasText: '已勾选 4 行' }).waitFor()
    await stmtPane.getByRole('combobox', { name: '人工归到', exact: true }).selectOption(String(state.sharedcardBookId))
    await stmtPane.getByRole('button', { name: '指定归属', exact: true }).click()
    assert.equal(await waitMetric('待复核', '0'), '待复核0')
    await waitUnassignedRows(0)

    // ⑦ **插件不制证** —— 界面上不该有「生成凭证」这个入口。
    assert.equal(await stmtPane.getByRole('button', { name: '生成凭证', exact: true }).count(), 0)
    await staff.page.screenshot({ path: fileURLToPath(new URL('plugin-sharedcard-rows.png', output)), fullPage: true })

    // ⑧ 卡级余额核对：**各账套该科目的本期发生额 vs 该记的收款/支出**。
    //    演练里两本账都**没有记过凭证** —— 所以账上一分没动，
    //    「少记」就等于各自该记的金额：第一本账
    //    收款 100,007,401 + 支出 10,530，第二本账支出 30,000,000。
    await stmtPane.getByRole('button', { name: '返回', exact: true }).click()
    await staff.page.getByTestId('sc-cards').waitFor()
    await sc.locator('.sc-card-click').filter({ hasText: '浏览器演练共享卡' }).click()
    await detail.getByRole('button', { name: '余额核对', exact: true }).click()
    await detail.getByRole('button', { name: '核对余额', exact: true }).click()
    const kv = async label => {
      const item = detail.locator('.sc-kv').filter({ hasText: label })
      await item.waitFor()
      return (await item.innerText()).replace(/\s+/g, '')
    }
    assert.equal(await kv('各账套该科目本期发生额'), '各账套该科目本期发生额借0.00/贷0.00')
    assert.equal(await kv('对账单该记的'), '对账单该记的借100,007,401.00/贷30,010,530.00')
    assert.equal(await kv('还没归属'), '还没归属（0行）收0.00/付0.00')
    // 「谁少记了」要**点名**。
    const shortCard = detail.locator('.sc-card').filter({ hasText: '谁少记了' }).first()
    await shortCard.waitFor()
    const shortText = (await shortCard.innerText()).replace(/\s+/g, '')
    assert.match(shortText, /共享卡演练账套甲.*少记100,017,931\.00/)
    assert.match(shortText, /共享卡演练账套乙.*少记30,000,000\.00/)
    await staff.page.screenshot({ path: fileURLToPath(new URL('plugin-sharedcard-balance.png', output)), fullPage: true })
    console.log('PASS shared card: one statement across two books, all rows allocated by hand, card-level reconciliation (no rules, no vouchers)')
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
      // 共享卡的**卡是全局表**，宿主只放行管理员写全局表 —— 所以管理员本人
      // 也必须被授权，否则这条线上没人能建卡。
      if (bundle.id === 'com.cwq.sharedcard') await grant(admin.page, bundle.id, true, 'plugin-admin')
    }
    await admin.page.screenshot({ path: fileURLToPath(new URL('plugin-install.png', output)), fullPage: true })
    console.log('PASS UI install, pending restart, zero initial grants, user search/picker/save')
  } else {
    const normal = state.packages[0], bad = state.packages[1]
    const adminToken = await admin.page.evaluate(() => localStorage.getItem('cwq_token'))
    const adminManifest = await admin.context.request.get(state.url + '/api/plugins/manifest', { headers: { Authorization: `Bearer ${adminToken}` } })
    // **共享卡有没有打进演练，决定这一轮断言什么**。
    //
    // 插件源码不在 git 里（/plugins/ 是 gitignore 的）：干净 checkout 里没有它，
    // 那时这一轮只跑另外三个插件 —— 名单与数量都跟着**实际打进演练的包**走。
    // 写死一个数就会在少跑场景的那一轮里红，而红的原因与被测的东西无关。
    const sharedcard = state.packages.find(p => p.id === 'com.cwq.sharedcard')
    // 管理员**默认**拿不到任何插件 —— 授权是显式的，管理员身份不等于授权。
    // 这条线上只显式给管理员授了共享卡（卡是全局表，只有管理员能建），
    // 所以管理员的名单里**只有它一个**。
    assert.deepEqual((await adminManifest.json()).plugins.map(p => p.id),
      sharedcard ? ['com.cwq.sharedcard'] : [])
    const staff = await login('plugin-staff')
    const token = await staff.page.evaluate(() => localStorage.getItem('cwq_token'))
    const manifest = await staff.context.request.get(state.url + '/api/plugins/manifest', { headers: { Authorization: `Bearer ${token}` } })
    assert.match(manifest.headers()['cache-control'], /(?:^|,\s*)no-store(?:,|$)/)
    const granted = (await manifest.json()).plugins
    assert.equal(granted.length, sharedcard ? 4 : 3)
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
    // ───────────────────────────────────────── 共享卡对账：一张卡、两个账套、一份对账单
    if (sharedcard) {
      await sharedcardScenario({ admin, staff, state, sharedcard })
    } else {
      console.log('SKIP shared card: 干净 checkout 里没有 plugins/com.cwq.sharedcard'
        + '（插件不入库）—— 这一段场景**没有跑**')
    }
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
    // 管理员页面上一段被导航去看共享卡插件了 —— 授权名单在**扩展管理页**上，
    // 回去再点「管理用户」。
    await manager(admin.page)
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
