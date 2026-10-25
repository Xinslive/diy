/**
 * 报表的显示口径（审计 §3.26）—— 前端这一半。
 *
 *  **口径定在后端**：`app/reports/evaluate.py` 是报表数值的唯一生产者，
 *  它给出的是**两位小数的十进制字符串**（舍入规则在 `app/core/money.py`，
 *  与 Excel 导出、HTML 打印、MCP 共用同一份）。页面、打印预览、Excel 导出
 *  读的都是这个值 —— 所以「屏幕与文件不是同一个数」这件事从根上没了。
 *
 *  前端这一层只做**排版**（加千分位），**不再自己舍入**：
 *  ① 舍入规则只能有一份（§4.5：同一个量只允许一个权威实现）；
 *  ② 自己再舍一次，就是两份规则在打架 —— 遇到边界值（恰好 .005）时
 *     两边会差一分钱，而那正是审计要消灭的「对不上」。
 *
 *  另外：口径要**在界面上说清楚**（§3.26 的修复要求「导出 quantize 到两位
 *  **并在界面注明**」），所以后端随数据带下来的 `display_scale.note`
 *  必须真的被显示出来。
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
const { reportNumber } = await server.ssrLoadModule('/src/utils/format.js')

const SRC = fs.readFileSync(new URL('../src/views/report/ReportView.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = async (name, fn) => { try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ } }

console.log('== 口径：前端只排版，不自己舍入 ==')

await t('后端给什么，前端就显示什么（一个字节都不改）', () => {
  assert.equal(reportNumber('0.33'), '0.33')
  assert.equal(reportNumber('0.00'), '0.00')
  assert.equal(reportNumber('-1234.50'), '-1,234.50')
})
await t('前端**不自己舍入**（口径由后端一处定，前端只加千分位）', () => {
  // 这条断言的方向容易搞反：前端**不该**再舍一次 —— 舍入规则只能有一份。
  // 「屏幕不再出现 28 位小数」这件事由**后端**保证（app/reports/evaluate.py
  // 给的就是两位小数的字符串），后端那侧有用例钉着（tests/test_audit_fix_report_caliber.py）。
  // 前端这一侧要保证的是：**原样带过去，一个字都不改**。
  assert.equal(reportNumber('0.3333333333333333333333333333'),
    '0.3333333333333333333333333333',
    '前端开始自己舍入了 —— 口径就变成两份规则在打架，边界值上两边会差一分钱')
})
await t('大额不因 float 丢精度（全程按字符串处理）', () => {
  // fmt 走 Number，这一串在双精度里会变成 1000000000000000000。
  assert.equal(reportNumber('999999999999999999.99'), '999,999,999,999,999,999.99')
})
await t('不是数字的东西原样返回（公式报错、占位符）', () => {
  assert.equal(reportNumber('科目不存在'), '科目不存在')
  assert.equal(reportNumber(''), '')
  assert.equal(reportNumber(null), '')
})

console.log('')
console.log('== 口径要在界面上说清楚（§3.26：并在界面注明） ==')

await t('后端带下来的口径说明被显示出来', () => {
  assert.ok(/data\.display_scale\?\.note/.test(SRC),
    '没有渲染 data.display_scale.note —— 口径就只存在于代码里，用户看不到')
  assert.ok(/\{\{ data\.display_scale\.note \}\}/.test(SRC),
    '口径说明渲染了但没把文字显示出来')
})
await t('口径说明是**一行小字**，不是告警条（口径是常驻事实，不是「本期出了状况」）', () => {
  // 它不该混进 notices —— notices 是「本期有什么特殊情况」的通道，
  // 没结转时必须是空数组（后端有两条用例钉着这个契约）。
  const noticesBlock = SRC.slice(
    SRC.indexOf('v-for="(n, i) in data.notices'),
    SRC.indexOf('</el-alert>', SRC.indexOf('v-for="(n, i) in data.notices')))
  assert.ok(noticesBlock, '找不到 notices 那个 v-for 块 —— 模板结构变了，这条要重写')
  assert.ok(!/display_scale/.test(noticesBlock),
    '口径说明被塞进 notices 了 —— 那会把常驻事实变成「本期特殊情况」')
  assert.ok(/class="report-scale-note no-print"/.test(SRC),
    '口径说明没有用 report-scale-note 这个样式（那是一行小字，不是告警条）')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
await server.close()
process.exit(fail === 0 ? 0 : 1)
