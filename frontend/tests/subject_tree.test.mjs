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

console.log('== 默认摊开几级（用户 2026-10-29 批注）==')
//
// 「科目表默认显示下一级。筛选条件为空或者为 1 级科目的时候，最多显示到 2 级科目，
//  筛选条件是 2 级科目的时候，最多显示到 3 级，以此类推。防止一次性展开太多级。」
//
// 这套判定**不是这一页新写的**：科目选择面板（utils/subjectSearch.js）早就是这么
// 做的。这里抠的是页面上的真实源码，喂的是面板那份判定 —— 两边必须算出同一个深度。

const search = await (async () => {
  const { createServer } = await import('vite')
  const { fileURLToPath } = await import('node:url')
  const ROOT = fileURLToPath(new URL('../', import.meta.url))
  const vite = await createServer({ root: ROOT, logLevel: 'error', server: { middlewareMode: true } })
  return {
    subjectSearch: await vite.ssrLoadModule('/src/utils/subjectSearch.js'),
    subjectCode: await vite.ssrLoadModule('/src/utils/subjectCode.js')
  }
})()
const { SEARCH_AUTO_DEPTH, rowOpen, searchNamed } = search.subjectSearch
const { matchSubject: match } = search.subjectCode

/** 抠出来的这几段要用的依赖，凑齐了装进 new Function 跑。 */
const DEP_DEPS = `
  const matchSubject = deps.matchSubject
  const searchNamed = deps.searchNamed
  const SEARCH_AUTO_DEPTH = deps.SEARCH_AUTO_DEPTH
  const rowOpen = deps.rowOpen
  const rowExpandOverrides = deps.rowExpandOverrides
`
const deps = {
  matchSubject: match,
  searchNamed,
  SEARCH_AUTO_DEPTH,
  rowOpen,
  rowExpandOverrides: { value: new Map() },
}

/** 页面上的那几个纯函数，抠出来装进一个能直接调用的工厂。 */
function treeFns(overrides = {}) {
  // `matchTree` 也要抠：`isHit` 调它、`autoDepthOf` 把它当参数传给 searchNamed。
  // 少抠一个，页面上真正在用的那段代码就少了一半 —— 测的就不是它了。
  const src = ['flattenSubjects', 'autoDepthOf', 'expandKeysOf', 'pruneTree', 'isHit', 'matchTree', 'onExpandChange']
    // **不能直接 .map(grabFn)**：map 会把「下标」当第二个参数传过去，
    // 而 grabFn 的第二个参数是「从哪抠」—— 拿到的就成了 0，抠的是空字符串。
    .map((name) => grabFn(name)).join('\n')
  const factory = new Function('deps', `
    ${DEP_DEPS}
    ${src}
    return { flattenSubjects, autoDepthOf, expandKeysOf, pruneTree, isHit, matchTree, onExpandChange }
  `)
  return factory({ ...deps, ...overrides })
}

/** 一棵四层的科目树（嵌套形状 = 接口给的那个形状）。 */
const TREE = [
  { id: 1, code: '1001', name: '库存现金', level: 1, children: [] },
  { id: 2, code: '1002', name: '银行存款', level: 1, children: [
    { id: 3, code: '100201', name: '工商银行', level: 2, children: [
      { id: 4, code: '10020101', name: '基本户', level: 3, children: [] },
    ] },
  ] },
  { id: 5, code: '5001', name: '主营业务收入', level: 1, children: [
    { id: 6, code: '500101', name: '星创空间', level: 2, children: [
      { id: 7, code: '50010101', name: '一层', level: 3, children: [
        { id: 8, code: '5001010101', name: '五号店', level: 4, children: [] },
      ] },
    ] },
  ] },
]

t('筛选为空 → 只摊到第二级（默认显示下一级）', () => {
  const f = treeFns()
  const flat = f.flattenSubjects(TREE)
  assert.equal(f.autoDepthOf(flat, ''), SEARCH_AUTO_DEPTH)
  assert.equal(SEARCH_AUTO_DEPTH, 1, '默认深度就是「显示到第二级」')
})

t('筛到 1 级科目 → 最多显示到 2 级', () => {
  const f = treeFns()
  const flat = f.flattenSubjects(TREE)
  assert.equal(f.autoDepthOf(flat, '1002'), 1)
})

