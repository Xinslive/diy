<template>
  <el-dialog v-model="visible" title="编辑科目" width="520px" :close-on-click-modal="false">
    <el-form :model="form" label-width="90px" label-position="right">
      <el-form-item label="科目编码" required>
        <el-input v-model="form.code" :disabled="codeLocked || saving" maxlength="32" />
        <div v-if="codeLocked" class="field-note">本年度已有发生额，只能修改科目名称</div>
      </el-form-item>
      <el-form-item label="科目名称" required>
        <el-input v-model="form.name" :disabled="saving" maxlength="128" @keyup.enter="submit" />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import * as api from '@/api/subject'
import { useSessionStore } from '@/stores/session'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  subject: { type: Object, default: null }
})
const emit = defineEmits(['update:modelValue', 'saved'])
const store = useSessionStore()
const saving = ref(false)
const form = reactive({ code: '', name: '' })
const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})
const codeLocked = computed(() => !!props.subject?.has_current_year_activity)

watch(() => props.modelValue, (open) => {
  if (!open || !props.subject) return
  form.code = props.subject.code || ''
  form.name = props.subject.name || ''
})

async function submit() {
  if (!store.canWrite) return ElMessage.warning('这个账套是只读的，不能编辑科目')
  if (!form.code.trim()) return ElMessage.warning('科目编码不能为空')
  if (!form.name.trim()) return ElMessage.warning('科目名称不能为空')
  saving.value = true
  try {
    const updated = await api.updateSubject(store.bookId, props.subject.id, {
      code: form.code.trim(), name: form.name.trim()
    })
    ElMessage.success(`已保存科目 ${updated.code} ${updated.name}`)
    visible.value = false
    emit('saved')
  } catch (error) {
    if (!error.response?.data?.detail) ElMessage.error('保存科目失败')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.field-note { margin-top: 6px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.4; }
</style>
