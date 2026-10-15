/**
 * 表格矩形选区（src/utils/cellRange.js，**线上那份源码**）。
 *
 *  这个特性最怕的不是「框不方正」，是**框大了**：原生选区从 C3 拖到 C9
 *  会把中间整行连同左右各列一起框进去，用户看到的就是这个。所以这里重点
 *  盯三件事 —— 拖动方向不影响结果、命中判定是闭合矩形、以及导出文本不把
 *  换行和多余空白带进剪贴板。
 *
 *  DOM 那半（定位、绘制、剪贴板）在 composables 里，只能源码级断言；
 *  形状与文本这半是纯函数，逐条跑。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const VITE = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
})
const {
  inRect, normRect, rectSize, rectToTsv,
} = await VITE.ssrLoadModule('/src/utils/cellRange.js')

const APP = fs.readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const HOOK = fs.readFileSync(new URL('../src/composables/useCellSelection.js', import.meta.url), 'utf8')
const CSS = fs.readFileSync(new URL('../src/styles/main.css', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== normRect：两个角归一，拖动方向不影响结果 ==')

t('左上 → 右下', () => {
  assert.deepEqual(normRect({ r: 2, c: 2 }, { r: 8, c: 2 }), { r1: 2, c1: 2, r2: 8, c2: 2 })
})
t('右下 → 左上（倒着拖，结果必须一样）', () => {
  assert.deepEqual(normRect({ r: 8, c: 2 }, { r: 2, c: 2 }), { r1: 2, c1: 2, r2: 8, c2: 2 })
})
t('同一格 = 单格选区', () => {
  assert.deepEqual(normRect({ r: 3, c: 3 }, { r: 3, c: 3 }), { r1: 3, c1: 3, r2: 3, c2: 3 })
})

console.log('== inRect：只认矩形里那一列，不多吃旁边 ==')

const col = normRect({ r: 2, c: 2 }, { r: 8, c: 2 })
t('C3→C9 这一列七格全部命中', () => {
  assert.deepEqual([2, 3, 4, 5, 6, 7, 8].map(r => inRect(r, 2, col)), Array(7).fill(true))
})
t('同一行的左右邻列都不命中（用户报的就是这个）', () => {
  assert.equal(inRect(5, 1, col), false)
  assert.equal(inRect(5, 3, col), false)
})
t('矩形外的行不命中', () => {
  assert.equal(inRect(1, 2, col), false)
  assert.equal(inRect(9, 2, col), false)
})
t('矩形选区四边闭合：角上的格子也在内', () => {
  const box = normRect({ r: 1, c: 1 }, { r: 3, c: 4 })
  assert.deepEqual([[1, 1], [3, 4], [1, 4], [3, 1]].map(([r, c]) => inRect(r, c, box)), Array(4).fill(true))
  assert.equal(inRect(4, 2, box), false)
  assert.equal(inRect(2, 5, box), false)
})

console.log('== rectSize：用来判断「有没有真的拖过」 ==')

t('单格是 1×1，拖过一格就是 2×1', () => {
  assert.deepEqual(rectSize(normRect({ r: 0, c: 0 }, { r: 0, c: 0 })), { rows: 1, cols: 1 })
  assert.deepEqual(rectSize(normRect({ r: 0, c: 0 }, { r: 1, c: 0 })), { rows: 2, cols: 1 })
})
t('横拖与竖拖分别算行和列', () => {
  assert.deepEqual(rectSize(normRect({ r: 4, c: 1 }, { r: 4, c: 5 })), { rows: 1, cols: 5 })
  assert.deepEqual(rectSize(normRect({ r: 4, c: 1 }, { r: 9, c: 1 })), { rows: 6, cols: 1 })
})

console.log('== rectToTsv：粘进 Excel 要成块 ==')

const GRID = [
  ['资产', '行次', '期末余额'],
  ['货币资金', '1', '1,712,379.87'],
  ['短期投资', '2', '0.00'],
  ['应收账款', '3', '320,492.35'],
]
const at = (r, c) => GRID[r]?.[c]

t('取一列七格 → 每行一个值，换行分隔', () => {
  assert.equal(rectToTsv(at, normRect({ r: 1, c: 2 }, { r: 3, c: 2 })),
    '1,712,379.87\n0.00\n320,492.35')
})
t('取一个矩形块 → 制表符分列', () => {
  assert.equal(rectToTsv(at, normRect({ r: 1, c: 0 }, { r: 2, c: 1 })),
    '货币资金\t1\n短期投资\t2')
})
t('空格与换行压成单格一个值（否则粘进 Excel 会错行）', () => {
  assert.equal(rectToTsv(() => '  备注\n第二行 ', normRect({ r: 0, c: 0 }, { r: 0, c: 0 })), '备注 第二行')
})
t('越界的列补空串，不留 undefined', () => {
  // 第 4 列本来就没有内容，选到它必须是空格子而不是字面量 undefined
  assert.equal(rectToTsv(at, normRect({ r: 2, c: 2 }, { r: 3, c: 3 })), '0.00\t\n320,492.35\t')
})
t('整行都在表外 → 空行，行数不减（粘过去仍是原样的行）', () => {
  assert.equal(rectToTsv(at, normRect({ r: 3, c: 0 }, { r: 4, c: 0 })), '应收账款\n')
})

console.log('== 接入点：全局装一次 + 原生选区确实关掉了 ==')

t('App.vue 装一次，卸载时拆掉', () => {
  assert.match(APP, /installCellSelection\(\)/)
  assert.match(APP, /onBeforeUnmount\(removeSelection\)/)
})
t('监听挂在 document 上（新加的表格不必改代码就生效）', () => {
  assert.match(HOOK, /document\.addEventListener\('mousedown'/)
  assert.match(HOOK, /document\.addEventListener\('mouseup'/)
  assert.match(HOOK, /document\.addEventListener\('mousemove'/)
})
t('重复装不会叠出两层监听（否则一次拖动弹两次提示）', () => {
  assert.match(HOOK, /addEventListener\('mousedown', onMouseDown, true\)[\s\S]*?removeEventListener\('mousedown', onMouseDown, true\)/)
})
t('输入框/按钮/下拉里的拖选不接管（跳过判断必须从 event.target 起算）', () => {
  assert.match(HOOK, /const SKIP = 'input,textarea,select,button,a,\[contenteditable="true"\][\s\S]*?'/)
  // 写成 cell.closest(SKIP) 就废了：输入框是 td 的后代，从 td 往上找祖先
  // 永远匹配不上，于是 preventDefault() 照常落在每个输入框上。
  // 断言前先剥掉注释，否则会命中注释里那句反面教材。
  const CODE = HOOK.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.doesNotMatch(CODE, /cell\.closest\(SKIP\)/,
    '跳过判断又从 td 起算了 —— 表格里的输入框会点不进焦点')
  assert.match(CODE, /event\.target\.closest\??\.\(SKIP\)/)
})
t('表格里的输入框把原生文本选区还回来（否则 Ctrl+A 重打这条路被堵死）', () => {
  assert.match(CSS, /table input[^{]*\{[^}]*user-select: text/)
})

console.log('== 行为：鼠标点在输入框上，到底拦没拦 ==')

// 这一段不再看源码，直接把 onMouseDown 调起来，用一个只实现 closest() 的
// 极简 DOM 喂它。上一版这里只有正则断言，而正则恰好把写错的那版也判成对
// —— 「测试通过」和「点得进去」之间隔着的正是这一步。

/** 极简节点：只实现 composable 真正用到的那几个成员。 */
function node(tag, { cls = '', parent = null } = {}) {
  const n = {
    tag, parent, children: [], clsSet: new Set(cls.split(/\s+/).filter(Boolean)), cells: [],
    closest(sel) {
      for (const part of String(sel).split(',')) {
        const s = part.trim()
        for (let cur = n; cur; cur = cur.parent) {
          if (s.startsWith('.') ? cur.clsSet.has(s.slice(1))
            : s.startsWith('[') ? false
              : cur.tag === s) return cur
        }
      }
      return null
    },
    get classList() {
      return {
        contains: c => n.clsSet.has(c),
        toggle: (c, on) => { on ? n.clsSet.add(c) : n.clsSet.delete(c) },
        remove: c => n.clsSet.delete(c),
      }
    },
  }
  parent?.children.push(n)
  return n
}