t('筛到 2 级科目 → 最多显示到 3 级', () => {
  const f = treeFns()
  const flat = f.flattenSubjects(TREE)
  assert.equal(f.autoDepthOf(flat, '100201'), 2)
})

t('筛到 3 级科目 → 最多显示到 4 级', () => {
  const f = treeFns()
  const flat = f.flattenSubjects(TREE)
  assert.equal(f.autoDepthOf(flat, '10020101'), 3)
})

t('指不出名来（片段命中一大片）→ 退回只摊到第二级', () => {
  const f = treeFns()
  const flat = f.flattenSubjects(TREE)
  // 「100」命中一整条链，不是指名某一条 → 不跟着它摊深
  assert.equal(f.autoDepthOf(flat, '100'), SEARCH_AUTO_DEPTH)
})

t('默认只展开第一级（有下级的那些），第二级往下收着', () => {
  const f = treeFns()
  const keys = f.expandKeysOf(TREE, SEARCH_AUTO_DEPTH)
  assert.deepEqual(keys.sort(), ['2', '5'], '只该展开 1002 与 5001 两支')
})

t('摊到第三级时，第一级与第二级都展开', () => {
  const f = treeFns()
  const keys = f.expandKeysOf(TREE, 2)
  assert.deepEqual(keys.sort(), ['2', '3', '5', '6'])
})

t('叶子节点不占展开位（没有下级，展开它没有意义）', () => {
  const f = treeFns()
  const keys = f.expandKeysOf(TREE, 5)
  assert.ok(!keys.includes('4'), '基本户是叶子，不该被列进展开位')
  assert.ok(!keys.includes('1'), '库存现金是叶子，不该被列进展开位')
})

t('展开位是**字符串** id（Element Plus 拿字符串键比，传数字一行都不展开）', () => {
  const f = treeFns()
  const keys = f.expandKeysOf(TREE, SEARCH_AUTO_DEPTH)
  // Element Plus 的树形表把行收进 treeData[parentId] 这个普通对象里，
  // Object.keys(treeData) 拿到的是**字符串**键，而 :expand-row-keys 的值
  // 是拿 includes(key) 跟它比的 —— 数字永远对不上，一行都不展开。
  // 实测症状：行上画着 ▸（认得出下级），但点了没反应、默认也不展开。
  for (const k of keys) assert.equal(typeof k, 'string', `展开位 ${k} 不是字符串`)
  assert.ok(keys.includes('2'), '1002 该在展开位里')
})

t('用户手动点开的行要留在展开位里（否则下一轮重算就把它收回去了）', () => {
  const f = treeFns()
  // 500101 是第三级，默认深度 1 摊不到它；用户点了 ▸ 就该留在展开位里。
  const keys = f.expandKeysOf(TREE, SEARCH_AUTO_DEPTH, new Set(['6']), null)
  assert.ok(keys.includes('6'), '手动点开的 500101 该留在展开位里')
  assert.ok(!keys.includes('7'), '它下面那支没被点开，不该跟着摊开')
})

t('用户手动点收的行要从展开位里去掉（点一下收起来不能是个摆设）', () => {
  const f = treeFns()
  const keys = f.expandKeysOf(TREE, SEARCH_AUTO_DEPTH, null, new Set(['2']))
  assert.ok(!keys.includes('2'), '1002 被点收了，不该在展开位里')
  assert.ok(keys.includes('5'), '5001 没被动过，还该摊着')
})

t('手动点开/点收都记进名单（记的是用户的意愿，不是当前状态）', () => {
  const rowExpandOverrides = { value: new Map() }
  const f = treeFns({ rowExpandOverrides })
  f.onExpandChange({ id: 6 }, true)
  assert.equal(rowExpandOverrides.value.get('6'), true, '点开的行要记 true')
  f.onExpandChange({ id: 6 }, false)
  assert.equal(rowExpandOverrides.value.get('6'), false, '点收的行要记 false')
})

t('树形表的 expand-change 第二个参数是**布尔**（不是「展开行数组」）', () => {
  const rowExpandOverrides = { value: new Map() }
  const f = treeFns({ rowExpandOverrides })
  // `type="expand"` 的列发的是**数组**，树形表发的是**布尔**。
  // 照抄流水对账页那版（拿数组 .some() 判开合）到这里会永远当成「开了」。
  f.onExpandChange({ id: 6 }, false)
  assert.equal(rowExpandOverrides.value.get('6'), false, 'false 必须记成「收着」')
})

