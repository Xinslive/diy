/**
 * 年度作用域：请求带的年度必须**真的传到后端**（2026-10-31 生产实测后补）。
 *
 * **生产上发生了什么。**
 * 账套「泉州劲松珠宝有限公司」有 2023~2026 **四张**科目表（科目表每个会计
 * 年度各有一份，同一个编码在不同年度是两行不同的记录、两个 id）。
 * 用户在 **2023 年**那张表上给 1123 建下级，接口回一句「上级科目不存在」——
 * 父科目明明就摆在屏幕上。
 *
 * 根因是**年度没传到后端**，两条路各有一种丢法：
 *
 *  1. ``POST /books/{id}/subjects`` 从**查询参数**读年度，而前端把年度放在
 *     **请求体**里 —— FastAPI 不会把请求体的字段当查询参数读，年度被**静默丢掉**，
 *     后端退回「账套当前在记的那一年」（2026），拿 2026 年的表去认 2023 年的
 *     父科目 → 「上级科目不存在」。
 *  2. ``POST /books/{id}/openings/parse`` 压根**没带年度** → 同样按 2026 年认科目
 *     → 解析出的每一行都指着 2026 年的科目 id → 前端往 2023 年的表格里填，
 *     一个都对不上 → **表格全是 0** → 刷新清空 → 保存把 0 存进去
 *     （后端 ``set_openings`` 的语义是「整年替换」，于是这一年的期初快照
 *     **被清空**）→ 死循环，钱一分没进去。
 *
 * 这一组断言钉的是**契约**：年度走查询参数、导入结果要核对年度、
 * 空表保存前要先把「会抹掉多少」说清楚。
 *
 * 后半段是**源码级断言**（照抄 tests/opening_balance.test.mjs 的思路）：
 * 单测只能证明纯函数是对的，证明不了组件真的接上了 —— 组件里又手拼一遍
 * 路径的话，单测照样全绿。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const { importScopeMismatch } = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/openingImport.js'))

const read = (p) => fs.readFileSync(new URL(p, import.meta.url), 'utf8')
const SUBJECT_API = read('../src/api/subject.js')
const BOOK_API = read('../src/api/book.js')
const FORM = read('../src/components/SubjectFormDialog.vue')
const IMPORT = read('../src/components/OpeningImportDialog.vue')
const OPENINGS = read('../src/views/book/OpeningBalance.vue')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 年度要进查询参数（接口从查询参数读，不从请求体读） ==')

t('新建科目：年度进查询参数', () => {
  assert.match(SUBJECT_API, /export const createSubject = \(bookId, payload\)/)
  assert.match(SUBJECT_API, /params: payload\?\.year \? \{ year: payload\.year \} : \{\}/,
    '年度没进查询参数 —— 后端会退回「账套当前在记的那一年」，认到另一张科目表')
})
t('新建科目：请求体里那份年度也留着（两处都收，查询参数优先）', () => {
  assert.match(FORM, /year: targetYear\.value/, '表单没把年度带进请求')
})

t('解析期初：年度进查询参数', () => {
  assert.match(BOOK_API, /export const parseOpenings = \(id, fd, basis = 'opening', year\)/)
  assert.match(BOOK_API, /params: \{ basis, \.\.\.\(year \? \{ year \} : \{\}\) \}/,
    '年度没进查询参数 —— 解析会认到另一年的科目，填进去一个都对不上')
})
t('解析期初：弹窗把当前年度带上去', () => {
  assert.match(IMPORT, /api\.parseOpenings\(store\.bookId, fd, b, store\.currentYear\)/)
})

console.log('== 导入结果必须与页面对得上，否则当场拒绝（不许静默填错） ==')

t('年度对不上：拒绝，并说清是哪一年', () => {
  const r = importScopeMismatch({
    parsedYear: 2026, pageYear: 2023, parsedIds: [1, 2], onScreenIds: [9, 10],
  })
  assert.ok(r, '年度对不上却放行了 —— 表格会一直是 0，而人以为导入没生效')
  assert.equal(r.reason, 'year')
  assert.match(r.text, /2026/)
  assert.match(r.text, /2023/)
  assert.match(r.text, /拒绝填入/)
})
t('年度对得上但科目 id 全对不上：也要拒绝', () => {
  const r = importScopeMismatch({
    parsedYear: 2023, pageYear: 2023, parsedIds: [1, 2], onScreenIds: [9, 10],
  })
  assert.ok(r, '一个科目都对不上却放行了')
  assert.equal(r.reason, 'ids')
})
t('年度与科目都对得上：放行', () => {
  const r = importScopeMismatch({
    parsedYear: 2023, pageYear: 2023, parsedIds: [9, 10], onScreenIds: [9, 10],
  })
  assert.equal(r, null)
})
t('只错一部分：先放过（那些行由导入预览自己报，不连坐）', () => {
  const r = importScopeMismatch({
    parsedYear: 2023, pageYear: 2023, parsedIds: [9, 999], onScreenIds: [9, 10],
  })
  assert.equal(r, null)
})
t('什么也没解析出来：不是「对不上」，是「没东西可填」', () => {
  assert.equal(importScopeMismatch({
    parsedYear: 2023, pageYear: 2023, parsedIds: [], onScreenIds: [9],
  }), null)
})
t('页面接上了这个判定', () => {
  assert.match(OPENINGS, /importScopeMismatch\(/, '页面没调用作用域判定')
  assert.match(OPENINGS, /importScopeMismatch[\s\S]{0,400}ElMessage\.error\(mismatch\.text\)/,
    '判定不通过时没有当场拒绝')
})

console.log('== 导入的数只活在表格里：刷新/离开前要问一句 ==')

t('刷新与离开前都要拦（导入的数没落库）', () => {
  assert.match(OPENINGS, /window\.addEventListener\('beforeunload', onBeforeUnload\)/,
    '刷新前没有拦 —— 导入进来的数一刷新就没')
  assert.match(OPENINGS, /onBeforeRouteLeave\(/, '页内换路由没有拦')
})
t('换账套 / 换年度丢掉没保存的数时要说一声', () => {
  assert.match(OPENINGS, /function warnDiscarded\(\)/, '换账套/换年度会悄悄丢掉没保存的期初')
  assert.match(OPENINGS, /watch\(\(\) => store\.bookId, \(\) => \{ warnDiscarded\(\); load\(\) \}\)/)
  assert.match(OPENINGS, /watch\(\(\) => store\.currentYear, \(\) => \{ warnDiscarded\(\); load\(\) \}\)/)
})
t('「有东西要存」算的是真要存的那部分', () => {
  assert.match(OPENINGS, /function hasUnsavedChanges\(\)/)
  assert.match(OPENINGS, /filledCount\.value > 0 \|\| storedCount\.value > 0/,
    '没改金额却弹提示 —— 提示了也是白提示')
})

console.log('== 保存是整年替换：空表要先把「会抹掉多少」说清楚 ==')

t('空表保存前先说清楚会清空多少条', () => {
  assert.match(OPENINGS, /const storedCount = computed\(/, '没算库里已存着多少条')
  assert.match(OPENINGS, /!rows\.length && storedCount\.value > 0/,
    '空表保存没有拦 —— 会把这一年的期初快照全删掉')
  assert.match(OPENINGS, /全部清空/, '拦下来了但没说清后果')
})
t('「库里已存着多少」取的是服务端那份数，不是编辑中的值', () => {
  assert.match(OPENINGS, /data\.value\.subjects\.filter\(\s*\(s\) => s\.is_leaf && moneyCents\(s\.signed\)/,
    'storedCount 取的不是服务端给的 signed（导入会改 amounts，不能拿它算）')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
