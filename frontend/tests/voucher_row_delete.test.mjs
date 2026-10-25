/**
 * 删分录行：**四行以内也能删**，删完后面的行自动往上排。
 *
 * 用户报的现象（2026-10-24）：「分录不足4行时不显示删除按钮」，于是想删掉
 * 前四行里的某一行时删不掉 —— 凭证只有四条分录的时候，表格上一个 ⊗ 都没有。
 *
 * 根因是删除按钮的门槛定成了「有没有第五行」（`entries.length > 4`）：
 * 四行被当成了**分录的护栏**。其实它是**行数的下限** —— 说的是「行得摆够，
 * 录第三条时不用临时找地方」，跟「前四行不许删」是两回事。
 *
 * 现在的规则：**底下那几行等着填的空位之外，哪一行都删得掉**。
 * 空位不给删，是因为它删了立刻又被补回来 —— 看着像没删，只会让人以为按钮坏了。
 *
 * 这里钉两件事：
 *   ① 规则本身（utils/voucherRows.js：rowCanBeDeleted / lastFilledRowAt）；
 *   ② 页面**真在用**的那份 removeRow —— 从 VoucherEdit.vue 里抠出来跑，
 *      验「后面的行自动往上排」「底下补回空位」「绿行跟着挪」。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { reactive, ref } from 'vue'
import {
  isBlankRow, lastFilledRowAt, rowCanBeDeleted
} from '../src/utils/voucherRows.js'

const SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

/** 抠出 `function <name>(…) { … }` 的整段源码（按大括号配平）。 */
function grabFn(name) {
  const at = SRC.indexOf(`function ${name}(`)
  assert.ok(at !== -1, `源码里找不到 function ${name}(`)
  let depth = 0, j = SRC.indexOf('{', at)
  for (; j < SRC.length; j++) {
    if (SRC[j] === '{') depth++
    else if (SRC[j] === '}') { depth--; if (depth === 0) break }
  }
  const out = SRC.slice(at, j + 1)
  assert.ok(out.endsWith('}'), `${name} 抠出来的不是一段完整的函数`)
  return out
}

const REMOVE_ROW_SRC = grabFn('removeRow')
const ENSURE_ROWS_SRC = grabFn('ensureRows')

/** 搭一套页面：真的 form、真的行对象，removeRow 是从组件源码里抠出来的那份。 */
function makePage(entries, { locked = false } = {}) {
  const form = reactive({ entries })
  const activeRow = ref(null)
  const hoverRow = ref(null)
  const markingRows = ref(new Set())
  // removeRow 还会收掉这一行「已提交的数」（键是分录对象）—— 见 VoucherEdit.vue
  const committedAmounts = ref(new Map())
  const readonly = ref(locked)
  const factory = new Function(
    'form', 'readonly', 'activeRow', 'hoverRow', 'markingRows', 'committedAmounts',
    'rowCanBeDeleted', 'lastFilledRowAt', `
    ${ENSURE_ROWS_SRC}
    function canDeleteRowAt(i) {
      return rowCanBeDeleted(form.entries, i, lastFilledRowAt(form.entries))
    }
    ${REMOVE_ROW_SRC}
    return { removeRow, canDeleteRowAt }
  `)
  const api = factory(form, readonly, activeRow, hoverRow, markingRows, committedAmounts,
    rowCanBeDeleted, lastFilledRowAt)
  return { form, activeRow, hoverRow, markingRows, committedAmounts, readonly, ...api }
}

/** 一条分录：给了科目就算有内容。 */
const E = (subject_id = 1, amount = '100', summary = '') =>
  ({ subject_id, direction: 'DEBIT', amount, summary })
/** 一行空位：科目、金额、摘要都没填。 */
const blank = (summary = '') => ({ subject_id: null, direction: 'DEBIT', amount: '', summary })

// ---------------------------------------------------------------- 规则

test('空位就是空位：科目和金额都没填', () => {
  assert.equal(isBlankRow(blank()), true)
  assert.equal(isBlankRow({ subject_id: 1, amount: '', summary: '' }), false)
  assert.equal(isBlankRow({ subject_id: null, amount: '100', summary: '' }), false)
  assert.equal(isBlankRow(null), true)
})

test('摘要不算数：新行带着上一行的摘要，空位上常常是有字的', () => {
  assert.equal(isBlankRow(blank('备用金')), true)
})

test('一张都没填 → 最后一行有内容的是 -1', () => {
  assert.equal(lastFilledRowAt([blank(), blank()]), -1)
})

test('最后一行有内容的是哪一行', () => {
  assert.equal(lastFilledRowAt([blank(), E(), blank()]), 1)
  assert.equal(lastFilledRowAt([E(), E(), E()]), 2)
})

