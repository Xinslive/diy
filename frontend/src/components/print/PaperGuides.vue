<template>
  <div class="guides">
    <!-- 可打印内容区 -->
    <div class="content" :style="contentStyle">
      <span class="clabel">可打印区 {{ Math.round(content.w) }} × {{ Math.round(content.h) }} px</span>
    </div>

    <!-- 四条边距线，拖线改边距 -->
    <i
      v-for="e in EDGES"
      :key="e.key"
      class="edge"
      :class="[e.key, { near: nearEdge === e.key }]"
      :style="edgeStyle(e)"
      :title="`拖动调整${e.cn}边距`"
      @pointerdown="startDrag($event, e.key)"
    >
      <em v-if="nearEdge === e.key">{{ Math.round(valueOf(e.key)) }} mm</em>
    </i>
  </div>
</template>

<script setup>
/**
 * 页边距编辑层：**在纸的边上直接拖**。
 *
 * 以前页边距是左边一栏四个数字输入框，每个都要眼睛盯着数字和预览对不上。
 * 拖动才是这件事本来的样子：把上边距往下拽，纸上的正文就跟着往下走，
 * 拖到哪就是哪，不用在「10 还是 12」之间猜。
 *
 * 坐标同样用纸坐标（PrintStage 已经把编辑层铺满整张纸），
 * 单位换算按后端 @page 用的毫米，范围沿用后端 MARGIN_RANGE 的 0–100。
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import { rafThrottle } from '@/utils/frame'

const props = defineProps({
  page: { type: Object, required: true },
  content: { type: Object, required: true },
  margins: { type: Object, required: true },
  canWrite: { type: Boolean, default: true },
  /** 外层纸面的屏幕缩放比；拖线位移要换回纸坐标。 */
  scale: { type: Number, default: 1 },
})
const emit = defineEmits(['change'])

const EDGES = [
  { key: 'top', cn: '上', axis: 'y' },
  { key: 'right', cn: '右', axis: 'x' },
  { key: 'bottom', cn: '下', axis: 'y' },
  { key: 'left', cn: '左', axis: 'x' },
]
const MIN = 0
const MAX = 100
const MM2PX = 96 / 25.4

const nearEdge = ref(null)
const valueOf = (k) => Number(props.margins?.[k]) || 0

const contentStyle = computed(() => ({
  left: props.content.x + 'px',
  top: props.content.y + 'px',
  width: Math.max(0, props.content.w) + 'px',
  height: Math.max(0, props.content.h) + 'px',
}))

function edgeStyle(e) {
  if (e.key === 'top') return { top: valueOf('top') * MM2PX - 6 + 'px', left: '0', right: '0' }
  if (e.key === 'bottom') return { bottom: valueOf('bottom') * MM2PX - 6 + 'px', left: '0', right: '0' }
  if (e.key === 'left') return { left: valueOf('left') * MM2PX - 6 + 'px', top: '0', bottom: '0' }
  return { right: valueOf('right') * MM2PX - 6 + 'px', top: '0', bottom: '0' }
}

let drag = null

function startDrag(e, key) {
  if (!props.canWrite) return
  e.preventDefault()
  nearEdge.value = key
  drag = {
    key, sx: e.clientX, sy: e.clientY, base: valueOf(key), rect: props.page,
    scale: props.scale || 1,
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}

// 按帧合并：pointermove 一秒几十次，每一下都在写 props.margins（响应式），
// 每一写都要排一次渲染。松手前会 flush()，最后那一下不会丢。
const onMove = rafThrottle(function (e) {
  const d = drag
  if (!d) return
  // 右/下边距往外拖 = 数值变大，往里拖 = 变小，和左/上边距是同一个方向感：
  // 用户拖的永远是「那条线往哪走」，不是「数值往哪变」。
  const raw = d.key === 'left' ? (e.clientX - d.sx) / d.scale
    : d.key === 'right' ? -(e.clientX - d.sx) / d.scale
      : d.key === 'top' ? (e.clientY - d.sy) / d.scale
        : -(e.clientY - d.sy) / d.scale
  const v = Math.round(Math.max(MIN, Math.min(MAX, d.base + raw / MM2PX)) * 10) / 10
  if (props.margins) props.margins[d.key] = v
  emit('change')
})

function onUp() {
  // 先把欠着的最后一帧落地，再作废拖动状态 —— 反了就停在倒数第二个位置
  onMove.flush()
  drag = null
  nearEdge.value = null
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
  emit('change')
}
onBeforeUnmount(() => {
  onMove.cancel()
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
})
</script>

<style scoped>
.guides { position: absolute; inset: 0; }

.content {
  position: absolute; box-sizing: border-box; pointer-events: none;
  outline: 1px dashed rgba(37, 99, 235, .55);
  outline-offset: -1px;
}
.clabel {
  position: absolute; right: 0; top: -17px; font-size: 10px; color: #2563eb;
  background: rgba(255, 255, 255, .9); padding: 0 4px; border-radius: 3px;
}

.edge {
  position: absolute; box-sizing: border-box; background: transparent;
}
.edge.near { background: var(--cwq-chrome-active, #2563eb); }
.edge.top, .edge.bottom { height: 13px; cursor: ns-resize; }
.edge.left, .edge.right { width: 13px; cursor: ew-resize; }
.edge em {
  position: absolute; font-style: normal; font-size: 11px; line-height: 18px;
  background: var(--cwq-chrome-active, #2563eb); color: #fff;
  padding: 0 7px; border-radius: 3px; white-space: nowrap;
}
.edge.top em { left: 50%; top: -22px; transform: translateX(-50%); }
.edge.bottom em { left: 50%; bottom: -22px; transform: translateX(-50%); }
.edge.left em { top: 50%; left: -4px; transform: translate(-100%, -50%); }
.edge.right em { top: 50%; right: -4px; transform: translate(100%, -50%); }
</style>
