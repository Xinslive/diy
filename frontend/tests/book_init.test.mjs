/**
 * 账套初始化两步向导的判定与文案（src/utils/bookInit.js，**线上那份源码**）。
 *
 *  这里测的是「什么时候允许进第 2 步」这一组判定 —— 它决定了用户能不能在
 *  科目还没定下来的时候录期初（录了也无处可挂，后端会 409）。
 *  判定散在向导和期初页两处各写一遍的话，迟早一边改了另一边没改，
 *  于是出现「向导说能做、期初页说不能进」。抽到这里就是为了让两边只有一份真相。
 *
 *  后半段是**源码级断言**：单测只能证明这份 utils 是对的，证明不了组件
 *  真的用了它 —— 组件里要是又写死了一套 `init.step === 1`，单测照样全绿。
 *  这条思路照抄 tests/opening_balance.test.mjs。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const {
  FOOTER_DONE, FOOTER_LABEL, FOOTER_NEXT, FOOTER_OPENINGS, STEP2_LOCK_HINT,
  balanceDiff, canEnterStep2, canImportSubjects, canKeepChart,
  formatInitTime, hasInit, importSummaryText, initFooterAction, initOpeningLabel,
  mismatchHint,
  initStep, initStepsActive, initSubjectLabel, initSummary,
  OPENINGS_SOURCE_LABEL, STEP_DONE, STEP_OPENINGS
} = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/bookInit.js'))

const read = (p) => fs.readFileSync(new URL(p, import.meta.url), 'utf8')
const WIZARD = read('../src/components/BookInitDialog.vue')
const OPENINGS = read('../src/views/book/OpeningBalance.vue')
const BOOKS = read('../src/views/book/BookManager.vue')
const IMPORT = read('../src/components/OpeningImportDialog.vue')
const API = read('../src/api/book.js')
const SESSION = read('../src/stores/session.js')
const SUBJECT_IMPORT = read('../src/components/SubjectImportDialog.vue')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

/** 一份「两步都没做」的 init，与后端 GET /books/:id/init 的字段一致 */
const fresh = (patch = {}) => ({
  book_id: 1, subjects_ready: false, subjects_source: null, subjects_at: null,
  subject_count: 0, openings_ready: false, openings_at: null, opening_count: 0,
  step: 1, can_keep_chart: true, can_import_subjects: true, ...patch
})
/** 第 1 步做完了、第 2 步没做 */
const subjectsDone = (patch = {}) => ({
  ...fresh(), subjects_ready: true, subjects_source: 'CHART', subject_count: 168,
  step: 2, ...patch
})
const allDone = (patch = {}) => ({
  ...subjectsDone(), openings_ready: true, opening_count: 42,
  openings_at: '2026-10-15T09:00:00+00:00', step: 0, ...patch
})

console.log('== initStep：该做第几步 ==')

t('什么都没做 → 第 1 步', () => {
  assert.equal(initStep(fresh()), 1)
})
t('科目做完了、期初没做 → 第 2 步', () => {
  assert.equal(initStep(subjectsDone()), 2)
})
t('两步都做完 → 0', () => {
  assert.equal(initStep(allDone()), 0)
})
t('init 整个是空的 → 按第 1 步（fail-closed）', () => {
  // 认不得就当没做完：按钮亮着然后被后端 409 拒掉，比按钮灰着更让人困惑
  for (const bad of [null, undefined, {}, 0, '']) {
    assert.equal(initStep(bad), 1, String(bad))
  }
})
t('后端那个派生的 step 字段不作数：按两个布尔重算', () => {
  // step 与 subjects_ready 打架时以布尔为准 —— 那两个布尔才是真相，step 只是缓存
  assert.equal(initStep(subjectsDone({ step: 1 })), 2, '被后端那个 step 带偏了')
  assert.equal(initStep(allDone({ step: 2 })), 0)
})

console.log('')
console.log('== 第 2 步的锁 ==')

