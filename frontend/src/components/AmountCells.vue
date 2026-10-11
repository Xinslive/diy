<template>
  <!-- 金额格的底纹 + 落格数字。它是纯展示层：合计行、分录行共用同一套落格规则，
       所以合计行的合计金额跟分录行的分录金额在视觉上完全对齐。

       聚焦时也**不藏**：竖线、蓝线分組、红线（元/角之间的小数点）都是这张表自己画的，
       一藏就成了一个光秃秃的大白输入框，这套线全白画了。
       改成在格子上头叠一根会眨的光标 —— 键盘行为照旧是普通数字输入框，
       只是「现在正在第几格」用格缝的位置说出来。 -->
  <!-- 负数 = 红字冲销，整格数字上红（红字的「红」得在这一眼看得见的地方） -->
  <div class="amt-cells" :class="{ 'is-red': red }">
    <!-- 选中范围画在数字**底下**，而且是半透明的。
         先铺一层实色再把数字盖上去的话，聚焦整格选中时数字会被整片吃掉 ——
         恰恰是最该看清「我要重打的是这几个数」的时候，一个数都看不见。 -->
    <span v-if="sel" class="amt-sel"
          :style="{ left: pct(sel[0]), width: pct(sel[1] - sel[0]) }" />
    <span v-for="(t, i) in cells" :key="i" class="amt-cell" :class="AMOUNT_SEPS[i]">
      {{ t }}
    </span>
    <!-- 光标压在数字上头：它只有 2px，压的是格缝，本来就碰不到字 -->
    <span v-if="caret >= 0" class="amt-caret" :style="{ left: pct(caret) }" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { AMOUNT_SEPS, distributeAmount, isNegativeAmount } from '@/utils/amountGrid'

const props = defineProps({
  value: { type: [String, Number], default: '' },
  /** 光标落在第几条格缝上（0-11）。-1 = 不画。 */
  caret: { type: Number, default: -1 },
  /** 选中范围的两条格缝 [起, 止)。null = 没选中。 */
  sel: { type: Array, default: null }
})

const cells = computed(() => distributeAmount(props.value))
const red = computed(() => isNegativeAmount(props.value))
/** 11 格等分，所以「第 k 条缝」就是 k/11 —— 不用去量 DOM，改个数宽度就跟着变 */
const pct = (k) => `${(k / 11) * 100}%`
</script>

<style scoped>
.amt-cells { display: flex; position: relative; width: 100%; height: 100%; }

.amt-cell {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  color: var(--cwq-text);
  font-variant-numeric: tabular-nums;
}
/* 红字冲销：老规矩负数用红笔写。颜色压过落格线的灰，一眼分得出正负，
   复核时不用把数倒过来再算一遍符号。 */
.is-red .amt-cell { color: #d93025; font-weight: 600; }
.is-red .amt-caret { background: #d93025; }

.plain { border-left: 1px solid #ededed; }
.blue { border-left: 1px solid #bde3f7; }
.red { border-left: 1px solid #f2cbca; }

/* 选中：半透明的一层蓝，数字压在它上头照常读得清，底下的竖线也还透得出来。
   不用浏览器自带那块选中底色 —— 那是给有字的文本框准备的，在这里字是透明的，
   只会剩一块凭空出现的蓝方块。 */
.amt-sel {
  position: absolute;
  top: 0;
  bottom: 0;
  background: rgba(64, 158, 255, .20);
  pointer-events: none;
}

/* 光标：一根 2px 的竖线，落在格缝上（贴着数字，不压数字）。
   上下留一点，闪起来像真的光标，不像画上去的一道杠。 */
.amt-caret {
  position: absolute;
  top: 12%;
  bottom: 12%;
  width: 2px;
  margin-left: -1px;
  background: var(--cwq-text, #303133);
  pointer-events: none;
  animation: amt-blink 1.1s steps(1, end) infinite;
}
@keyframes amt-blink { 0%, 55% { opacity: 1; } 56%, 100% { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .amt-caret { animation: none; } }
</style>
