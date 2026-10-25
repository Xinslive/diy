import { ElMessage } from 'element-plus'
import { inRect, normRect, rectSize, rectToTsv } from '@/utils/cellRange'

/**
 * 全系统的**表格矩形选区**：文档级装一次，所有表格自动生效。
 *
 *  浏览器原生选区在表格里是按文档顺序线性扩的 —— 从 C3 拖到 C9 会把中间
 *  几整行连同左右各列一起框进去。CSS 改不了选区形状，所以形状自己算
 *  （判定在 `utils/cellRange`），这里只管定位、绘制和写剪贴板。
 *
 *  **为什么装在 document 上而不是每个表格各接一次**：这个系统里的表格有
 *  三种写法（裸 `<table>`、Element Plus 的 `el-table`、打印台拼出来的
 *  表格），逐个接意味着十几个文件各写一遍，而以后新加的表格还会漏。
 *  事件是代理的，按 `td` 认格子，跟表格用什么实现无关。
 *
 *  **刻意不吞点击**：接管时不改事件传播，表格里原本的「点一行打开详情」
 *  「点格子改公式」全都照旧。只有落在输入框、按钮、链接、下拉里的拖选
 *  不接管 —— 那些地方要的正是原生文本选区。
 */
const PAINT = 'cwq-cell-picked'
/** 这些元素内部的拖选是选文字，别接管。 */
const SKIP = 'input,textarea,select,button,a,[contenteditable="true"],.el-input,.el-select'

/** 当前选区：{ rows, rect }，rows 是按下那一刻的表体行，拖动期间不重取。 */
let current = null
/** 按下的那一格 { r, c, table, rows }，同时兼任「正在拖」的标记。 */
let anchor = null
let frame = 0

/** el-table 的表体是 table 本身，裸 table 落在 tbody 上；两者都能取到行。 */
const rowsOf = table => Array.from(table.tBodies[0]?.rows || [])

function locate(target) {
  const cell = target.closest?.('td')
  if (!cell || !anchor) return null
  const table = cell.closest('table')
  if (!table || table !== anchor.table) return null
  const row = cell.parentElement
  const r = anchor.rows.indexOf(row)
  if (r < 0) return null
  return { r, c: Array.from(row.cells).indexOf(cell) }
}

function paint() {
  if (!current) return
  const { rows, rect } = current
  rows.forEach((row, r) => {
    const cells = row.cells
    for (let c = 0; c < cells.length; c++) {
      // 只在状态真的变了时才动 class：一次 rAF 里几千个格子来回 toggle
      // 会把主线程吃满，而绝大多数格子其实没进选区。
      const on = inRect(r, c, rect)
      if (on !== cells[c].classList.contains(PAINT)) cells[c].classList.toggle(PAINT, on)
    }
  })
}

const schedulePaint = () => {
  if (frame) return
  frame = requestAnimationFrame(() => { frame = 0; paint() })
}

function clear() {
  if (current) {
    current.rows.forEach(row => Array.from(row.cells).forEach(cell => cell.classList.remove(PAINT)))
    current = null
  }
  anchor = null
}

function tsvOf(rows) {
  const { rect } = current
  return rectToTsv((r, c) => rows[r]?.cells?.[c]?.innerText ?? '', rect)
}

/**
 * 复制时用的行：**必须是还挂在文档上的那些**。
 *
 * 以前 `current.rows` 是按下那一刻的**快照**，表格重渲染、弹窗关闭、
 * 路由切换之后它还留着 —— 于是 Ctrl+C 复制到的是**已经不在页面上的旧表格
 * 内容**，而提示条还在说「已复制 N 行 × M 列」（审计 §3.32）。
 *
 * 现在复制前先验一次：行还挂在文档上才复制，而且**按当前表格重新取一遍行**，
 * 免得表格重渲染之后取到的是同一张表的旧行。取不到就**放弃复制并说清楚**，
 * 绝不把旧内容塞进剪贴板。
 */
function liveRows() {
  if (!current || !current.rows?.length) return null
  // 按下时抓的那一行已经脱离文档 → 这份选区整个作废。
  if (!current.rows[0].isConnected) return null
  const table = current.table
  if (!table || !table.isConnected) return null
  return rowsOf(table)
}

/** 选区作废了：说清楚为什么，别让人以为复制成功。 */
function refuseCopy() {
  ElMessage.warning('这张表已经变了，这次没有复制 —— 请重新框选')
  clear()
}

/**
 * 写剪贴板。`navigator.clipboard` 只在安全上下文可用，而这套系统常被
 * 部署在局域网 http 上，那时拿不到 —— 所以留 execCommand 兜底。
 */
