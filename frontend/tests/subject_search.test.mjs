/**
 * 科目面板的搜索（**线上那份 `utils/subjectSearch.js`**）。
 *
 * 用户报的两件事都钉在这里：
 *   ① 搜「5001」时整条链一路摊开，面板推出几十行 —— 搜索结果**默认只摊到第二级**，
 *      更深的自己点 ▸ 往下走（rowOpen 那条规矩）。
 *   ② 只摊两层之后「三级那条命中的怎么找得到」—— 命中项的**整条祖先路**都得留在
 *      结果里（searchHitIds 那条规矩）。以前只回溯一级，搜 100202 会冒出一条
 *      没有上级的孤儿科目。
 *
 * 另有纯源码断言守住两个页面真的走了这套判定（明细账默认 1001 库存现金）。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const VITE = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
})
const { rowOpen, searchHitIds, SEARCH_AUTO_DEPTH } =
  await VITE.ssrLoadModule('/src/utils/subjectSearch.js')

const PICKER = fs.readFileSync(new URL('../src/components/SubjectPicker.vue', import.meta.url), 'utf8')
const DETAIL = fs.readFileSync(new URL('../src/views/ledger/DetailLedger.vue', import.meta.url), 'utf8')
const VOUCHER = fs.readFileSync(new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

/** 跟 demo 账套同形的科目树（页面上那几级） */
const TREE = [
  { id: 1, code: '1001', name: '库存现金', parent_id: null },
  { id: 2, code: '1002', name: '银行存款', parent_id: null },
  { id: 3, code: '100201', name: '工商银行', parent_id: 2 },
  { id: 4, code: '100202', name: '建设银行', parent_id: 3 },
  { id: 5, code: '5001', name: '主营业务收入物业管理费', parent_id: null },
  { id: 6, code: '500101', name: '星创空间', parent_id: 5 },
  { id: 7, code: '50010101', name: '1层', parent_id: 6 },
  { id: 8, code: '5001010101', name: '5号店', parent_id: 7 }
]
const code = (kw) => TREE.filter((s) => searchHitIds(TREE, kw).has(s.id)).map((s) => s.code)
/** 照组件那套 walk 拍平：命中留下，摊开才往下走 */
const shown = (kw, expanded = new Set()) => {
  const h = searchHitIds(TREE, kw)
  const out = []
  const kidsOf = (id) => TREE.filter((s) => s.parent_id === id)
  const walk = (list, depth) => {
    for (const n of list) {
      if (h && !h.has(n.id)) continue
      out.push(`${'  '.repeat(depth)}${n.code}`)
      if (rowOpen({ hasKids: kidsOf(n.id).length > 0, depth, searching: true, expanded, id: n.id })) {
        walk(kidsOf(n.id), depth + 1)
      }
    }
  }
  walk(TREE.filter((s) => s.parent_id == null), 0)
  return out
}

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 搜索结果默认只摊到第二级 ==')

t('自动摊到第二级（一级摊开、二级收着）', () => {
  assert.equal(SEARCH_AUTO_DEPTH, 1)
  assert.equal(rowOpen({ hasKids: true, depth: 0, searching: true }), true)
  assert.equal(rowOpen({ hasKids: true, depth: 1, searching: true }), false)
  assert.equal(rowOpen({ hasKids: true, depth: 2, searching: true }), false)
})

t('搜「5001」只摆到第二级，不再一路摊到末级', () => {
  assert.deepEqual(shown('5001'), [
    '5001',
    '  500101'
  ])
})

t('人点过的 ▸ 照样能摊开（不然第三级以后走不进去）', () => {
  assert.deepEqual(shown('5001', new Set([6])), [
    '5001',
    '  500101',
    '    50010101'
  ])
  assert.equal(rowOpen({ hasKids: true, depth: 3, searching: true, expanded: new Set([7]), id: 7 }), true)
})

t('搜的时候点一级那个自动摊开的 ▾，要真能收起来', () => {
  // 以前是摆设：搜索时 expanded 被忽略，画着 ▾ 的箭头点了没反应。
  // 收起来的那一笔记进 collapsed，压过「按级数自动摊开」这条默认规矩。
  assert.equal(rowOpen({ hasKids: true, depth: 0, searching: true, collapsed: new Set([5]), id: 5 }), false)
  assert.equal(rowOpen({ hasKids: true, depth: 0, searching: true, expanded: new Set([5]), collapsed: new Set([5]), id: 5 }), false)
})

t('搜的时候点一级那个自动摊开的 ▾ 要收，但回车不是（回车是「看下级」）', () => {
  // 这两件事必须分开：箭头是照着画面点的，画着 ▾ 就该收；
  // 回车是「我要看下级」，早先搜索时 expanded 被忽略、回车点了画面不动，
  // 凭证页「打编码 → 回车 → ↓ → 回车选末级」这条链才走得通。
  assert.ok(/function flipRow\(n\)/.test(PICKER), '点箭头的翻面动作没了')
  assert.ok(/function toggleRow\(n\)/.test(PICKER), '回车的摊开动作没了')
  assert.ok(/searching\.value && !collapsed\.value\.has\(n\.id\)\) \{ expand\(n\.id, true\); return \}/.test(PICKER),
    '回车在搜索时不该把自动摊开的收起来')
  assert.ok(/hasKids\(n\) && \(!props\.selectParents \|\| event\?\.target\.closest\('\.sp-caret'\)\)\) \{\s*flipRow\(n\)/.test(PICKER),
    '点行/点箭头应当翻画面（flipRow），不是 toggle')
})

