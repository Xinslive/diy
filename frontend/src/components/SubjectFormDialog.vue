<template>
  <el-dialog v-model="visible" title="新增科目" width="640px" :close-on-click-modal="false">
    <el-form :model="form" label-width="110px" label-position="right" class="sf">
      <el-form-item label="上级科目" required>
        <el-tree-select
          v-model="form.parent_id"
          :data="selectTree"
          node-key="id"
          check-strictly
          filterable
          default-expand-all
          clearable
          placeholder="请选择（留空则新建一级科目）"
          style="width: 100%"
        />
      </el-form-item>

      <el-form-item label="科目编码" required>
        <el-input v-model="form.code" placeholder="选好上级科目会自动接号" />
      </el-form-item>

      <el-form-item label="科目名称" required>
        <el-input v-model="form.name" placeholder="如：银行存款-中国银行" @keyup.enter="submit" />
      </el-form-item>

      <el-form-item label="科目类别" required>
        <!-- 选了上级科目，类别就跟着上级走：同一个大类里再分小类，
             否则「资产」下面挂一个「负债」科目，报表口径当场就乱了 -->
        <el-select v-model="form.category" :disabled="!!form.parent_id" style="width: 100%">
          <el-option v-for="(label, value) in CATEGORY_LABEL" :key="value" :label="label" :value="value" />
        </el-select>
      </el-form-item>

      <el-form-item label="余额方向" required>
        <el-radio-group v-model="form.direction">
          <el-radio value="DEBIT">借</el-radio>
          <el-radio value="CREDIT">贷</el-radio>
        </el-radio-group>
      </el-form-item>

      <!-- 编码规则提示贴在左下角：它是「说明」不是「一项」，压在表单右边最显眼的位置
           反而让人以为是必填项 -->
      <p class="sf-note"><em>注意：</em>科目编码规则：{{ rule }}。</p>
    </el-form>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import * as api from '@/api/subject'
import { useSessionStore } from '@/stores/session'
import { CATEGORY_DEFAULT_DIRECTION, CATEGORY_LABEL, nextCode } from '@/utils/subjectCode'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // 整棵科目树（前端要靠它算「父编码 + 下一个序号」）
  tree: { type: Array, default: () => [] },
  // 打开时就选好的上级科目：科目余额表的「下级」、凭证里点「+ 点击添加」都走这个
  parentId: { type: [Number, String], default: null }
})
const emit = defineEmits(['update:modelValue', 'saved'])

const store = useSessionStore()
const saving = ref(false)
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

const form = reactive({
  parent_id: null, code: '', name: '',
  category: 'ASSET', direction: 'DEBIT'
})

const rule = computed(() => store.currentBook?.subject_code_rule || '4-2-2-2')

/** 下拉里显示「编码 名称」：科目一多，光看名字根本分不清是哪一张 */
const selectTree = computed(() => props.tree.map(n => ({
  id: n.id,
  label: `${n.code} ${n.name}`,
  children: (n.children || []).map(c => ({
    id: c.id,
    label: `${c.code} ${c.name}`,
    children: (c.children || []).map(g => ({ id: g.id, label: `${g.code} ${g.name}` }))
  }))
})))

/** 摊平整棵树：算序号要看所有兄弟编码，不只是叶子 */
const allSubjects = computed(() => {
  const out = []
  const walk = (nodes) => nodes.forEach((n) => {
    out.push(n)
    if (n.children?.length) walk(n.children)
  })
  walk(props.tree)
  return out
})

const parentOf = (id) => allSubjects.value.find((s) => s.id === id) || null

// 打开时重置，并把上级科目带的属性一次性铺好
watch(() => props.modelValue, (open) => {
  if (!open) return
  const parent = parentOf(props.parentId)
  form.parent_id = parent ? parent.id : null
  form.name = ''
  form.category = parent ? parent.category : 'ASSET'
  form.direction = CATEGORY_DEFAULT_DIRECTION[form.category] || 'DEBIT'
  form.code = nextCode(rule.value, parent, allSubjects.value)
})

// 换上级科目 → 编码、类别、余额方向跟着重算
watch(() => form.parent_id, (id) => {
  const parent = parentOf(id)
  form.category = parent ? parent.category : form.category
  form.direction = CATEGORY_DEFAULT_DIRECTION[form.category] || 'DEBIT'
  form.code = nextCode(rule.value, parent, allSubjects.value)
})

async function submit() {
  if (!form.code.trim()) return ElMessage.warning('科目编码不能为空')
  if (!form.name.trim()) return ElMessage.warning('科目名称不能为空')
  saving.value = true
  try {
    const created = await api.createSubject(store.bookId, {
      parent_id: form.parent_id || null,
      code: form.code.trim(), name: form.name.trim(),
      category: form.category, direction: form.direction,
      is_leaf: true, carry_forward: false, sort_no: 0
    })
    ElMessage.success(`已新增科目 ${created.code} ${created.name}`)
    visible.value = false
    emit('saved', created)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.sf :deep(.el-form-item) { margin-bottom: 18px; }
/* 贴着弹窗左下角，不跟着 label 缩进 —— 原来往右顶了 110px，看着像表单的一部分 */
.sf-note { margin: 4px 0 0; font-size: 13px; color: #606266; }
.sf-note em { color: #f56c6c; font-style: normal; }
</style>
