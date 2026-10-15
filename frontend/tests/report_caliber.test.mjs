/**
 * 报表口径标注与模板契约（审计 P1-15 / P1-05 的前端一半）。
 *
 *  README 开头承诺「不存在某个报表和总账对不上」。这句话要成立，
 * 用户就得**能自己看出**为什么对不上：账簿口径含结转凭证、报表的损益口径
 * 不含，差额恰好是本期结转凭证金额。后端已经把信号发过来了
 * （cols[].excludes_carry_forward / notices / carry_forward_period_amount），
 * 这一组用例守住「界面确实把它说出来」，别让它烂在后端没人读。
 *
 * 后端那一半有 942 条测试盯着，这里测前端这一半：
 *   · api/report.js 真发出去的请求（拦下 http 看参数）
 *   · ReportView.vue 真用到的口径字段与提示（源码级断言）
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

globalThis.localStorage = {
  _m: {}, getItem(k) { return this._m[k] ?? null },
  setItem(k, v) { this._m[k] = String(v) }, removeItem(k) { delete this._m[k] }
}
globalThis.location = { hash: '#/reports' }

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const server = await createServer({ root: ROOT, logLevel: 'error', server: { middlewareMode: true } })
const { http } = await server.ssrLoadModule('/src/api/client.js')
const api = await server.ssrLoadModule('/src/api/report.js')

const calls = []
const realPost = http.post
http.post = (...args) => { calls.push(args); return Promise.resolve({ data: { ok: true } }) }

const SRC = fs.readFileSync(new URL('../src/views/report/ReportView.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = async (name, fn) => { try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ } }

console.log('== P1-05 报表模板是全局模板：改它必须说清以哪个账套的身份 ==')

await t('createTemplate 带上 book_id（后端已 fail-closed，缺了就 400）', async () => {
  calls.length = 0
  await api.createTemplate({ name: '新模板' }, 7)
  assert.equal(calls[0][0], '/reports/templates')
  assert.equal(calls[0][2]?.params?.book_id, 7, 'book_id 没发出去 —— 后端会拒')
})
await t('缺 book_id 时宁可一个参数都不发（由后端拒），不偷偷塞一个默认账套', async () => {
  calls.length = 0
  await api.createTemplate({ name: '新模板' })
  const params = calls[0][2]?.params
  assert.ok(!params || params.book_id === undefined, '前端替用户猜了一个账套：' + JSON.stringify(params))
})

console.log('')
console.log('== P1-15 报表口径必须显式标注 ==')

await t('列头按 excludes_carry_forward 挂「不含结转」角标', () => {
  assert.ok(/v-if="c\.excludes_carry_forward"/.test(SRC), '损益口径的列头没标')
  assert.ok(/class="caliber/.test(SRC), '角标没渲染出来')
})
await t('角标说的是「本期金额（不含结转损益）」，并且说清与总账的差额是什么', () => {
  assert.ok(/不含结转损益/.test(SRC), '口径角标没有把这句话说完整')
  assert.ok(/结转凭证金额/.test(SRC), '没说明与总账差的就是结转凭证金额')
})
await t('notices 非空时在表头下方挂一条提示', () => {
  assert.ok(/v-for="\(n, i\) in data\.notices \|\| \[\]"/.test(SRC), 'notices 没渲染')
  assert.ok(/{{ n\.text }}/.test(SRC), '提示只挂了外壳，没把后端那句话显示出来')
  assert.ok(/carry_forward_period_amount|n\.amount/.test(SRC), '没把结转金额一起摆出来')
})
await t('没有 notices 的时候什么都不显示（后端本期没结转时不标）', () => {
  // notices 为空数组时 v-for 不渲染 —— 这一条是「不硬编一条常驻提示」
  assert.ok(!/const NOTICE_TEXT\s*=\s*['"]/.test(SRC), '口径提示被写死在前端，后端改了文案就各说各话')
})
await t('公式保存被 400 拒掉时把 detail 弹给人看，并保留未保存的改动', () => {
  assert.ok(/catch \(e\)[\s\S]{0,400}detail/.test(SRC), 'saveAll 没接住 400')
  assert.ok(/ElMessageBox\.alert/.test(SRC), '公式语法错没有弹窗（以前是存进去、渲染时才炸）')
  // 弹窗之后不能把 dirty 清掉：整批被拒，一格都没存
  const after = SRC.slice(SRC.indexOf('async function saveAll'))
  const alertAt = after.indexOf('ElMessageBox.alert')
  const resetAt = after.indexOf('dirtyKeysReset()')
  assert.ok(alertAt >= 0 && resetAt > alertAt, '报错弹窗之后仍清了改动 —— 改的东西就丢了')
})

console.log('== 报表设计：列名与项目名改得动 ==')

await t('列标题用 v-model —— 退成只读绑定会把刚打的字还原掉', () => {
  // el-input 每次 input 后都用 props.modelValue 回填 DOM（Element Plus
  // handleInput 末尾的 setNativeInputValue）。只绑 :model-value 而不回写，
  // 打字就等于没打 —— 这不是猜测，是浏览器实测：值会被还原并追加。
  assert.ok(/<el-input[^>]*v-if="designMode"[^>]*v-model="c\.label"/s.test(SRC),
    '列标题没用 v-model，打字会被 el-input 回填成旧值')
})

await t('落库层不得再拿入参与数据源比相等（v-model 已经先改过源值了）', () => {
  // 这是「项目名列改不了」的真正成因：v-model 先把新值写回 c.label，@change
  // 再触发时 `label === col.label` 恒成立，第一行就 return，请求一次也不发。
  assert.ok(!/function renameCol\(col, label\)\s*\{\s*if \(label === col\.label\)/.test(SRC),
    'renameCol 又加回了「值没变就不发请求」的短路，改动存不住')
  assert.ok(!/function saveSideLabel\([\s\S]{0,200}?if \(label === /.test(SRC),
    'saveSideLabel 又加回了「值没变就不发请求」的短路')
})

await t('表体行名左右栏写回各自的存储位置，且分「写本地 / 落库」两步', () => {
  // 左右两栏的项目名存在两个地方（row.label / row.style.label_right），
  // v-model 只能绑一条固定路径，所以拆成 update:model-value 写本地 +
  // change 落库。少了写本地那一步，输入框里的字进不去。
  assert.ok(/:model-value="labelText\(row, it\.side\)"/.test(SRC), '行名输入框没有读 labelText()')
  assert.ok(/@update:model-value="draftSideLabel\(row, it\.side, \$event\)"/.test(SRC),
    '行名输入框没有在打字时把新值写回本地')
  assert.ok(/@change="saveSideLabel\(row, it\.side, \$event\)"/.test(SRC), '行名输入框失焦时没有落库')
  assert.ok(/function draftSideLabel\(row, side, value\)[\s\S]{0,240}label_right: value/.test(SRC),
    '右栏没有写进 row.style.label_right')
  assert.ok(/async function saveSideLabel\(row, side, label\)[\s\S]{0,200}\{ style: row\.style \}/.test(SRC),
    '右栏落库没有整份提交 style —— 行次/缩进/加粗会被抹掉')
})

await t('项目名列的口径下拉要说清「改不了的是口径，不是列名」', () => {
  // 「项目名（固定）」这个措辞会被读成整列锁死，而用户想改的恰恰是列名。
  assert.ok(/:title="colKindHint\(c\)"/.test(SRC), '口径下拉没有挂说明')
  assert.ok(/function colKindHint\(col\)[\s\S]{0,900}列名在上面那个输入框/.test(SRC),
    '说明里没告诉用户列名在哪里改')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
http.post = realPost
await server.close()
process.exit(fail === 0 ? 0 : 1)