/**
 * 凭证判平：整数分口径（审查 P0-1）＋ 抽出来的纯函数可测（P3-9）。
 *
 * P0-1 的原状是 `total` 用 JS number 累加、`balanced` 用 `===` 比：
 * `100.10 + 200.20 = 300.29999999999995 ≠ 300.3`，于是**合法的凭证**被判成不平，
 * 而手动保存和自动保存都只认 `balanced` 一个闸门 —— 存不进去。
 *
 * 这里不重写一份等价逻辑（那只能证明等价物是对的），而是从**真实组件源码**里
 * 抠出整段金额实现跑：被测的是页面上真在用的那份代码，跟
 * voucher_race.test.mjs 对 load() 的做法是同一个路子。
 *
 * 抠的是 `// ---- 金额：… ----` 到 `// ---- 金额段结束 ----` 之间那一段 ——
 * 判平 / 差额 / 合计 / 补差的公共依赖都在里头，一个都不外借。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { computed, reactive } from 'vue'
import { fmt } from '../src/utils/format.js'

const SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

const START = '// ---- 金额：一律整数分'
const END = '// ---- 金额段结束'

let pass = 0, fail = 0
const tests = []
/** 注册用例。别在这里 await —— 统一在末尾串行跑。 */
const t = (name, fn) => tests.push([name, fn])

/** 抠出金额那一段，并自检抠到的是它（抠空了就让断言失败，别让测试空转）。 */
function amountBlock() {
  const a = SRC.indexOf(START)
  const b = SRC.indexOf(END)
  assert.ok(a !== -1, `源码里找不到标记 ${START}`)
  assert.ok(b > a, `源码里找不到结束标记 ${END}`)
  const block = SRC.slice(a, b)
  for (const fn of ['centsOf', 'centsText', 'voucherTotals', 'voucherSignedTotals', 'autoBalanceNeed']) {
    assert.ok(block.includes(`function ${fn}(`), `抠出来的段里没有 ${fn}() —— 标记挪位置了？`)
  }
  return block
}
const BLOCK = amountBlock()

/**
 * 用真实的金额实现搭一套判平状态。form 是 reactive 的，所以改一条分录之后
 * 再读 balanced / diffTag 会重新算 —— 补差那条路（补完必须变平）才测得动。
 */
function state(entries) {
  const form = reactive({ entries: entries.map((e) => ({ subject_id: 1, summary: '', ...e })) })
  const factory = new Function('computed', 'fmt', 'form', `${BLOCK}
    return { form, totals, total, signedTotals, diffCents, balanced, diffTag, blank,
             centsOf, centsText, voucherTotals, voucherSignedTotals, autoBalanceNeed }`)
  return factory(computed, fmt, form)
}

/** 页脚那一行文案。照抄模板里的优先级：还没录 → 平 → 差额（VoucherEdit.vue:179-181）。 */
function footerTag(s) {
  if (s.blank.value) return '还没录金额'
  if (s.balanced.value) return '借贷平衡 ✓'
  return s.diffTag.value
}

const D = (amount, subject_id = 1) => ({ direction: 'DEBIT', amount, subject_id })
const C = (amount, subject_id = 2) => ({ direction: 'CREDIT', amount, subject_id })

console.log('== P0-1 合法凭证判平（整数分口径）==')

// 先证明「这个 bug 是真的」：JS 的浮点确实凑不出这两个和。
// 哪天真凑出来了，下面两条用例会失去意义 —— 那时该改的是测试，不是判平。
t('浮点残差确实存在（测试前提，不是被测行为）', () => {
  assert.notEqual(100.10 + 200.20, 300.30,
    '100.10+200.20 已经等于 300.30 了，P0-1 不复现 —— 请复核这个测试还测的是不是同一件事')
  assert.notEqual(0.1 + 0.2, 0.3)
})

t('借 100.10 + 借 200.20 / 贷 300.30 → 平', () => {
  const s = state([D('100.10'), D('200.20'), C('300.30')])
  assert.equal(s.totals.value.debit, 30030)
  assert.equal(s.totals.value.credit, 30030)
  assert.equal(s.balanced.value, true, '两借一贷约 22% 的凭证本来被判成不平')
  assert.equal(s.blank.value, false)
  assert.equal(footerTag(s), '借贷平衡 ✓')
})

