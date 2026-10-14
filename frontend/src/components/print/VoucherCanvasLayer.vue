<template>
  <!-- 槽位框：这一张凭证在纸上的活动范围 -->
  <div class="slot" :style="slotStyle" @pointerdown.self="emit('select', null)">
    <div class="slot-label">
      <span v-if="slots > 1">第 {{ activeSlot + 1 }} 张 / 共 {{ slots }} 张</span>
    </div>

    <div
      v-for="el in elements"
      :key="el.id"
      class="el"
      :class="{ sel: el.id === selectedId, off: !el.visible, typing: editingTextId === el.id }"
      :style="boxStyle(el)"
      @pointerdown="startDrag($event, el, 'move')"
      @dblclick.stop="startTextEdit(el)"
    >
      <span class="tag">{{ typeName(el) }} · {{ el.bind && el.bind !== 'custom' ? bindLabel(el.bind) : '自定义' }}</span>

      <!-- 表格：列分隔线直接在纸上拖 -->
      <template v-if="el.type === 'table' && el.id === selectedId">
        <i
          v-for="(b, i) in colBounds"
          :key="i"
          class="colgrip"
          :style="{ left: b + '%' }"
          :title="`拖动调整「${columns[i]?.label || columns[i]?.key}」的列宽`"
          @pointerdown.stop="startColDrag($event, el, i)"
        />
      </template>

      <!-- 选中框的四个角把手：拖角改尺寸 -->
      <template v-if="el.id === selectedId">
        <i v-for="(pos, i) in (el.type === 'line' ? ['nw', 'ne'] : CORNERS)" :key="i" class="grip" :class="pos" @pointerdown.stop="startDrag($event, el, pos)" />
      </template>
    </div>

    <div v-if="!elements.length" class="slot-empty">
      这一张凭证还没有内容 —— 左边「元素」页签里挑一个加进来
    </div>

    <!-- 就地改文字：双击后输入框直接盖在纸上 -->
    <div
      v-if="editingTextId"
      class="inline-edit"
      :style="boxStyle(currentEdit)"
      @pointerdown.stop
    >
      <textarea
        ref="ta"
        v-model="editText"
        rows="2"
        placeholder="用 {字段} 插入数据，如 凭证字号：{voucher_no}"
        @keydown.esc.prevent="cancelTextEdit"
        @keydown.meta.enter="endTextEdit"
        @keydown.ctrl.enter="endTextEdit"
        @blur="endTextEdit"
      />
      <div class="inline-tip">
        <span>⌘ / Ctrl + Enter 保存 · Esc 取消</span>
        <button type="button" @pointerdown.prevent @click="endTextEdit">完成</button>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * 凭证元素编辑层：**直接贴在真实预览纸上**。
 *
 * 以前这块画布是一个「自己用 div 画一张长得像凭证的假纸」，
 * 画布上摆的位置和打印出来的不一定对得上，用户只能将信将疑。
 * 现在这层覆盖在真实预览（后端渲染的那张纸）上：元素框画在哪，
 * 打印出来就在哪 —— 拖一下就是真的在改版式，而不是在改一张示意图。
 *
 * 坐标：元素 x/y/w/h 是**相对槽位的百分比**，与后端 .voucher-element 一致；
 * 槽位矩形由 PrintStage 按后端 .voucher-sheet 的 grid 算好后传入。
 * 所以这里不做任何 mm/px 换算 —— 换算就是错位的来源。
 */
import { computed, nextTick, onBeforeUnmount, ref, useTemplateRef, watch } from 'vue'

const props = defineProps({
  elements: { type: Array, default: () => [] },
  columns: { type: Array, default: () => [] },
  /** PrintStage 算好的槽位矩形。 */
  slotRects: { type: Array, default: () => [] },
  slots: { type: Number, default: 1 },
  activeSlot: { type: Number, default: 0 },
  selectedId: { type: [String, Number], default: null },
  canWrite: { type: Boolean, default: true },
  /** 外层纸面的屏幕缩放比；拖拽位移要先还原回纸坐标。 */
  scale: { type: Number, default: 1 },
})
const emit = defineEmits(['change', 'select'])

const CORNERS = ['nw', 'ne', 'sw', 'se']
const TYPE_CN = { text: '文本', table: '表格', line: '线条', rectangle: '矩形' }
const FIELD_CN = {
  company: '单位', book: '账套', period: '期间', voucher_no: '凭证字号',
  date: '日期', attachments: '附单据', accountant: '制单', checker: '审核',
  cashier: '出纳', remark: '备注', capital: '合计大写', page: '页码',
  total: '总页数', custom: '自定义', entries: '分录',
}
const typeName = (el) => TYPE_CN[el.type] || el.type
const bindLabel = (b) => FIELD_CN[b] || b

const slotStyle = computed(() => {
  const r = props.slotRects[props.activeSlot] || props.slotRects[0]
  if (!r) return { display: 'none' }
  return { left: r.x + 'px', top: r.y + 'px', width: r.w + 'px', height: r.h + 'px' }
})