/** table > tbody > tr > td，行里第一格是带输入框的项目名列。 */
function fakeTable(withInput) {
  const table = node('table'), tbody = node('tbody', { parent: table })
  table.tBodies = [tbody]
  tbody.rows = []
  for (let i = 0; i < 3; i++) {
    const tr = node('tr', { parent: tbody })
    tr.parentElement = tbody
    const tds = []
    for (let j = 0; j < 3; j++) {
      const td = node('td', { cls: j === 0 ? 'c-label' : 'c-num', parent: tr })
      td.parentElement = tr
      tds.push(td)
      tr.cells.push(td)
    }
    if (withInput && i === 0) {
      const wrap = node('div', { cls: 'el-input el-input--small row-label', parent: tds[0] })
      node('input', { parent: wrap })   // 真正的按下目标
    }
    tbody.rows.push(tr)
  }
  return table
}

/** 装一遍 hook，把它注册到假 document 上的 mousedown 抓出来。 */
async function mountHook() {
  const handlers = {}
  const doc = {
    addEventListener: (type, fn) => { (handlers[type] ||= []).push(fn) },
    removeEventListener: (type, fn) => {
      handlers[type] = (handlers[type] || []).filter(f => f !== fn)
    },
  }
  globalThis.document = doc
  globalThis.requestAnimationFrame = cb => { cb(); return 0 }
  const { installCellSelection } = await VITE.ssrLoadModule('/src/composables/useCellSelection.js')
  installCellSelection()
  return handlers.mousedown[0]
}