t('借 0.1 + 借 0.2 / 贷 0.3 → 平', () => {
  const s = state([D('0.1'), D('0.2'), C('0.3')])
  assert.equal(s.totals.value.debit, 30)
  assert.equal(s.balanced.value, true)
  assert.equal(footerTag(s), '借贷平衡 ✓')
})

t('红字凭证 借 −300 / 贷 −300 → 平（红字照存，语义不许破）', () => {
  const s = state([D('-300'), C('-300')])
  // 负数算到对面去：两边各摆 300.00
  assert.equal(s.totals.value.debit, 30000)
  assert.equal(s.totals.value.credit, 30000)
  assert.equal(s.balanced.value, true, '红字冲销的老凭证必须照存')
  assert.equal(footerTag(s), '借贷平衡 ✓')
})

t('正负相抵 借 111 / 借 −111 → 平（按平处理，语义不许破）', () => {
  const s = state([D('111'), D('-111')])
  // 红字把 −111 搬去了对面，两栏一样大
  assert.equal(s.totals.value.debit, 11100)
  assert.equal(s.totals.value.credit, 11100)
  assert.equal(s.balanced.value, true, '冲销凭证被当成不平了')
  assert.equal(footerTag(s), '借贷平衡 ✓')
})

t('空表 → 「还没录金额」，不是「平」也不是红字差额', () => {
  const s = state([])
  assert.equal(s.blank.value, true)
  assert.equal(s.balanced.value, true, '空表两栏都是 0，balanced 为真 —— 但 blank 先说上')
  assert.equal(footerTag(s), '还没录金额')
})

t('真的差钱时页脚说的是差多少、哪一边大', () => {
  const s = state([D('100.10'), C('200.20')])
  assert.equal(s.balanced.value, false)
  assert.equal(s.diffCents.value, -10010)
  assert.equal(footerTag(s), '贷方大 100.10')
  // 借方大那一侧也说清楚
  assert.equal(state([D('200.20'), C('100.10')]).diffTag.value, '借方大 100.10')
})

t('任何一种不平衡都报不出「0.00 差额」', () => {
  // 原来 fmt(Math.abs(借 − 贷)) 会把浮点残差抹成「贷方大 0.00」，人看不懂自己错在哪
  for (const [d, c] of [['100.10', '200.20'], ['0.1', '0.2'], ['1.11', '2.22'], ['10.01', '10.02']]) {
    const s = state([D(d), C(c)])
    const tag = footerTag(s)
    assert.notEqual(tag, '贷方大 0.00', `${d}/${c} 报出了 0.00 差额`)
    assert.notEqual(tag, '借方大 0.00', `${d}/${c} 报出了 0.00 差额`)
  }
})

t('枚举：凡是真平的一律判平，老口径会拒掉其中一大片', () => {
  const amounts = ['0.1', '0.2', '0.3', '1.1', '2.2', '10.01', '100.10', '200.20', '300.30', '1111.11']
  let checked = 0, oldWouldReject = 0, rejected = []
  for (const a of amounts) {
    for (const b of amounts) {
      const c = +(a) + +(b)
      const text = c.toFixed(2)
      const entries = [D(a), D(b), C(text)]
      // 老口径：float 累加 + 严格相等
      const oldBalanced = (+a + +b) === +text
      if (oldBalanced) continue          // 老口径能过的，这里不重复算命中
      checked++
      const s = state(entries)
      if (!s.balanced.value) { rejected.push(`${a}+${b}/${text}`); oldWouldReject++ }
    }
  }
  assert.ok(checked > 0, '一个用例都没跑到 —— 金额表或判平逻辑变了？')
  assert.deepEqual(rejected, [],
    `整数分口径下仍被判为不平：${rejected.slice(0, 5).join('、')}`)
  console.log(`       （老口径会拒掉的组合：${checked} 个，现在全部判平）`)
})

