/**
 * 科目树（前端）：删除科目的**在途守卫** + 加载的**代次守卫**。
 *
 * ① **行为级**：从**真实源码**里抠出 `withBusy` / `remove` / `load` 跑一遍，
 *    测的是页面上真在用的那段代码 —— 不是抄一份等价实现。
 * ② **源码级**：模板上的 `:disabled` / `:loading` 绑定。
 *
 * 为什么要有这一份（审计 §7 路线图 44 / §3.29 / §3.30）：
 *   删除是这一页唯一的写动作。双击「删除」→ 两个 DELETE 并发 → 第一个删成功，
 *   第二个拿到 404「科目不存在」—— 用户刚看到「已删除」，转眼又一条红字，
 *   像删除失败了。后端**不是**幂等的（`delete_subject` 先 `db.get` 再查引用），
 *   所以这道闸只能在前端。
 *
 * ⚠️ **抠取器先有已知答案的自测**：抠取器自己出错的话，
 * 后面所有「绿」都是假绿 —— 恒真的探针比没有探针更坏。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const SRC = fs.readFileSync(
  new URL('../src/views/subject/SubjectTree.vue', import.meta.url), 'utf8')
const SCRIPT = SRC.slice(SRC.indexOf('<script setup>'), SRC.indexOf('</script>'))

let pass = 0, fail = 0
const tests = []
const t = (name, fn) => tests.push([name, fn])

/**
 * 配平括号：跳过字符串字面量与注释。
 *
 * **为什么必须跳过字符串**：模板字符串里的 `${...}` 里有花括号，
 * 不跳过就会把函数体的配平数算错 —— 抠出来的源码被腰斩，
 * 后面所有断言都在空转，还会「全绿」。
 */
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

/**
 * 抠出 `function <name>(...) {...}` 的整段源码。
 *
 * 抠的是**页面上真在用的那段代码**，不是抄一份等价实现。
 */
