/**
 * 期间结账/反结账的乐观锁与 409（审计 P1-06 / P1-19 的前端一半）。
 *
 * 要守住两件事：
 *   1. **请求带上 version** —— 不带就退化成「不比对」，后端会把陈旧页面的
 *      操作照做（他屏幕上确实写着「未结账」）。
 *   2. **409 与 400 分开说** —— 409 是「别人先动了手，刷新一下」，
 *      400 是「你参数写错了」。按 400 弹一句「结账失败」，人会去查自己的表单、
 *      反复重试，而真相是页面上这份状态已经是昨天的了。
 *
 * handlePeriodError 是纯逻辑：从**真实组件源码**里抠出来跑（跟 voucher_race
 * 同一套手法），注入假的 ElMessage / store，断言它说的话和它做的事。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const SRC = fs.readFileSync(
  new URL('../src/views/period/PeriodPanel.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const tests = []
/** 注册用例。别在这里 await —— 统一在末尾串行跑。
 *  注意 t() 必须 await：这一组有用例是异步的（handlePeriodError 是 async），
 *  用同步的 try/catch 包一层的话，断言失败会变成一次没人接的 rejection，
 *  用例照样打印 OK —— 恒绿测试就是这么来的。 */
const t = (name, fn) => tests.push([name, fn])

/** 抠出 `async function <name>(...) {...}` 整段源码，做成可执行的函数。 */
function grab(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(SRC)
  if (!m) throw new Error(`源码里找不到 function ${name}(`)
  const i = m.index
  let depth = 0, started = false, j = i
  for (; j < SRC.length; j += 1) {
    const ch = SRC[j]
    if (ch === '(') { depth += 1; started = true }
    else if (ch === ')') { depth -= 1; if (started && depth === 0) { j += 1; break } }
    else if (ch === '{' && SRC[j - 1] === ')') { break }     // 形参结束，是函数体
  }
  let end = SRC.indexOf('{', j)
  depth = 0
  for (let k = end; k < SRC.length; k += 1) {
    if (SRC[k] === '{') depth += 1
    else if (SRC[k] === '}') { depth -= 1; if (depth === 0) { end = k; break } }
  }
  return SRC.slice(i, end + 1)
}

/** 把抠出来的函数放进一个最小作用域里跑：注入 ElMessage / store / http 三个依赖 */
async function runHandlePeriodError(err) {
  const said = { warnings: [], errors: [] }
  const refreshed = []
  const fn = new Function('ElMessage', 'store', 'deps',
    `return (${grab('handlePeriodError')})`)(
    {
      warning: (arg) => (typeof arg === 'string'
        ? said.warnings.push(arg) : (said.warnings.push(arg.message), arg.duration)),
      error: (arg) => said.errors.push(arg)
    },
    { loadPeriods: async () => refreshed.push(1) },
    {}
  )
  await fn(err, '结账失败')
  return { said, refreshed }
}

const errOf = (status, detail) => ({ response: { status, data: detail === undefined ? {} : { detail } } })

console.log('== 请求必须带上期间版本 ==')

t('close 带上 version（不传 = 不比对，等于没有乐观锁）', () => {
  assert.ok(/periods\/\$\{p\.id\}\/close`,\s*\{\s*version: p\.version\s*\}/.test(SRC),
    '结账请求没带 version —— 等于告诉后端「别比」')
})
t('unclose 也带上 version（它会把区间内凭证批量退回未记账，更不能按陈旧状态做）', () => {
  assert.ok(/revert_posting: true,\s*version: p\.version/.test(SRC))
})
t('版本号来自 GET /periods 的那一项，不另存一份（两处版本号迟早对不上）', () => {
  assert.ok(/version: p\.version/.test(SRC))
  assert.ok(!/versionOf|myVersion|localVersion/.test(SRC), '页面上自己存了一份版本号')
})

console.log('')
console.log('== 409 与 400 必须分开说 ==')

t('409：说「被人改过、刷新一下」，并把期间重新拉一遍', async () => {
  const detail = '2026-03 已被他人操作（你手上的版本 1，库里已是 2）。'
    + '本次操作没有执行，请刷新页面确认当前状态后再操作。'
  const { said, refreshed } = await runHandlePeriodError(errOf(409, detail))
  assert.equal(said.warnings.length, 1, '409 没说话')
  assert.equal(said.errors.length, 0, '409 被当成参数错误弹了 —— 人会去查自己的表单')
  assert.ok(said.warnings[0].includes('刷新'), '没告诉人该刷新：' + said.warnings[0])
  assert.equal(refreshed.length, 1, '409 之后必须重拉期间，否则拿旧版本重试只会再撞一次 409')
})

t('400：按参数错处理，原文照给（结账失败十有八九是损益没结平，别吞掉）', async () => {
  const detail = '2026-03 结账失败：损益类科目本期未结平'
  const { said } = await runHandlePeriodError(errOf(400, detail))
  assert.equal(said.errors.length, 1)
  assert.equal(said.errors[0], detail)
  assert.equal(said.warnings.length, 0)
})

t('其他失败（500 / 断网）：也走普通错误，不假装是版本冲突', async () => {
  const { said } = await runHandlePeriodError(errOf(500, undefined))
  assert.equal(said.errors[0], '结账失败', '后端没给 detail 时的兜底文案')
  assert.equal(said.warnings.length, 0)
})

console.log('')
for (const [name, fn] of tests) {
  try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}
console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)