t('摊平后 parent_id 与深度都对（级次不能靠库里那个 level 字段）', () => {
  const f = treeFns()
  const flat = f.flattenSubjects(TREE)
  const byId = new Map(flat.map((s) => [s.id, s]))
  assert.equal(byId.get(1).parent_id, null)
  assert.equal(byId.get(3).parent_id, 2)
  assert.equal(byId.get(8).parent_id, 7)
  assert.equal(byId.get(1).depth, 0)
  assert.equal(byId.get(3).depth, 1)
  assert.equal(byId.get(8).depth, 3)
})

t('这一页的命中口径：编码或名称（子串）—— 与筛选框上写的一致', () => {
  const f = treeFns()
  assert.ok(f.isHit({ id: 1, code: '1001', name: '库存现金' }, '1001'), '编码没命中')
  assert.ok(f.isHit({ id: 1, code: '1001', name: '库存现金' }, '库存'), '名称没命中')
  assert.ok(f.isHit({ id: 1, code: '1001', name: '库存现金' }, '现金'), '名称片段没命中')
  assert.ok(!f.isHit({ id: 1, code: '1001', name: '库存现金' }, '银行存款'), '不该命中别的科目')
})

t('这一页**不**走拼音首字母（拼音字典没加载 → 时灵时不灵，比搜不到更糟）', async () => {
  const f = treeFns()
  // 选择面板那一路是带拼音的（matchSubject + 拼音字典）；科目表页刻意不用它。
  await search.subjectCode.ensurePinyin()
  assert.ok(deps.matchSubject({ id: 1, code: '1001', name: '库存现金' }, 'kcxj'),
    '全应用那一份本来就该支持拼音首字母（选择面板靠它）')
  assert.ok(!f.isHit({ id: 1, code: '1001', name: '库存现金' }, 'kcxj'),
    '科目表页不该把拼音当命中口径 —— 它对不上筛选框上写的那句')
})

t('「摊多深」跟这一页的命中口径是**同一套**（拼音不算命中 → 指不出名来 → 默认深度）', async () => {
  const f = treeFns()
  await search.subjectCode.ensurePinyin()
  const flat = f.flattenSubjects(TREE)
  // 「星创空间」是二级科目，拼音首字母 xckj 全表只它一条 ——
  // 按全应用那一份（带拼音）算，它就是「指名了二级」→ 该摊到第三级。
  // 但科目表页的筛选框写的是「按编码或名称筛选」，拼音**不算命中** →
  // 指不出名来 → 退回默认的「摊到第二级」。
  assert.equal(f.autoDepthOf(flat, 'xckj'), SEARCH_AUTO_DEPTH,
    '拼音不是这一页的命中口径，它不该把深度带深')
  // 用中文名指名同一个二级科目 → 摊到第三级（这一条两端一致）
  assert.equal(f.autoDepthOf(flat, '星创空间'), 2,
    '中文名指名二级科目就该摊到第三级')
})

t('剪枝保留命中的整棵子树与祖先路径', () => {
  const f = treeFns()
  const pruned = f.pruneTree(TREE, '100201')
  assert.equal(pruned.length, 1, '只该留下 1002 这一支')
  assert.equal(pruned[0].code, '1002', '祖先要留着（剪掉就成了没有上级的孤儿）')
  assert.equal(pruned[0].children[0].code, '100201')
  assert.equal(pruned[0].children[0].children[0].code, '10020101', '命中项的子树要留着')
})

console.log('== 模板上的展开绑定 ==')

t('表格用 expand-row-keys 驱动展开，不再是 default-expand-all', () => {
  assert.match(SRC, /:expand-row-keys="expandKeys"/)
  assert.ok(!/default-expand-all/.test(SRC), 'default-expand-all 会把整棵树摊开，正是不防的那件事')
})

t('这一页用的是选择面板那一份判定（同一件事、同一个口径）', () => {
  assert.ok(/from '@\/utils\/subjectSearch'/.test(SRC), '没引 utils/subjectSearch')
  assert.match(SRC, /rowOpen\(\{/)
  // 判定（摊多深）用面板那一份；「算什么算命中」由这一页作为参数传进去。
  assert.match(SRC, /searchNamed\([^)]*matchTree\)/,
    'searchNamed 要把这一页自己的匹配器传进去 —— 否则「摊多深」和「命中没命中」就成两套口径了')
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
