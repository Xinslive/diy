/**
 * N4 + §3.23：凭证编辑页的「保存被拦住」这件事。
 *
 * ① **N4**：§3.23 的修法（save() 里对「有金额没科目」明确报错）引入了一个新毛病 ——
 *    那条早退发生在 ``sentSig`` 取出来**之前**，而 ``failedSig`` 只在 catch 里记，
 *    于是这一版**没被记下来** → 自动保存的 700ms 防抖每触发一次就再弹一条
 *    「第 N 行还差会计科目」。报错成了背景噪音。
 *
 * ② **§3.23**：显示口径与提交口径必须**是同一份集合**（payableEntries），
 *    且「有金额没科目」必须在**发请求之前**就报出来（不���把行静默丢掉）。
 *
 * 两条都从**真实组件源码**里抠实现跑：
 *   · 金额那一段（centsOf / centsText / voucherTotals / payableEntries /
 *     rowsMissingSubject / totals / total / diffCents / balanced）——
 *     与 tests/voucher_balance.test.mjs 抠的是**同一段**；
 *   · save() 与 autoSave()（自动保存那一趟）。
 *
 * ⚠️ 与 tests/voucher_deadcode.test.mjs 的区别：那份用例里 payableEntries /
 *   rowsMissingSubject 是**自己 stub 的**（等于测了一份等价物）。这里用**真的**。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { computed, reactive } from 'vue'
import { moneyText } from '../src/utils/format.js'
import { centsToText, moneyCents } from '../src/utils/amountGrid.js'

const ROOT = new URL('../', import.meta.url)
const SRC = fs.readFileSync(new URL('src/views/voucher/VoucherEdit.vue', ROOT), 'utf8')

const START = '// ---- 金额：一律整数分'
const END = '// ---- 金额段结束'

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
function grabFn(name, src = SRC) {
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

/** 抠出金额那一段（centsOf / centsText / voucherTotals / payableEntries / …）。 */
function amountBlock() {
  const a = SRC.indexOf(START)
  const b = SRC.indexOf(END)
  assert.ok(a !== -1, `源码里找不到标记 ${START}`)
  assert.ok(b > a, `源码里找不到结束标记 ${END}`)
  const block = SRC.slice(a, b)
  for (const fn of ['centsOf', 'centsText', 'voucherTotals', 'autoBalanceNeed']) {
    assert.ok(block.includes(`function ${fn}(`), `抠出来的段里没有 ${fn}() —— 标记挪位置了？`)
  }
  for (const c of ['payableEntries', 'rowsMissingSubject', 'balanced']) {
    assert.ok(block.includes(`const ${c} =`), `抠出来的段里没有 ${c} —— 标记挪位置了？`)
  }
  return block
}
const BLOCK = amountBlock()

console.log('== 抠取器自测（抠错了后面全是假绿）==')