function grabFn(name, src = SCRIPT) {
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

/** 让所有微任务跑完（await 一个宏任务即可）。 */
const flush = () => new Promise((r) => setTimeout(r, 0))

console.log('== 抠取器自测（它自己错了的话，后面全是假绿）==')

t('抠取器自测①：抠出来的是**那一段**，不是一份等价实现', () => {
  const fake = [
    'async function demo(a, b) {',
    '  const s = `x ${a} y`',
    "  if (a) { return { deep: '}' } }",
    '  return b',
    '}',
    'function other() { return 1 }',
  ].join('\n')
  const got = grabFn('demo', fake)
  assert.ok(got.startsWith('async function demo(a, b) {'), '开头不对')
  assert.ok(got.includes('const s = `x ${a} y`'), '函数体被腰斩了')
  assert.ok(got.includes("return { deep: '}' }"), '字符串里的花括号把配平算错了')
  assert.ok(got.endsWith('}'), '结尾不对')
  assert.ok(!got.includes('function other'), '多抠了下一段')
})

t('抠取器自测②：注释里的括号与引号不能带偏配平', () => {
  const fake = [
    'function demo() {',
    "  // 这一行里有 ) 和 ' 这样的字符",
    '  /* 块注释里也有 } */',
    '  return 1',
    '}',
  ].join('\n')
  const got = grabFn('demo', fake)
  assert.ok(got.includes('return 1'), '函数体被腰斩了')
  assert.ok(got.endsWith('}'), '结尾不对')
})

t('抠取器自测③：抠真实源码里那两个函数（形状对得上）', () => {
  for (const name of ['withBusy', 'remove', 'load']) {
    const got = grabFn(name)
    assert.ok(got.includes(`function ${name}`), `${name} 抠错了`)
    assert.ok(got.endsWith('}'), `${name} 抠出来的不是完整函数`)
  }
})

console.log('== 删除科目的在途守卫 ==')

/** 造一套假依赖，把抠出来的真实源码装进去跑。 */
function makeEnv() {
  const calls = { del: [], confirm: [], ok: [], load: 0 }
  const env = {
    calls,
    busy: { value: false },
    removingId: { value: null },
    api: {
      deleteSubject: async (bookId, id) => { calls.del.push([bookId, id]) },
    },
    ElMessage: {
      success: (m) => calls.ok.push(m),
      error: (m) => calls.ok.push('ERR:' + m),
    },
    ElMessageBox: {
      confirm: async (msg, title) => { calls.confirm.push([msg, title]) },
    },
    store: { bookId: 7 },
    load: async () => { calls.load++ },
  }
  const src = `${grabFn('withBusy')}\n${grabFn('remove')}`
  const factory = new Function('deps', `
    const busy = deps.busy
    const removingId = deps.removingId
    const api = deps.api
    const ElMessage = deps.ElMessage
    const ElMessageBox = deps.ElMessageBox
    const store = deps.store
    const load = deps.load
    ${src}
    return { remove, withBusy }
  `)
  return { env, fns: factory(env) }
}

const ROW = { id: 11, code: '1001', name: '库存现金' }

t('双击「删除」只发一次 DELETE', async () => {
  const { env, fns } = makeEnv()
  let release
  const gate = new Promise((r) => { release = r })
  env.api.deleteSubject = async (bookId, id) => {
    env.calls.del.push([bookId, id])
    await gate
  }
  const p1 = fns.remove(ROW)
  const p2 = fns.remove(ROW)   // 双击
  await flush()
  assert.equal(env.calls.del.length, 1, `双击发了 ${env.calls.del.length} 次 DELETE`)
  assert.equal(env.busy.value, true, '在途时 busy 必须是 true（按钮要灰着）')
  assert.equal(env.removingId.value, ROW.id, '在途时要标出正在删的是哪一行')
  release()
  await p1
  await p2
  assert.equal(env.calls.del.length, 1, '第二趟也发出去了')
  assert.equal(env.busy.value, false, '结束后 busy 要放开')
  assert.equal(env.removingId.value, null, '结束后要清掉正在删的行')
  assert.deepEqual(env.calls.ok, ['已删除'])
  assert.equal(env.calls.load, 1, '删完要重载一次树')
})

t('删除完之后还能再删下一个（busy 没有卡死）', async () => {
  const { env, fns } = makeEnv()
  await fns.remove(ROW)
  assert.equal(env.busy.value, false)
  env.calls.del.length = 0
  await fns.remove({ id: 12, code: '1002', name: '银行存款' })
  assert.equal(env.calls.del.length, 1, '上一趟结束了却发不出去')
  assert.deepEqual(env.calls.del[0], [7, 12])
})

t('点了「取消」不发 DELETE，也不说「已删除」', async () => {
  const { env, fns } = makeEnv()
  env.ElMessageBox.confirm = async () => { throw new Error('cancel') }
  await fns.remove(ROW)
  assert.equal(env.calls.del.length, 0, '取消了还发 DELETE')
  assert.deepEqual(env.calls.ok, [], `取消了还说已删除：${JSON.stringify(env.calls.ok)}`)
  assert.equal(env.calls.load, 0)
  assert.equal(env.busy.value, false, '取消之后要放开按钮')
})

t('删除失败（拦截器已弹原因）时不弹「已删除」，也不重载', async () => {
  const { env, fns } = makeEnv()
  env.api.deleteSubject = async () => { throw new Error('404 科目不存在') }
  await fns.remove(ROW)
  assert.deepEqual(env.calls.ok, [], `失败了还说已删除：${JSON.stringify(env.calls.ok)}`)
  assert.equal(env.calls.load, 0, '失败了还去重载')
  assert.equal(env.busy.value, false, '失败之后也要放开按钮')
})

t('确认框的文案带着科目编码和名称（认得出删的是哪一个）', async () => {
  const { env, fns } = makeEnv()
  await fns.remove(ROW)
  assert.equal(env.calls.confirm.length, 1)
  assert.match(env.calls.confirm[0][0], /1001/)
  assert.match(env.calls.confirm[0][0], /库存现金/)
})

console.log('== 加载的代次守卫 ==')

t('**行为**：过期的那一趟不写进树上', async () => {
  const STALE = Symbol('stale')
  const tree = { value: [{ id: 1, code: '1001' }] }
  const seen = []
  const factory = new Function('deps', `
    const store = deps.store
    const year = deps.year
    const api = deps.api
    const run = deps.run
    const isStale = deps.isStale
    const tree = deps.tree
    ${grabFn('load')}
    return load
  `)
  const deps = {
    store: { bookId: 7 },
    year: { value: 2026 },
    api: { subjectTree: async (bookId, params) => {
      seen.push([bookId, params])
      return [{ id: 2, code: '2001' }]
    } },
    tree,
    // 第一趟是过期的（模拟「编辑保存触发的 load」晚到），第二趟是新的
    run: async (task) => { await task(); return deps.n++ === 0 ? STALE : [{ id: 2, code: '2001' }] },
    isStale: (v) => v === STALE,
    n: 0,
  }
  const load = factory(deps)
  await load()
  assert.deepEqual(tree.value, [{ id: 1, code: '1001' }], '过期的那趟把树覆盖了')
  await load()
  assert.deepEqual(tree.value, [{ id: 2, code: '2001' }], '最新那趟没写进树')
  assert.deepEqual(seen[0], [7, { year: 2026 }], '请求参数拼错了')
})

t('没有账套或年度时不发请求', async () => {
  const factory = new Function('deps', `
    const store = deps.store
    const year = deps.year
    const api = deps.api
    const run = deps.run
    const isStale = deps.isStale
    const tree = deps.tree
    ${grabFn('load')}
    return load
  `)
  for (const [bookId, year] of [[0, 2026], [7, 0]]) {
    let called = 0
    const load = factory({
      store: { bookId }, year: { value: year },
      api: { subjectTree: async () => { called++ } },
      run: async (task) => task(),
      isStale: () => false,
      tree: { value: [] },
    })
    await load()
    assert.equal(called, 0, `bookId=${bookId} year=${year} 时不该发请求`)
  }
})

console.log('== 模板上的绑定 ==')

t('删除按钮在途时灰着，正在删的那一行转圈', () => {
  assert.match(SRC, /:disabled="busy"/)
  assert.match(SRC, /:loading="removingId === row\.id"/)
  // 转圈只给**正在删的那一行**，不是满屏都在转
  assert.ok(!/:loading="busy"/.test(SRC), '不该整页一起转圈')
})

t('工具栏上的写按钮也按住（导入是整年覆盖，跟删除撞上会残缺）', () => {
  const bar = SRC.slice(SRC.indexOf('<div class="toolbar no-print">'), SRC.indexOf('</div>\n\n'))
  assert.match(bar, /:disabled="!store\.canWrite \|\| busy"/)
  const hits = bar.match(/:disabled="!store\.canWrite \|\| busy"/g) || []
  assert.equal(hits.length, 2, '导入并覆盖 / 新增科目 两个按钮都要按住')
})

t('加载失败要在页面上有一条，不是渲染成「没有科目」', () => {
  assert.match(SRC, /v-if="loadError"/)
  assert.match(SRC, /加载失败/)
})

console.log('')
for (const [name, fn] of tests) {
  try {
    await fn()
    console.log('  OK  ', name)
    pass++
  } catch (e) {
    console.log('  FAIL', name, '—', String(e.message).split('\n')[0])
    fail++
  }
}
console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
