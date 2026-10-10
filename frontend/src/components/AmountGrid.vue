<template>
  <div class="amt" :class="{ 'is-focus': focused, 'is-ro': readonly }">
    <!-- 金额式表头：亿千百十万千百十元角分 -->
    <div v-if="showHead" class="amt-head">
      <span v-for="(u, i) in UNITS" :key="i" class="amt-unit" :class="unitCls(i)">{{ u }}</span>
    </div>

    <!-- 数字落格：整 9 格（亿…元），元角分合成一个整体，跟记账习惯一致 -->
    <div class="amt-body">
      <span
        v-for="(d, i) in highDigits"
        :key="'h' + i"
        class="amt-digit"
        :class="digitCls(i)"
      >{{ d }}</span>
      <span class="amt-yuan" :class="{ 'is-active': focused }">{{ yuanPart }}</span>
    </div>

    <!-- 透明输入层：键盘行为完全按普通数字输入框来，
         只是把结果显示成分栏的样子。自己造「一位一格」的编辑器很容易在
         光标跳转、退格、粘贴这些地方出怪毛病，不如借浏览器现成的输入行为。 -->
    <input
      ref="el"
      class="amt-input"
      :value="raw"
      :disabled="readonly"
      inputmode="decimal"
      autocomplete="off"
      @input="onInput"
      @focus="onFocus"
      @blur="focused = false"
      @keydown.enter.prevent="$emit('enter')"
      @click.stop
    />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

/** 金额式表头，从亿到分。万位和元位是分节的地方，视觉上要断开。 */
const UNITS = ['亿', '千', '百', '十', '万', '千', '百', '十', '元', '角', '分']
const YUAN_IDX = 8

const props = defineProps({
  modelValue: { type: [String, Number], default: '' },
  readonly: { type: Boolean, default: false },
  // 金额式表头「亿千百十万千百十元角分」每行印一遍又占地方又噪，
  // 只在第一行顶一遍就够了，跟纸质凭证一致
  showHead: { type: Boolean, default: true }
})
const emit = defineEmits(['update:modelValue', 'enter', 'focus'])

const el = ref(null)
const focused = ref(false)

/** 只留数字和一个小数点：最多 9 位整数、2 位小数。 */
function sanitize(v) {
  let s = String(v ?? '').replace(/[^\d.]/g, '')
  const dot = s.indexOf('.')
  if (dot !== -1) s = s.slice(0, dot + 1) + s.slice(dot + 1).replace(/\./g, '')
  let [i, d = ''] = s.split('.')
  i = i.replace(/^0+(?=\d)/, '').slice(0, 9)
  d = d.slice(0, 2)
  if (!i) return d ? `0.${d}` : ''
  return d ? `${i}.${d}` : i
}

const raw = computed(() => sanitize(props.modelValue))
const parts = computed(() => {
  const [i = '', d = ''] = raw.value.split('.')
  return { i, d }
})

/** 亿…十 这 9 格，右对齐，值不够长就前面留空 */
const highDigits = computed(() => parts.value.i.padStart(9, '0').split(''))

/** 元角分整体：有小数点才显示，空值显示 0.00 */
const yuanPart = computed(() => {
  const { i, d } = parts.value
  if (!i && !d) return '0.00'
  if (!d) return `${i}.00`
  return `${i}.${d}`
})

// 直接点输入框也要把「当前行」告诉外面：透明输入层把 click 拦了，
// 只靠点外层容器的话，光标进得去、整行高亮却不会出现
function onFocus() {
  focused.value = true
  emit('focus')
}
function onInput(e) {
  emit('update:modelValue', sanitize(e.target.value))
}
function unitCls(i) {
  return { 'sep-wan': i === 4, 'sep-yuan': i === 8 }
}
function digitCls(i) {
  return { 'sep-wan': i === 4, 'sep-yuan': i === YUAN_IDX }
}
function focusInput() {
  if (props.readonly) return
  el.value?.focus()
  emit('focus')
}

defineExpose({ focusInput })
</script>

<style scoped>
.amt {
  position: relative;
  width: 100%;
  border: 1px solid var(--cwq-border, #dcdfe6);
  border-radius: 4px;
  background: #fff;
  overflow: hidden;
  cursor: text;
}
.amt.is-ro { cursor: default; background: #f7f8fa; }

.amt-head {
  display: flex;
  border-bottom: 1px solid #ebeef5;
  background: #fafbfc;
}
.amt-unit {
  flex: 1;
  text-align: center;
  font-size: 11px;
  color: #909399;
  padding: 1px 0;
  line-height: 1.2;
}
/* 万位、元位是记账的分节线：蓝色/红色，跟柠檬云一致 */
.sep-wan { border-left: 1px solid #a3c9ff; }
.sep-yuan { border-left: 1px solid #f0a3a3; }

.amt-body {
  display: flex;
  align-items: stretch;
  min-height: 26px;
}
.amt-digit {
  flex: 1;
  text-align: center;
  font-size: 13px;
  line-height: 26px;
  color: #303133;
}
.amt-yuan {
  flex: 2.4;
  text-align: right;
  padding-right: 6px;
  font-size: 13px;
  line-height: 26px;
  font-weight: 600;
  color: #303133;
  border-left: 1px solid #f0a3a3;
}
.amt.is-focus .amt-yuan {
  box-shadow: inset 0 0 0 2px #67c23a;
  border-radius: 2px;
}

/* 输入层透明覆盖在上层：键盘行为走浏览器原生，视觉走分栏 */
.amt-input {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  border: 0;
  outline: none;
  background: transparent;
  color: transparent;
  caret-color: transparent;
  font: inherit;
  padding: 0 6px;
}
.amt-input::selection { background: transparent; }
.amt.is-ro .amt-input { cursor: default; }
</style>