/** 百分比坐标换算成槽位内像素，父容器负责在纸面上定位槽位。 */
function boxStyle(el) {
  const r = props.slotRects[props.activeSlot] || props.slotRects[0]
  if (!r) return { display: 'none' }
  const w = (Number(el.w) || 0) / 100 * r.w
  const h = (Number(el.h) || 0) / 100 * r.h
  // .el 是 .slot 的子节点，槽位左上角已经由 slotStyle 放置过了；
  // 这里必须只用槽位内坐标，否则会把内容区的 x/y 再加一遍。
  return {
    left: (Number(el.x) || 0) / 100 * r.w + 'px',
    top: (Number(el.y) || 0) / 100 * r.h + 'px',
    width: w + 'px',
    height: el.type === 'line' ? (Number(el.line_width) || 0.3) + 'mm' : h + 'px',
  }
}

/** 列的累计宽度（百分比），纸上画分隔线用。 */
const colBounds = computed(() => {
  let acc = 0
  return (props.columns || []).slice(0, -1).map((c) => (acc += Number(c.width) || 0))
})

const clamp = (n, lo, hi) => Math.round(Math.max(lo, Math.min(hi, Number(n) || 0)) * 10) / 10

// ------------------------------------------------------------------ 拖拽
let drag = null

function startDrag(e, el, mode) {
  if (!props.canWrite) return
  e.preventDefault()
  emit('select', el.id)
  const r = props.slotRects[props.activeSlot] || props.slotRects[0]
  if (!r) return
  drag = {
    id: el.id, mode, w: r.w, h: r.h, pointerId: e.pointerId, moved: false,
    scale: props.scale || 1,
    sx: e.clientX, sy: e.clientY,
    x: Number(el.x) || 0, y: Number(el.y) || 0,
    ew: Number(el.w) || 0, eh: Number(el.h) || 0,
  }
  e.currentTarget.setPointerCapture?.(e.pointerId)
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}

function onMove(e) {
  const d = drag
  if (!d || e.pointerId !== d.pointerId || !(e.buttons & 1)) return
  if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 3) return
  d.moved = true
  const el = props.elements.find((x) => x.id === d.id)
  if (!el) return
  const dx = (e.clientX - d.sx) / d.scale / d.w * 100
  const dy = (e.clientY - d.sy) / d.scale / d.h * 100
  if (d.mode === 'move') {
    const nx = clamp(d.x + dx, 0, 100 - d.ew)
    const ny = clamp(d.y + dy, 0, 100 - d.eh)
    Object.assign(el, snap(nx, ny, el))
  } else {
    if (d.mode.includes('w')) {
      el.x = clamp(d.x + dx, 0, d.x + d.ew - 2)
      el.w = clamp(d.x + d.ew - el.x, 2, 100 - el.x)
    } else el.w = clamp(d.ew + dx, 2, 100 - d.x)
    if (el.type !== 'line') {
      if (d.mode.includes('n')) {
        el.y = clamp(d.y + dy, 0, d.y + d.eh - 1)
        el.h = clamp(d.y + d.eh - el.y, 1, 100 - el.y)
      } else el.h = clamp(d.eh + dy, 1, 100 - d.y)
    }
  }
  emit('change')
}

/**
 * 贴边吸附：左/中/右对槽位的左/中/右，上/中/下对槽位的上/中/下。
 *
 * 这一步是「直接在纸上拖」和「对着示意图拖」最直观的差别 ——
 * 没有吸附时，拖出来的框永远差着零点几，眼睛看着是齐的，打印出来却不在一条线上。
 */
function snap(x, y, el) {
  const TOL = 1.2
  const w = Number(el.w) || 0
  const h = Number(el.h) || 0
  const xs = [0, 50 - w / 2, 100 - w]
  const ys = [0, 50 - h / 2, 100 - h]
  let nx = x, ny = y
  for (const v of xs) if (Math.abs(v - x) < TOL) { nx = v; break }
  for (const v of ys) if (Math.abs(v - y) < TOL) { ny = v; break }
  return { x: nx, y: ny }
}

function onUp() {
  const changed = drag?.moved
  drag = null
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  if (changed) emit('change')
}

// ------------------------------------------------------------------ 列宽
let colDrag = null

function startColDrag(e, el, idx) {
  if (!props.canWrite) return
  e.preventDefault()
  const r = props.slotRects[props.activeSlot] || props.slotRects[0]
  if (!r) return
  colDrag = {
    idx, sx: e.clientX, w: r.w * (Number(el.w) || 100) / 100, scale: props.scale || 1, pointerId: e.pointerId,
    widths: (props.columns || []).map((c) => Number(c.width) || 0),
  }
  window.addEventListener('pointermove', onColMove)
  window.addEventListener('pointerup', onColUp)
}

function onColMove(e) {
  if (!colDrag || e.pointerId !== colDrag.pointerId || !(e.buttons & 1)) return
  const { idx, widths } = colDrag
  const MIN = 4
  let dx = (e.clientX - colDrag.sx) / colDrag.scale / colDrag.w * 100
  // 右边那一列反向变化，总宽守恒
  dx = Math.max(MIN - widths[idx], Math.min(dx, widths[idx + 1] - MIN))
  props.columns[idx].width = Math.round((widths[idx] + dx) * 10) / 10
  props.columns[idx + 1].width = Math.round((widths[idx + 1] - dx) * 10) / 10
  emit('change')
}

