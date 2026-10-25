/**
 * §3.31：分录行的行盒子（rowEls）必须**按行下标**记。
 *
 * 复核方指出的缺口：实现已改成按下标键，但**没有任何用例断言 rowEls 的行为** ——
 * 于是「删中间行 → 后面那行上移」这条路上，行盒子是不是还对得上，无人过问。
 *
 * 现场（Vue 3.5 的函数式 ref 生命周期）：
 *   · ``v-for`` 用的是**下标 key**（``:key="i"``）→ Vue 按位置复用 DOM；
 *   · 删掉中间一行时，最后那个 vnode 被**卸载** → 它的 ref 函数会以
 *     ``null`` 再被调一次（runtime-core 的 setRef：isUnmount 时 value = null）；
 *   · 那条「陈旧的 null 调用」删的**键**必须是一个**已经不存在**的下标 ——
 *     按下标键时它是尾部下标，删掉正合适；按分录对象键时它删的恰恰是
 *     **刚上移那一行**的条目 → 那一行从此取不到元素，
 *     keepActiveRowAnchored 直接 return → **滚动锚定失效**，
 *     同时被删行的条目永久残留在 Map 里。
 *
 * 这里**照着 Vue 的真实调用顺序**模拟一遍：先按位置 patch（下标 0、1），
 * 最后才卸载多出来的那个（按下标 2）。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const ROOT = new URL('../', import.meta.url)
const SRC = fs.readFileSync(new URL('src/views/voucher/VoucherEdit.vue', ROOT), 'utf8')

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

/**
 * 抠出模板里那一行分录的 ref 表达式（``:ref="(el) => setRowRef(el, i)"``）。
 *
 * **必须抠模板里那一句**，不能自己写一个：函数式 ref 的**闭包捕获**才是
 * 这条 bug 的关键 —— 闭包捕获的是「下标」还是「分录对象」，决定那条
 * 陈旧的 null 调用删掉的是谁。抄一份自己写的闭包 = 测了一份等价物。
 */
function grabRowRefExpr() {
  const m = /:ref="([^"]*setRowRef[^"]*)"/.exec(SRC)
  assert.ok(m, '模板里找不到分录行的 :ref 表达式 —— 抠不出闭包就测不了这件事')
  return m[1]
}

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

t('抠取器自测：ref 表达式抠的是**模板里那一句**', () => {
  const expr = grabRowRefExpr()
  assert.match(expr, /setRowRef/, '抠到的不是那一行分录的 ref')
  assert.match(expr, /\bi\b/, '闭包捕获的不是行下标 —— 模板改了？')
  assert.ok(grabFn('setRowRef', SRC).includes('rowEls'), 'setRowRef 里没有 rowEls —— 抠错了')
})

/**
 * 搭一套真的 rowEls + setRowRef。
 * @param {string} expr 模板里那一行分录的 ref 表达式
 */
function boxes(expr) {
  const rowEls = new Map()
  const factory = new Function('rowEls', `
    ${grabFn('setRowRef', SRC)}
    return setRowRef`)
  const setRowRef = factory(rowEls)
  /** 按模板里那句话造一个 ref 闭包（捕获的到底是什么，全看模板）。 */
  const makeRef = (i) => new Function('setRowRef', 'i', `return ${expr}`)(setRowRef, i)
  return { rowEls, setRowRef, makeRef }
}

console.log('== §3.31 删中间行 → 后面那行上移 ==')

t('**行为**：上移后那行**仍能取到元素**（滚动锚定没失效）', () => {
  const expr = grabRowRefExpr()
  const { rowEls, makeRef } = boxes(expr)
  const el = (n) => ({ tag: 'div', n })

  // 第一次渲染：三行
  const el0 = el(0), el1 = el(1), el2 = el(2)
  const refs1 = [makeRef(0), makeRef(1), makeRef(2)]
  refs1[0](el0); refs1[1](el1); refs1[2](el2)
  assert.equal(rowEls.size, 3, '第一次渲染后行盒子没记上三行')

  // 删掉**中间**那一行 → 变成两行（第三行上移到下标 1）
  const el1b = el('1b')            // Vue 按位置复用 DOM：下标 1 拿到的是同一个盒子
  const refs2 = [makeRef(0), makeRef(1)]
  // Vue 的顺序（runtime-core patchUnkeyedChildren）：先按位置 patch，
  refs2[0](el0)
  refs2[1](el1b)
  // 再卸载多出来的那个 vnode → 它的 ref（上一轮那个）以 null 被调一次
  refs1[2](null)

  assert.equal(rowEls.get(1), el1b,
    '上移后那一行取不到元素了 —— keepActiveRowAnchored 直接 return，滚动锚定失效（审计 §3.31）')
})

t('**行为**：Map 里**没有**指向已不存在行的残留条目', () => {
  const expr = grabRowRefExpr()
  const { rowEls, makeRef } = boxes(expr)
  const el = (n) => ({ tag: 'div', n })
  const el0 = el(0), el1 = el(1), el2 = el(2)
  const refs1 = [makeRef(0), makeRef(1), makeRef(2)]
  refs1[0](el0); refs1[1](el1); refs1[2](el2)

  const el1b = el('1b')
  const refs2 = [makeRef(0), makeRef(1)]
  refs2[0](el0)
  refs2[1](el1b)
  refs1[2](null)          // ← 被卸载的那一行：它的 ref 以 null 被调一次

  // 现在活着的行是下标 0 与 1；Map 的键就该是这两个下标
  assert.deepEqual([...rowEls.keys()].sort((a, b) => a - b), [0, 1],
    `Map 里有指向已不存在行的残留条目：${JSON.stringify([...rowEls.keys()])}`)
  assert.equal(rowEls.has(2), false, '被删掉的那一行（下标 2）还在 Map 里')
})

t('**对称**：不删行的时候，Map 就是当前那几行的下标', () => {
  const expr = grabRowRefExpr()
  const { rowEls, makeRef } = boxes(expr)
  const el = (n) => ({ tag: 'div', n })
  const el0 = el(0), el1 = el(1), el2 = el(2)
  const refs = [makeRef(0), makeRef(1), makeRef(2)]
  refs[0](el0); refs[1](el1); refs[2](el2)
  assert.deepEqual([...rowEls.keys()].sort((a, b) => a - b), [0, 1, 2],
    '刚渲染完，Map 里就该是这三行的下标')
  assert.equal(rowEls.get(2), el2, '第三行的盒子没记上')
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