/** 按一下，返回这次按下有没有被接管（被接管 = defaultPrevented）。 */
function press(onMouseDown, table, target) {
  const event = { button: 0, target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true } }
  onMouseDown(event)
  return event.defaultPrevented
}

const mousedown = await mountHook()

t('鼠标点表格里的输入框：不许接管（拦了就点不进焦点，框里的字改不动）', () => {
  const table = fakeTable(true)
  const input = table.tBodies[0].rows[0].cells[0].children?.[0]?.children?.[0]
  assert.ok(input, '假 DOM 构造失败')
  assert.equal(press(mousedown, table, input), false,
    '按下被 preventDefault 了 —— 输入框聚焦不了，真人点进去打不进字')
})
t('鼠标点表格里的普通金额格：要接管（否则矩形选区形同虚设）', () => {
  const table = fakeTable(false)
  assert.equal(press(mousedown, table, table.tBodies[0].rows[1].cells[2]), true,
    '普通格子没被接管，拖选直接失效')
})
t('点击行内按钮也不接管（插删行、加粗那些 ⊕ ✕ B ◉ ⇤ ⇥）', () => {
  const table = fakeTable(false)
  const td = table.tBodies[0].rows[2].cells[0]
  const btn = node('button', { parent: td })
  assert.equal(press(mousedown, table, btn), false)
})
t('单击不抢剪贴板（表格里单击多半是打开详情）', () => {
  assert.match(HOOK, /size\.rows \* size\.cols < 2/)
})
t('Ctrl+C 走 copy 事件，局域网 http 部署也有兜底', () => {
  assert.match(HOOK, /addEventListener\('copy', onCopy\)/)
  assert.match(HOOK, /clipboardData\?\.setData\('text\/plain'/)
  assert.match(HOOK, /execCommand\('copy'\)/)
})
t('表格的原生选区关掉，选中态样式在', () => {
  assert.match(CSS, /table \{ user-select: none/)
  assert.match(CSS, /\.cwq-cell-picked \{/)
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
