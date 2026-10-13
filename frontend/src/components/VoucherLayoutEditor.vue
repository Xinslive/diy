<template>
  <div class="vle">
    <!-- 控件面板 -->
    <div class="rail">
      <div class="rail-head">控件</div>
      <button v-for="b in PALETTE" :key="b.key" type="button" class="ctl" @click="addElement(b.key)">
        <i class="swatch" :class="b.key"></i>{{ b.label }}
      </button>

      <div class="rail-head gap">元素<span class="count">{{ elements.length }}</span></div>
      <ul class="elist">
        <li v-for="el in elements" :key="el.id"
            :class="{ on: el.id === selectedId, off: !el.visible }"
            @click="select(el.id)">
          <span class="ename">{{ elementName(el) }}</span>
          <span class="eops">
            <el-button link size="small" @click.stop="toggleVisible(el)">
              {{ el.visible ? '隐藏' : '显示' }}
            </el-button>
            <el-button link size="small" type="danger" @click.stop="removeElement(el)">删</el-button>
          </span>
        </li>
        <li v-if="!elements.length" class="empty">还没有控件，从上面挑一个加进来</li>
      </ul>
    </div>

    <!-- 画布 -->
    <div class="stage">
      <div class="stage-bar">
        <span class="stage-title">单张凭证</span>
        <span class="stage-tip">{{ sheetTip }}</span>
        <el-button v-if="selectedId" size="small" @click="resetSelected">复位</el-button>
      </div>
      <div class="canvas-wrap">
        <div ref="canvasRef" class="canvas" :class="{ dragging: !!drag }" :style="canvasStyle"
             @pointerdown.self="select(null)">
          <div v-for="el in elements" :key="el.id"
               class="el" :class="[el.type, { sel: el.id === selectedId, hidden: !el.visible }]"
               :style="boxStyle(el)"
               @pointerdown="startDrag($event, el, 'move')">
            <template v-if="el.type === 'text'">
              <span class="txt" :style="textStyle(el)">{{ renderText(el) }}</span>
            </template>
            <template v-else-if="el.type === 'table'">
              <div class="tmock" :class="{ noborder: el.border === false }"
                   :style="{ '--vhead': headPctOf(el) + '%', '--vrow': rowPctOf(el) + '%' }">
                <div v-if="el.show_header !== false" class="thead">
                  <span v-for="(c, i) in tpl.columns" :key="i" :style="{ width: c.width + '%' }">{{ c.label }}</span>
                </div>
                <div v-for="n in rowCount" :key="n" class="trow">
                  <span v-for="(c, i) in tpl.columns" :key="i" :style="{ width: c.width + '%' }"></span>
                </div>
                <div class="trow total">
                  <span v-for="(c, i) in tpl.columns" :key="i" :style="{ width: c.width + '%' }">
                    {{ i === labelAt ? labelOf(el) : '' }}
                  </span>
                </div>
              </div>
              <div v-if="el.id === selectedId" class="colhandles">
                <i v-for="(b, i) in colBounds" :key="i" class="ch" :style="{ left: b + '%' }"
                   @pointerdown.stop="startColDrag($event, i)"></i>
              </div>
            </template>
            <template v-else-if="el.type === 'line'"></template>
            <template v-else></template>

            <i v-if="el.id === selectedId" class="handle"
               @pointerdown.stop="startDrag($event, el, 'resize')"></i>
          </div>
          <div v-if="!elements.length" class="canvas-empty">从左侧控件面板添加元素</div>
        </div>
      </div>
    </div>

    <!-- 属性 -->
    <div class="props">
      <div class="rail-head">属性</div>
      <template v-if="cur">
        <el-form label-width="46px" label-position="left" size="small">
          <el-form-item label="内容">
            <el-select v-if="cur.type === 'text'" v-model="cur.bind" filterable style="width: 100%"
                       @change="onBindChange">
              <el-option v-for="f in FIELDS" :key="f.value" :label="f.label" :value="f.value" />
            </el-select>
            <span v-else class="ro">{{ TYPE_CN[cur.type] }}</span>
          </el-form-item>
          <el-form-item v-if="cur.type === 'text'" label="文字">
            <el-input v-model="cur.text" type="textarea" :rows="2"
                      placeholder="可直接输入；用 {字段} 插入数据" @input="changed" />
          </el-form-item>
          <el-form-item v-if="cur.type === 'text'" label="字号">
            <el-input-number v-model="cur.font_size" :min="6" :max="40" :step="1"
                             :controls="false" style="width: 74px" @change="changed" />
            <el-checkbox v-model="cur.bold" class="ml" @change="changed">粗</el-checkbox>
            <el-select v-model="cur.align" size="small" class="ml" style="width: 62px" @change="changed">
              <el-option label="左" value="left" />
              <el-option label="中" value="center" />
              <el-option label="右" value="right" />
            </el-select>
          </el-form-item>

          <!-- 拆页后只在尾页出现的开关。合计大写默认开着：一张凭证拆成几张之后
               只有最后一张是完整的，每张都印合计会让人以为是整张凭证的合计。 -->
          <el-form-item v-if="cur.type === 'text'" label="尾页">
            <el-checkbox :model-value="lastPageOnly(cur)" @change="setLastPageOnly">
              仅在尾页显示
            </el-checkbox>
          </el-form-item>

          <template v-if="cur.type === 'table'">
            <div class="rail-head gap">表格</div>
            <div class="grid2">
              <label>行高(mm)
                <input :value="rowMm.toFixed(1)" type="number" min="3" max="60" step="0.5"
                       @change="setRowMm($event.target.value)" />
              </label>
              <label>分录行数
                <input :value="rowCount" type="number" min="1" max="5" step="1"
                       @change="setRows($event.target.value)" />
              </label>
            </div>
            <div class="tip-line">分录行与合计行等高，改行高等于改表格高度（最多 5 行）。</div>

            <div class="rail-head gap">表头</div>
            <div class="grid2">
              <label>显示表头
                <input type="checkbox" :checked="cur.show_header !== false"
                       @change="setShowHeader($event.target.checked)" />
              </label>
              <label>列头高(mm)
                <input :value="headerMm.toFixed(1)" type="number" min="0" max="60" step="0.5"
                       :disabled="cur.show_header === false"
                       @change="setHeaderMm($event.target.value)" />
              </label>
            </div>
            <div class="tip-line">不显示表头时，剩下的高度全部分给分录行和合计行。</div>

            <div class="rail-head gap">合计行</div>
            <div class="colw">
              <label>
                <span>合计文字</span>
                <input :value="labelOf(cur)" type="text" maxlength="8"
                       @change="setTotalLabel($event.target.value)" />
              </label>
            </div>
            <div class="rail-head gap">列宽（%）</div>
            <div class="colw">
              <label v-for="(c, i) in tpl.columns" :key="i">
                <span>{{ c.label || c.key }}</span>
                <input :value="c.width" type="number" min="4" max="80" step="0.5"
                       @change="setColWidth(i, $event.target.value)" />
              </label>
            </div>
            <div class="tip-line">也可以直接在画布上左右拖动列分隔线。</div>
          </template>

          <template v-if="cur.type === 'line' || cur.type === 'rectangle'">
            <div class="rail-head gap">边框</div>
            <div class="grid2">
              <label>线宽(mm)
                <input :value="cur.line_width ?? 0.3" type="number" min="0.1" max="3" step="0.1"
                       @change="setLineWidth($event.target.value)" />
              </label>
              <label>线型
                <select :value="cur.line_style || 'solid'" @change="setLineStyle($event.target.value)">
                  <option value="solid">实线</option>
                  <option value="dashed">虚线</option>
                  <option value="dotted">点线</option>
                </select>
              </label>
            </div>
          </template>

          <div class="rail-head gap">位置与尺寸（%）</div>
          <div class="grid2">
            <label>X<input v-model.number="cur.x" type="number" min="0" max="100" step="0.5" @change="clampXY(cur)" /></label>
            <label>Y<input v-model.number="cur.y" type="number" min="0" max="100" step="0.5" @change="clampXY(cur)" /></label>
            <label>宽<input v-model.number="cur.w" type="number" min="1" max="100" step="0.5" @change="clampWH(cur)" /></label>
            <label>高<input v-model.number="cur.h" type="number" min="1" max="100" step="0.5" @change="clampWH(cur)" /></label>
          </div>
          <div class="row-btns">
            <el-button size="small" @click="alignH('left')">靠左</el-button>
            <el-button size="small" @click="alignH('center')">水平居中</el-button>
            <el-button size="small" @click="alignH('right')">靠右</el-button>
            <el-button size="small" @click="alignV('top')">靠上</el-button>
            <el-button size="small" @click="alignV('middle')">垂直居中</el-button>
            <el-button size="small" @click="alignV('bottom')">靠下</el-button>
          </div>
          <!-- 表格的这个开关管的是框线，不是显不显示：表格要是能被整个藏掉，
               「显示/隐藏」就得挪到元素列表去，那儿已经有了。 -->
          <el-form-item v-if="cur.type === 'table'" label="框线">
            <el-switch :model-value="cur.border !== false" @change="setBorder" />
          </el-form-item>
          <el-form-item v-else label="显示">
            <el-switch v-model="cur.visible" @change="changed" />
          </el-form-item>
          <el-button size="small" type="danger" plain style="width: 100%" @click="removeElement(cur)">
            删除该控件
          </el-button>
        </el-form>
      </template>
      <div v-else class="props-empty">选中画布上的控件后在这里调属性</div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  tpl: { type: Object, required: true },
  /** 每页几张凭证，由纸型决定 */
  slots: { type: Number, default: 2 },
})
const emit = defineEmits(['change'])