t('第 1 步没完成：第 2 步进不去', () => {
  assert.equal(canEnterStep2(fresh()), false)
  assert.equal(canEnterStep2(null), false)
})
t('第 1 步完成后第 2 步才开', () => {
  assert.equal(canEnterStep2(subjectsDone()), true)
})
t('锁着时那句话自带出路（不是只说「第 2 步未完成」）', () => {
  assert.ok(/第 1 步/.test(STEP2_LOCK_HINT))
  assert.ok(/科目/.test(STEP2_LOCK_HINT), '没告诉用户该做什么，用户只看到一个灰按钮')
})

console.log('')
console.log('== 按钮的 disabled 口径 ==')

t('沿用标准科目表：认后端的 can_keep_chart', () => {
  assert.equal(canKeepChart(subjectsDone({ can_keep_chart: true })), true)
  assert.equal(canKeepChart(subjectsDone({ can_keep_chart: false })), false)
})
t('账上根本没有科目时不能沿用（点了就是塞一份没人要的科目）', () => {
  assert.equal(canKeepChart(fresh({ can_keep_chart: false })), false)
})
t('can_keep_chart 缺失 → 灰着，不放行', () => {
  assert.equal(canKeepChart({}), false)
  assert.equal(canKeepChart(null), false)
})
t('覆盖导入看 can_import_subjects（有凭证/有期初的账套后端会拒）', () => {
  assert.equal(canImportSubjects(fresh({ can_import_subjects: true })), true)
  assert.equal(canImportSubjects(subjectsDone({ can_import_subjects: false })), false)
})

console.log('')
console.log('== 文案 ==')

t('科目来源两种取值各有名字', () => {
  assert.match(initSubjectLabel(subjectsDone({ subjects_source: 'IMPORT' })), /用友科目表/)
  assert.match(initSubjectLabel(subjectsDone({ subjects_source: 'CHART' })), /系统标准科目表/)
})
t('时间戳按本地时区渲染，不用 toISOString（那会差 8 小时）', () => {
  // 08:32 UTC 在 UTC+8 是 16:32。断言它**不含** 08 —— 差 8 小时的时间戳
  // 只会让人以为是系统坏了
  const label = initSubjectLabel(subjectsDone({ subjects_at: '2026-10-15T08:32:00+00:00' }))
  assert.match(label, /^科目已确定（来源：系统标准科目表，时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}）$/)
})
t('没做完时那一格返回空串（由组件自己渲染「待完成」，别长出第二套文案）', () => {
  assert.equal(initSubjectLabel(fresh()), '')
  assert.equal(initOpeningLabel(fresh()), '')
})
t('期初完成态带条数', () => {
  assert.match(initOpeningLabel(allDone()), /^期初余额已导入（42 条，时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}）$/)
})
t('认不出的时间戳返回空串，不冒出 Invalid Date', () => {
  assert.equal(formatInitTime('不是时间'), '')
  assert.equal(formatInitTime(null), '')
  assert.equal(formatInitTime(''), '')
})
t('副标题说清还差第几步', () => {
  assert.match(initSummary(fresh()), /第 1 步/)
  assert.match(initSummary(subjectsDone()), /第 2 步/)
  assert.match(initSummary(allDone()), /已完成/)
})
t('导入统计：读入 / 没匹配上的 / 跳过的都要说出来', () => {
  const txt = importSummaryText({ matched: 42, unmatched: 3, skipped: 499 })
  assert.match(txt, /已读入 42 条/)
  assert.match(txt, /3 条科目在本账套不存在/, '没匹配上的那部分原来一声不吭 —— 恰恰是丢东西的那部分')
  assert.match(txt, /跳过 499 行/)
})
t('没有 unmatched/skipped 时那句话保持干净（不硬凑两个 0）', () => {
  assert.equal(importSummaryText({ matched: 42 }), '已读入 42 条')
})
t('导入结果整个缺失也不炸（老后端没这几个计数）', () => {
  assert.equal(importSummaryText(null), '已读入 0 条')
  assert.equal(balanceDiff(null), 0)
})

console.log('')
console.log('== hasInit：区分「不知道」和「知道了还没做」 ==')

t('没有 init 时 hasInit 为假 —— 不该替后端下结论挂拦截条', () => {
  assert.equal(hasInit(null), false)
  assert.equal(hasInit(undefined), false)
})
t('有 init 时为真（哪怕内容全是 false）', () => {
  assert.equal(hasInit(fresh()), true)
})