function writeClipboard(text) {
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => legacyCopy(text))
    return
  }
  legacyCopy(text)
}

function legacyCopy(text) {
  const holder = document.createElement('textarea')
  holder.value = text
  holder.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none'
  document.body.appendChild(holder)
  holder.select()
  try { document.execCommand('copy') } finally { holder.remove() }
}

function onMouseDown(event) {
  if (event.button !== 0) return
  // 先问「按在哪儿了」，再问「是不是在格子里」。顺序不能反：判断输入框必须
  // 从 event.target 往上找，而格子要从 target 往上找到 td 再判断。写成
  // cell.closest(SKIP) 就等于从 td 往上找祖先 —— 输入框是 td 的**后代**，
  // 永远匹配不上，于是下面那句 preventDefault() 会照常落在每个输入框上，
  // 鼠标一点就聚焦不了，看起来就是「框里的字改不动」。
  if (event.target.closest?.(SKIP)) { clear(); return }
  const cell = event.target.closest?.('td')
  if (!cell) { clear(); return }
  const table = cell.closest('table')
  const rows = table && rowsOf(table)
  if (!rows?.length) { clear(); return }
  // 接管这次拖选：拦掉原生选区，免得指针拖出表格后又连着选中整页文字。
  event.preventDefault()
  clear()
  const row = cell.parentElement
  const r = rows.indexOf(row)
  const c = Array.from(row.cells).indexOf(cell)
  anchor = { r, c, table, rows }
  // table 也存一份：复制时要按**当前**的表格重新取行（见 liveRows）。
  current = { rows, table, rect: normRect(anchor, anchor) }
  paint()
}

function onMouseMove(event) {
  if (!anchor || !current || event.buttons !== 1) return
  const at = locate(event.target)
  if (!at) return
  current.rect = normRect(anchor, at)
  schedulePaint()
}

function onMouseUp() {
  if (!current || !anchor) return
  const size = rectSize(current.rect)
  anchor = null
  // 单击不写剪贴板：表格里的单击多半是打开详情，别把用户的剪贴板抢了。
  if (size.rows * size.cols < 2) return
  const rows = liveRows()
  if (!rows) { refuseCopy(); return }
  writeClipboard(tsvOf(rows))
  ElMessage.success(`已复制 ${size.rows} 行 × ${size.cols} 列`)
}

/** Ctrl/Cmd+C 走浏览器的 copy 事件：单格选区也能复制，且不依赖安全上下文。 */
function onCopy(event) {
  if (!current) return
  // 选区还在，但**它指的那张表已经不是页面上的那张了** → 复制的是旧内容。
  // 宁可复制失败，也不能把「已经不在这儿的表格」的内容塞进剪贴板。
  const rows = liveRows()
  if (!rows) {
    event.preventDefault()
    refuseCopy()
    return
  }
  event.clipboardData?.setData('text/plain', tsvOf(rows))
  event.preventDefault()
}

function onKeyDown(event) {
  if (event.key === 'Escape') clear()
}

/**
 * 装一次全局矩形选区，返回拆除函数。
 *
 *  重复调用是安全的：装之前先拆，App 热更新重挂一次也不会叠出两层监听，
 *  否则一次拖动会被处理两遍（画两遍、弹两次提示）。
 */
export function installCellSelection() {
  document.removeEventListener('mousedown', onMouseDown, true)
  document.removeEventListener('copy', onCopy)
  document.removeEventListener('keydown', onKeyDown)
  document.addEventListener('mousedown', onMouseDown, true)
  document.addEventListener('mouseup', onMouseUp, true)
  document.addEventListener('mousemove', onMouseMove, true)
  document.addEventListener('copy', onCopy)
  document.addEventListener('keydown', onKeyDown)
  // **切页面要把选区清掉**：这个系统用 hash 路由，切一次路由就换一张表，
  // 上一页那份选区留着的话，Ctrl+C 会复制到已经不在这儿的表格（审计 §3.32）。
  // 监听挂在 window 上（hashchange 是 window 的事件，不经过 document）。
  const onRouteChange = () => clear()
  if (typeof window !== 'undefined') {
    window.removeEventListener('hashchange', onRouteChange)
    window.addEventListener('hashchange', onRouteChange)
  }
  return () => {
    clear()
    document.removeEventListener('mousedown', onMouseDown, true)
    document.removeEventListener('mouseup', onMouseUp, true)
    document.removeEventListener('mousemove', onMouseMove, true)
    document.removeEventListener('copy', onCopy)
    document.removeEventListener('keydown', onKeyDown)
    if (typeof window !== 'undefined') window.removeEventListener('hashchange', onRouteChange)
  }
}
