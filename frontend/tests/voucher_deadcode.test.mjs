/**
 * 凭证编辑页的死代码清理（审查 P3-9）。
 *
 * 原来 `save({ quiet })` 里挂着一条 `quiet === false` 的分支：
 * 「弹一句『已保存』 + router.push 回列表」——那是**手动保存**的老路。
 * 这一页根本没有保存按钮（工具栏只有返回 / 前一张 / 后一张 / 插入 / 增加 / 打印），
 * 两个调用方 autoSave() 与 flushIfDirty() 都传 quiet: true，
 * 于是 `!quiet` 恒假，那段分支走不到，已经删掉。
 *
 * 删死代码最怕两件事，所以这里各钉一根钉子：
 *   ① 别把「走不到」当成「猜的」 —— 用例真的把 save() 抠出来跑一遍，
 *      让每条失败路径都**说得出话**（这正是那个恒假分支挡掉的东西）；
 *   ② 别顺手把还在用的也删了 —— 新凭证落库后对齐标签地址那条活路径照常验。
 *
 * 被测的是**真实组件源码**里那段 save()，不是抄一份等价实现
 * （抄一份只能证明抄的那份是对的）。抠法与 voucher_race.test.mjs 一致。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const tests = []
/** 注册用例。别在这里 await —— 统一在末尾串行跑。 */
const t = (name, fn) => tests.push([name, fn])

/**
 * 抠出 `function <name>(...) {...}` 的整段源码（按大括号配平）。
 *
 * 两个坑，都踩过：
 *  ① 名字后面必须紧跟 `(` —— 用 indexOf('function save') 会先撞上 saveDraft 之类，
 *     抠出来的就是另一个函数，而断言照样「通过」。
 *  ② **不能从签名里第一个 '{' 开始数花括号**。解构形参（`save({ quiet = false })`）
 *     的右花括号会被当成函数结束，抠出来只剩半个签名，
 *     后面报的还是 `Unexpected token 'return'` 这种看不懂的语法错。
 *     所以先把参数表的括号配平，从**函数体**那个 '{' 才开始数。
 *
 * 抠完立刻自检：抠错了要让断言失败，别让测试空转。
 */
function grab(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(SRC)
  if (!m) throw new Error(`源码里找不到 function ${name}(`)
  const i = m.index
  let depth = 0, j = SRC.indexOf('(', i)
  for (; j < SRC.length; j++) {
    if (SRC[j] === '(') depth++
    else if (SRC[j] === ')') { depth--; if (depth === 0) break }
  }
  assert.equal(SRC[j], ')', `${name} 的参数表没配平`)
  // 参数表收尾到函数体之间只该是空白，中间还有东西就说明抠错了地方
  const bodyStart = /^\s*$/.test(SRC.slice(j + 1, SRC.indexOf('{', j)))
    ? SRC.indexOf('{', j) : -1
  assert.ok(bodyStart !== -1, `${name} 的参数表与函数体之间还有别的内容，抠法不对`)
  depth = 0
  for (j = bodyStart; j < SRC.length; j++) {
    if (SRC[j] === '{') depth++
    else if (SRC[j] === '}') { depth--; if (depth === 0) break }
  }
  return SRC.slice(i, j + 1)
}