t('抠取器自测：抠出来的是**那一段**，不是一份等价实现', () => {
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

t('抠取器自测：金额那一段里真的有那几样东西', () => {
  assert.ok(BLOCK.includes('const payableEntries = computed'), '抠到的不是真实现')
  assert.ok(BLOCK.includes('function columnTotal('), '合计函数不在这一段里')
  assert.ok(grabFn('save').includes('rowsMissingSubject'), 'save() 里没有那道拦截')
  assert.ok(grabFn('autoSave').includes('failedSig'), 'autoSave() 里没有 failedSig 那道闸')
})

/**
 * 搭一套**真的**凭证页状态：金额、合计、判平、 payableEntries 都是源码里抠出来的那份。
 */
function state(entries) {
  const form = reactive({
    voucher_date: '2026-01-01',
    attachments: 0,
    remark: '',
    entries: entries.map((e) => ({ subject_id: null, direction: 'DEBIT', amount: '', summary: '', ...e }))
  })
  const factory = new Function('computed', 'moneyText', 'form', 'moneyCents', 'centsToText', `${BLOCK}
    return { form, payableEntries, rowsMissingSubject, totals, total, diffCents, balanced,
             centsOf, centsText, voucherTotals, columnTotal }`)
  return factory(computed, moneyText, form, moneyCents, centsToText)
}

/**
 * 搭一套 save() + autoSave()：金额相关的判据全部来自**真实实现**。
 * 其余依赖（路由、接口、提示条）由外面喂进去。
 */
function saver(st, over = {}) {
  const said = []
  const requests = []
  const ctx = {
    readonly: { value: false },
    posted: { value: false },
    locked: { value: false },
    balanced: st.balanced,
    saving: { value: false },
    failedSig: { value: '' },
    lastSaved: { value: '' },
    savedAt: { value: '' },
    voucherId: { value: 0 },
    insertCtx: { value: null },
    formBookId: { value: 3 },
    voucherPeriod: { value: null },
    voucherPeriodId: { value: 0 },
    entrySig: { value: 'sig-1' },
    newNonLeafRows: { value: [] },
    form: st.form,
    store: { canWrite: true, bookId: 3, periods: [], createdVoucher: null },
    centsOf: st.centsOf,
    centsText: st.centsText,
    // **真的那一份**（不是 stub）：显示口径与提交口径共用它（审计 §3.23）
    payableEntries: st.payableEntries,
    rowsMissingSubject: st.rowsMissingSubject,
    rowProblemText: (r) => `非末级科目 ${r.subject_id}`,
    createKey: { value: '' },
    voucherVersion: { value: '' },
    versionConflict: { value: false },
    newIdempotencyKey: () => 'test-idem-key',
    api: {
      createVoucher: async (b, payload, key) => {
        requests.push(['create', b, payload, key])
        return { id: 7, period_id: 5, version: 'v1' }
      },
      updateVoucher: async (b, id, payload) => {
        requests.push(['update', b, id, payload])
        return { id, period_id: 5, version: 'v1' }
      }
    },
    history: { state: null, replaceState() {} },
    tabs: { open: () => {} },
    loadNeighbour: async () => {},
    ElMessage: {
      warning: (m) => said.push(['warning', m]),
      error: (m) => said.push(['error', m]),
      success: (m) => said.push(['success', m])
    },
    router: { push: () => {} },
    ...over
  }
  const factory = new Function(...Object.keys(ctx), `
    let savingPromise = null
    let autoSaveTimer = null
    const AUTO_SAVE_DELAY = 700
    ${grabFn('save')}
    ${grabFn('autoSave')}
    return { save, autoSave }`)
  const fns = factory(...Object.values(ctx))
  return { ...fns, said, requests, ctx }
}

/** 第 2 行有金额、没科目（§3.23 的现场）。 */
const ROWS_WITH_MISSING = [
  { subject_id: 1, direction: 'DEBIT', amount: '100.00' },
  { subject_id: null, direction: 'DEBIT', amount: '50.00' },   // ← 有金额没科目
  { subject_id: 2, direction: 'CREDIT', amount: '100.00' }
]
/** 同样的三行，但第 2 行把科目补上了（金额凑成平的）。 */
const ROWS_COMPLETE = [
  { subject_id: 1, direction: 'DEBIT', amount: '100.00' },
  { subject_id: 3, direction: 'DEBIT', amount: '50.00' },
  { subject_id: 2, direction: 'CREDIT', amount: '150.00' }
]

console.log('== §3.23 显示口径与提交口径必须是同一份集合 ==')

t('**行为**：有金额没科目的那一行**不在**能落库的那一份里（显示与提交共用它）', () => {
  const st = state(ROWS_WITH_MISSING)
  assert.equal(st.payableEntries.value.length, 2, '能落库的那一份的行数不对')
  assert.deepEqual(st.payableEntries.value.map((e) => e.subject_id), [1, 2],
    '没科目的那一行还在「能落库」的那一份里 —— 提交时它会被静默丢掉')
  assert.deepEqual(st.rowsMissingSubject.value.map((e) => e.amount), ['50.00'],
    '「有金额没科目」的那一行没被单独认出来')
})

t('**行为**：合计与判平也取自**同一份**（页面上说的平，就是提交出去的那一份平）', () => {
  const st = state(ROWS_WITH_MISSING)
  // 第 2 行那 50 元不在合计里（它提交时不存在，摆在合计里就是个提交后不存在的数）
  assert.equal(st.total.value.debit, '100.00', '合计里混进了提交时会被丢掉的数')
  assert.equal(st.total.value.credit, '100.00')
  assert.equal(st.balanced.value, true, '这一份是平的（借 100 = 贷 100）')
})

t('**行为**：save() 在**发请求之前**就报「第 2 行还差会计科目」，且不发请求', async () => {
  const st = state(ROWS_WITH_MISSING)
  const s = saver(st)
  const ok = await s.save()
  assert.equal(ok, false, '这一版根本不该落库，却说存成了')
  assert.deepEqual(s.requests, [], '请求发出去了 —— 那一行会被静默丢掉，后端只回「借贷不平衡」')
  assert.equal(s.said.length, 1, `应该报且只报一条：${JSON.stringify(s.said)}`)
  const [kind, text] = s.said[0]
  assert.equal(kind, 'error', '这是错误，不是提醒')
  assert.match(text, /第 2 行还差会计科目/, `报错没点名是第几行：${text}`)
})

t('**对称**：科目补齐后 → 请求照发，发出去的**就是**页面上算平的那一份', async () => {
  const st = state(ROWS_COMPLETE)
  const s = saver(st)
  const ok = await s.save()
  assert.equal(ok, true, '一张平凭证没存成')
  assert.equal(s.requests.length, 1, `请求数不对：${s.requests.length}`)
  const [kind, , payload] = s.requests[0]
  assert.equal(kind, 'create', '新建那张走的是 create')
  const sent = payload.entries
  assert.deepEqual(sent.map((e) => e.subject_id), st.payableEntries.value.map((e) => e.subject_id),
    '发出去的与页面上算的不是同一份')
  assert.deepEqual(sent.map((e) => e.amount),
    st.payableEntries.value.map((e) => st.centsText(st.centsOf(e.amount))),
    '发出去的金额与页面上算的不是同一套定点化')
  assert.deepEqual(s.said, [], `存成了却弹了东西：${JSON.stringify(s.said)}`)
})

console.log('== N4 同一版不许重复弹同一条错 ==')

t('**行为**：被拦住之后，自动保存再跑一趟**不再弹第二条**', async () => {
  const st = state(ROWS_WITH_MISSING)
  const s = saver(st)
  await s.save()
  assert.equal(s.said.length, 1, `第一次就该只报一条：${JSON.stringify(s.said)}`)
  // 自动保存的 700ms 防抖到了 → autoSave() 再跑一趟
  await s.autoSave()
  assert.equal(s.said.length, 1,
    `同一版又弹了一遍：${JSON.stringify(s.said)} —— 报错成了背景噪音`)
  assert.deepEqual(s.requests, [], '被拦住的那一版不该发请求')
})

t('**行为**：人把那行改完（签名变了）→ 下一趟自动保存照常走，不再被旧签名挡住', async () => {
  const st = state(ROWS_WITH_MISSING)
  const s = saver(st)
  await s.save()
  assert.equal(s.ctx.failedSig.value, 'sig-1', '被拦住的那一版没被记下来')
  // 人把第 2 行的科目补上（顺手把第 1 行的金额改一下，让这张还是平的）
  st.form.entries[1].subject_id = 3
  st.form.entries[0].amount = '50.00'
  s.ctx.entrySig.value = 'sig-2'
  assert.equal(st.balanced.value, true, '这一版不平 —— 用例的前提不成立')
  const saidBefore = s.said.length
  await s.autoSave()
  assert.equal(s.requests.length, 1, '改完之后那一趟自动保存没走 —— 改动被吞了')
  assert.equal(s.said.length, saidBefore, '改完之后又报了一遍那条错')
})

t('**对称**：非末级科目那条拦截也是同一版只报一次', async () => {
  const st = state(ROWS_COMPLETE)
  const s = saver(st, { newNonLeafRows: { value: [{ subject_id: 99 }] } })
  await s.save()
  assert.equal(s.said.length, 1, `应该报且只报一条：${JSON.stringify(s.said)}`)
  assert.match(s.said[0][1], /非末级科目/)
  await s.autoSave()
  assert.equal(s.said.length, 1, `同一版又弹了一遍：${JSON.stringify(s.said)}`)
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
