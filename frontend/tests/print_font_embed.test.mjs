/**
 * 「锁定字体」：字体随应用一起发布，换台机器印出来一模一样。
 *
 * 盯的是三件事：
 *   1. **等字体再打印** —— 字体没到位就 print()，纸上出来的是系统字体那一版，
 *      与用户看到的预览不是同一份版式，正是要消灭的东西；
 *   2. **等字体再量** —— 预览量的是「排版后内容占多高」，字体没到位时量到的
 *      是回退字体的排版；
 *   3. **开关摆在设计器里** —— 不然用户根本没法打开它。
 *
 * 照应的是后端 `app/printing/fonts.py`、`app/printing/template.py`、
 * `app/api/print_assets.py`。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const read = (rel) => fs.readFileSync(new URL(rel, import.meta.url), 'utf8')

const FONT_UTIL = read('../src/utils/printFont.js')
const PRINT_API = read('../src/api/print.js')
const PREVIEW = read('../src/components/PrintPreview.vue')
const STAGE = read('../src/components/print/PrintStage.vue')
const INSPECTOR = read('../src/components/print/TemplateInspector.vue')
const BACKEND_TEMPLATE = read('../../backend/app/printing/template.py')
const BACKEND_FONTS = read('../../backend/app/printing/fonts.py')
const BACKEND_ASSETS = read('../../backend/app/api/print_assets.py')

/** 去掉注释再断言「代码里没有某个写法」—— 解释这个坑的注释本身会命中。 */
const stripComments = (src) => src
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/^\s*\/\/.*$/gm, '')

const CODE = {
  FONT_UTIL: stripComments(FONT_UTIL),
  PRINT_API: stripComments(PRINT_API),
  PREVIEW: stripComments(PREVIEW),
  STAGE: stripComments(STAGE),
  INSPECTOR: stripComments(INSPECTOR),
  BACKEND_TEMPLATE: stripComments(BACKEND_TEMPLATE),
  BACKEND_FONTS: stripComments(BACKEND_FONTS),
  BACKEND_ASSETS: stripComments(BACKEND_ASSETS),
}

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 字体到位之前不许打印、不许量 ==')

t('print.js 在 print() 之前等字体', () => {
  const i = CODE.PRINT_API.indexOf('fontsReady(')
  assert.ok(i >= 0, 'print.js 里没有 fontsReady —— 字体没到位就打印了')
  const j = CODE.PRINT_API.indexOf('w.print()')
  assert.ok(j >= 0, 'print.js 里没有 w.print()')
  assert.ok(i < j, 'print() 排在 fontsReady 前面了')
})

t('预览量高之前等字体', () => {
  assert.ok(/fontsReady\(/.test(CODE.PREVIEW), 'PrintPreview.vue 没有等字体')
  // 等到之后要重量一次 —— 只等不重量等于没等。
  const seg = CODE.PREVIEW.slice(CODE.PREVIEW.indexOf('fontsReady('))
  assert.ok(/apply\(\)/.test(seg), '等完字体没有重新量')
})

t('设计器预览也等字体', () => {
  assert.ok(/fontsReady\(/.test(CODE.STAGE), 'PrintStage.vue 没有等字体')
  const seg = CODE.STAGE.slice(CODE.STAGE.indexOf('fontsReady('))
  assert.ok(/apply\(\)/.test(seg), '等完字体没有重新量')
})

t('等字体必须有上限 —— 字体加载失败不能把打印按钮卡死', () => {
  assert.ok(/setTimeout/.test(CODE.FONT_UTIL), 'fontsReady 没有超时兜底')
  assert.ok(/Promise\.race/.test(CODE.FONT_UTIL), 'fontsReady 没有 race 超时')
})

t('没有 fonts 的文档直接放行', () => {
  assert.ok(/if \(!fonts \|\| !fonts\.ready\)/.test(CODE.FONT_UTIL),
    '文档没有 fonts 时会卡住')
})

console.log('== 开关摆在设计器里 ==')

t('设计器有「锁定字体」开关，绑的是 font.embed', () => {
  assert.ok(/锁定字体/.test(CODE.INSPECTOR), '设计器里没有「锁定字体」这一项')
  assert.ok(/tpl\.font\.embed|embedOn/.test(CODE.INSPECTOR), '开关没有绑到 font.embed')
})

t('关掉时不留下多余的字体键', () => {
  assert.ok(/delete props\.tpl\.font\.embed_font/.test(CODE.INSPECTOR),
    '关掉开关后 embed_font 还留在模板里')
})

console.log('== 后端：字体随应用发布 ==')

t('打印页用 <link> 引那份 @font-face，不内联', () => {
  // 101 条 @font-face 带 unicode-range，内联是 100 KB 起步。
  assert.ok(/font_stylesheet_link/.test(CODE.BACKEND_TEMPLATE), '打印页没引字体样式表')
  assert.ok(/<link rel="stylesheet"/.test(CODE.BACKEND_FONTS),
    '字体样式表不是一个 <link>')
})

t('字体族排在最前面 —— 排在后面等于没开', () => {
  assert.ok(/font-family: \{family_stack\}/.test(CODE.BACKEND_TEMPLATE),
    'font-family 没走 family_stack')
  assert.ok(/embedded_family/.test(CODE.BACKEND_TEMPLATE),
    '内嵌字体没进字体栈')
})

t('没开锁定字体时 CSP 一点没松', () => {
  assert.ok(/font-src \{font_src\}/.test(CODE.BACKEND_TEMPLATE), 'CSP 没按需放开 font-src')
  assert.ok(/style-src \{style_src\}/.test(CODE.BACKEND_TEMPLATE), 'CSP 没按需放开 style-src')
  assert.ok(/script-src 'none'/.test(CODE.BACKEND_TEMPLATE), 'script-src 被动过')
  // 不用这个功能的人不该为它付代价：没开时两条都回到最严的那一份。
  assert.ok(/style_src = .*if embed else/.test(CODE.BACKEND_TEMPLATE),
    'style-src 不是按 embed 决定的')
  assert.ok(/font_src = .*if embed else/.test(CODE.BACKEND_TEMPLATE),
    'font-src 不是按 embed 决定的')
})

t('字体路由只认白名单里的文件名', () => {
  assert.ok(/_FONT_NAME_RE/.test(CODE.BACKEND_ASSETS), '字体路由没有白名单')
  assert.ok(/HTTPException\(404/.test(CODE.BACKEND_ASSETS), '字体路由没挡住非法名字')
})

console.log(`\n${pass} 通过 / ${fail} 失败`)
if (fail) process.exit(1)