const PALETTE = [
  { key: 'text', label: '文本' },
  { key: 'table', label: '表格' },
  { key: 'line', label: '线条' },
  { key: 'rectangle', label: '矩形' },
]
const TYPE_CN = { text: '文本', table: '表格', line: '线条', rectangle: '矩形' }
const FIELDS = [
  { value: 'company', label: '单位' }, { value: 'book', label: '账套' },
  { value: 'period', label: '期间' }, { value: 'voucher_no', label: '凭证字号' },
  { value: 'date', label: '日期' }, { value: 'attachments', label: '附单据' },
  { value: 'accountant', label: '制单' }, { value: 'checker', label: '审核' },
  { value: 'cashier', label: '出纳' }, { value: 'remark', label: '备注' },
  { value: 'capital', label: '合计大写' }, { value: 'page', label: '页码' },
  { value: 'total', label: '总页数' }, { value: 'custom', label: '自定义' },
]
const FIELD_CN = Object.fromEntries(FIELDS.map((f) => [f.value, f.label]))

/** 画布上的示例值，让画布看起来和真打印一致。 */
const SAMPLE = {
  title: '记 账 凭 证', subtitle: '', company: '紫欣星悦荟', book: '租金账套',
  // 画布上的示例值要跟打印出来的一致：日期是中文写法，凭证号带分页号
  period: '2026-01', voucher_no: '记001号 - 001/001', date: '2026年01月05日', attachments: '2',
  accountant: '', checker: '', cashier: '', remark: '首月租金',
  capital: '壹仟贰佰叁拾肆元伍角陆分', page: '1', total: '1', custom: '',
}

