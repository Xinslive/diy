<template>
  <!-- 一个金额格 = 整格一格，不是一格一格。
       亿…分那 11 根竖线是这一格里头的落格线，真正能点的、能打字的，
       是从亿一直连到分的**整个格子**：点哪儿都是往这一格填数，
       聚焦时绿框套住整格（柠檬云就是这么个手感）。

       聚焦**不翻格**：底纹、辅助线、已经落好的数字全留着，
       只是叠上一根会眨的光标，让人知道下一个数字落在第几格。
       真要是聚焦时把落格层藏掉、换成一块白底大字，这一格就退化成
       一个普通大输入框，11 条线和它们代表的记账口径全白画了。 -->
  <div class="amt" :class="{ 'is-focus': focused, 'is-ro': readonly, 'is-warn': note }">
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
      @mousedown="byMouse = true"
      @click="onClick"
      @blur="onBlur"
      @keydown="onKeydown"
      @keydown.enter.prevent="$emit('enter')"
      @keydown.up.prevent="$emit('up')"
      @keydown.down.prevent="$emit('down')"
    />
    <!-- 超位提示：角上一个小三角 + 一条提示说清楚这一格怎么了。
         这一格只有 11 个数位的位置，值再长也落不下（钱没有第 10 个「元」位）。
         但**落不下的是格子，不是那个数**：
           · 超过 9 位整数 → 值原样保留，界面显示**原值** + 这个角标
             （显示一个被改小的数，用户会以为库里存的就是它 —— 审查 P0-1）
           · 超过 18 位整数 → 数据模型装不下，**不收**，输入框里那串字恢复原样
         两种都要让人看见（审计 P2-19：悄悄截断的代价是这笔数看着对、其实少了几位）。 -->
    <span v-if="note" class="amt-clip" :title="note" />

  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import AmountCells from './AmountCells.vue'
import {
  amountInputText, caretBoundary, inspectAmount, MAX_INT_DIGITS,
  sanitizeAmount, sanitizeAmountDetailed, toggleAmountSign,
} from '@/utils/amountGrid'

const props = defineProps({
  modelValue: { type: [String, Number], default: '' },
  readonly: { type: Boolean, default: false }
})
const emit = defineEmits(['update:modelValue', 'enter', 'focus', 'up', 'down', 'left', 'right',
  'balance', 'commit'])

const el = ref(null)
const focused = ref(false)
/** 光标 / 选中落在第几条格缝（0-11）；-1 = 不画 */
const caret = ref(-1)
const sel = ref(null)
/** 这次**输入**丢了东西 / 装不下的话（一行字），角上挂个标、弹一条提示。null = 没有 */
const clipNote = ref(null)

/**
 * 载入态的提示：值本身就放不进 11 格。
 *
 *  以前角标只在**用户输入**时挂得上，于是「载入一个 10 位金额」这件事
 *  一声不吭 —— 界面显示的还是那个被截小的数（P1-1 的根）。
 *  现在载入态也挂角标：这一格装不下，**显示的是原值**，角标把话说清楚。
 */
const overNote = computed(() => {
  const info = inspectAmount(props.modelValue)
  if (!info.overGrid) return null
  return `金额格只有 ${MAX_INT_DIGITS} 位整数位（亿…元），这一格放不下 ${info.intDigits} 位的金额；` +
    '显示的是原值，没有改动，请用其它方式录入'
})

/** 角标上挂的那句话：输入态的提示优先，没有就看载入态的。 */
const note = computed(() => clipNote.value || overNote.value)

/**
 * 把「这一格怎么了」说出来。
 *
 *  · 装不下（超过 9 位整数）→ 值原样保留，角标 + 提示说清楚放不下
 *  · 收不了（超过 18 位整数）→ **不收**，红字提示（这一类不是「丢了几位」，是「不认」）
 *
 *  11 根格线摆在那儿，第 10 个整数位无处可落 —— 但**落不下的是格子，不是那个数**：
 *  不能让人以为那几位是自己没敲（值看着对、其实少了几位，
 *  而这一格会跟着自动保存存进账里）。角标标在这一格上。
 * 重新敲成不超长的值就撤掉，走了这一格也撤掉 —— 提示只对当下这笔负责。
 */
