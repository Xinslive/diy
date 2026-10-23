/**
 * 审查 P1-1：`flushIfDirty()` 恒返回 false ——「改动已保存却报警」，拆分凭证被永久误拦。
 *
 * 原来那一行是 `return save() === true`：
 *   save() 是 `async function`，返回的是 **Promise 对象**，
 *   拿它跟 `true` 比**恒为假** —— 于是
 *     · 离开凭证页（onBeforeRouteLeave）每次都弹「这一张还有改动没能存上」，
 *       而保存其实早就成功了；
 *     · 拆分凭证（openSplit）在有未落库改动时被**永久误拦**，提示语还是错的。
 *
 * 这条 bug 能活这么久，是因为它被一条**文本断言**当成了期望值钉死
 * （voucher_deadcode.test.mjs 断言调用点字面量是 `return save() === true`）。
 * 所以这里测的是**行为**：从**真实组件源码**里抠出 flushIfDirty 跑一遍，
 * 不抄一份等价实现（抄一份只能证明抄的那份是对的）。
 * 抠法与 voucher_balance.test.mjs / voucher_race.test.mjs 一致。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

/**
 * 抠出 `function <name>(...) {...}` 的整段源码（按大括号配平）。
 *
 * 坑：不能从签名里第一个 '{' 开始数花括号（解构形参会数错），
 * 所以先把参数表的括号配平，从**函数体**那个 '{' 才开始数。
 * 抠完立刻自检：抠错了要让断言失败，别让测试空转。
 */
function grabFn(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(SRC)
  assert.ok(m, `源码里找不到 function ${name}(`)
  const i = m.index
  let depth = 0, j = SRC.indexOf('(', i)
  for (; j < SRC.length; j++) {
    if (SRC[j] === '(') depth++
    else if (SRC[j] === ')') { depth--; if (depth === 0) break }
  }
  assert.equal(SRC[j], ')', `${name} 的参数表没配平`)
  const bodyStart = SRC.indexOf('{', j)
  assert.ok(bodyStart !== -1, `${name} 找不到函数体`)
  depth = 0
  for (j = bodyStart; j < SRC.length; j++) {
    if (SRC[j] === '{') depth++
    else if (SRC[j] === '}') { depth--; if (depth === 0) break }
  }
  const src = SRC.slice(i, j + 1)
  assert.ok(src.endsWith('}'), `${name} 抠出来的不是一段完整的函数`)
  return src
}

/**
 * 用真实源码搭一套 flushIfDirty。状态与依赖全由外面喂进去。
 * savingPromise 在组件里是模块级 let，这里照样用 let 声明，好让「在途那一趟」可测。
 */
function buildFlush(over = {}) {
  const src = grabFn('flushIfDirty')
  const ctx = {
    readonly: { value: false },
    entrySig: { value: 'dirty' },
    lastSaved: { value: 'saved' },
    balanced: { value: true },
    hasCompleteEntry: { value: true },
    save: async () => true,
    savingPromise: null,
    ...over,
  }
  const factory = new Function('ctx', `
    let savingPromise = ctx.savingPromise
    const readonly = ctx.readonly
    const entrySig = ctx.entrySig
    const lastSaved = ctx.lastSaved
    const balanced = ctx.balanced
    const hasCompleteEntry = ctx.hasCompleteEntry
    const save = ctx.save
    ${src}
    return flushIfDirty
  `)
  return { flush: factory(ctx), ctx }
}

test('有脏改动且 save() 存成了 → flushIfDirty 返回 true（这就是修好的那一行）', async () => {
  // 老写法 `save() === true` 在这里返回 false：
  // 于是「改动已经落库」被报成「没存上」，离开页时弹红框、拆分被永久误拦。
  let called = 0
  const { flush } = buildFlush({ save: async () => { called++; return true } })
  assert.equal(await flush(), true, '存上了却报成没存上 —— 离开凭证页会误弹「改动没能存上」')
  assert.equal(called, 1, '有脏改动却没有去存')
})

test('有脏改动且 save() 没存上 → flushIfDirty 返回 false（放人走之前要拦一下）', async () => {
  const { flush } = buildFlush({ save: async () => false })
  assert.equal(await flush(), false, '没存上却放行了 —— 改动就丢了')
})

test('save() 必须是**等出来**的结果，不是 Promise 对象', async () => {
  // Promise 对象跟 true 比恒为假：这里把 save 换成一个「慢一拍才 resolve」的函数，
  // 只要实现里少一个 await，返回的就是 Promise 而不是布尔值。
  const { flush } = buildFlush({ save: async () => true })
  const r = await flush()
  assert.equal(typeof r, 'boolean', `flushIfDirty 返回了 ${typeof r} —— 没有 await save()`)
})

test('没有改动 → 直接放行，根本不去存', async () => {
  let called = 0
  const { flush } = buildFlush({
    entrySig: { value: 'same' }, lastSaved: { value: 'same' },
    save: async () => { called++; return true },
  })
  assert.equal(await flush(), true)
  assert.equal(called, 0, '没改动也去存了一遍')
})

test('只读凭证 → 放行（这张本来就不能改）', async () => {
  let called = 0
  const { flush } = buildFlush({
    readonly: { value: true },
    save: async () => { called++; return true },
  })
  assert.equal(await flush(), true)
  assert.equal(called, 0, '只读凭证不该发保存请求')
})

test('还没录平 → 放行（那是「还没录完」，不是「没存上」）', async () => {
  let called = 0
  const { flush } = buildFlush({
    balanced: { value: false },
    save: async () => { called++; return true },
  })
  assert.equal(await flush(), true)
  assert.equal(called, 0)
})

test('一条能落库的分录都还没有 → 放行（空表不是「有改动没存上」）', async () => {
  let called = 0
  const { flush } = buildFlush({
    hasCompleteEntry: { value: false },
    save: async () => { called++; return true },
  })
  assert.equal(await flush(), true)
  assert.equal(called, 0)
})

test('上一趟保存还在路上：等它回来再判断，别把同一张存两遍', async () => {
  const order = []
  let release
  const inflight = new Promise((r) => { release = r })
  const { flush } = buildFlush({
    savingPromise: inflight,
    save: async () => { order.push('save'); return true },
  })
  const p = flush()
  order.push('已调用')
  release()
  assert.equal(await p, true)
  assert.deepEqual(order, ['已调用', 'save'], '没等在途那一趟就先去存了')
})
