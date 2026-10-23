/**
 * 没有账套时的落点（src/utils/bookGuard.js + src/router/index.js，**线上那份源码**）。
 *
 * 这里钉的是两件事：
 *
 *  1. **判定本身**：哪些页面在没有账套时该把人送回账套管理。
 *     以前只有期初余额页自己写了 `router.push('/books')`，其余账套相关页面
 *     （科目表、凭证、报表、打印模板…）在无账套时打开，得到的是一张
 *     「暂无数据」的空表 —— 看不出是「没账套」还是「真没数据」。
 *
 *  2. **别把有账套的人误判掉**：账套列表还没拉回来时 books 是空的，
 *     那是「还没问」，不是「没有」。判定必须等「问过了」这个事实。
 *     这是这条规则最容易出事的地方 —— 判错一次，用户打开深链接就被踢回
 *     账套管理，而且他根本不知道为什么。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const { needsBookRedirect } = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/bookGuard.js'))

const read = (p) => fs.readFileSync(new URL(p, import.meta.url), 'utf8')
const ROUTER = read('../src/router/index.js')
const SESSION = read('../src/stores/session.js')

/** 账套相关页面：没有账套就干不了活，一律回账套管理 */
const BOOK_ROUTES = [
  'openings', 'vouchers', 'voucher-new', 'voucher-edit', 'balance',
  'general-ledger', 'detail-ledger', 'reports', 'report-view', 'periods',
  'subjects', 'audit', 'voucher-organize', 'voucher-import', 'invoice-import',
  'print-templates',
]
/** 全站级页面：跟账套无关，管理员自己没有账套时也必须进得去 */
const SYSTEM_ROUTES = [
  'books', 'users', 'ocr-settings', 'notify-settings', 'data-security',
  'plugins', 'scheduler-tasks',
  // 个人资料：改自己的名字与口令，本来就与账套无关。
  // 标了 requiresBook 的话，**没有账套的人连自己的口令都改不了** ——
  // 而「自己没分到账套」在这个系统里是常态（管理员自己也可能一个账套都没有）。
  'profile',
]

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

/** 取某个路由名那一行的 meta */
function metaOf(name) {
  const m = ROUTER.match(new RegExp(`name: '${name}'[^\\n]*`))
  return m ? m[0] : ''
}

console.log('== 判定本身 ==')

t('账套相关页面 + 问过了 + 一个账套都没有 = 送回账套管理', () => {
  assert.equal(needsBookRedirect({ requiresBook: true, booksLoaded: true, bookCount: 0 }), true)
})

t('有账套就不送（绝大多数时候）', () => {
  assert.equal(needsBookRedirect({ requiresBook: true, booksLoaded: true, bookCount: 3 }), false)
})

t('还没问过列表 = 不送（这是最容易错的那一条）', () => {
  assert.equal(needsBookRedirect({ requiresBook: true, booksLoaded: false, bookCount: 0 }), false,
    '把「还没问」当成了「没有」—— 有账套的用户会被踢回账套管理')
})

t('不是账套相关页面就不送（系统管理那几页没有账套也得进得去）', () => {
  assert.equal(needsBookRedirect({ requiresBook: false, booksLoaded: true, bookCount: 0 }), false)
})

t('缺参数时是「不送」，不是「送」', () => {
  assert.equal(needsBookRedirect(), false)
  assert.equal(needsBookRedirect({}), false)
})

console.log('')
console.log('== 路由标注 ==')

t('账套相关的页面都标了 requiresBook', () => {
  const missing = BOOK_ROUTES.filter(n => !/requiresBook/.test(metaOf(n)))
  assert.deepEqual(missing, [], '这些页面漏标了：无账套时它们会显示成「暂无数据」的空表')
})

t('全站级页面一个都没标', () => {
  const marked = SYSTEM_ROUTES.filter(n => /requiresBook/.test(metaOf(n)))
  assert.deepEqual(marked, [], '这些页面跟账套无关，标了会把管理员挡在门外')
})

t('账套管理页自己不标（不能自己跳自己，转成死循环）', () => {
  assert.doesNotMatch(metaOf('books'), /requiresBook/, '账套管理页自己要求有账套 —— 会变成无限重定向')
})

t('路由表里没有漏网的账套相关页面', () => {
  // 侧栏里能点到的账套相关页面（nav.js 的 items）都该在上面的名单里，
  // 少一个就说明新加页面时忘了标。
  const nav = read('../src/router/nav.js')
  const navNames = [...nav.matchAll(/name: '([a-z-]+)'/g)].map(m => m[1])
  const navBookish = navNames.filter(n =>
    !['voucher', 'ledger', 'master', 'system', 'extensions'].includes(n) &&
    /subjects|books|openings|print-templates|vouchers|invoices|periods|balance|ledger|reports|audit/.test(n))
  const missing = navBookish.filter(n => !BOOK_ROUTES.includes(n) && n !== 'books')
  assert.deepEqual(missing, [], '侧栏里这些页面没在名单里 —— 要么漏标 requiresBook，要么该补进名单')
})

console.log('')
console.log('== 守卫怎么用它 ==')

t('守卫走的是 utils 里那一份判定，不在守卫里另写一套', () => {
  assert.match(ROUTER, /needsBookRedirect\(/, '守卫没用 utils 的判定')
  assert.match(ROUTER, /import \{ needsBookRedirect \}/, '都没 import 判定函数')
})

t('判「没有账套」之前先保证列表问过了', () => {
  assert.match(ROUTER, /requiresBook[\s\S]{0,400}?await store\.loadBooks\(\)/,
    '守卫没有先拉账套列表就判空 —— 会误伤有账套的人')
  assert.match(ROUTER, /await store\.loadBooks\(\)[\s\S]{0,400}?needsBookRedirect\(/,
    '判定发生在拉列表之前')
})

t('拉列表失败时放行，不把人困住', () => {
  assert.match(ROUTER, /catch \{[\s\S]{0,200}?\}/, '拉账套列表失败没有兜底')
})

console.log('')
console.log('== 「问过了」这个事实存在哪 ==')

t('loadBooks 成功之后把 booksLoaded 置为 true', () => {
  assert.match(SESSION, /this\.books = await bookApi\.listBooks\(\)[\s\S]{0,200}?this\.booksLoaded = true/,
    '拉完账套列表没有记下「问过了」')
})

t('登出时把它复位（下一个人的账套列表还没问过）', () => {
  assert.match(SESSION, /clearLocal\(\)[\s\S]{0,400}?this\.booksLoaded = false/,
    '登出没复位 booksLoaded —— 下一个用户会拿着上一份「问过了」去判空')
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