function reportClipping(notices) {
  clipNote.value = notices.length ? notices.map((n) => n.text).join('；') : null
  for (const n of notices) {
    // grouping：连续敲第 10、11 位弹的是同一条，合并成一条，别刷屏
    if (n.code === 'MONEY_LIMIT') {
      ElMessage.error(`金额超限：${n.text}`, { grouping: true, duration: 4000 })
    } else {
      ElMessage.warning(`金额超位：${n.text}`, { grouping: true, duration: 2600 })
    }
  }
}

/**
 * 框里摆的值 —— **原值，一个字符都不改**。
 *
 *  这里以前是「显示也过一遍输入净化」，于是读回来的 10 位金额被截成 9 位：
 *  分录格里显示 123456789.12、合计行显示 1234567890.12，同一屏上差 10 倍（P1-1），
 *  而用户只要碰一下那一格，被改小的数还会被写回状态里存进账（P0-1）。
 *
 *  净化只该发生在**用户敲进去**的那条路上（onInput）；显示一律用原值。
 *  放不下 11 格时由 AmountCells 改用「原值 + 角标」摆出来。
 */
const raw = computed(() => sanitizeAmount(props.modelValue))

/**
 * 落在格子里的样子：**角分永远补齐两位**。
 *
 *  角分是这笔钱的一部分，不是可有可无的小数位：合计行也一直按两位小数落格，
 *  跟打印出来那张纸上的写法一致。合计行落 2332.00、分录行落 2332，看着像少了一笔，
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

/** 这次聚焦是鼠标点进来的吗。mousedown 先于 focus，所以这里分得出两条路 */
let byMouse = false
function onFocus() {
  focused.value = true
  // 鼠标点进来：浏览器已经把光标摆到点中的位置，别再去动它 ——
  // 人要点哪儿就得能在哪儿落笔；「一律落到元/角」是把点哪儿的能力也一起收走了
  // 键盘进来（Tab、↑↓）：没有点中的位置可依，才落到元/角之间
  if (byMouse) byMouse = false
  else caretToDecimal()
  // 聚焦要把「当前行」告诉外面：整行高亮跟着光标走
  emit('focus')
}
/**
 * 只用在**键盘**进来的时候（见 onFocus）：光标落到**元/角之间那根红线**上，
 * 那儿是小数点该在的地方。整格选中看着省事，实际改不了小数位：
 * 光标被按在末尾，想填角分得先退格把小数点顶出来，而金额天天在改的就是角分。
 *
 *  空格子也落到同一个位置，第一个数字正好落进元位。
 */
function caretToDecimal() {
  const e = el.value
  if (!e) return
  const dot = e.value.indexOf('.')
  const pos = dot === -1 ? e.value.length : dot + 1
  e.setSelectionRange(pos, pos)
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
  const pos = e.target.selectionStart ?? typed.length
  // 净化把「动过手」的地方一并说出来：钱只有 亿…分 那么多格，超出的位
  // 落不下只能丢，但丢了就得有人知道（审计 P2-19）。提示挂在这一格的角上，
  // 同时弹一条 —— grouping 让连续敲第 10、11 位只弹一条，不会刷屏。
  const { value: next, notices } = sanitizeAmountDetailed(typed)
  // 超出**数据模型上限**（18 位整数，后端也存不下）：**不收**。
  // 这一类不是「丢了几位」，是「这个数根本不认」——
  // 悄悄截成 18 位会让人以为那就是这笔钱（差一分就是另一笔钱）。
  // 框里的字恢复成原来的那个数，不发 update（原来的值一个字都不动）。
  if (notices.some((n) => n.code === 'MONEY_LIMIT')) {
    e.target.value = props.modelValue ?? ''
    reportClipping(notices)
    nextTickSelection(pos)
    return
  }
  reportClipping(notices)
  // 小数点刚敲下去、角分还没跟上来的那一瞬，框里得**留着那个点**
  const shownNext = amountInputText(typed, next)
  // 框里的字**当场**收回值的样子。少了这一步，受控的值没变 → Vue 不重渲染这一格 →
  // 手敲进去的多余负号就留在框里（「-303000-」），人看着跟存的数对不上；
  // 接着敲的数字还会被那个多出来的负号顶到别的数位上去。
  if (e.target.value !== shownNext) e.target.value = shownNext
  emit('update:modelValue', next)
  // 受控输入把值重写一遍，浏览器就把光标甩到末尾 —— 在元/角之间改角分时，
  // 「1234.56」敲一个 9 变成「1234.95」（尾上那个 6 被两位小数的上限截掉），
  // 字符串短了一位，光标就跟着掉到最后一位，再敲的数字全跑到分位去了。
  // 记下按键时它停在哪，写回去时摆回同一个位置。
  nextTickSelection(pos)
}
/**
 * 点一下：光标落到**鼠标点中的那条落格线**上，而不是浏览器自己算的那个位置。
 *
 *  这层输入框是透明的，字还是靠左排的、字号也小，它眼里「点 20% 处」约莫落在第三个字旁边；
 *  可人眼睛看的是右边那 11 格（亿…分）。两者对不上，点千位却把光标放到了末尾 ——
 *  「点哪儿选中哪儿」就成了一句空话。
 *
 *  所以这里自己按 x 换算：格宽是十一等分，第 k 条缝就是 k/11，
 *  再把「第 k 条缝」反推成字符串里的第几个字（跟 caretBoundary 正对着写，
 *  一头一尾必须同一套规则）。反推完只挪光标、不改值。
 *
 *  有选区就说明人在拖选（或者双击选词），那是有意选的，别去动它。
 */
