/**
 * §3.29：拆分凭证弹窗的「确认拆分」在途不许发第二趟。
 *
 * 实现已修（submitting ref + ``if (submitting.value) return`` + 模板的
 * :disabled / :loading），但此前的用例**只断言模板字符串**，没断言
 * submit() 的逻辑早退 —— 于是「在途早退」这件事没人钉着。
 *
 * 这里只补**行为级**用例：从真实组件源码里抠出 submit() 跑一遍。
 * 实现不改（除非用例证明它还有洞）。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const ROOT = new URL('../', import.meta.url)
const DIALOG = fs.readFileSync(new URL('src/components/VoucherSplitDialog.vue', ROOT), 'utf8')

let pass = 0, fail = 0
const tests = []
/** 注册用例。别在这里 await —— 统一在末尾串行跑。 */
const t = (name, fn) => tests.push([name, fn])

/** 配平括号：跳过字符串字面量与注释。 */
function matchBracket(src, start, open, close) {
  let depth = 0, quote = null, k = start
  while (k < src.length) {
    const c = src[k]
    if (quote) {
      if (c === '\\') { k += 2; continue }
      if (c === quote) quote = null
      k++
      continue
    }
    if (c === '/' && src[k + 1] === '/') {
      const nl = src.indexOf('\n', k)
      k = nl === -1 ? src.length : nl
      continue
    }
    if (c === '/' && src[k + 1] === '*') {
      const end = src.indexOf('*/', k + 2)
      k = end === -1 ? src.length : end + 2
      continue
    }
    if (c === "'" || c === '"' || c === '`') { quote = c; k++; continue }
    if (c === open) depth++
    else if (c === close) { depth--; if (depth === 0) return k }
    k++
  }
  return -1
}

/** 抠出 `function <name>(...) {...}` 的整段源码。 */
function grabFn(name, src) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(src)
  assert.ok(m, `源码里找不到 function ${name}(`)
  const i = m.index
  const close = matchBracket(src, src.indexOf('(', i), '(', ')')
  assert.ok(close !== -1, `${name} 的参数表没配平`)
  const bodyStart = src.indexOf('{', close)
  assert.ok(bodyStart !== -1, `${name} 找不到函数体`)
  const bodyEnd = matchBracket(src, bodyStart, '{', '}')
  assert.ok(bodyEnd !== -1, `${name} 的函数体没配平`)
  const out = src.slice(i, bodyEnd + 1)
  assert.ok(out.endsWith('}'), `${name} 抠出来的不是一段完整的函数`)
  return out
}

const SUBMIT_SRC = grabFn('submit', DIALOG)

console.log('== 抠取器自测（抠错了后面全是假绿）==')

t('抠取器自测：抠出来的是**那一段**，不是一份等价实现', () => {
  const fake = [
    'function demo(a, b) {',
    '  const s = `x ${a} y`',
    "  if (a) { return { deep: '}' } }",
    '  return b',
    '}',
    'function other() { return 1 }',
  ].join('\n')
  const got = grabFn('demo', fake)
  assert.ok(got.startsWith('function demo(a, b) {'), '开头不对')
  assert.ok(got.includes('const s = `x ${a} y`'), '函数体被腰斩了')
  assert.ok(got.includes("return { deep: '}' }"), '字符串里的花括号把配平算错了')
  assert.ok(got.endsWith('}'), '结尾不对')
  assert.ok(!got.includes('function other'), '多抠了下一段')
})

t('抠取器自测：submit() 抠对了（它得是在途早退的那一段）', () => {
  assert.ok(SUBMIT_SRC.includes('submitting'), 'submit() 里没有 submitting —— 抠错了')
  assert.ok(SUBMIT_SRC.includes('emit('), 'submit() 里没有 emit —— 抠错了')
  assert.ok(!SUBMIT_SRC.includes('async'), 'submit() 不是同步函数 —— 抠错了')
})

/**
 * 搭一套真实的 submit()：状态与依赖全由外面喂进去。
 * @param {{canConfirm: boolean, atLine: number}} init
 */
function dialog(init = {}) {
  const emitted = []
  const deps = {
    canConfirm: { value: init.canConfirm !== false },
    submitting: { value: false },
    atLine: { value: init.atLine ?? 2 },
    emit: (name, payload) => emitted.push([name, payload])
  }
  const factory = new Function('deps', `
    const canConfirm = deps.canConfirm
    const submitting = deps.submitting
    const atLine = deps.atLine
    const emit = deps.emit
    ${SUBMIT_SRC}
    return submit`)
  const submit = factory(deps)
  return { submit, emitted, deps }
}

console.log('== §3.29 「确认拆分」在途不许发第二趟 ==')

t('**行为**：点一次 → 只发一次 confirm', () => {
  const d = dialog()
  d.submit()
  assert.equal(d.emitted.length, 1, `确认事件发了 ${d.emitted.length} 次`)
  assert.equal(d.emitted[0][0], 'confirm', '发的事件不对')
  assert.equal(d.emitted[0][1].at_line, 2, '带上的切分位置不对')
  assert.equal(d.deps.submitting.value, true, '在途标志没置起来')
})

t('**行为**：在途时再点一次 → **不发第二趟**（双击不重复拆）', () => {
  const d = dialog()
  d.submit()
  d.submit()          // ← 双击的第二下
  d.submit()
  assert.equal(d.emitted.length, 1,
    `在途时又发了 ${d.emitted.length - 1} 趟 —— 服务端会把凭证截短（审计 §3.29）`)
})

t('**行为**：上一趟回来（done）→ 又能点（失败时要让人再试一次）', () => {
  const d = dialog()
  d.submit()
  const payload = d.emitted[0][1]
  assert.equal(typeof payload.done, 'function', '没有把「这一趟结束了」交给父组件')
  payload.done()
  assert.equal(d.deps.submitting.value, false, 'done 之后在途标志没放开 —— 按钮永远灰着')
  d.submit()
  assert.equal(d.emitted.length, 2, 'done 之后点不动了 —— 失败时没法再试一次')
})

t('**对称**：还不能确认（服务端没给可拆的地方）→ 一声不吭，不发事件', () => {
  const d = dialog({ canConfirm: false })
  d.submit()
  assert.deepEqual(d.emitted, [], '不能确认却发了确认事件')
  assert.equal(d.deps.submitting.value, false, '不能确认却把在途标志置起来了')
})

t('模板上那两颗也钉着（按钮灰着 + 转圈）', () => {
  assert.match(DIALOG, /:disabled="!canConfirm \|\| submitting"/, '按钮没被在途状态按住')
  assert.match(DIALOG, /:loading="submitting"/, '按钮没有转圈')
})

for (const [name, fn] of tests) {
  try {
    await fn()
    console.log('  OK   ' + name)
    pass++
  } catch (e) {
    console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0])
    fail++
  }
}

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
