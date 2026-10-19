/**
 * 报表画布缩放。
 *
 * 用户报的现象是「设计模式一开，界面就挤压，一大半内容显示不出来」。修法是
 * 把报表做成可缩放的画布（照搬「打印模板」舞台的做法），所以这里守住三件事：
 *
 *   · **纯计算**（src/utils/canvasZoom.js）用行为测试跑真函数 ——
 *     这一段不能只靠正则：缩放比没夹住、适应宽度拿容器宽当分母，
 *     源码看着都挺像那么回事，只有真跑一遍才发现不对。
 *   · **两层结构**用源码断言守住：只有 transform 没有占位层的话，
 *     滚动容器会按未缩放的尺寸算可滚范围，缩小时右边空一大片、
 *     放大时底部被截断 —— 这个 bug 在源码里看不出破绽，跑起来才现形。
 *   · **量测顺序**守住：宽度变了必须等 DOM 更新完再量第二遍，
 *     否则收起面板那一刻会按旧宽度算出 130% 的缩放（这条真的踩过）。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

globalThis.localStorage = {
  _m: {}, getItem(k) { return this._m[k] ?? null },
  setItem(k, v) { this._m[k] = String(v) }, removeItem(k) { delete this._m[k] }
}
globalThis.location = { hash: '#/reports/14' }

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const server = await createServer({ root: ROOT, logLevel: 'error', server: { middlewareMode: true } })
const zoom = await server.ssrLoadModule('/src/utils/canvasZoom.js')

const SRC = fs.readFileSync(new URL('../src/views/report/ReportView.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = async (name, fn) => { try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ } }

console.log('== 画布缩放：纯计算 ==')

await t('缩放比夹在区间内，且只留两位小数', () => {
  assert.equal(zoom.clampScale(0.05), 0.3, '太小要抬到下限')
  assert.equal(zoom.clampScale(9), 2.5, '太大要压到上限')
  assert.equal(zoom.clampScale(0.8776), 0.88, '两位小数，读数才稳定')
  // 连点几下不能攒出一串浮点尾巴
  let s = 1
  for (let i = 0; i < 3; i++) s = zoom.clampScale(s + 0.1)
  assert.equal(s, 1.3)
})

await t('非法值回落到 100%，而不是 NaN 把整张表弄没', () => {
  assert.equal(zoom.clampScale(NaN), 1)
  assert.equal(zoom.clampScale(Infinity), 1)
  assert.equal(zoom.clampScale(undefined), 1)
})

await t('适应宽度拿内容宽当分母（用容器宽会恒等于 1，按钮等于没做）', () => {
  // 实测过的真实一组：画布 652，表格内容 743 → 88%
  assert.equal(zoom.fitScale(652, 743), 0.88)
  // 表格本来就比容器窄时，铺满就是 100%，不是无限放大
  assert.equal(zoom.fitScale(900, 600), 1.5)
})

await t('量不出宽高时返回 null，调用方据此保持原档', () => {
  assert.equal(zoom.fitScale(0, 743), null)
  assert.equal(zoom.fitScale(652, 0), null)
  assert.equal(zoom.fitScale(NaN, 743), null)
})

await t('适应宽度也要受上下限管住', () => {
  // 表格宽出容器太多：夹到下限，而不是缩成一条线
  assert.equal(zoom.fitScale(100, 100000), zoom.MIN_SCALE)
  // 表格比容器窄很多：夹到上限，而不是放大到点不中格子
  assert.equal(zoom.fitScale(100000, 100), zoom.MAX_SCALE)
})

await t('表格自然宽度 = 各列宽之和，这就是 min-width 的下限', () => {
  // 小企01表 8 列的真实列宽
  assert.equal(zoom.naturalWidth(
    [210, 46, 118, 118, 210, 46, 118, 118].map((width) => ({ width }))
  ), 984)
  // 列宽在模型里可空（Optional[int]），少一列不能把整张表的宽度拖成 NaN
  assert.equal(zoom.naturalWidth([{ width: 210 }, { width: null }, { width: 46 }]), 256)
  // 还没加载出列时是 0，min-width:0 相当于没设，表格不会凭空留白
  assert.equal(zoom.naturalWidth([]), 0)
  assert.equal(zoom.naturalWidth(undefined), 0)
  // 字符串列宽按数值累加，不能拼成 "0210118118..."
  assert.equal(zoom.naturalWidth([{ width: '210' }, { width: '46' }]), 256)
})

console.log('== 画布缩放：接进页面 ==')

await t('两层结构：占位层按缩放后尺寸，缩放层才做 transform', () => {
  // transform 不改变布局盒，光缩放的话滚动范围会按未缩放尺寸算
  assert.match(SRC, /class="canvas-box" :style="canvasBoxStyle"/,
    '少一层占位：缩小时右边空一大片、放大时底部被截断')
  assert.match(SRC, /class="canvas-zoom" :style="zoomLayerStyle"/)
  assert.match(SRC, /\.canvas-zoom \{[^}]*transform-origin: top left/,
    '原点不在左上，缩放后表格会整体偏移')
  assert.match(SRC, /\.canvas-box \{[^}]*overflow: hidden/,
    '占位层必须裁掉缩放后的溢出，否则表格会压到工具栏上')
  // 占位层的宽高都乘了 scale —— 只乘宽不乘高的话，底部会被截断
  assert.match(SRC, /tableBox\.w \* scale\.value/)
  assert.match(SRC, /tableBox\.h \* scale\.value/)
})

await t('缩放层按未缩放的可用宽排版，表格才不会一缩放就重排', () => {
  assert.match(SRC, /zoomLayerStyle[\s\S]{0,200}width: `\$\{availW\.value\}px`/,
    '层宽必须是未缩放的可用宽；跟着 scale 走会让表格反复折行，量出来的高度跟着跳')
})

await t('量测顺序：宽度变了要等 DOM 更新完再量第二遍', () => {
  assert.match(SRC, /availW\.value = w\s*\n\s*nextTick\(measure\)\s*\n\s*return/,
    '改完 availW 直接量表，量到的是上一版布局（踩过：面板一收就跳到 130%）')
  // 适应宽度是「一直铺满」，不是量一次定死
  assert.match(SRC, /if \(fitMode\.value\) fitToWidth\(\)/)
})

await t('容器与表格都要观察：少观察一个就留下半截状态', () => {
  assert.match(SRC, /canvasRO\.observe\(scrollHost\.value\)/, '窗口变了要重算')
  assert.match(SRC, /canvasRO\.observe\(zoomLayer\.value\)/, '行高变了要重算')
  assert.match(SRC, /onBeforeUnmount\(\(\) => \{[\s\S]{0,120}canvasRO\?\.disconnect\(\)/,
    '离开页面不 disconnect，观察器会一直量')
})

await t('缩放控件三件套都在，百分比能一键回 100%', () => {
  assert.match(SRC, /zoomBy\(-0\.1\)/)
  assert.match(SRC, /zoomBy\(0\.1\)/)
  assert.match(SRC, /Math\.round\(scale \* 100\) }}%/)
  assert.match(SRC, /function resetZoom\(\) \{[\s\S]{0,120}scale\.value = 1/)
})

await t('Ctrl/⌘ + 滚轮缩放，且不挡普通滚动', () => {
  assert.match(SRC, /function onWheel\(e\) \{[\s\S]{0,80}!e\.ctrlKey && !e\.metaKey[\s\S]{0,40}return/,
    '普通滚轮必须放行，否则表格没法上下翻')
  assert.match(SRC, /e\.preventDefault\(\)/)
})

await t('打印时把缩放还原：缩到 60% 的表打出来是看不清的', () => {
  assert.match(SRC, /@media print \{[\s\S]*\.canvas-zoom \{ transform: none !important/)
  assert.match(SRC, /@media print \{[\s\S]*\.canvas-box \{[^}]*overflow: visible/)
})

await t('表格先按自己的列宽站住，再交给画布整体缩', () => {
  // 少了 min-width 就是用户报的「支持缩放了，怎么还是这么挤压」：
  // 容器比列宽之和窄时浏览器把列压到两三个字，适应宽度缩小的
  // 只是一张已经被压扁的表——整张表看得全，可每列还是读不出字
  assert.match(SRC, /class="report-table"[\s\S]{0,160}minWidth: `\$\{tableMinWidth\}px`/,
    '表格必须挂 min-width，否则缩放只是把压扁的表整体缩小')
  // 取各列宽之和而不是写死一个数：改过列宽的模板得跟着变
  assert.match(SRC, /const tableMinWidth = computed\(\(\) => naturalWidth\(data\.value\.cols\)\)/)
})

console.log('== 公式面板可收起 ==')

await t('收起按钮跟着面板走，不再挂在设计提示条上', () => {
  assert.match(SRC, /const inspectorOpen = ref\(true\)/)
  // 收起是「关于这块面板本身」的操作，和「改这一格」不是一回事，
  // 所以摆在清空公式/应用修改那一行的左端
  assert.match(SRC, /class="inspector-actions">[\s\S]{0,400}class="inspector-toggle" @click="inspectorOpen = false"/)
  // 空状态（还没选单元格）也得能收起，否则刚进设计模式就没人收得掉
  assert.match(SRC, /class="inspector-empty">[\s\S]{0,400}class="inspector-toggle" @click="inspectorOpen = false"/)
  assert.doesNotMatch(SRC, /design-strip[\s\S]{0,400}inspector-toggle/,
    '按钮待在提示条上时既不挨着面板，也不挨着它要收起的东西')
  // 全局的 .spacer 只认 .toolbar/.topbar，这里不自己撑开的话
  // 收起按钮会跟「应用修改」贴在一起
  assert.match(SRC, /\.inspector-actions \.spacer \{ flex: 1; \}/)
})

await t('收起面板时画布真的拿到那 330px（不是只把 aside 藏起来）', () => {
  assert.match(SRC, /:class="\{ 'has-inspector': designMode && inspectorOpen \}"/,
    '布局列宽跟着面板走，面板藏了列还在，画布一点没变宽')
  assert.match(SRC, /<aside v-if="designMode && inspectorOpen"/)
})

await t('面板收起时点单元格会自动带出来，不把人堵死', () => {
  assert.match(SRC, /function onCellClick\([\s\S]{0,160}inspectorOpen\.value = true/)
})

console.log(`\n通过 ${pass} / 失败 ${fail}`)
// 必须关掉 vite server 再退出：留着它事件循环不结束，测试全绿时进程会挂住。
await server.close()
process.exit(fail === 0 ? 0 : 1)