console.log('')
console.log('== 负数在整数分口径下的处理（不假设，实测）==')

t('红字写进对面那一栏，不是相减', () => {
  // 借方红字落到贷方栏、贷方红字落到借方栏 —— 红字的本意（冲销同一笔），
  // 不是「贷方减了 300」。要是照带符号显示，这一格会摆出负数，
  // 页脚的差额也就凭空多出一份。
  assert.deepEqual(state([D('-100.10')]).totals.value, { debit: 0, credit: 10010 },
    '借方红字没搬到贷方那一栏')
  assert.deepEqual(state([C('-300')]).totals.value, { debit: 30000, credit: 0 },
    '贷方红字没搬到借方那一栏')
  // 带符号的合计仍然照减 —— 补差要的是这个，不是搬过栏的那个
  assert.deepEqual(state([D('-100.10'), C('-100.10')]).signedTotals.value,
    { debit: -10010, credit: -10010 })
  assert.equal(state([D('-100.10'), C('-100.10')]).balanced.value, true)
})

t('借 −100.10 / 贷 +100.10 是不平的（别把它当成红字冲销）', () => {
  // 红字只冲掉了一边：带符号是 −100.10 对 +100.10。判平看的就是这个 ——
  // 两栏看着一样大也不等于平，它跟「借贷平衡 ✓」不是一回事。
  const s = state([D('-100.10'), C('100.10')])
  assert.equal(s.balanced.value, false)
  assert.equal(s.blank.value, false)
})

t('落格用的真正减号 U+2212 也认成红字', () => {
  const s = state([D('−300'), C('−300')])
  assert.equal(s.totals.value.debit, 30000, 'U+2212 被当成垃圾字符剔掉的话会变成正的 300')
  assert.equal(s.balanced.value, true)
})

t('只敲了一个负号 / 空格子 → 0，不是 NaN', () => {
  const s = state([D('-'), C('')])
  assert.equal(Number.isNaN(s.totals.value.debit), false, 'NaN 一进合计，连「平不平」都说不出来了')
  assert.equal(s.blank.value, true)
})

t('带符号合计：借 100 / 借 −100 两边都归零（补差靠这个口径）', () => {
  const s = state([D('100'), D('-100')])
  assert.deepEqual(s.signedTotals.value, { debit: 0, credit: 0 })
  assert.equal(s.balanced.value, true, '正负相抵按平处理（2026-10-12 用户明确要求）')
})

console.log('')
console.log('== 定点化本身 ==')

t('0.1 与 0.10 是同一个数，负号与大小写都不影响', () => {
  const s = state([D('0.1'), C('0.10')])
  assert.equal(s.balanced.value, true)
})

t('接口给的 number 金额按同一套定点化处理', () => {
  // 后端返回的 amount 可能是 number 而不是字符串
  assert.equal(state([D(300.3), C(300.3)]).balanced.value, true)
  assert.equal(state([D(0.1), C(0.3), D(0.2)]).balanced.value, true)
})

t('小数只取到分（金额列就是两位，多余的位次静默截断）', () => {
  const s = state([])
  assert.equal(s.centsOf('1.005'), 100, '1.005 应截成 1.00')
  assert.equal(s.centsOf('1.999'), 199, '1.999 应截成 1.99')
  assert.equal(s.centsOf('.5'), 50)
  assert.equal(s.centsOf('300'), 30000)
})

t('centsText 与 centsOf 互为逆运算', () => {
  const s = state([])
  for (const c of [0, 1, -1, 99, 100, -30050, 99999999999, -99999999999]) {
    assert.equal(s.centsOf(s.centsText(c)), c, `${c} 往返不回来`)
  }
  assert.equal(s.centsText(-30050), '-300.50')
  assert.equal(s.centsText(0), '0.00')
})

t('认不出来的输入一律当 0，不许把 NaN 放进合计', () => {
  const s = state([])
  for (const v of ['', '   ', '-', 'abc', null, undefined, NaN, {}, []]) {
    const c = s.centsOf(v)
    assert.equal(Number.isFinite(c), true, `centsOf(${JSON.stringify(v)}) = ${c}，不是有限整数`)
  }
})