console.log('')
console.log('== 底部主按钮：做完就换词，不能留一颗点不动的「下一步」 ==')

t('两步都做完了 → 「完成」，不再是「下一步」', () => {
  // 这就是那个 bug：主按钮写死成「下一步」，初始化早就结束了它还在，
  // 用户只能理解成「还有一步没走」，而它按下去什么也不发生
  assert.equal(initFooterAction(allDone()), FOOTER_DONE)
  assert.equal(FOOTER_LABEL[FOOTER_DONE], '完成')
})
t('「声明无期初」也算做完 → 同样是「完成」', () => {
  const none = allDone({ openings_source: 'NONE', opening_count: 0 })
  assert.equal(initFooterAction(none), FOOTER_DONE)
})
t('只剩第 2 步 → 按钮直接指向那件事，不叫「下一步」', () => {
  assert.equal(initFooterAction(subjectsDone()), FOOTER_OPENINGS)
  assert.equal(FOOTER_LABEL[FOOTER_OPENINGS], '去导入期初余额')
})
t('第 1 步都没做 → 维持「下一步」（且由组件置灰）', () => {
  assert.equal(initFooterAction(fresh()), FOOTER_NEXT)
  assert.equal(FOOTER_LABEL[FOOTER_NEXT], '下一步')
})
t('没有 init 时按 fail-closed 走「下一步」', () => {
  assert.equal(initFooterAction(null), FOOTER_NEXT)
})
t('每种判定都有对应的中文词 —— 少了就把那颗按钮渲染成空白', () => {
  for (const a of [FOOTER_DONE, FOOTER_OPENINGS, FOOTER_NEXT]) {
    assert.ok(FOOTER_LABEL[a], `${a} 没有对应的按钮文案`)
  }
})

console.log('')
console.log('== 步骤条高亮：按已完成步数算，不按组件里的本地 step ==')

t('完成 0 / 1 / 2 步分别高亮 0 / 1 / 2', () => {
  assert.equal(initStepsActive(fresh()), 0)
  assert.equal(initStepsActive(subjectsDone()), 1)
  assert.equal(initStepsActive(allDone()), 2, '两步都做完了步骤条还停在第 1 步')
})
t('步骤条不采信后端那个派生的 step 字段', () => {
  assert.equal(initStepsActive(allDone({ step: 1 })), 2)
  assert.equal(initStepsActive(fresh({ step: 2 })), 0)
})

console.log('')
console.log('== 组件确实用了这一份判定（源码级断言） ==')

