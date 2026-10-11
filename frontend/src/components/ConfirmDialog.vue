<template>
  <el-dialog
    v-model="open"
    class="cwq-confirm"
    :width="width"
    align-center
    :show-close="false"
    :close-on-click-modal="false"
    @close="onCancel"
  >
    <div class="cwq-confirm__head">
      <span class="cwq-confirm__icon" :class="tone">
        <el-icon><component :is="icon" /></el-icon>
      </span>
      <div class="cwq-confirm__text">
        <div class="cwq-confirm__title">{{ title }}</div>
        <p v-if="lead" class="cwq-confirm__lead">{{ lead }}</p>
      </div>
    </div>

    <div v-if="chips.length" class="cwq-confirm__chips">
      <span v-for="(c, i) in chips" :key="`${c}-${i}`" class="cwq-confirm__chip">{{ c }}</span>
    </div>

    <template #footer>
      <el-button @click="close">{{ cancelText }}</el-button>
      <el-button :type="tone" @click="$emit('confirm')">{{ confirmText }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed } from 'vue'
import { CircleCheck } from '@element-plus/icons-vue'

const props = defineProps({
  modelValue: Boolean,
  title: String,
  lead: String,
  /** 关键对象（期间号 / 凭证号），用胶囊列出来比塞进一句话好读 */
  chips: { type: Array, default: () => [] },
  confirmText: { type: String, default: '确定' },
  cancelText: { type: String, default: '取消' },
  /** 同时决定图标底色和确认按钮色 */
  tone: { type: String, default: 'primary' },
  icon: { type: [Object, Function], default: () => CircleCheck },
  width: { type: String, default: '440px' }
})

const emit = defineEmits(['update:modelValue', 'confirm', 'cancel'])

const open = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

function close() {
  open.value = false
}
function onCancel() {
  emit('cancel')
}
</script>

<style>
.cwq-confirm.el-dialog {
  border-radius: 10px;
  overflow: hidden;
  box-shadow: 0 12px 32px rgba(24, 32, 51, .12);
}
.cwq-confirm .el-dialog__header { display: none; }
.cwq-confirm .el-dialog__body { padding: 20px 22px 18px; }
.cwq-confirm .el-dialog__footer {
  padding: 0 22px 20px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.cwq-confirm .el-dialog__footer .el-button { margin-left: 0; }

.cwq-confirm__head { display: flex; align-items: flex-start; gap: 11px; }
.cwq-confirm__text { min-width: 0; padding-top: 1px; }
.cwq-confirm__title { font-size: 15px; font-weight: 650; letter-spacing: -0.01em; color: var(--cwq-text); }
.cwq-confirm__lead {
  margin: 5px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--cwq-muted);
}

.cwq-confirm__icon {
  width: 26px;
  height: 26px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  font-size: 15px;
  flex: none;
}
.cwq-confirm__icon.primary { background: #eff5ff; color: #2563eb; }
.cwq-confirm__icon.warning { background: #fff7ed; color: #b45309; }
.cwq-confirm__icon.danger  { background: #fef2f2; color: #dc2626; }
.cwq-confirm__icon.success { background: #f0fdf4; color: #15803d; }

.cwq-confirm__chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
.cwq-confirm__chip {
  font-size: 12px;
  line-height: 1;
  font-variant-numeric: tabular-nums;
  padding: 5px 9px;
  border-radius: 5px;
  background: #f4f6f9;
  border: 1px solid var(--cwq-border);
  color: var(--cwq-text);
}
</style>