test('四行以内也能删：老门槛（有没有第五行）下这一条删不掉', () => {
  const rows = [E(), E(), blank(), blank()]
  assert.equal(rows.length, 4)
  assert.equal(rowCanBeDeleted(rows, 0), true, '第一行必须删得掉')
  assert.equal(rowCanBeDeleted(rows, 1), true, '第二行必须删得掉')
})

test('底下那几行等着填的空位删不掉（删了立刻又被补回来）', () => {
  const rows = [E(), E(), blank(), blank()]
  assert.equal(rowCanBeDeleted(rows, 2), false)
  assert.equal(rowCanBeDeleted(rows, 3), false)
})

test('中间的空行删得掉：它下面还有内容，它不是「底下那几行空位」', () => {
  const rows = [E(), blank('写了一半'), E()]
  assert.equal(rowCanBeDeleted(rows, 1), true)
  assert.equal(rowCanBeDeleted(rows, 2), true)
})

test('越界的一行删不掉', () => {
  const rows = [E(), E()]
  assert.equal(rowCanBeDeleted(rows, -1), false)
  assert.equal(rowCanBeDeleted(rows, 2), false)
  assert.equal(rowCanBeDeleted([], 0), false)
})

test('154 行时每行只问一次 O(1) 的事（不退回逐行现算的 154²）', () => {
  const rows = Array.from({ length: 154 }, (_, i) => (i < 100 ? E() : blank()))
  const last = lastFilledRowAt(rows)
  assert.equal(last, 99)
  // 页面把 lastFilled 算成一个 computed 传进来：每行只做一次比较
  assert.equal(rowCanBeDeleted(rows, 0, last), true)
  assert.equal(rowCanBeDeleted(rows, 99, last), true)
  assert.equal(rowCanBeDeleted(rows, 100, last), false)
})

// ---------------------------------------------------------------- 页面

test('删掉第一行：后面的行自动往上排', () => {
  const p = makePage([E(1), E(2), E(3), blank()])
  p.removeRow(0)
  assert.deepEqual(p.form.entries.map((e) => e.subject_id), [2, 3, null, null])
  assert.equal(p.form.entries.length, 4, '底下补回空位，还是四行')
})

test('删掉中间一行：后面的行自动往上排', () => {
  const p = makePage([E(1), E(2), E(3), blank()])
  p.removeRow(1)
  assert.deepEqual(p.form.entries.map((e) => e.subject_id), [1, 3, null, null])
})

test('四行以内删得掉：删完底下补回空位，行数还是四', () => {
  const p = makePage([E(1), E(2), blank(), blank()])
  p.removeRow(0)
  assert.equal(p.form.entries.length, 4)
  assert.deepEqual(p.form.entries.map((e) => e.subject_id), [2, null, null, null])
})

test('空位点删不动', () => {
  const p = makePage([E(1), blank(), blank(), blank()])
  p.removeRow(3)
  assert.equal(p.form.entries.length, 4, '空位不该被删掉')
  assert.equal(p.form.entries[0].subject_id, 1)
})

test('绿行跟着往上挪：被删的那行已经不在表里，鼠标还压在这个位置上', () => {
  const p = makePage([E(1), E(2), blank(), blank()])
  const second = p.form.entries[1]
  p.hoverRow.value = second
  p.activeRow.value = second
  p.removeRow(1)
  assert.notEqual(p.hoverRow.value, second, '绿行不能还指着被删掉的那一行')
  assert.equal(p.hoverRow.value, p.form.entries[1], '绿行得落在顶上来的那一行上')
  assert.equal(p.activeRow.value, p.form.entries[1])
})

test('删掉的那一行正在标记科目：标记也得收掉', () => {
  const p = makePage([E(1), E(2), blank(), blank()])
  const second = p.form.entries[1]
  p.markingRows.value = new Set([second])
  p.removeRow(1)
  assert.equal(p.markingRows.value.has(second), false)
})

test('只读凭证删不动', () => {
  const p = makePage([E(1), E(2), blank(), blank()], { locked: true })
  p.removeRow(0)
  assert.equal(p.form.entries.length, 4)
  assert.equal(p.form.entries[0].subject_id, 1)
})

test('页面里不再有「有没有第五行」那道门槛', () => {
  assert.ok(!/entries\.length > 4/.test(SRC), '删除门槛还停在「有没有第五行」')
  assert.ok(!/canDeleteRow\b/.test(SRC.replace(/canDeleteRowAt/g, '')),
    '旧的 canDeleteRow computed 还留着')
  assert.ok(/v-if="readonly \|\| canDeleteRowAt\(i\)"/.test(SRC),
    '⊗ 的显示条件应当是「只读，或者这一行删得掉」')
})

test('⊗ 只跟着绿着的那一行走（挂在行右边外面）', () => {
  assert.ok(/\.vg-del > \* \{ visibility: hidden; \}/.test(SRC),
    '⊗ 不许在没绿的行上露出来')
  assert.ok(/\.vg-row\.is-on \.vg-del > \* \{ visibility: visible; \}/.test(SRC))
})