t('向导、期初页、账套卡片都从 utils/bookInit 取判定，没有各写一套', () => {
  for (const [name, src] of [['BookInitDialog.vue', WIZARD], ['OpeningBalance.vue', OPENINGS], ['BookManager.vue', BOOKS]]) {
    assert.ok(/from '@\/utils\/bookInit'/.test(src), `${name} 没有引 utils/bookInit`)
  }
})
t('两个组件都不许出现自造的 step 判定', () => {
  // init.step === 1 这类判断写进组件，就是又开了一份真相
  for (const [name, src] of [['BookInitDialog.vue', WIZARD], ['OpeningBalance.vue', OPENINGS], ['BookManager.vue', BOOKS]]) {
    assert.ok(!/init\.step\s*===/.test(src) && !/init\.step\s*!==/.test(src),
      `${name} 又按后端那个 step 字段判定了`)
  }
})
t('向导有第 2 步的锁态（置灰 + 锁图标 + 一句说明）', () => {
  assert.ok(/is-locked/.test(WIZARD), '锁态没有视觉表现')
  assert.ok(/<Lock \/>/.test(WIZARD), '锁态没有锁图标')
  assert.ok(/STEP2_LOCK_HINT/.test(WIZARD), '锁着时没有那句话')
})
t('向导不许再留一个本地 step —— 那份状态跟 init 对不上就是那颗死按钮', () => {
  assert.ok(!/const\s+step\s*=\s*ref/.test(WIZARD), '又写了一个本地 step')
  assert.ok(!/step\.value\s*=/.test(WIZARD), '又在手动改 step')
  assert.ok(/initStepsActive\(/.test(WIZARD), '步骤条高亮没有走 utils')
})
t('底部主按钮的词来自 utils，不是模板里写死的「下一步」', () => {
  assert.ok(/footerLabel/.test(WIZARD), '按钮文案没有走 utils')
  assert.ok(/initFooterAction\(/.test(WIZARD), '按钮行为没有走 utils')
  // 模板里除了「下一步」那句注释，不该再有写死的下一步
  const tpl = WIZARD.split('</template>')[0]
  assert.ok(!/>下一步</.test(tpl), '模板里还写死了「下一步」')
})
t('第 2 步在**别的页面**做完，状态得跟着回到账套管理', () => {
  // 向导把人送去期初页导入，保存发生在那边。保存接口已经把最新的 init
  // 带回来了（后端 keep/skip/导入科目三个口子都带），期初页必须写回 store ——
  // 不写的话用户回到账套管理，向导和卡片都还在说「还没做」，
  // 而那一步他刚刚明明做完了。
  assert.match(OPENINGS, /setOpenings[\s\S]{0,400}?markInit\(/,
    '保存期初后没有把 init 写回 store')
})
t('打开向导前先核一遍这份账的状态，不拿卡片上那份就开', () => {
  // 卡片上的 init 来自列表接口，可能停在用户上次进这个页面的那一刻
  assert.match(BOOKS, /openInit[\s\S]{0,300}?loadInit\(/,
    'openInit 没有先刷新就弹向导')
})
t('科目表对不上时要说出「多半是第 1 步选错了」', () => {
  // 用友余额表 + 系统标准科目表：只读进个位数、跳过两百条，表格平的但合计差几百万，
  // 于是保存被拒、初始化第 2 步永远不完成。现象横跨三个页面，
  // 不点破的话用户只能看到「导入成功了，初始化就是不完成」。
  const bad = { matched: 6, unmatched: 207, skipped: 333 }
  const hint = mismatchHint(bad)
  assert.ok(hint, '对不上时一句话都不给')
  assert.match(hint, /207/, '没说出漏了多少')
  assert.match(hint, /科目表/, '没说清跟什么对不上')
})
t('少漏几个属正常，不该报警', () => {
  assert.equal(mismatchHint({ matched: 213, unmatched: 0 }), '')
  assert.equal(mismatchHint({ matched: 200, unmatched: 5 }), '', '漏五个是常态')
  assert.equal(mismatchHint(null), '')
})
t('卡片底部三颗控件共用一个尺寸来源（漏挂一颗就又歪了）', () => {
  // 这一排曾经三颗三种高度：两颗被 CSS 硬改成 30px，中间那颗忘了，还是 24px
  const row = BOOKS.split('book-tile-actions')[1].split('</div>')[0]
  const tagged = (row.match(/class="tile-action /g) || []).length
  assert.equal(tagged, 3, `这一排里只有 ${tagged} 颗挂了 tile-action`)
  assert.ok(!/size="small"/.test(row), '这一排里有 size="small"，高度跟同排的对不上')
  assert.match(BOOKS, /\.tile-action \{[^}]*height: 30px/, '共享的高度规则不见了')
  assert.ok(!/\.enter-book-button \{[^}]*height/.test(BOOKS), '又给单颗按钮单独写高度了')
  assert.ok(!/\.edit-book-button \{[^}]*height/.test(BOOKS), '又给单颗按钮单独写高度了')
})
t('向导的「去导入期初余额」用 query 交接，不自己在向导里导', () => {
  assert.match(WIZARD, /path: '\/openings', query: \{ import: '1' \}/)
})
t('期初页消费 ?import=1 之后把 query 清掉（否则刷新会再弹一次）', () => {
  assert.match(OPENINGS, /route\.query\.import === '1'/)
  assert.match(OPENINGS, /router\.replace\(\{ path: '\/openings' \}\)/,
    'query 没被清掉 —— 刷新一次弹一次')
})
t('期初页的拦截条同时挡导入和保存（只挡一边等于没挡）', () => {  const alerts = OPENINGS.match(/initBlocking/g) || []
  assert.ok(alerts.length >= 3, `拦截只用在 ${alerts.length} 处，导入/保存/拦截条三处都该用`)
})
t('期初页不再有内联 el-upload，导入走弹窗', () => {
  assert.ok(!/<el-upload/.test(OPENINGS), '期初页里那个内联 el-upload 应该换成弹窗')
  assert.ok(/<OpeningImportDialog/.test(OPENINGS))
  assert.ok(/<BookInitDialog/.test(OPENINGS), '拦截条上的「去完成第 1 步」没有去向')
})
t('导入弹窗收 .xls（旧的上传只收 .xlsx，用友导出的是 .xls）', () => {
  assert.match(IMPORT, /accept="\.xls,\.xlsx"/)
  // accept 只是给文件选择框的建议，拖进来的文件不经过它 —— 组件里要再拦一道。
  // 断言那一行本身（而不是整段的字面量）：后缀正则里的反斜杠一层层转义太容易写错，
  // 抄错一个字就变成一条永远为假的断言 —— 那比没有断言更坏。
  const guard = IMPORT.split('\n').find((l) => l.includes('.test(name)'))
  assert.ok(guard && /xlsx\?\$/.test(guard) && /\/i\b/.test(guard),
    '拖进来的文件没有后缀兜底')
})
t('期初/期末切换走的是重新解析，不是换个标签', () => {
  assert.ok(/has_closing/.test(IMPORT), '后端说有期末列却不给切换')
  assert.match(IMPORT, /api\.parseOpenings\(store\.bookId, fd, b\)/, 'basis 没传进解析请求')
  assert.match(IMPORT, /async function reparse\(b\)/, 'basis 变化没有触发重新解析')
})
t('确认导入这一条走 API 层，不在组件里手拼路径', () => {
  assert.match(API, /export const parseOpenings = \(id, fd, basis = 'opening'\)/)
  assert.match(API, /export const getInit = \(id\)/)
  assert.match(API, /export const keepChartSubjects = \(id\)/)
  assert.ok(!/openings\/parse/.test(WIZARD), '向导里又手拼了一次后端路径')
})
t('matched 为 0 时不许确认导入（没什么可导入的）', () => {
  assert.match(IMPORT, /canConfirm[\s\S]{0,120}matched\.value > 0/)
})
t('借贷不平放行但要二次确认，并写清保存时会被拒', () => {
  assert.match(IMPORT, /ElMessageBox\.confirm/)
  assert.match(IMPORT, /保存期初余额/, '二次确认没告诉用户保存会发生什么')
})
t('导入只填表、不落库：明说还得点保存', () => {
  assert.match(IMPORT, /emit\('imported'/, '没把结果交回给页面')
  assert.match(IMPORT, /保存期初余额/, '完成页没说明还没落库')
  assert.ok(!/setOpenings/.test(IMPORT), '导入弹窗里直接落库了 —— 那就跳过了人工复核')
})
t('导入计数进了页面提示（原来只有一句「已读入 N 条」）', () => {
  assert.match(OPENINGS, /importSummaryText\(res\)/)
})
t('账套卡片挂了「待初始化」标记和继续初始化的入口', () => {
  assert.match(BOOKS, /待初始化/)
  assert.match(BOOKS, /继续初始化/)
  assert.ok(!/<el-alert v-if="newBookId"/.test(BOOKS), '创建弹窗里那条小提示条还在')
})
t('建完账套直接弹向导，且 lastCreatedBookId 机制还在（重挂载会丢提示）', () => {
  assert.match(BOOKS, /initVisible\.value = true/)
  assert.match(BOOKS, /store\.setLastCreatedBook\(book\.id\)/)
  assert.match(BOOKS, /store\.lastCreatedBookId/)
})
t('store 登出必须连 initByBook 一起清', () => {
  // 切的是**定义**那一段（`clearLocal() {` … `async loadBooks()`）。
  // 切 `clearLocal()` 会先撞上 logout 里那句 this.clearLocal() 调用点，
  // 而 loadBooks() 更早出现在 login 里 —— 起止点一颠倒，切出来是空串，
  // 断言会一直「通过」而什么都没测到。
  const start = SESSION.indexOf('clearLocal() {')
  const end = SESSION.indexOf('async loadBooks()')
  assert.ok(start > 0 && end > start, '没定位到 clearLocal 的函数体')
  const clear = SESSION.slice(start, end)
  assert.ok(/initByBook = \{\}/.test(clear),
    '上一个用户的初始化状态漏给了下一个 —— 跟 books/periods 同一条理由')
})
t('markInit 顺手把账套行那一列也换了（否则卡片标记要等下一次 loadBooks）', () => {
  const m = SESSION.slice(SESSION.indexOf('markInit(bookId, init)'))
  assert.ok(/this\.books = this\.books\.map/.test(m), '只更新了 initByBook，卡片上的标记不跟着变')
})

// ==================================================================
// 「初始化作用在哪本账上」—— 踩过的坑，不钉住还会再踩
// ==================================================================
console.log('')
console.log('== 初始化必须作用在**被初始化的那本账**上 ==')

t('建完账套要把会话切到新账套（不切的话顶栏停在旧账，初始化表现为「怎么都做不完」）', () => {
  const body = BOOKS.slice(BOOKS.indexOf('async function submit()'))
  assert.ok(/store\.setBook\(book\.id\)/.test(body),
    'submit() 里没有 setBook(new.id)：向导开着、当前账套是旧的，'
    + '任何走「当前账套」的入口都会作用到旧账')
  // 切账套的时机要在向导弹出来之前，否则中间态仍停在旧账
  const setBook = body.indexOf('store.setBook(book.id)')
  const openWizard = body.indexOf('initVisible.value = true')
  assert.ok(setBook >= 0 && openWizard > setBook, '先弹向导再切账套，中间那一瞬仍是旧账')
})

t('科目表导入弹窗必须能指定目标账套（向导可以对任意一本账打开）', () => {
  assert.ok(/bookId:\s*\{\s*type:\s*\[Number, String\]/s.test(SUBJECT_IMPORT),
    'SubjectImportDialog 没有 bookId 入参')
  assert.ok(!/api\.previewSubjectImport\(store\.bookId/.test(SUBJECT_IMPORT),
    '预览还认当前账套：给别的账套做初始化时读的是这一本的科目表')
  assert.ok(!/api\.importSubjects\(store\.bookId/.test(SUBJECT_IMPORT),
    '确认导入还认当前账套：会把当前账套的科目表覆盖掉')
  assert.match(WIZARD, /<SubjectImportDialog[^>]*:book-id="targetId"/s,
    '向导没把目标账套透传给科目表导入弹窗')
})

t('向导自己的两个写接口都用目标账套，不用当前账套', () => {
  assert.match(WIZARD, /api\.keepChartSubjects\(targetId\.value\)/)
  assert.ok(!/keepChartSubjects\(store\.bookId/.test(WIZARD))
})

// ==================================================================
// 无期初余额：新公司第一年、上一年度本来就是空账
// ==================================================================
console.log('')
console.log('== 本期没有期初余额，也得能把初始化做完 ==')

t('第 2 步有第二条出口，不是只有「导入」', () => {
  assert.match(WIZARD, /本期无期初余额/)
  assert.match(WIZARD, /api\.skipOpenings\(targetId\.value\)/)
  assert.match(API, /skipOpenings/)
})

t('「确认没有期初」不能和「导入过」说成同一句话', () => {
  assert.match(initOpeningLabel({ openings_ready: true, openings_source: 'NONE' }),
    /无期初余额/)
  assert.match(initOpeningLabel({ openings_ready: true, openings_source: 'IMPORT', opening_count: 12 }),
    /12 条/)
  assert.equal(OPENINGS_SOURCE_LABEL.NONE, '本期无期初余额')
  // 两种来源下都带时间：事后要查「当时是不是本来就没有」
  assert.match(initOpeningLabel({ openings_ready: true, openings_source: 'NONE', openings_at: '2026-10-15T09:00:00+00:00' }),
    /时间/)
})

t('「没有期初」不能冒充「还没做」—— step 只看两个布尔', () => {
  const none = { subjects_ready: true, openings_ready: true, openings_source: 'NONE' }
  assert.equal(initStep(none), STEP_DONE, '声明无期初后应算两步都完成')
  assert.equal(initStep({ subjects_ready: true, openings_ready: false }), STEP_OPENINGS)
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