function onClick(e) {
  const inp = el.value
  if (!inp) return
  const s0 = inp.selectionStart ?? 0
  if ((inp.selectionEnd ?? s0) !== s0) return
  const p = posFromX(e.clientX)
  inp.setSelectionRange(p, p)
  syncSelection()
}

/** 鼠标 x → 字符串里的位置。反过来就是 caretBoundary，两边必须对得上。 */
function posFromX(clientX) {
  const inp = el.value
  const r = inp.getBoundingClientRect()
  const k = Math.max(0, Math.min(11, Math.round(((clientX - r.left) / r.width) * 11)))
  const v = sanitizeAmount(inp.value)
  // 负号**不占位**（整格的属性，不是第一个数位）—— 与 caretBoundary 对称：
  // 整数右对齐占 亿…元 九格，所以「第 k 条缝」在整数里是 k - 9 + 整数长度个字，
  // 前面若带负号还要把摘掉负号让出来的那一位补回去。
  const neg = v.startsWith('-')
  const signLen = neg ? 1 : 0
  const body = neg ? v.slice(1) : v
  const bdot = body.indexOf('.')
  const head = bdot === -1 ? body : body.slice(0, bdot)
  const p = k <= 9
    ? signLen + k - 9 + head.length
    : (bdot === -1 ? v.length : signLen + bdot + 1 + (k - 9))
  return Math.max(0, Math.min(p, v.length))
}

/**
 * 退格/删除不许只吃掉那个小数点。
 *
 *  点格子时光标落在元/角之间，也就是**紧贴在小数点后面**（老数据存回来是「200.00」，
 *  角分两位是它自带的）。这时候按一下退格，默认行为会把小数点删掉：
 *  「200.00」→「20000」，金额凭空大了一百倍，还被自动保存存进账里。
 *
 *  所以光标紧贴小数点时的退格/删除，一并把小数点和它后面的小数删干净 ——
 *  人想抹的本来就是「那角分」，不是「那个点」。
 */
/**
 * 按一下负号：整格翻负号。
 *
 *  光标要跟着挪一位，翻完它还压在**原来那个数字上** —— 人在哪一位上按的，
 *  翻完就该还在哪一位上接着敲。光标跟着数一起走，才对得上「我看的是哪一格」。
 */
function toggleSign() {
  const inp = el.value
  if (!inp) return
  const shownBefore = inp.value
  // 光标位置得**先记下来**：往 input.value 上赋值，浏览器顺手把光标甩到末尾，
  // 之后再问 selectionStart 拿到的永远是末尾 —— 那光标就跟着数跑了，不跟人走。
  const pos = inp.selectionStart ?? 0
  const after = toggleAmountSign(shownBefore)
  const shownAfter = amountInputText(shownBefore, after)
  if (inp.value !== shownAfter) inp.value = shownAfter
  emit('update:modelValue', after)
  nextTickSelection(pos + shownAfter.length - shownBefore.length)
}

