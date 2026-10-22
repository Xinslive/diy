/**
 * 账套删除机制在界面上的样子：**没有样子**。
 *
 * 这套机制有两条路径（管理员收回最后一份授权 / 唯一授权用户改名成「删除」），
 * 但它**刻意不外露**：
 *   · 界面上没有「删除账套」按钮；
 *   · 没有任何文案提示「改名成删除就能删」；
 *   · 没有第三条路（前端也没有账套删除接口）。
 *
 * 为什么要钉这一组：机制一旦被写在界面上，它就变成了**产品功能** ——
 * 于是所有人都会去用它，包括那些只想把账套改个名字的人；
 * 而「一个授权用户都没有的账套就删掉」这条规则，本意是**收拾残局**
 * （错账、重记的账、别人建了又不要的账），不是给人日常用的。
 *
 * 用源码级断言而不是渲染快照：要防的是「这段话又被写回去」，
 * 断的是页面上还有没有这句话，不是它长什么样。
 * 只看 ``<template>`` —— 那是用户看得见的部分；
 * ``<script>`` 里的注释是写给我们自己看的，不构成外露。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const SRC = path.join(ROOT, 'src')

const read = (p) => fs.readFileSync(p, 'utf8')

/** 一个 .vue 文件里用户看得见的那部分（<template> 之间的内容）。 */
const templateOf = (src) => {
  const i = src.indexOf('<template>')
  const j = src.indexOf('</template>')
  if (i < 0 || j < 0) return ''
  return src.slice(i, j)
}

/** 递归找所有 .vue / .js 源文件。 */
function walk(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name)
    if (e.isDirectory()) walk(p, out)
    else if (/\.(vue|js)$/.test(e.name)) out.push(p)
  }
  return out
}

const BOOK_MANAGER = read(path.join(SRC, 'views/book/BookManager.vue'))
const USER_ADMIN = read(path.join(SRC, 'views/system/UserAdmin.vue'))
const BOOK_API = read(path.join(SRC, 'api/book.js'))
const ALL_SOURCES = walk(SRC)

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 界面上不许出现删除入口 ==')

t('账套管理页的模板里没有「删除」两个字', () => {
  assert.doesNotMatch(templateOf(BOOK_MANAGER), /删除/,
    '账套管理页出现了「删除」文案 —— 这套机制不该有界面入口')
})

t('账套管理页没有「删除账套」按钮', () => {
  assert.doesNotMatch(BOOK_MANAGER, /删除账套/, '页面上出现了一个删除账套的入口')
})

t('用户与授权页也没有删除账套的入口', () => {
  assert.doesNotMatch(templateOf(USER_ADMIN), /删除账套/,
    '管理员页出现了删除账套的入口')
})

t('全站界面上没有「删除账套」这四个字', () => {
  for (const f of ALL_SOURCES) {
    const tpl = f.endsWith('.vue') ? templateOf(read(f)) : ''
    assert.doesNotMatch(tpl, /删除账套/, `${path.relative(ROOT, f)} 的界面上出现了「删除账套」`)
  }
})

t('全站界面上没有解释「改名即删除」的文案', () => {
  // 这两句话是机制的说明书 —— 一旦出现在界面上，机制就变成了功能。
  for (const f of ALL_SOURCES) {
    const tpl = f.endsWith('.vue') ? templateOf(read(f)) : ''
    assert.doesNotMatch(tpl, /改名.{0,8}删|删.{0,8}改名/,
      `${path.relative(ROOT, f)} 的界面上解释了「改名即删除」`)
  }
})

console.log('')
console.log('== 也没有第三条路 ==')

t('前端没有账套删除接口', () => {
  assert.doesNotMatch(BOOK_API, /http\.delete\(\s*[`'"]\/books/,
    '前端多了一条账套删除接口 —— 机制应当只有那两条路径')
})

console.log('')
console.log('== 但机制本身要真的跑得通 ==')

t('改名提交后认后端返回的 deleted 标记', () => {
  // 不认的话，账套已经没了，界面却弹一句「已保存」——
  // 用户看着磁贴凭空消失，只会以为系统出错了。
  assert.match(BOOK_MANAGER, /res\.deleted|res\s*&&\s*res\.deleted/,
    '改名提交后没有处理「账套已被删除」的返回')
})

t('删除后有一条中性的提示', () => {
  assert.match(BOOK_MANAGER, /账套已删除/, '账套没了却没有任何提示')
})

t('这条提示不解释机制', () => {
  const m = BOOK_MANAGER.match(/ElMessage\.success\('([^']*)'\)/g) || []
  const texts = m.map((x) => x.replace(/^.*success\('|'\).*$/g, ''))
  for (const s of texts) {
    assert.doesNotMatch(s, /改名|保留名|授权/, `提示语解释了机制：${s}`)
  }
})

console.log('')
console.log(`== ${pass} 通过 / ${fail} 失败 ==`)
if (fail) process.exit(1)
