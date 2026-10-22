/**
 * 页面查询条件 ↔ URL（composables/useUrlConditions.js）。
 *
 * 背景：标签栏不做 keep-alive（见 stores/tabs.js），切走再切回来 = 组件重建。
 * 条件只活在组件的 ref 里的话，回来必然是默认值 —— 那就是「查询参数被重置」
 * 那个毛病。所以条件一律住进 URL，标签栏记的完整地址带着它。
 *
 * 这里跑的是**线上那份源码**（vite 的 ssrLoadModule），不复制逻辑：
 * vue-router 换成 tests/router_stub.mjs，让它落到测试自己手里的 route/router 上。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import { nextTick, reactive, ref } from 'vue'
import { env as stubEnv } from './router_stub.mjs'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const server = await createServer({
  root: ROOT,
  logLevel: 'error',
  server: { middlewareMode: true },
  resolve: {
    alias: {
      'vue-router': fileURLToPath(new URL('./router_stub.mjs', import.meta.url)),
    },
  },
})
const { useUrlConditions } = await server.ssrLoadModule('/src/composables/useUrlConditions.js')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}
const ta = async (name, fn) => {
  try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

/**
 * 一个假的路由环境：记录每一次 replace，并把 query 写回 route。
 *
 * **没有 push**：组合式函数要是用了 push，这儿直接抛错 ——
 * 每改一次条件就塞一条历史记录的话，用户按一次「后退」要按很多下。
 */
function makeEnv(initialQuery = {}, path = '/detail-ledger') {
  const calls = []
  const route = reactive({ path, query: { ...initialQuery }, fullPath: path })
  const router = {
    replace(to) {
      calls.push(to)
      route.query = { ...(to.query || {}) }
      const q = new URLSearchParams(route.query).toString()
      route.fullPath = route.path + (q ? '?' + q : '')
    },
    push() {
      throw new Error('条件不该用 push —— 一组条件一条历史记录，后退键会按到想哭')
    },
  }
  stubEnv.route = route
  stubEnv.router = router
  return { route, router, calls }
}

console.log('== 条件 ↔ URL：默认值、复原、同步 ==')

t('都是默认值：一个都不写进 URL（地址栏只留用户真改过的那些）', () => {
  const env = makeEnv()
  const subject = ref(null)
  const unposted = ref(true)
  const { sync } = useUrlConditions({
    subject: { ref: subject, type: 'id' },
    unposted: { ref: unposted, type: 'bool', def: true },
  })
  sync()
  assert.equal(env.calls.length, 0, '默认值也写进去了')
  subject.value = 12
  unposted.value = false
  sync()
  assert.deepEqual(env.calls.at(-1).query, { subject: '12', unposted: '0' })
})

t('空字符串与 null 都不写（URL 上不留一串空参数）', () => {
  const env = makeEnv()
  const kw = ref('')
  const min = ref(null)
  const { sync } = useUrlConditions({
    kw: { ref: kw, type: 'str' },
    min: { ref: min, type: 'num' },
  })
  sync()
  assert.equal(env.calls.length, 0, '空值也写进去了')
})

t('地址没变就不写（不空刷路由）', () => {
  const env = makeEnv({ kw: '记0' })
  const kw = ref('记0')
  const { sync } = useUrlConditions({ kw: { ref: kw, type: 'str' } })
  sync()
  assert.equal(env.calls.length, 0, '同一组条件重复写了一遍')
})

await ta('复原之前不许往 URL 写：页面初始化塞的默认值不能把用户那组条件顶掉', async () => {
  const env = makeEnv({ kw: '用户的条件' })
  const kw = ref('')
  const { restore } = useUrlConditions({ kw: { ref: kw, type: 'str' } })
  // 页面初始化：先给 ref 塞默认值（「默认落在 1001 库存现金」那种）
  kw.value = '页面默认值'
  await nextTick()
  assert.equal(env.calls.length, 0,
    '复原之前就写了 URL —— 用户地址栏里那组条件被默认值顶掉了，正好把要修的毛病又造一遍')
  restore()
  assert.equal(kw.value, '用户的条件', '条件要从 URL 捡回来')
  assert.equal(env.calls.length, 0, '复原后的值与 URL 相同，不该多写一次')
})

await ta('复原之后条件一变，URL 跟着变', async () => {
  const env = makeEnv({ kw: '旧条件' })
  const kw = ref('')
  const { restore } = useUrlConditions({ kw: { ref: kw, type: 'str' } })
  restore()
  kw.value = '新条件'
  await nextTick()
  assert.deepEqual(env.calls.at(-1).query, { kw: '新条件' })
})

await ta('写 URL 用 replace：条件不是「去过的这个地方」', async () => {
  const env = makeEnv()
  const kw = ref('x')
  const { sync } = useUrlConditions({ kw: { ref: kw, type: 'str' } })
  sync()          // 假 router 的 push 会直接抛错
  assert.equal(env.calls.length, 1)
})