// 表头占表格高度的比例，与后端 VOUCHER_HEAD_PCT 保持一致；
// 不一致的话画布上算出的行高和打印出来的对不上。
const HEAD_PCT = 14
const MIN_COL_PCT = 4

const elements = computed(() => props.tpl.elements || [])
const selectedId = ref(null)
const cur = computed(() => elements.value.find((e) => e.id === selectedId.value) || null)
const rowCount = computed(() => {
  const raw = (props.tpl.voucher || {}).rows_per_page
  return Math.max(1, Math.min(5, Number(raw) || 5))
})

/**
 * 分录行高（mm）。行高不是独立配置项，而是由表格元素的高度推导出来的 ——
 * 两者必须是同一个数，存两份必然会打架。
 * 表头有没有、占多高也是模板的一部分，所以行高按**元素**算而不是按全局算：
 * 一张凭证上摆两个表格，各自定义表头高度时画布和打印才对得上。
 */
function headPctOf(el) {
  if (!el || el.show_header === false) return 0
  const v = Number(el.header_height)
  return Number.isFinite(v) && v >= 0 ? clamp(v, 0, 100) : HEAD_PCT
}
function rowPctOf(el) {
  return (100 - headPctOf(el)) / (rowCount.value + 1)
}
/** 表格元素占槽位多高（mm）—— 毫米和百分比之间就靠它换算。 */
function tableMm(el) {
  return slotMm.value.h * (Number(el?.h) || 0) / 100
}
const rowMm = computed(() => {
  const c = cur.value
  if (!c || c.type !== 'table') return 0
  return tableMm(c) * rowPctOf(c) / 100
})
const headerMm = computed(() => {
  const c = cur.value
  if (!c || c.type !== 'table') return 0
  return tableMm(c) * headPctOf(c) / 100
})

