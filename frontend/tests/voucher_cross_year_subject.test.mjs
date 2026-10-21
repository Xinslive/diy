/**
 * 跨年凭证的科目看不见了（2026-10-22 用户报）。
 *
 * **现象**：在当前年度用关键词搜到以前年度的凭证，点开那张凭证，
 * 「会计科目」那一列是空的 —— 摘要、金额都在，就是没有科目。
 *
 * **机制**：科目表每个会计年度各有一份（后端迁移 0032），**同一个编码在
 * 两个年度是两个 id、两行记录**。凭证页若按顶栏那一年去取科目表，
 * 往年凭证上的 subject_id 在那张表里一条都对不上，而界面上是「按 id 找
 * 科目名」—— 找不到就是空的，**还不报任何错**（那串 id 在表里确实不存在，
 * 只是没人提这件事）。
 *
 * 所以凭证页必须取**这张凭证自己那一年**的科目表。这份文件钉住三件事：
 *
 *   1. 科目格显示什么，完全由「传进来的是哪张表」决定（不是由凭证决定）；
 *   2. 这一页该用哪一年，由凭证自己说了算（服务端随凭证给出 `year`）；
 *   3. 停用科目要**显示**（老凭证指着它）但不能被**新选中**。
 *
 * 测试的路子是本仓的老规矩：**从真实组件源码里抠出那段实现来跑** ——
 * 被测的是页面上真在用的那份代码，不是另写一份等价物。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { computed } from 'vue'

const VOUCHER_SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')
const PICKER_SRC = fs.readFileSync(
  new URL('../src/components/SubjectPicker.vue', import.meta.url), 'utf8')
const DIALOG_SRC = fs.readFileSync(
  new URL('../src/components/SubjectFormDialog.vue', import.meta.url), 'utf8')
const LEDGER_SRC = fs.readFileSync(
  new URL('../src/views/ledger/DetailLedger.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const tests = []
/** 注册用例。别在这里 await —— 统一在末尾串行跑。 */
const t = (name, fn) => tests.push([name, fn])

/** 抠出一段实现，并自检抠到的是它（抠空了就让断言失败，别让测试空转）。 */
function block(src, start, end, what) {
  const a = src.indexOf(start)
  const b = src.indexOf(end)
  assert.ok(a !== -1, `源码里找不到标记 ${start}（${what}）`)
  assert.ok(b > a, `源码里找不到结束标记 ${end}（${what}）`)
  return src.slice(a, b)
}

// --------------------------------------------------------------------- 抠实现

/** 凭证页的年度口径：这一页用哪一年的科目表。 */
const YEAR_BLOCK = block(
  VOUCHER_SRC, '// ---- 科目表年度：这一页用哪一年的科目表 ----', '// ---- 科目表年度段结束 ----',
  '年度口径')

/** 科目格里显示什么：current / byId / isOff / displayName / resting。 */
const PICKER_BLOCK = block(
  PICKER_SRC, 'const current = computed(', 'const editing = computed(', '科目名显示')

/** 拿一份 store 替身，跑真实的年度口径。 */
function yearResolver(store) {
  const factory = new Function('store', `${YEAR_BLOCK}\nreturn yearOfVoucher`)
  return factory(store)
}

/** 拿一份 props 替身，跑真实的科目名显示逻辑。 */
function pickerState(props) {
  const factory = new Function('computed', 'props', `${PICKER_BLOCK}
    return { current, byId, isOff, displayName, resting }`)
  return factory(computed, props)
}

/**
 * 两张科目表：同编码、不同 id —— 这正是「每个年度各有一份」的样子。
 * 2026 那张的 1001 是 id 1，2027 那张的 1001 是 id 2（**不是同一个 id**）。
 */
const CHART_2026 = [
  { id: 1, code: '1001', name: '库存现金', parent_id: null, active: true }
]
const CHART_2027 = [
  { id: 2, code: '1001', name: '库存现金', parent_id: null, active: true }
]
/** 顶栏停在 2026，凭证是 2025 年的 —— 用户报的那一幕。 */
const STORE = {
  currentYear: 2026,
  periods: [
    { id: 11, year: 2025, month: 12, code: '2025-12' },
    { id: 12, year: 2026, month: 1, code: '2026-01' }
  ]
}

console.log('== 一、科目格显示什么，只由「传进来的是哪张表」决定 ==')

t('拿错年度的表 → 科目格是空的（用户报的那个现象）', () => {
  // 这张 2025 年的凭证挂的是 2025 那张表里的 id（这里用 2 代替）
  const wrong = pickerState({ options: CHART_2026, modelValue: 2, allowInactive: true })
  assert.equal(wrong.resting.value, '',
    '拿 2026 的表去认 2025 的凭证，科目格竟然有字 —— 机制变了？')
})

t('取对年度的表 → 科目名照旧显示', () => {
  const right = pickerState({ options: CHART_2027, modelValue: 2, allowInactive: true })
  assert.equal(right.resting.value, '库存现金')
})

