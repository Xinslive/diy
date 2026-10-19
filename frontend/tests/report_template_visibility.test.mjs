/**
 * 报表模板列表：自有模板一到，同类内置模板让位（src/utils/reportTemplates.js，
 * **线上那份源码**）。
 *
 *  这个特性最怕的是「藏多了」和「藏少了」两头：
 *   · 藏多了 —— 用户只想换掉资产负债表，却把利润表也弄没了；
 *   · 藏少了 —— 内置模板照样列出来，用户打开的还是那份通用口径。
 * 另外还有一处容易漏的：报表页靠列表反查当前模板（名字、是否内置），
 * 正在看的那张被藏掉就会查不到，所以单开一条 keepId。
 *
 * 最后一组是源码级断言，盯住「列表页与报表页都真的走了这个过滤」。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const { visibleTemplates } = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/reportTemplates.js'))

const LIST = fs.readFileSync(new URL('../src/views/report/ReportList.vue', import.meta.url), 'utf8')
const VIEW = fs.readFileSync(new URL('../src/views/report/ReportView.vue', import.meta.url), 'utf8')
const API = fs.readFileSync(new URL('../src/api/report.js', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

const names = list => list.map(x => x.name)

/** 内置的两张（seed 里就这两张：资产负债表、利润表）+ 一张用户自建的。 */
const BUILTIN_BS = { id: 1, name: '资产负债表', type: 'BALANCE_SHEET', is_builtin: true }
const BUILTIN_IS = { id: 2, name: '利润表', type: 'INCOME_SHEET', is_builtin: true }
const IMPORTED = (id, name, type) => ({ id, name, type, is_builtin: false, code: `IMPORTED_${id}` })
const CUSTOM = (id, name) => ({ id, name, type: 'CUSTOM', is_builtin: false })

console.log('== 可见性：自有模板一到，同类内置让位 ==')

t('没导任何模板时两张内置都在（空账套也得有表可看）', () => {
  assert.deepEqual(names(visibleTemplates([BUILTIN_BS, BUILTIN_IS])),
    ['资产负债表', '利润表'])
})

t('导了资产负债表 → 只藏内置资产负债表，利润表原样留着', () => {
  const out = visibleTemplates([BUILTIN_BS, BUILTIN_IS, IMPORTED(9, '星悦荟2026资产负债表', 'BALANCE_SHEET')])
  assert.deepEqual(names(out), ['利润表', '星悦荟2026资产负债表'])
})

t('两类都导了 → 两张内置都藏', () => {
  const out = visibleTemplates([
    BUILTIN_BS, BUILTIN_IS,
    IMPORTED(9, '星悦荟2026资产负债表', 'BALANCE_SHEET'),
    IMPORTED(10, '星悦荟2026利润表', 'INCOME_SHEET'),
  ])
  assert.deepEqual(names(out), ['星悦荟2026资产负债表', '星悦荟2026利润表'])
})

t('自定义报表不误伤内置（内置没有 CUSTOM，不能顺手把别的藏了）', () => {
  const out = visibleTemplates([BUILTIN_BS, BUILTIN_IS, CUSTOM(11, '门店流水')])
  assert.deepEqual(names(out), ['资产负债表', '利润表', '门店流水'])
})

t('同一类型导了多张 → 一张都不藏，内置照样让位', () => {
  const out = visibleTemplates([
    BUILTIN_BS,
    IMPORTED(9, '星悦荟2026资产负债表', 'BALANCE_SHEET'),
    IMPORTED(12, '集团2026资产负债表', 'BALANCE_SHEET'),
  ])
  assert.deepEqual(names(out), ['星悦荟2026资产负债表', '集团2026资产负债表'])
})

t('删掉最后一张自有模板 → 内置模板自动回来', () => {
  const list = [BUILTIN_BS, BUILTIN_IS, IMPORTED(9, '星悦荟2026资产负债表', 'BALANCE_SHEET')]
  const after = list.filter(x => x.id !== 9)
  assert.deepEqual(names(visibleTemplates(after)), ['资产负债表', '利润表'])
})

t('顺序只筛不排：导入的模板留在原来的位置', () => {
  const out = visibleTemplates([BUILTIN_IS, BUILTIN_BS, IMPORTED(9, '自建BS', 'BALANCE_SHEET')])
  assert.deepEqual(out.map(x => x.id), [2, 9])
})

console.log('== keepId：正在看的那张不能从下拉里消失 ==')

t('keepId 指向已隐藏的内置模板时，它仍留在结果里', () => {
  const out = visibleTemplates(
    [BUILTIN_BS, BUILTIN_IS, IMPORTED(9, '星悦荟2026资产负债表', 'BALANCE_SHEET')], 1)
  assert.deepEqual(out.map(x => x.id), [1, 2, 9])
})

t('keepId 只保自己，不把别的内置也一起放出来', () => {
  const out = visibleTemplates(
    [BUILTIN_BS, BUILTIN_IS, IMPORTED(9, '自建BS', 'BALANCE_SHEET')], 2)
  assert.deepEqual(out.map(x => x.id), [2, 9])
})

t('后端返回空列表 / 非数组时不炸（首屏还没加载完也是这个值）', () => {
  assert.deepEqual(visibleTemplates([]), [])
  assert.deepEqual(visibleTemplates(null), [])
  assert.deepEqual(visibleTemplates(undefined), [])
})

console.log('== 接入点：两个页面都走同一套判定 ==')

t('列表页用 visibleTemplates 过滤，且不再渲染来源说明', () => {
  assert.match(LIST, /visibleTemplates\(await api\.templates\(store\.bookId\)\)/)
  assert.ok(!LIST.includes('来源说明'), '来源说明列还在')
  assert.ok(!/row\.remark/.test(LIST), '还在读 remark 渲染那一列')
})

t('报表页下拉与删除后都重算可见性', () => {
  assert.match(VIEW, /visibleTemplates\(await api\.templates\(store\.bookId\), templateId\.value\)/)
  assert.match(VIEW, /allTemplates\.value = visibleTemplates\(await api\.templates\(store\.bookId\)\)/)
})

t('列表必须带账套：服务端要靠 book_id 判可见性并按 code 去重', () => {
  // 模板按账套隔离之后，/templates 不带 book_id 就不知道「哪些模板对当前
  // 账套可见」，也没法做「基线 vs 本账套副本，同 code 取副本」的去重。
  assert.match(API, /export const templates = \(bookId\) =>[\s\S]{0,160}book_id: bookId/)
  assert.ok(!/api\.templates\(\)/.test(LIST + VIEW), '还有地方没带账套就拉模板列表')
})

t('进设计模式先 fork：内置原版不能直接改，且地址栏要跟着换', () => {
  // 内置的资产负债表/利润表是所有账套共用的原版，直接改会扩散到别的账套。
  // 所以点「设计报表」先复制一份到本账套再改。
  assert.match(VIEW, /async function ensureOwnCopy\(\)/)
  assert.match(VIEW, /api\.forkTemplate\(templateId\.value, store\.bookId\)/)
  // URL 不换的话刷新会回到没改过的原版，看上去就是「改动没保存」。
  assert.match(VIEW, /router\.replace\(`\/reports\/\$\{forked\.id\}`\)/)
  assert.match(API, /export const forkTemplate/)
})

t('删掉当前报表后要改 templateId（同名路由不重新挂载，只换 URL 会停在已删的表上）', () => {
  assert.match(VIEW, /templateId\.value = allTemplates\.value\[0\]\.id/)
  assert.ok(!/const remaining = await api\.templates\(\)/.test(VIEW), '还在用没过滤的 remaining')
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