t('不搜的时候 collapsed 也算数 —— 人点过的一律作数', () => {
  assert.equal(rowOpen({ hasKids: true, depth: 0, searching: false, expanded: new Set([2]), collapsed: new Set([2]), id: 2 }), false)
  assert.equal(rowOpen({ hasKids: true, depth: 0, searching: false, expanded: new Set([2]), id: 2 }), true)
})

t('没下级的行不摊开', () => {
  assert.equal(rowOpen({ hasKids: false, depth: 0, searching: true }), false)
})

t('不搜的时候只看人点过的', () => {
  assert.equal(rowOpen({ hasKids: true, depth: 0, searching: false }), false)
  assert.equal(rowOpen({ hasKids: true, depth: 0, searching: false, expanded: new Set([2]), id: 2 }), true)
})

console.log('== 命中的整条祖先路都留下 ==')

t('搜三级编码，一级、二级都得跟着留下（不然是孤儿科目）', () => {
  assert.deepEqual(code('100202'), ['1002', '100201', '100202'])
})

t('搜末级编码，一路留到一级', () => {
  assert.deepEqual(code('5001010101'), ['5001', '500101', '50010101', '5001010101'])
})

t('空关键字 = 没在搜', () => {
  assert.equal(searchHitIds(TREE, ''), null)
  assert.equal(searchHitIds(TREE, '   '), null)
})

t('搜不到就是空集，不是 null', () => {
  assert.equal(searchHitIds(TREE, 'zzz').size, 0)
})

console.log('== 两个页面真的走这套判定 ==')

t('SubjectPicker 用的是 utils/subjectSearch 那份 rowOpen / searchHitIds', () => {
  assert.ok(/from '@\/utils\/subjectSearch'/.test(PICKER), 'SubjectPicker 没引 utils/subjectSearch')
  assert.ok(/rowOpen\(\{/.test(PICKER), '面板的 ▸/▾ 判定没走 rowOpen')
  assert.ok(/searchHitIds\(props\.options, keyword\.value\)/.test(PICKER), '筛选没走 searchHitIds')
})

t('depth 是当参数传进去的，不是从 n 上读', () => {
  // 坑过一次：visible 里那圈 walk 手里的是 options 里的**原始**科目，没有 depth 字段。
  // 照着 n.depth 读会一律当成 0 —— 面板上箭头都收着了，下级却还一条条摊在那儿。
  assert.ok(/isOpen\(n, depth\)/.test(PICKER), 'walk 里的摊开判定没拿到 depth')
  assert.ok(/isOpen\(n, n\.depth\)/.test(PICKER), '模板里的箭头没拿到 depth')
})

t('明细账默认科目是 1001 库存现金', () => {
  assert.ok(/s\.code === '1001' && s\.active/.test(DETAIL), '明细账没有优先选 1001')
  const after = DETAIL.slice(DETAIL.indexOf('loadSubjects'))
  assert.ok(after.indexOf("'1001'") < after.indexOf('is_leaf && s.active'),
    '1001 得排在「第一个启用末级」前面 —— 1001 自己有下级，只认末级就永远选不中')
})

console.log('== 两行科目名：只在左下角真有余额时才贴上沿 ==')

t('贴不贴上沿是两件事：折行(wraps) × 有余额(hasBottomHint)', () => {
  // 用户报过：已记账凭证不显示科目余额，科目名折两行却没有上下居中。
  // 原因是 is-wrap 只要折行就贴上沿 —— 那条规则本来只为「别压住左下角余额」而设，
  // 余额根本不显示的时候它就成了平白歪掉一行。
  assert.ok(/:class="\{ 'is-nonleaf': isNonLeaf, 'is-wrap': wraps && hasBottomHint \}"/.test(PICKER),
    "is-wrap 没收在 'wraps && hasBottomHint' 上 —— 没有余额的两行又被顶到上沿去了")
  assert.ok(/wraps\.value = need > lh/.test(PICKER), 'autosize 没量出「折没折」')
  assert.ok(/\.sp\.is-wrap \{ align-items: flex-start; \}/.test(PICKER), 'is-wrap 的贴上沿样式没了')
})

t('凭证页按这一格到底有没有余额来传 hasBottomHint', () => {
  // balanceText 收两个参数了（行号 + 行）：余额是**滚动**的 ——
  // 同一张凭证里同一科目的三行显示 100 / 200 / 300，各算各的，
  // 所以得知道是哪一行（见 tests/voucher_running_balance.test.mjs）。
  assert.ok(/:has-bottom-hint="!!balanceText\(i, row\)"/.test(VOUCHER),
    'VoucherEdit 没把 balanceText(i, row) 传给 has-bottom-hint')
})

t('摘要格一直是居中的（对齐的基准没被动过）', () => {
  // 居中的那根基准线是 .td-sum 那一格：两行科目名要看着跟它齐，得是同一个 align-items
  assert.ok(/\.td-sum textarea/.test(VOUCHER) || /align-items: center/.test(VOUCHER),
    '摘要格的居中基准找不到了')
})

console.log('== 面板那个 ▸ 箭头的命中区 ==')

t('箭头的可点盒子有 20×20，不是那个 10 来像素的字', () => {
  // 用户报过「展开按钮有点小，有时候点不到」：字形才 ~10px，靠字形点。
  const caret = PICKER.slice(PICKER.indexOf('.sp-caret {'))
  assert.ok(/width: 20px/.test(caret) && /height: 20px/.test(caret), '箭头命中区还是原来那个小盒子')
  assert.ok(/margin: -2px 0 -2px -8px/.test(caret), '箭头的负边距没了（盒子会挤走编码列）')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
