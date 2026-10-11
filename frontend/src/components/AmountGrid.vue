<template>
  <!-- 一个金额格 = 整格一格，不是一格一格。
       亿…分那 11 根竖线是这一格里头的落格线，真正能点的、能打字的，
       是从亿一直连到分的**整个格子**：点哪儿都是往这一格填数，
       聚焦时绿框套住整格（柠檬云就是这么个手感）。

       聚焦**不翻格**：底纹、辅助线、已经落好的数字全留着，
       只是叠上一根会眨的光标，让人知道下一个数字落在第几格。
       真要是聚焦时把落格层藏掉、换成一块白底大字，这一格就退化成
       一个普通大输入框，11 条线和它们代表的记账口径全白画了。 -->
  <div class="amt" :class="{ 'is-focus': focused, 'is-ro': readonly }">
    <AmountCells class="amt-bg" :value="shown" :caret="caret" :sel="sel" />

    <!-- 透明输入层：键盘行为完全按普通数字输入框来，只是把结果显示成分栏的样子。
         字始终透明 —— 显示权交给上面那层落格数字，免得同一格里出现两份数字。

         ↑ / ↓ 在单行输入框里本来什么也干不了（框里没有第二行），在这里是
         「跳到上一行 / 下一行的同一侧」：连续录凭证正是一路顺着金额列往下敲，
         让上下键带着手走，落过去就是选中 + 重打，跟鼠标点过去一样。 -->
    <input
      ref="el"
      class="amt-input"
      :value="raw"
      :disabled="readonly"
      placeholder="0.00"
      inputmode="decimal"
      autocomplete="off"
      @input="onInput"
      @focus="onFocus"
      @click.stop="selectAll"
      @blur="onBlur"
      @keydown.enter.prevent="$emit('enter')"
      @keydown.up.prevent="$emit('up')"
      @keydown.down.prevent="$emit('down')"
    />
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import AmountCells from './AmountCells.vue'
import { caretBoundary, sanitizeAmount } from '@/utils/amountGrid'

const props = defineProps({
  modelValue: { type: [String, Number], default: '' },
  readonly: { type: Boolean, default: false }
})
const emit = defineEmits(['update:modelValue', 'enter', 'focus', 'up', 'down', 'balance'])

const el = ref(null)
const focused = ref(false)
/** 光标 / 选中落在第几条格缝（0-11）；-1 = 不画 */
const caret = ref(-1)
const sel = ref(null)

const raw = computed(() => sanitizeAmount(props.modelValue))

/**
 * 落在格子里的样子：**角分永远补齐两位**。
 *
 *  凭证上没有「二千三百三十二块」这种写法，只有二千三百三十二元零角零分；
 *  合计行也一直按两位小数落格。分录行空着角分，同一张纸上两行写法对不上，
 *  人会以为这笔钱还没录完。敲「2332」就该看到 2332.00。
 *
 *  补出来的 0 是**格子的底子**，不是人敲的数：光标仍停在最后一个手敲的数位上，
 *  补出来的两个 0 在它右边。再敲「.5」就是往角位写，一样按位置走。
 */
const shown = computed(() => {
  const s = raw.value
  if (!s) return s
  const [i, d = ''] = s.split('.')
  return d.length >= 2 ? s : `${i}.${d.padEnd(2, '0')}`
})

function onFocus() {
  focused.value = true
  selectAll()
  syncSelection()
  // 聚焦要把「当前行」告诉外面：整行高亮跟着光标走
  emit('focus')
}
/** 整格选中：改金额就是重打一遍，不用先把旧数字拖掉。
 *  再点一次已经聚焦的格子不会触发 focus，所以 click 上也得来一遍。 */
function selectAll() {
  el.value?.select()
  syncSelection()
}
function onInput(e) {
  const typed = e.target.value
  // 「=」不是金额，是一句指令：把这一格补成让整张凭证平掉的那个数。
  // 它不能落进格子里，所以这里不让它变成值 —— 把输入框恢复原样，
  // 具体补多少由外面按借贷差额算（这一格只知道有个人在这儿点了「=」）。
  if (typed.includes('=')) {
    e.target.value = raw.value
    syncSelection()
    emit('balance')
    return
  }
  emit('update:modelValue', sanitizeAmount(typed))
  // 打字之后光标已经挪位了：selectionchange 在受控输入里不一定按时来，主动对一次
  nextTickSelection()
}
function onBlur() {
  focused.value = false
  caret.value = -1
  sel.value = null
  document.removeEventListener('selectionchange', onSelectionChange)
}

/**
 * 把原生光标/选区翻译成「第几条格缝」。
 * 原生那条光标是看不见的（字透明了它也透明），所以选中时得自己画一块浅蓝，
 * 不然聚焦整格选中这件事一点提示都没有 —— 人不知道重打一遍会盖掉旧数字。
 */
function syncSelection() {
  const e = el.value
  if (!e || !focused.value) return
  const s = e.selectionStart ?? 0
  const t = e.selectionEnd ?? s
  caret.value = caretBoundary(e.value, t)
  sel.value = t > s
    ? [caretBoundary(e.value, s), caretBoundary(e.value, t)]
    : null
}
/** 受控 value 换过之后浏览器还攥着旧选区，等下一帧再对一次，别用上一版的数 */
let selTimer = null
function nextTickSelection() {
  clearTimeout(selTimer)
  selTimer = setTimeout(syncSelection, 0)
}
// 方向键、鼠标拖选、双击选词都只动选区不动 value，keydown/input 一个都不触发 ——
// 唯一能把它们全兜住的是 selectionchange
function onSelectionChange() {
  if (!focused.value || document.activeElement !== el.value) return
  syncSelection()
}

watch(focused, (v) => {
  if (v) document.addEventListener('selectionchange', onSelectionChange)
  else document.removeEventListener('selectionchange', onSelectionChange)
})
onBeforeUnmount(() => {
  clearTimeout(selTimer)
  document.removeEventListener('selectionchange', onSelectionChange)
})

/** 回车一路往下走时要能直接落到这一格 */
defineExpose({ focus: () => el.value?.focus() })
</script>

<style scoped>
.amt {
  position: relative;
  width: 100%;
  height: 100%;
  background: transparent;
  cursor: text;
}
.amt.is-ro { cursor: default; }

/* 聚焦：绿框套住整格，但**不铺白底**。铺了就把这一行的淡绿底和落格线盖掉，
   而这一格在纸面上本来就不是一块白板 —— 它是 11 个数位挨着排的一格。 */
.amt.is-focus { box-shadow: inset 0 0 0 2px #44b44a; }

.amt-bg { position: absolute; inset: 0; }

.amt-input {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  border: 0;
  outline: none;
  background: transparent;
  /* 字、竖线光标、选中块一律透明：显示交给落格层，这里只负责收键盘 */
  color: transparent;
  caret-color: transparent;
  font: inherit;
  font-size: 15px;
  text-align: left;
  padding: 0 10px;
}
.amt-input::placeholder { color: transparent; }
.amt-input::selection { background: transparent; }
.amt.is-ro .amt-input { cursor: default; }
</style>
