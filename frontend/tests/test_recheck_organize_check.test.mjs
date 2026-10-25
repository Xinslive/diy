/**
 * N5：整理凭证页的「体检」状态机 —— **只有体检跑成功了**才允许
 * 「按当前顺序重新编号」。
 *
 * 复核方实测确认的现场（§3.27 的残留）：
 *   · ``checkFailed = ref(false)``、``renumBlocked = checkFailed || outOfOrder``；
 *   · 体检是异步的 → **初始态与在途态**下 checkFailed 还是 false、
 *     outOfOrder 还是 false → 「按当前顺序重新编号」是**可选的**；
 *   · 绕过的正是 §3.27 的核心不变量（「不知道时序对不对」不能当成「没问题」）。
 *
 * 这里把 checkState / outOfOrder / renumBlocked / loadCheck / preview 全部从
 * **真实源码**里抠出来跑（不是抄一份等价实现）。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const ROOT = new URL('../', import.meta.url)
const ORGANIZE = fs.readFileSync(new URL('src/views/voucher/VoucherOrganize.vue', ROOT), 'utf8')

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

/** 抠出 `const <name> = computed(...)` 整段。 */
function grabComputed(name, src) {
  const marker = `const ${name} = computed(`
  const i = src.indexOf(marker)
  assert.ok(i !== -1, `源码里找不到 ${marker}`)
  const open = i + marker.length - 1
  const close = matchBracket(src, open, '(', ')')
  assert.ok(close !== -1, `${name} 的 computed(...) 没配平`)
  return src.slice(i, close + 1)
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

console.log('== 抠取器自测（抠错了后面全是假绿）==')

t('抠取器自测：抠出来的是**那一段**，不是一份等价实现', () => {
  const fake = [
    'const x = computed(() => {',
    '  const s = `a ${1} b`',
    "  return { deep: '}' }",
    '})',
    'const y = computed(() => 2)',
  ].join('\n')
  const got = grabComputed('x', fake)
  assert.ok(got.startsWith('const x = computed('), '开头不对')
  assert.ok(got.includes('const s = `a ${1} b`'), '函数体被腰斩了')
  assert.ok(got.includes("return { deep: '}' }"), '字符串里的花括号把配平算错了')
  assert.ok(got.endsWith('})'), '结尾不对')
  assert.ok(!got.includes('const y'), '多抠了下一段')
})

t('抠取器自测：五个目标都抠得出来（抠空了要让断言失败，别让用例空转）', () => {
  assert.ok(grabComputed('checkState', ORGANIZE).includes('__state'), 'checkState 抠错了')
  assert.ok(grabComputed('outOfOrder', ORGANIZE).includes('checkState'), 'outOfOrder 抠错了')
  assert.ok(grabComputed('renumBlocked', ORGANIZE).includes('checkFailed'), 'renumBlocked 抠错了')
  assert.ok(grabFn('loadCheck', ORGANIZE).includes('__state'), 'loadCheck 抠错了')
  assert.ok(grabFn('preview', ORGANIZE).includes('renumBlocked'), 'preview 抠错了')
})

/**
 * 搭一套整理凭证页：状态机与函数全部来自**真实源码**。
 * @param {object} over 依赖覆盖（check 的返回、接口行为…）
 */
function organize(over = {}) {
  const calls = { warn: [], preview: 0, check: 0 }
  const deps = {
    store: { bookId: 7, periodId: 3 },
    check: { value: null },
    checkFailed: { value: false },
    strategy: { value: 'RENUM' },
    loading: { value: false },
    plan: { value: null },
    voucherApi: {
      voucherPeriodCheck: async () => { calls.check++; return { time_out_of_order: [] } },
      organizePreview: async () => { calls.preview++; return { total_changed: 0, total_gaps: 0 } }
    },
    ElMessage: {
      warning: (m) => calls.warn.push(m),
      success: () => {}
    },
    ...over
  }
  const factory = new Function('deps', `
    // 真 Vue 的 computed 是一个惰性求值的引用；这里用取值器模拟同样的语义。
    const computed = (fn) => ({ get value() { return fn() } })
    const store = deps.store
    const check = deps.check
    const checkFailed = deps.checkFailed
    const strategy = deps.strategy
    const loading = deps.loading
    const plan = deps.plan
    const voucherApi = deps.voucherApi
    const ElMessage = deps.ElMessage
    ${grabComputed('checkState', ORGANIZE)}
    ${grabComputed('hasOrderIssue', ORGANIZE)}
    ${grabComputed('outOfOrder', ORGANIZE)}
    ${grabComputed('renumBlocked', ORGANIZE)}
    ${grabFn('payload', ORGANIZE)}
    ${grabFn('loadCheck', ORGANIZE)}
    ${grabFn('preview', ORGANIZE)}
    return { checkState, hasOrderIssue, outOfOrder, renumBlocked, loadCheck, preview }`)
  const fns = factory(deps)
  return { ...fns, deps, calls }
}

console.log('== N5 体检状态机：只有「跑成功」才允许按当前顺序压号 ==')

t('**行为**：**还没体检**（初始态）→ RENUM 被按住', () => {
  const o = organize()
  assert.equal(o.checkState.value, 'idle', '初始态不是 idle')
  assert.equal(o.renumBlocked.value, true,
    '还没体检就放开了「按当前顺序重新编号」—— 「不知道」被当成了「没问题」')
})

t('**行为**：**体检在途** → RENUM 被按住，且执行时说得清为什么', async () => {
  let release = null
  const gate = new Promise((r) => { release = r })
  const o = organize({
    voucherApi: {
      voucherPeriodCheck: async () => { await gate; return { time_out_of_order: [] } },
      organizePreview: async () => { throw new Error('不该走到这里') }
    }
  })
  const p = o.loadCheck()             // 不 await：体检还在路上
  assert.equal(o.checkState.value, 'loading', '在途态没被认出来')
  assert.equal(o.renumBlocked.value, true, '体检在途却放开了 RENUM')
  await o.preview()                   // URL 里恢复出来的 strategy 可能已经是 RENUM
  assert.equal(o.calls.warn.length, 1, '体检在途却一句话不说')
  assert.match(o.calls.warn[0], /体检/, `提示没说到体检：${o.calls.warn[0]}`)
  release()
  await p
  assert.equal(o.checkState.value, 'ok', '体检跑完了却不是 ok')
})

t('**对称**：体检跑成功、时序没问题 → RENUM **放开**', async () => {
  const o = organize()
  await o.loadCheck()
  assert.equal(o.checkState.value, 'ok')
  assert.equal(o.hasOrderIssue.value, false)
  assert.equal(o.renumBlocked.value, false,
    '体检成功了却还按着 RENUM —— 「知道没问题」也被当成「不知道」')
  await o.preview()
  assert.equal(o.calls.preview, 1, '体检成功了却整理不了')
  assert.deepEqual(o.calls.warn, [], '体检成功了还乱弹提示')
})

t('**对称**：体检跑成功、但时序对不上 → RENUM 仍被按住', async () => {
  const o = organize({
    voucherApi: {
      voucherPeriodCheck: async () => ({
        time_out_of_order: [{ prev_no: 1, prev_date: '2026-01-02', voucher_no: 2, voucher_date: '2026-01-01' }]
      }),
      organizePreview: async () => { throw new Error('不该走到这里') }
    }
  })
  await o.loadCheck()
  assert.equal(o.checkState.value, 'ok')
  assert.equal(o.hasOrderIssue.value, true, '时序错位没被认出来')
  assert.equal(o.renumBlocked.value, true, '时序不对却放开了 RENUM')
})

t('**行为**：体检失败 → RENUM 被按住，且提示说到「体检」', async () => {
  const o = organize({
    voucherApi: {
      voucherPeriodCheck: async () => { throw new Error('500') },
      organizePreview: async () => { throw new Error('不该走到这里') }
    }
  })
  await o.loadCheck()
  assert.equal(o.checkState.value, 'failed', '失败态没被认出来')
  assert.equal(o.renumBlocked.value, true, '体检失败了却放开了 RENUM')
  await o.preview()
  assert.equal(o.calls.warn.length, 1, '体检失败了却一句话不说')
  assert.match(o.calls.warn[0], /体检/, `提示没说到体检：${o.calls.warn[0]}`)
})

t('页面上看得见「体检中…」这一档（在途不是一个看不见的中间态）', () => {
  assert.match(ORGANIZE, /v-if="checkState === 'loading'"/, '页面没有「体检中」这一档')
  assert.match(ORGANIZE, /正在做「整理就绪」体检/, '页面上的在途提示没说到体检')
  // RENUM 那一档仍然绑在 renumBlocked 上（选项是禁用的）
  assert.match(ORGANIZE, /value="RENUM" :disabled="renumBlocked"/, 'RENUM 的禁用条件变了')
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