console.log('')
console.log('== P0-1 第二重矛盾：补差之后必须真的存得下 ==')

/**
 * 「=」补差在组件里的口径：`opposite` = 写进去那一格的**对面**合计，
 * `same` = **本侧**合计；这一行会挪走，所以再把 `mine` 减掉。
 * 照抄组件 autoBalance() 里那四行，免得测试里另立一套口径。
 */
function needFor(s, i, side) {
  const row = s.form.entries[i]
  const st = s.signedTotals.value
  const mineSide = row.direction === 'CREDIT' ? 'c' : 'd'
  return s.autoBalanceNeed({
    opposite: side === 'd' ? st.credit : st.debit,
    same: side === 'd' ? st.debit : st.credit,
    mine: s.centsOf(row.amount),
    mineSide,
    side,
  })
}

t('「=」补差：借 100 + 借 200 + 借 200，第三行往贷方补 300 就该平', () => {
  const s = state([D('100'), D('200'), D('200')])
  assert.equal(s.balanced.value, false)
  const need = needFor(s, 2, 'c')
  assert.equal(s.centsText(need), '300.00', '补 500 就等于凭空吃掉那 200')
  // 补进去，重新算 —— 这一步就是「提示已补上却依然存不了」那个 bug
  s.form.entries[2].direction = 'CREDIT'
  s.form.entries[2].amount = s.centsText(need)
  assert.equal(s.balanced.value, true, '补完差仍然存不进去 —— 判平与补差不是同一套口径')
})

t('浮点残差那一组：补出来的正好是 300.30，补完真的存得下', () => {
  // 100.10 + 200.20 + 300.30 三条借方。补差要拿「对面 − 本侧 − 这一行自己」，
  // 老口径在 float 上算出来是 300.29999999999995 —— 写进格子还是存不了。
  const s = state([D('100.10'), D('200.20'), D('300.30')])
  const need = needFor(s, 2, 'c')
  assert.equal(need, 30030, `补差算出了 ${need} 分，不是整数分 —— 还在用 float`)
  assert.equal(s.centsText(need), '300.30')
  s.form.entries[2].direction = 'CREDIT'
  s.form.entries[2].amount = s.centsText(need)
  assert.equal(s.balanced.value, true)
  assert.equal(footerTag(s), '借贷平衡 ✓')
})

t('补出来是负数就红字（冲销），方向别翻', () => {
  // 借 100 / 贷 500，在第一行的贷方格按「=」：这一行的借方会挪走，
  // 所以补出来是 −500（红字），不是 +400。补完仍然平。
  const s = state([D('100'), C('500')])
  const need = needFor(s, 0, 'c')
  assert.equal(s.centsText(need), '-500.00')
  s.form.entries[0].direction = 'CREDIT'
  s.form.entries[0].amount = s.centsText(need)
  assert.equal(s.balanced.value, true, '红字补差之后应该判平')
  // 一张里红字和正数混着，照样按平处理
  assert.equal(state([D('-300'), C('-500'), C('200')]).balanced.value, true,
    '带符号是 −300 对 −500+200，是平的')
})

t('已经平了的时候，空格按「=」算出来是 0（提示「这一格得空着」）', () => {
  // 0.1 + 0.2 / 0.15 + 0.15：分上是 30 对 30，平的。老口径在 float 上
  // 两边是 0.30000000000000004 对 0.3 —— 补差算出的 0 是 toFixed(2) 凑出来的，
  // 不是真的 0。现在是在整数分上算出来的，两边一分不差。
  const s = state([D('0.1'), D('0.2'), C('0.15'), C('0.15'), { direction: 'DEBIT', amount: '' }])
  assert.equal(s.balanced.value, true)
  assert.equal(needFor(s, 4, 'c'), 0, '平着的凭证，空格按「=」该得 0')
})

let i = 0
for (const [name, fn] of tests) {
  try {
    await fn()
    console.log('  OK   ' + name)
    pass++
  } catch (e) {
    console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0])
    fail++
  }
  i++
}

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)