/** 用真实源码搭一套 save()，状态与依赖全由外面喂进去。 */
function saveFromSource(over = {}) {
  const src = grab('save')
  assert.equal(src.match(/^async\s+function\s+(\w+)\s*\(/)[1], 'save',
    '抠出来的是别的函数，本测试会空转')
  const said = []            // 弹过的话，按顺序记下来
  const pushed = []          // 跳过的地址
  const opened = []          // 通知标签栏的调用
  const ctx = {
    readonly: { value: false },
    posted: { value: false },
    locked: { value: false },
    balanced: { value: true },
    saving: { value: false },
    failedSig: { value: '' },
    lastSaved: { value: 'sig' },
    savedAt: { value: '' },
    voucherId: { value: 1 },        // 有 id = 改老凭证（不是新建那条分支）
    insertCtx: { value: null },
    formBookId: { value: 3 },
    voucherPeriod: { value: null },
    voucherPeriodId: { value: 0 },
    entrySig: { value: 'sig' },
    newNonLeafRows: { value: [] },
    form: { entries: [
      { subject_id: 1, direction: 'DEBIT', amount: '100.00', summary: '' },
      { subject_id: 2, direction: 'CREDIT', amount: '100.00', summary: '' }
    ] },
    store: { canWrite: true, bookId: 3, periods: [] },
    // 金额定点化在 voucher_balance.test.mjs 里有自己的口径；这里只关心 save() 的控制流，
    // 只需要 round-trip 成立就够，不重复测一遍金额本身。
    num: (s) => Number(s),
    centsOf: (s) => Math.round(Number(s) * 100),
    centsText: (c) => (c / 100).toFixed(2),
    rowProblemText: (r) => `非末级科目 ${r.subject_id}`,
    // 新建凭证的幂等键（审计 §3.28）：新建才带，更新不带。
    createKey: { value: '' },
    // 乐观锁令牌与冲突标志（审计 §3.15 的 409 契约）——
    // save() 会读 versionConflict、往请求体里放 voucherVersion。
    voucherVersion: { value: '' },
    versionConflict: { value: false },
    newIdempotencyKey: () => 'test-idem-key',
    api: { updateVoucher: async () => ({ id: 7, period_id: 5 }),
           createVoucher: async () => ({ id: 7, period_id: 5 }) },
    history: { state: null, replaceState() {} },
    tabs: { open: (r) => opened.push(r) },
    loadNeighbour: async () => {},
    ElMessage: {
      warning: (m) => said.push(['warning', m]),
      error: (m) => said.push(['error', m]),
      success: (m) => said.push(['success', m])
    },
    router: { push: (p) => pushed.push(p) },
    ...over
  }
  // save() 取的是「能落库的那一份分录」（与页面上算平的那一份是**同一份**，
  // 见 VoucherEdit.vue 的 payableEntries）。ctx.form 是普通对象不是 reactive，
  // 所以这里喂两个等价的 { value: [...] }。
  //
  // 判据用 `centsOf`（整数分）而不是 `num` —— 与真正发出去的那个金额
  // `centsText(centsOf(...))` 同一个算法（审计 §3.23）。
  ctx.payableEntries = {
    value: ctx.form.entries.filter((e) => e.subject_id && ctx.centsOf(e.amount) !== 0)
  }
  ctx.rowsMissingSubject = {
    value: ctx.form.entries.filter((e) => !e.subject_id && ctx.centsOf(e.amount) !== 0)
  }
  // savingPromise 在组件里是模块级 let，save() 会写它 —— 作用域里得先有
  const factory = new Function(...Object.keys(ctx),
    `let savingPromise = null
     ${src}
     return save`)
  const save = factory(...Object.values(ctx))
  return { save, said, pushed, opened, ctx }
}

/** 源码里**真正的代码行**（注释里提到 save() 不算数 —— 注释正是在解释为什么删）。 */
function codeLines() {
  return SRC.split('\n')
    .map((l, i) => [i + 1, l])
    .filter(([, l]) => !/^\s*(?:\/\/|\*|\/\*)/.test(l))
}

console.log('== P3-9 死代码：`quiet` 那条走不到的路已经删掉了 ==')

t('源码里已经没有 quiet 这个名字了（形参、条件、调用点都得没有）', () => {
  // 留着它就等于还留着一条「看起来手动保存还在」的路
  const hits = codeLines().filter(([, l]) => l.includes('quiet'))
  assert.deepEqual(hits, [],
    `第 ${hits.map(([n]) => n).join('、')} 行还有代码在用 quiet：\n${hits.map(([n, l]) => `${n}: ${l}`).join('\n')}`)
})

t('save() 不再带任何形参（手动保存那条路的入口整个去掉了）', () => {
  // **这一条是有意守文本的**，不是权宜之计：
  // 它守的是「save() 不长形参」这个**产品决定** —— 一旦 save() 又长出形参，
  // 说明有人把「手动保存」那条老路接了回来，而那条路与「自动保存 + 离开前 flush」
  // 的语义是打架的（见文件头）。这种决定守不出行为，只能守住字面。
  // 至于 save() 的**行为**，由下面那几条用例真跑一遍源码来验。
  assert.match(grab('save'), /^async function save\(\)/,
    'save() 又长出形参了 —— 真加了手动保存按钮的话，这里该配一条说清楚的话的路')
})

t('没有调用点还在往 save 里传对象（连传空对象都不传）', () => {
  const calls = codeLines()
    .filter(([, l]) => /(?<![.\w])save\s*\(/.test(l) && !/function\s+save/.test(l))
    .map(([, l]) => l.trim())
  // `return await save()` —— **await 不能省**：save() 是 async，
  // 少一个 await 就是拿 Promise 对象跟 true 比，恒为假（审查 P1-1）。
  // flushIfDirty 的行为由 tests/test_reviewfix_voucher_flush.test.mjs 真跑一遍验。
  assert.deepEqual(calls, ['await save()', 'return await save()'],
    `调用点变了，逐条复核一下：\n${calls.join('\n')}`)
})

t('模板里没有保存控件（这条就是「手动保存那条路走不到」的根据）', () => {
  const tpl = SRC.slice(0, SRC.indexOf('</template>'))
  assert.equal(/(?:@click|@keydown)\s*=\s*["'][^"']*\bsave\b/.test(tpl), false,
    '模板里出现了一个调 save() 的控件 —— 那条 !quiet 分支就不是死代码了，得连提示一起恢复')
  assert.equal(/>\s*保存\s*</.test(tpl), false, '模板里出现了「保存」按钮')
})

console.log('')
console.log('== 删掉恒假分支之后：失败一定要说（那正是 quiet 挡掉的东西）==')

t('只读账套：说得清为什么存不进去，且不误报成「已保存」', async () => {
  const s = saveFromSource({ readonly: { value: true }, store: { canWrite: false, bookId: 3, periods: [] } })
  assert.equal(await s.save(), false, '只读账套竟然存成了')
  assert.deepEqual(s.said, [['warning', '这个账套是只读的，不能修改凭证']],
    '只读时一声不吭 —— 自动保存失败而页面不吭声，人是会信的')
  assert.deepEqual(s.pushed, [], '存不进去还跳页面')
})

t('空表 / 借贷不平 / 非末级科目：三条拦路都出声，不是一声不吭地 return', async () => {
  const empty = saveFromSource({ form: { entries: [] } })
  assert.equal(await empty.save(), false)
  assert.deepEqual(empty.said, [['warning', '请至少填写一条分录']])

  const unbal = saveFromSource({ balanced: { value: false } })
  assert.equal(await unbal.save(), false)
  assert.deepEqual(unbal.said, [['error', '借贷不平衡，无法保存']])

  const leaf = saveFromSource({ newNonLeafRows: { value: [{ subject_id: 99 }] } })
  assert.equal(await leaf.save(), false)
  assert.deepEqual(leaf.said, [['error', '非末级科目 99']])
})

t('存成：不再弹「已保存」、不再把人踢回列表（人得留在这一张上接着录）', async () => {
  const s = saveFromSource()
  assert.equal(await s.save(), true, '一张平凭证没存成')
  assert.deepEqual(s.said, [], `存成了却弹了东西：${JSON.stringify(s.said)}`)
  // 这条是被删掉的那段死代码的原样行为；一旦有人「顺手」改成无条件跳回列表，这里就红
  assert.deepEqual(s.pushed, [], `自动保存完把人踢回了 ${s.pushed.join(',')} —— 正是删掉的那条`)
})

t('老凭证走 update，不重发号（删死代码没碰发号那一段）', async () => {
  const calls = []
  const s = saveFromSource({
    voucherId: { value: 7 },
    api: { updateVoucher: async (b, id) => { calls.push(['update', b, id]); return { id, period_id: 5 } } },
  })
  assert.equal(await s.save(), true)
  assert.deepEqual(calls, [['update', 3, 7]])
  assert.deepEqual(s.opened, [], '改老凭证不该再动标签地址（只有新建那张要换地址）')
})

t('**新建**凭证仍要把标签地址换成真 id（这是活路径，删死代码不许把它删掉）', async () => {
  const s = saveFromSource({ voucherId: { value: 0 } })
  assert.equal(await s.save(), true, '新凭证没存成')
  assert.deepEqual(s.opened, [{ name: 'voucher-new', path: '/vouchers/7' }],
    '新建后没有对齐标签地址 —— 关掉标签再点回来会跳到另一张空凭证')
})

t('存失败：记下这一版、不重复报错（错误已经由拦截器弹过了）', async () => {
  const s = saveFromSource({
    api: { updateVoucher: async () => { throw new Error('500') } }
  })
  assert.equal(await s.save(), false, '存失败了却报成功 —— 离开页面前那趟 flush 就靠它放人走')
  assert.deepEqual(s.said, [], '不该在这里重复报一遍，拦截器已经弹过了')
  assert.equal(s.ctx.failedSig.value, 'sig', '失败的这一版没记下来，同一个签名会被反复重试')
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