/** 合计行那个标签落在哪一列 —— 与后端 _voucher_total_row 同一个算法。 */
const labelAt = computed(() => {
  const cols = props.tpl.columns || []
  const dr = cols.findIndex((c) => c.key === 'debit')
  return dr > 0 ? dr - 1 : Math.max(0, cols.length - 3)
})
function labelOf(el) { return el?.total_label ?? '合　计' }

/** 每列的右边界（累计百分比），画布上拿它放拖拽把手。 */
const colBounds = computed(() => {
  const cols = props.tpl.columns || []
  let acc = 0
  return cols.slice(0, -1).map((c) => (acc += Number(c.width) || 0))
})

/** 一页几张 + 横竖向 -> 槽位在纸上的摆放方式，与后端 render_voucher_sheet 保持一致。 */
const sheetTip = computed(() => {
  if (props.slots === 1) return '当前：每页 1 张凭证'
  return props.tpl.orientation === 'landscape'
    ? '当前：A4 横向，每页 2 张（左右排列）'
    : '当前：A4 纵向，每页 2 张（上下排列）'
})

// ------------------------------------------------------------------ 画布几何
const canvasRef = ref(null)
const canvasH = ref(360)
const drag = ref(null)
let ro = null

const PAPERS = { A4: [210, 297], A3: [297, 420], A5: [148, 210], B5: [176, 250], CONTINUOUS: [240, 140] }

/** 单个槽位的真实毫米尺寸 —— 画布要按它算比例，否则拖出来的位置不是打印位置。 */
const slotMm = computed(() => {
  const [pw, ph] = PAPERS[props.tpl.paper] || PAPERS.A4
  const land = props.tpl.orientation === 'landscape'
  const w = land ? ph : pw
  const h = land ? pw : ph
  const m = props.tpl.margins || {}
  const cw = w - (Number(m.left) || 0) - (Number(m.right) || 0)
  const ch = h - (Number(m.top) || 0) - (Number(m.bottom) || 0)
  const gap = props.slots === 2 ? 3 : 0
  return { w: cw, h: (ch - gap * (props.slots - 1)) / props.slots, gap }
})
const canvasStyle = computed(() => ({ aspectRatio: `${slotMm.value.w} / ${slotMm.value.h}` }))
/** 毫米 -> 画布像素，用来把 pt 字号缩放到画布上。 */
const ptToPx = computed(() => 1.3333 * (canvasH.value / (slotMm.value.h * 3.7795)))

function boxStyle(el) {
  const s = { left: el.x + '%', top: el.y + '%', width: el.w + '%', height: el.h + '%' }
  // 线条/矩形的线宽线型要在画布上如实反映，否则画布和打印对不上
  if (el.type === 'line' || el.type === 'rectangle') {
    const w = (el.line_width ?? 0.3) * 3.78   // mm -> px，和后端 96dpi 换算一致
    s.borderColor = '#555'
    s.borderStyle = el.line_style || 'solid'
    if (el.type === 'line') s.borderTop = `${w}px ${s.borderStyle} ${s.borderColor}`
    else s.border = `${w}px ${s.borderStyle} ${s.borderColor}`
  }
  return s
}
function textStyle(el) {
  return {
    fontSize: Math.max(6, el.font_size * ptToPx.value) + 'px',
    textAlign: el.align || 'left',
    fontWeight: el.bold ? 700 : 400,
  }
}

