/**
 * 账套「起始年度」的候选年份（src/utils/startYear.js，**线上那份源码**）。
 *
 * 用户报的是一句大白话：「起始年度能选的范围太少了，我想要想选哪年就选哪年」。
 * 以前那个下拉只有 6 个写死的候选（去年到四年后），想建一本 1998 年起步的账，
 * 界面上根本选不出来 —— 那不是「候选少」，是**没给出口**，剩下的路只有去改库。
 *
 * 这里钉两件事：
 *   ① 候选铺满整个可用区间（1900 年到今年往后 5 年），且倒序、顶头停在近年；
 *   ② 界面上每个年份都是**真实选项**（不走 allow-create），
 *      所以选中的一定是合法年份，不需要「手输了半截年份」那一套收口。
 *
 * 另外钉住一个容易踩的坑：**default-first-option 的默认值是 false**，
 * 不是 true。Element Plus 里这个属性没写就没有 —— 于是「输完年份按回车」
 * 落不到高亮的那一项上，按回车等于什么都没发生（选中项还停在原值上）。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const {
  startYearOptions, START_YEAR_MIN, START_YEAR_MAX, START_YEAR_FUTURE
} = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/startYear.js'))

const BOOKS = fs.readFileSync(new URL('../src/views/book/BookManager.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

/** 固定一个「今年」，免得测试跑到跨年那天自己变了 */
const NOW = new Date('2026-10-20T00:00:00')
const opts = startYearOptions(NOW)

console.log('== 候选年份：范围足够宽 ==')

t('不再是那 6 个写死的候选', () => {
  assert.ok(opts.length > 100, `候选只有 ${opts.length} 个，还是选不出想选的年份`)
})

t('包含今年', () => {
  assert.ok(opts.includes(2026), '候选里没有今年')
})

t('包含 30 年前', () => {
  assert.ok(opts.includes(1996), '候选里没有 30 年前')
})

t('包含 1900 年（区间底）', () => {
  assert.ok(opts.includes(1900), '候选里选不到 1900')
})

t('包含往后 5 年', () => {
  assert.ok(opts.includes(2026 + START_YEAR_FUTURE), '候选里没有 5 年后')
})

t('倒序：新年份在上', () => {
  const sorted = [...opts].sort((a, b) => b - a)
  assert.deepEqual(opts, sorted, '候选年份的顺序变了')
})

t('顶头停在近年，不是顶头一堆没人选的远年份', () => {
  // 倒序列表的第一屏就是用户最先看到的：顶上摆 2098、2097 这种年份，
  // 等于把真正要选的年份挤到屏幕外。
  assert.ok(opts[0] <= NOW.getFullYear() + START_YEAR_FUTURE,
    `顶上第一个是 ${opts[0]}，太靠后了`)
})

t('候选不越界（后端收得下）', () => {
  for (const y of opts) {
    assert.ok(y >= START_YEAR_MIN && y <= START_YEAR_MAX, `${y} 越界`)
  }
})

console.log('')
console.log('== 界面上真的能选到 ==')

t('起始年度那个下拉是可搜索的', () => {
  const m = BOOKS.match(/<el-select[^>]*v-model="form\.start_year"[^>]*>/)
  assert.ok(m, '没找到起始年度的下拉')
  assert.match(m[0], /filterable/, '下拉不能搜，一百多个年份翻起来太费劲')
})

t('写了 default-first-option（它的默认值是 false）', () => {
  // 不写就没有「输完按回车落到高亮项」这回事 —— 回车会按原值收场，
  // 用户看着像是输了没反应。
  const m = BOOKS.match(/<el-select[^>]*v-model="form\.start_year"[^>]*>/)
  assert.ok(m, '没找到起始年度的下拉')
  assert.match(m[0], /default-first-option/, '没写 default-first-option，回车选不中')
})

t('不走 allow-create：每个年份都是真实选项', () => {
  const m = BOOKS.match(/<el-select[^>]*v-model="form\.start_year"[^>]*>/)
  assert.doesNotMatch(m[0], /allow-create/, '开了 allow-create 就多出「新建」这种半截年份')
})

t('旧的写死范围已经拿掉', () => {
  assert.doesNotMatch(BOOKS, /length: 6/, '那个 6 个候选的写死范围还在')
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