function onColUp() {
  colDrag = null
  window.removeEventListener('pointermove', onColMove)
  window.removeEventListener('pointerup', onColUp)
  normalizeWidths()
  emit('change')
}

/** 列宽是百分比，改完必须重新摊到 100%，否则打印出来总宽就对不上。 */
function normalizeWidths() {
  const cols = props.columns || []
  const total = cols.reduce((s, c) => s + (Number(c.width) || 0), 0) || 1
  cols.forEach((c) => { c.width = Math.round((Number(c.width) || 0) / total * 1000) / 10 })
}

// ------------------------------------------------------------------ 就地改文字
const editingTextId = ref(null)
const editText = ref('')
const ta = useTemplateRef('ta')
const currentEdit = computed(() => props.elements.find((e) => e.id === editingTextId.value) || {})

function startTextEdit(el) {
  if (!props.canWrite || el.type !== 'text') return
  editingTextId.value = el.id
  editText.value = el.text ?? ''
  nextTick(() => ta.value?.focus({ preventScroll: true }))
}

function endTextEdit() {
  const el = currentEdit.value
  editingTextId.value = null
  if (!el?.id || el.text === editText.value) return
  el.text = editText.value
  emit('change')
}

function cancelTextEdit() { editingTextId.value = null }
watch(() => props.elements.map((el) => el.id), (ids) => {
  if (editingTextId.value && !ids.includes(editingTextId.value)) cancelTextEdit()
})
onBeforeUnmount(() => {
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  window.removeEventListener('pointermove', onColMove)
  window.removeEventListener('pointerup', onColUp)
})

defineExpose({ normalizeWidths })
</script>

<style scoped>
.slot { position: absolute; }
.slot-label {
  position: absolute; top: -20px; left: 0;
  font-size: 11px; color: #94a3b8; pointer-events: none;
}

/* 元素框：平时完全不挡视线，悬停/选中才显形 */
.el {
  position: absolute; box-sizing: border-box;
  border: 1px solid transparent; border-radius: 2px;
  cursor: move;
}
.el:hover { border-color: rgba(37, 99, 235, .45); background: rgba(37, 99, 235, .05); }
.el.sel {
  border-color: var(--cwq-chrome-active, #2563eb);
  background: rgba(37, 99, 235, .04);
  box-shadow: 0 0 0 1px rgba(37, 99, 235, .25);
}
.el.off { opacity: .35; border-style: dashed; }
.el.typing { cursor: text; }

.tag {
  position: absolute; top: -17px; left: -1px; display: none;
  padding: 1px 6px; border-radius: 4px 4px 0 0;
  background: var(--cwq-chrome-active, #2563eb); color: #fff;
  font-size: 10px; line-height: 14px; white-space: nowrap; pointer-events: none;
}
.el.sel .tag { display: block; }

/* 角把手：只比边框宽一点点，鼠标好抓，又不至于盖住内容 */
.grip {
  position: absolute; width: 9px; height: 9px; background: #fff;
  border: 1.5px solid var(--cwq-chrome-active, #2563eb); border-radius: 2px;
}
.grip.nw { left: -5px; top: -5px; cursor: nwse-resize; }
.grip.ne { right: -5px; top: -5px; cursor: nesw-resize; }
.grip.sw { left: -5px; bottom: -5px; cursor: nesw-resize; }
.grip.se { right: -5px; bottom: -5px; cursor: nwse-resize; }

/* 列分隔线：竖着的细条，中间一小段更好抓 */
 .colgrip {
  position: absolute; top: 0; bottom: 0; width: 9px; margin-left: -4.5px;
  cursor: col-resize; background: transparent;
}
.colgrip::after {
  content: ''; position: absolute; left: 4px; top: 35%; height: 30%;
  width: 1px; background: var(--cwq-chrome-active, #2563eb);
}
.colgrip:hover { background: rgba(37, 99, 235, .14); border-radius: 3px; }

.slot-empty {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: #94a3b8; font-size: 12px; pointer-events: none; text-align: center; padding: 0 20px;
}

/* 就地编辑 */
.inline-edit {
  position: absolute; z-index: 5; box-sizing: border-box;
  background: #fff; border: 1.5px solid var(--cwq-chrome-active, #2563eb);
  border-radius: 3px; box-shadow: 0 6px 20px rgba(15, 23, 42, .18);
  padding: 4px; min-width: 140px;
}
.inline-edit textarea {
  width: 100%; height: 100%; min-height: 34px; resize: vertical;
  border: 0; outline: none; font: inherit; line-height: 1.4; color: #1f2733;
}
.inline-tip {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  font-size: 10px; color: #94a3b8; padding-top: 2px;
}
.inline-tip button {
  border: 0; background: var(--cwq-chrome-active, #2563eb); color: #fff;
  font-size: 10px; border-radius: 3px; padding: 2px 8px; cursor: pointer;
}
</style>