function renderText(el) {
  let out = el.text || ''
  for (const [k, v] of Object.entries(SAMPLE)) out = out.split('{' + k + '}').join(v)
  return out
}

// ------------------------------------------------------------------ 选中 / 拖拽
function select(id) { selectedId.value = id }

function startDrag(e, el, mode) {
  e.preventDefault()
  selectedId.value = el.id
  const rect = canvasRef.value.getBoundingClientRect()
  drag.value = {
    id: el.id, mode, rect,
    sx: e.clientX, sy: e.clientY,
    x: el.x, y: el.y, w: el.w, h: el.h,
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}

function onMove(e) {
  const d = drag.value
  const el = elements.value.find((x) => x.id === d.id)
  if (!el) return
  const dx = (e.clientX - d.sx) / d.rect.width * 100
  const dy = (e.clientY - d.sy) / d.rect.height * 100
  if (d.mode === 'move') {
    el.x = clamp(d.x + dx, 0, 100 - el.w)
    el.y = clamp(d.y + dy, 0, 100 - el.h)
  } else {
    el.w = clamp(d.w + dx, 2, 100 - el.x)
    el.h = clamp(d.h + dy, 1, 100 - el.y)
  }
  emit('change')
}

function onUp() {
  drag.value = null
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  emit('change')
}

function clamp(n, lo, hi) { return Math.round(Math.max(lo, Math.min(hi, n || 0)) * 10) / 10 }

// ------------------------------------------------------------------ 表格行列
function setRowMm(mm) {
  const c = cur.value
  const v = Number(mm)
  if (!c || c.type !== 'table' || !(v > 0)) return
  // 行 mm = 槽位高 × 元素高% × 行占比%，反解出元素高度百分比
  c.h = clamp((v * 10000) / (slotMm.value.h * rowPctOf(c)), 5, 100 - c.y)
  changed()
}

/**
 * 列头高度是「占表格高度的比例」，改它只在这张表格内部重新分配 ——
 * 表格本身不缩放，所以不会把别的元素挤出槽位，分录行会自动补上差的那截。
 */
function setHeaderMm(mm) {
  const c = cur.value
  const v = Number(mm)
  const tableH = tableMm(c)
  if (!c || c.type !== 'table' || !(v >= 0) || !(tableH > 0)) return
  c.header_height = clamp((v * 100) / tableH, 0, 100)
  changed()
}

function setShowHeader(v) { if (cur.value) { cur.value.show_header = !!v; changed() } }

/** 老模板里没有这个字段，按绑定字段给默认值 —— 与后端 validate_template 同一套规则。 */
function lastPageOnly(el) {
  return el?.last_page_only ?? (el?.bind === 'capital')
}
function setLastPageOnly(v) { if (cur.value) { cur.value.last_page_only = !!v; changed() } }

function setTotalLabel(v) { if (cur.value) { cur.value.total_label = String(v ?? ''); changed() } }

function setRows(v) {
  const n = Math.round(Number(v))
  if (!n || n < 1) return
  props.tpl.voucher = { ...(props.tpl.voucher || {}), rows_per_page: clamp(n, 1, 5) }
  changed()
}

function setColWidth(i, v) {
  const cols = props.tpl.columns
  const n = Number(v)
  if (!cols[i] || !(n > 0)) return
  cols[i].width = clamp(n, MIN_COL_PCT, 80)
  normalizeWidths()
  changed()
}

/** 列宽是百分比，改完必须重新摊到 100%，否则打印出来总宽就对不上。 */
function normalizeWidths() {
  const cols = props.tpl.columns || []
  const total = cols.reduce((s, c) => s + (Number(c.width) || 0), 0) || 1
  cols.forEach((c) => { c.width = Math.round((Number(c.width) || 0) / total * 1000) / 10 })
}

// ---- 画布上左右拖列分隔线 ----
let colDrag = null

function startColDrag(e, idx) {
  e.preventDefault()
  const el = cur.value
  if (!el) return
  const rect = canvasRef.value.getBoundingClientRect()
  colDrag = { idx, sx: e.clientX, rect, widths: (props.tpl.columns || []).map((c) => Number(c.width) || 0) }
  window.addEventListener('pointermove', onColMove)
  window.addEventListener('pointerup', onColUp)
}

function onColMove(e) {
  if (!colDrag) return
  const cols = props.tpl.columns
  const { idx, widths } = colDrag
  let dx = (e.clientX - colDrag.sx) / colDrag.rect.width * 100
  // 右边那一列跟着反向变化，总宽守恒
  dx = Math.max(MIN_COL_PCT - widths[idx], Math.min(dx, widths[idx + 1] - MIN_COL_PCT))
  cols[idx].width = Math.round((widths[idx] + dx) * 10) / 10
  cols[idx + 1].width = Math.round((widths[idx + 1] - dx) * 10) / 10
  emit('change')
}

function onColUp() {
  colDrag = null
  window.removeEventListener('pointermove', onColMove)
  window.removeEventListener('pointerup', onColUp)
  normalizeWidths()
  emit('change')
}

// ------------------------------------------------------------------ 增删改
let seq = 0
function addElement(kind) {
  const id = 'el-' + Date.now().toString(36) + '-' + (seq++)
  const base = { id, x: 10, y: 10, w: 40, h: 6, font_size: 9, bold: false,
                 align: 'left', visible: true, border: true }
  const el = { ...base, type: 'text', bind: 'custom', text: '新文本' }
  if (kind === 'table') { Object.assign(el, { type: 'table', bind: 'entries', text: '', x: 3, y: 24, w: 94, h: 55 }) }
  if (kind === 'line') {
    Object.assign(el, { type: 'line', bind: 'custom', text: '', x: 5, y: 50, w: 90, h: 1,
                       line_width: 0.3, line_style: 'solid' })
  }
  if (kind === 'rectangle') {
    Object.assign(el, { type: 'rectangle', bind: 'custom', text: '', x: 20, y: 30, w: 50, h: 25,
                        line_width: 0.3, line_style: 'solid' })
  }
  elements.value.push(el)
  selectedId.value = id
  emit('change')
}

function removeElement(el) {
  const i = elements.value.findIndex((x) => x.id === el.id)
  if (i < 0) return
  elements.value.splice(i, 1)
  if (selectedId.value === el.id) selectedId.value = null
  emit('change')
}

function toggleVisible(el) { el.visible = !el.visible; emit('change') }

/** 表格的框线开关。旧模板没有 border 字段，按「有框线」处理。 */
/** 线条/矩形的线宽线型。老模板没这两个字段，按 0.3mm 实线处理。 */
function setLineWidth(v) {
  const n = Number(v)
  if (cur.value) { cur.value.line_width = Math.min(3, Math.max(0.1, n || 0.3)); changed() }
}
function setLineStyle(v) { if (cur.value) { cur.value.line_style = v || 'solid'; changed() } }

function setBorder(v) { if (cur.value) { cur.value.border = !!v; emit('change') } }
function changed() { emit('change') }

function onBindChange(bind) {
  // 换字段时把占位符一起换掉，否则文字里会留着上一个字段的 {…}。
  const def = { voucher_no: '凭证字号：{voucher_no}', date: '日期：{date}', remark: '备注：{remark}',
                company: '单位：{company}', period: '期间：{period}', attachments: '附单据：{attachments}',
                capital: '合计大写：{capital}', page: '第 {page} 页', total: '共 {total} 页' }[bind]
  if (def) cur.value.text = def
  else if (bind === 'custom') cur.value.text = '自定义文字'
  emit('change')
}

const NAME_BY_ID = { title: '标题', subtitle: '副标题', footer: '页脚', table: '分录表格' }

function elementName(el) {
  if (NAME_BY_ID[el.id]) return NAME_BY_ID[el.id]
  if (el.type === 'table') return '分录表格'
  if (el.type === 'line') return '线条'
  if (el.type === 'rectangle') return '矩形'
  if (el.bind && el.bind !== 'custom') return FIELD_CN[el.bind] || el.bind
  return (el.text || '文本').slice(0, 8)
}

function clampXY(el) { el.x = clamp(el.x, 0, 100 - el.w); el.y = clamp(el.y, 0, 100 - el.h); changed() }
function clampWH(el) { el.w = clamp(el.w, 1, 100 - el.x); el.h = clamp(el.h, 1, 100 - el.y); changed() }
function alignH(pos) {
  if (!cur.value) return
  cur.value.x = pos === 'left' ? 0 : pos === 'right' ? 100 - cur.value.w : (100 - cur.value.w) / 2
  changed()
}
function alignV(pos) {
  if (!cur.value) return
  cur.value.y = pos === 'top' ? 0 : pos === 'bottom' ? 100 - cur.value.h : (100 - cur.value.h) / 2
  changed()
}
function resetSelected() {
  if (!cur.value) return
  Object.assign(cur.value, { x: 10, y: 10, w: 40, h: 6 })
  changed()
}

function measure() {
  if (canvasRef.value) canvasH.value = canvasRef.value.getBoundingClientRect().height || 360
}
onMounted(() => {
  measure()
  ro = new ResizeObserver(measure)
  if (canvasRef.value) ro.observe(canvasRef.value)
})
onBeforeUnmount(() => {
  ro?.disconnect()
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  onColUp()
})
watch(() => [props.tpl.paper, props.tpl.orientation, props.slots, props.tpl.margins], () => {
  requestAnimationFrame(measure)
})
</script>

<style scoped>
.vle { display: grid; grid-template-columns: 152px minmax(0, 1fr) 226px; gap: 10px; align-items: stretch; }

.rail, .props { display: flex; flex-direction: column; gap: 6px; }
.rail-head { font-size: 12px; color: #909399; display: flex; align-items: center; gap: 6px; }
.rail-head.gap { margin-top: 8px; }
.rail-head .count { color: #c0c4cc; }

.ctl {
  display: flex; align-items: center; gap: 7px; width: 100%;
  padding: 7px 9px; border: 1px solid var(--cwq-border); border-radius: 4px;
  background: #fff; color: #303133; font-size: 13px; cursor: pointer; text-align: left;
}
.ctl:hover { border-color: var(--el-color-primary); color: var(--el-color-primary); }
.swatch { width: 14px; height: 14px; flex: none; border: 1px solid #c0c4cc; background: #fff; }
.swatch.table { background: repeating-linear-gradient(#a0cfff 0 1px, transparent 1px 4px), #fff; }
.swatch.line { height: 0; border-top: 2px solid #606266; border-left: 0; }
.swatch.rectangle { background: #f6f7f9; }

.elist { list-style: none; margin: 0; padding: 0; overflow: auto; max-height: 340px; }
.elist li {
  display: flex; align-items: center; justify-content: space-between; gap: 4px;
  padding: 4px 6px; border-radius: 3px; font-size: 12px; cursor: pointer; border: 1px solid transparent;
}
.elist li:hover { background: #f4f4f5; }
.elist li.on { background: #ecf5ff; border-color: var(--el-color-primary); color: var(--el-color-primary); }
.elist li.off .ename { text-decoration: line-through; color: #c0c4cc; }
.elist .ename { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.elist .empty { color: #c0c4cc; cursor: default; justify-content: flex-start; }
.eops { flex: none; }

.stage { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.stage-bar { display: flex; align-items: center; gap: 10px; font-size: 12px; color: #909399; }
.stage-title { color: #303133; font-weight: 600; }
.stage-tip { flex: 1; }
.canvas-wrap { background: #fff; padding: 14px; border-radius: 4px; overflow: auto; }
/* 用高度定尺寸、让 aspect-ratio 反推宽度：不同纸型宽高比差很多（A4 纵向上下排是
   接近正方，A5 单张是竖长条），按宽度铺会把 A5 撑成一屏高的长条。 */
.canvas {
  position: relative; margin: 0 auto; background: #fff;
  height: clamp(300px, 56vh, 600px); width: auto; max-width: 100%;
  box-shadow: 0 1px 6px rgba(0, 0, 0, .14); user-select: none;
}
.canvas.dragging { cursor: grabbing; }
.canvas-empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #c0c4cc; font-size: 13px; }

.el { position: absolute; box-sizing: border-box; }
.el.text { padding: 0 1mm; overflow: hidden; }
.el.line, .el.rectangle { border-color: #555; }
.el.hidden { opacity: .32; }
.el.sel { outline: 1px solid var(--el-color-primary); outline-offset: 0; cursor: move; }
.el .handle {
  position: absolute; right: -4px; bottom: -4px; width: 8px; height: 8px;
  background: var(--el-color-primary); cursor: nwse-resize; display: block;
}
.txt { display: block; line-height: 1.3; white-space: pre-wrap; word-break: break-word; }

/* 行高用 flex-basis 写死百分比，而不是 flex:1 让浏览器分空间 —— 后者会多算
   合计行那条 border-top，画布上「每行等高」就成了假的。变量名和值都跟后端一致。 */
.tmock { height: 100%; display: flex; flex-direction: column; border: 1px solid #333;
  box-sizing: border-box; }
.tmock .thead, .tmock .trow { flex: 0 0 auto; }
.tmock .thead { flex-basis: var(--vhead, 14%); }
.tmock .trow { flex-basis: var(--vrow, 14.3333%); box-sizing: border-box; }
.colhandles { position: absolute; inset: 0; }
.colhandles .ch {
  position: absolute; top: 0; bottom: 0; width: 7px; margin-left: -3.5px;
  cursor: col-resize; display: block;
}
.colhandles .ch:hover { background: rgba(64, 158, 255, .35); }
.tmock .thead, .tmock .trow { display: flex; }
/* box-sizing 必须显式给：合计行多一条 border-top，不这么写它会比别的行高 1px。 */
.tmock .thead span, .tmock .trow span {
  border-right: 1px solid #999; border-bottom: 1px solid #999; padding: 1px 2px;
  font-size: 9px; overflow: hidden; box-sizing: border-box;
}
.tmock .thead span { border-top: 0; }
.tmock .thead { border-bottom: 1px solid #999; }
/* 这里不能再出现 flex:1 —— 它会覆盖上面的 flex-basis，把行高又变回"浏览器分空间"。 */
.tmock .trow { min-height: 0; }
.tmock .trow span { border-bottom: 0; }
.tmock .trow.total { font-weight: 700; border-top: 1px solid #999; }
.tmock .trow.total span { border-bottom: 1px solid #999; }
/* 关框线只是把线变透明，格子本身和行高都留着 */
.tmock.noborder, .tmock.noborder .thead, .tmock.noborder .thead span,
.tmock.noborder .trow, .tmock.noborder .trow span,
.tmock.noborder .trow.total { border-color: transparent; }

.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 5px 6px; margin-bottom: 8px; }
.grid2 label { display: flex; align-items: center; gap: 4px; font-size: 12px; color: #606266; }
.grid2 input {
  width: 100%; min-width: 0; padding: 3px 5px; font-size: 12px;
  border: 1px solid var(--cwq-border); border-radius: 3px;
}
/* 复选框跟着文字走，别被上面的 width:100% 拉成一整行 */
.grid2 input[type=checkbox] { width: auto; margin: 0; }
.grid2 input:disabled { background: #f5f7fa; color: #c0c4cc; }
.row-btns { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }
.row-btns :deep(.el-button) { margin-left: 0; padding: 5px 8px; }
.colw { display: flex; flex-direction: column; gap: 4px; }
.colw label { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #606266; }
.colw label > span { flex: none; width: 58px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.colw input {
  width: 100%; min-width: 0; padding: 3px 5px; font-size: 12px;
  border: 1px solid var(--cwq-border); border-radius: 3px;
}
.tip-line { font-size: 11px; color: #909399; line-height: 1.6; margin: 2px 0 4px; }
.props-empty { color: #c0c4cc; font-size: 12px; line-height: 1.8; }
.ro { color: #909399; font-size: 12px; }
.ml { margin-left: 8px; }
@media (max-width: 1100px) {
  .vle { grid-template-columns: 132px minmax(0, 1fr); }
  .props { grid-column: 1 / -1; }
}
</style>