function onKeydown(e) {
  const inp = el.value
  if (!inp) return

  // 负号是**整格的属性**：光标在哪一位数上都一样，按一下整格翻负，再按一下翻正。
  // 它不是往数字中间插的字符，所以不用先挪光标，也不用管有没有选区。
  if (e.key === '-') {
    e.preventDefault()
    toggleSign()
    return
  }

  // 光标走到这一头的边界上，方向键就**出格**：← 出到左边那一格，→ 出到右边那一格。
  //
  // 金额是「一格一格填」的，光标压在最高位左边再按 ←，浏览器只会把它按在原地 ——
  // 可人按这一下想的就是「退回上一格」，跟退格键是一回事，却什么都没发生。
  // 整张凭证一路向右录下来，回头想改一下科目，却得先 Tab 绕一圈找回来。
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    const s = inp.selectionStart ?? 0
    if ((inp.selectionEnd ?? s) !== s) return    // 有选区：先照默认的收掉选区，别急着出格
    const edge = e.key === 'ArrowLeft' ? 0 : inp.value.length
    if (s !== edge) return                      // 还在这一格里头走：交给默认行为
    e.preventDefault()
    emit(e.key === 'ArrowLeft' ? 'left' : 'right')
    return
  }

  if (e.key !== 'Backspace' && e.key !== 'Delete') return
  const v = inp.value
  const dot = v.indexOf('.')
  if (dot === -1) return
  const s = inp.selectionStart ?? 0
  if (s !== (inp.selectionEnd ?? s)) return          // 有选区：按默认的来
  const atDot = e.key === 'Backspace' ? s === dot + 1 : s === dot
  if (!atDot) return
  e.preventDefault()
  inp.value = v.slice(0, dot)
  inp.setSelectionRange(dot, dot)
  onInput({ target: inp })     // onInput 读的是 e.target，这里直接把框本身当 target 递进去
}

function onBlur() {
  byMouse = false        // 走了就把「刚才是鼠标点进来的」作废，别留给下一次键盘聚焦
  focused.value = false
  caret.value = -1
  sel.value = null
  clipNote.value = null  // 提示只对当下这一笔负责，人已经走了就不用再挂着
  document.removeEventListener('selectionchange', onSelectionChange)
  // **这一格的值定了** —— 发出去让外面把「正在敲的数」换成「敲完的数」。
  //
  // 外面那个科目余额是照着填的数：敲一个 1 余额就跳一下，人还没填完
  // 就被半截数牵着走（用户 2026-10-25：「我希望的是，我输入完 100，
  // 然后光标离开这个金额格，才更新余额」）。所以提交点必须是**离开这一格**，
  // 不是每敲一个键。
  emit('commit')
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
/**
 * 受控 value 换过之后浏览器还攥着旧选区，等 DOM 更新完再对一次，别用上一版的数。
 * pos 给了就把光标摆回那个位置（超过新长度就压在末尾）。
 *
 *  必须用 nextTick（微任务）而不是 setTimeout：连续打字时两次按键之间只隔一个任务，
 *  setTimeout 还没轮到执行，下一个数字就照着被甩到末尾的光标插进去了 ——
 *  「1234.56」在元/角之间连敲两个 9，本该得到 1234.99，实测成了 1234.959。
 */
let pending = 0
function nextTickSelection(pos) {
  const mine = ++pending
  nextTick(() => {
    if (mine !== pending) return          // 后一次输入已经把这次作废了
    if (pos != null && focused.value && el.value) {
      // 长度是**框里那串字**的长度：input 元素自己没有 length（那是 textarea 的），
      // 取错了会算出 NaN，光标被 setSelectionRange 甩到 0 —— 下一个数字就从最前面插进来了
      const p = Math.min(pos, el.value.value.length)
      el.value.setSelectionRange(p, p)
    }
    syncSelection()
  })
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
/* 超位/超容量：框上再套一道琥珀色，角上挂一个小三角。
   三角压在**最左**（亿位那一格）—— 绝大多数金额右对齐，左边是空的，
   摆在这儿不会盖住任何一个已经落好的数位。
   完整的话在 title 和那条提示里，三角只负责说「是这一格」。 */
.amt.is-warn { box-shadow: inset 0 0 0 2px #e6a23c; }
.amt.is-warn.is-focus { box-shadow: inset 0 0 0 2px #e6a23c; }

.amt-clip {
  position: absolute;
  left: 1px;
  top: 1px;
  width: 0;
  height: 0;
  border-left: 6px solid transparent;
  border-top: 6px solid #e6a23c;
  pointer-events: auto;
}

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