t('脏参数不认：手敲错的 id 不该进条件', () => {
  makeEnv({ subject: 'abc', min: 'x', ok: '12' })
  const subject = ref(null)
  const min = ref(null)
  const ok = ref(null)
  const { restore } = useUrlConditions({
    subject: { ref: subject, type: 'id' },
    min: { ref: min, type: 'num' },
    ok: { ref: ok, type: 'num' },
  })
  restore()
  assert.equal(subject.value, null, '"abc" 不是 id，不该认')
  assert.equal(min.value, null, '"x" 不是数字，不该认')
  assert.equal(ok.value, 12, '正常的数字要认')
})

t('布尔只认 1/0 与 true/false：地址栏是人能手改的', () => {
  makeEnv({ flag: 'yes' })
  const flag = ref(true)
  const { restore } = useUrlConditions({ flag: { ref: flag, type: 'bool' } })
  restore()
  assert.equal(flag.value, true, '"yes" 不是布尔，不该当成真')
})

t('get/set 写法：URL 里放编码，页面里用的是 id（编码跨年度不变）', () => {
  makeEnv({ subject: '1001' })
  const rows = [{ code: '1001', id: 7 }, { code: '2202', id: 9 }]
  const subjectId = ref(null)
  const { restore } = useUrlConditions({
    subject: {
      type: 'str',
      get: () => rows.find((r) => r.id === subjectId.value)?.code || '',
      set: (c) => { const hit = rows.find((r) => r.code === c); if (hit) subjectId.value = hit.id },
    },
  })
  restore()
  assert.equal(subjectId.value, 7, '编码要换成本页认的 id')
})

t('get/set 写法：页面上是 id，URL 上写的是编码', () => {
  const env = makeEnv()
  const rows = [{ code: '1001', id: 7 }]
  const subjectId = ref(7)
  const { sync } = useUrlConditions({
    subject: {
      type: 'str',
      get: () => rows.find((r) => r.id === subjectId.value)?.code || '',
      set: (c) => { const hit = rows.find((r) => r.code === c); if (hit) subjectId.value = hit.id },
    },
  })
  sync()
  assert.deepEqual(env.calls.at(-1).query, { subject: '1001' })
})

t('复原可以只复原一部分（明细账的子科目范围要分两步走）', () => {
  makeEnv({ a: '1', b: '2' })
  const a = ref(null)
  const b = ref(null)
  const { restore } = useUrlConditions({
    a: { ref: a, type: 'str' },
    b: { ref: b, type: 'str' },
  })
  restore(['a'])
  assert.equal(a.value, '1')
  assert.equal(b.value, null, '没点名的那一项不该动')
})

console.log(`\n通过 ${pass} / 失败 ${fail}`)

console.log('')
console.log('== 各页面的接线：条件确实住进了 URL ==')

/**
 * 这一组盯的是**接线**，不是行为：行为在浏览器里验（.run/verify_conditions.mjs）。
 * 这里防的是「有人把 restore() 删了」这类回归 —— 那种改动不报错、不报警，
 * 只是条件又悄悄被重置了，等到有人用的时候才发现。
 */
const PAGES = [
  { file: 'src/views/ledger/DetailLedger.vue', name: '明细账', keys: ['subject', 'from', 'to', 'unposted'] },
  { file: 'src/views/ledger/GeneralLedger.vue', name: '总账', keys: ['subject', 'from', 'to', 'unposted'] },
  { file: 'src/views/ledger/BalanceSheet.vue', name: '科目余额表', keys: ['scope', 'mode', 'period', 'from', 'unposted'] },
  { file: 'src/views/voucher/VoucherList.vue', name: '凭证', keys: ['kw', 'min', 'max'] },
  { file: 'src/views/subject/SubjectTree.vue', name: '科目表', keys: ['kw'] },
  { file: 'src/views/voucher/VoucherOrganize.vue', name: '整理凭证', keys: ['strategy'] },
  { file: 'src/views/print/PrintTemplate.vue', name: '打印模板', keys: ['scope', 'tpl'] },
]
for (const p of PAGES) {
  t(`${p.name}：条件接上了 URL，也接上了复原`, () => {
    const src = fs.readFileSync(new URL('../' + p.file, import.meta.url), 'utf8')
    assert.ok(src.includes('useUrlConditions'), '没接 useUrlConditions —— 条件还只活在组件里')
    assert.ok(/\brestore\(/.test(src), '没调 restore() —— 切回标签时条件捡不回来')
    for (const k of p.keys) {
      assert.ok(new RegExp(`\\b${k}\\s*:`).test(src), `条件 ${k} 没进 URL`)
    }
  })
}

t('明细账：复原必须排在科目树到位之后（科目 id 那时才认得出来）', () => {
  const src = fs.readFileSync(
    new URL('../src/views/ledger/DetailLedger.vue', import.meta.url), 'utf8')
  const loaded = src.indexOf('await loadSubjects()')
  const restored = src.indexOf('restore()', loaded)
  assert.ok(loaded !== -1, '源码里找不到 loadSubjects()')
  assert.ok(restored > loaded,
    'restore() 排在 loadSubjects() 之前了 —— 科目树还没到位，URL 里的科目复原不到')
})

console.log(`\n通过 ${pass} / 失败 ${fail}`)
process.exit(fail ? 1 : 0)