t('同一个 id，换一张表就换一副面孔（id 才是真身）', () => {
  const a = pickerState({ options: CHART_2026, modelValue: 1, allowInactive: true })
  const b = pickerState({ options: CHART_2027, modelValue: 1, allowInactive: true })
  assert.equal(a.resting.value, '库存现金')
  assert.equal(b.resting.value, '', '2027 那张表里没有 id=1 —— 本来就该是空的')
})

console.log('')
console.log('== 二、这一页用哪一年，由凭证自己说了算 ==')

t('凭证自带 year → 用它那一年，不是顶栏那一年', () => {
  const y = yearResolver(STORE)
  assert.equal(y({ year: 2025, period_id: 11 }), 2025,
    '往年凭证被算成顶栏那一年 —— 拿错表，科目格就空了')
})

t('凭证没带 year → 按 period_id 本地反查', () => {
  const y = yearResolver(STORE)
  assert.equal(y({ period_id: 11 }), 2025)
  assert.equal(y({ period_id: 12 }), 2026)
})

t('两样都没有 → 退回顶栏那一年（不猜、不空）', () => {
  const y = yearResolver(STORE)
  assert.equal(y({}), 2026)
  assert.equal(y(null), 2026)
})

t('新凭证落在参照凭证那一期时，用参照那一年', () => {
  // 插入凭证的预览带的是参照凭证的 period_id / year
  const y = yearResolver(STORE)
  assert.equal(y({ year: 2025, period_id: 11 }), 2025)
})

console.log('')
console.log('== 三、源码层面：凭证页确实按凭证那一年取表 ==')

t('凭证分支按 yearOfVoucher 取表', () => {
  assert.ok(VOUCHER_SRC.includes('await loadSubjects(yearOfVoucher(v))'),
    '凭证页没有按凭证自己的年度取科目表 —— 跨年凭证的科目格会是空的')
})

t('新凭证分支按插入参照/顶栏那一年取表', () => {
  assert.ok(
    VOUCHER_SRC.includes('await loadSubjects(yearOfVoucher(insertCtx.value || { period_id: store.periodId }))'),
    '新凭证没有按「参照那一期」取表')
})

t('取表时带上 include_inactive：停用科目也要显示', () => {
  assert.ok(/subjectTree\([\s\S]{0,160}include_inactive: true/.test(VOUCHER_SRC),
    '取科目表时没带 include_inactive —— 老凭证上已停用的科目会显示成空格子')
})

t('取表时不再直接用顶栏那一年', () => {
  const m = VOUCHER_SRC.match(/subjectApi\.subjectTree\([\s\S]{0,200}?\)/)
  assert.ok(m, '找不到取科目表的那一行')
  assert.ok(!m[0].includes('store.currentYear'),
    '取科目表还在用顶栏那一年 —— 这正是跨年凭证看不到科目的根因')
})

console.log('')
console.log('== 四、停用科目：显示得了，选不了 ==')

t('录凭证时不许选停用科目', () => {
  const s = pickerState({ options: [], modelValue: null, allowInactive: false })
  assert.equal(s.isOff({ active: false }), true)
  assert.equal(s.isOff({ active: true }), false)
})

t('只读页面（明细账）仍可挑停用科目看它的明细', () => {
  const s = pickerState({ options: [], modelValue: null, allowInactive: true })
  assert.equal(s.isOff({ active: false }), false,
    '停用科目在明细账里选不了了 —— 那是只读查询入口，不该被这条改动波及')
})

t('凭证页显式不许选停用科目', () => {
  assert.ok(VOUCHER_SRC.includes(':allow-inactive="false"'),
    '凭证页没关掉「选停用科目」—— 选了它，保存时服务端会拒（科目不存在或已停用）')
})

t('明细账没被这条改动波及', () => {
  assert.ok(!LEDGER_SRC.includes('allow-inactive'),
    '明细账不该关掉「选停用科目」—— 它是只读查询入口')
})

console.log('')
console.log('== 五、凭证页当场新增科目，要落在凭证所属年度 ==')

t('弹窗收 year，凭证页把它自己的年度传进去', () => {
  assert.ok(DIALOG_SRC.includes('year: { type: Number, default: 0 }'),
    '新增科目弹窗没有 year 参数')
  assert.ok(VOUCHER_SRC.includes(':year="subjectYear"'),
    '凭证页没有把「这张凭证所属的年度」传给新增科目弹窗')
})

t('新增科目按传入的年度提交，不是顶栏那一年', () => {
  assert.ok(/createSubject\([\s\S]{0,300}?year: targetYear\.value/.test(DIALOG_SRC),
    '新增科目还在按顶栏那一年提交 —— 往年凭证页新增的科目会落到错的那张表上')
  assert.ok(!/createSubject\([\s\S]{0,300}?year: store\.currentYear/.test(DIALOG_SRC),
    '新增科目还在用 store.currentYear')
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
