<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-input v-model="keyword" placeholder="按编码或名称筛选" clearable style="width: 240px" />
      <span class="spacer" />
      <el-button type="primary" :disabled="!store.canWrite" @click="openCreate()">新增科目</el-button>
    </div>

    <div class="card">
      <el-table :data="tree" row-key="id" border default-expand-all
                :tree-props="{ children: 'children' }" v-loading="loading">
        <el-table-column label="编码" width="120" prop="code" />
        <el-table-column label="科目名称" min-width="240" prop="name" />
        <el-table-column label="类别" width="110">
          <template #default="{ row }">{{ CATEGORY[row.category] || row.category }}</template>
        </el-table-column>
        <el-table-column label="余额方向" width="90" align="center">
          <template #default="{ row }">{{ row.direction === 'DEBIT' ? '借' : '贷' }}</template>
        </el-table-column>
        <el-table-column label="损益结转" width="90" align="center">
          <template #default="{ row }">{{ row.carry_forward ? '是' : '' }}</template>
        </el-table-column>
        <el-table-column v-if="store.canWrite" label="操作" width="150" align="center">
          <template #default="{ row }">
            <el-button link type="primary" @click="openCreate(row)">下级</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="visible" :title="parentId ? '新增下级科目' : '新增科目'" width="440px">
      <el-form :model="form" label-width="86px">
        <el-form-item label="编码"><el-input v-model="form.code" :disabled="!!form.id" /></el-form-item>
        <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="类别">
          <el-select v-model="form.category" style="width: 100%">
            <el-option v-for="(v, k) in CATEGORY" :key="k" :label="v" :value="k" />
          </el-select>
        </el-form-item>
        <el-form-item label="余额方向">
          <el-radio-group v-model="form.direction">
            <el-radio value="DEBIT">借</el-radio>
            <el-radio value="CREDIT">贷</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="损益结转">
          <el-switch v-model="form.carry_forward" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="visible = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '@/api/subject'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const tree = ref([])
const loading = ref(false)
const keyword = ref('')
const visible = ref(false)
const parentId = ref(null)

const CATEGORY = { ASSET: '资产', LIABILITY: '负债', EQUITY: '所有者权益', COST: '成本', PROFIT: '损益' }
const form = reactive({ id: null, code: '', name: '', category: 'ASSET', direction: 'DEBIT', carry_forward: false })

const filtered = computed(() => {
  if (!keyword.value) return tree.value
  const kw = keyword.value.toLowerCase()
  const walk = (nodes) => nodes.flatMap((n) => {
    const hit = n.code.toLowerCase().includes(kw) || n.name.toLowerCase().includes(kw)
    const kids = walk(n.children || [])
    return hit ? [n] : kids
  })
  return walk(tree.value)
})

async function load() {
  if (!store.bookId) return
  loading.value = true
  try { tree.value = await api.subjectTree(store.bookId) } finally { loading.value = false }
}

function openCreate(parent = null) {
  Object.assign(form, { id: null, code: '', name: '', category: parent?.category || 'ASSET',
    direction: parent?.category === 'LIABILITY' || parent?.category === 'EQUITY' || parent?.category === 'PROFIT' ? 'CREDIT' : 'DEBIT',
    carry_forward: parent?.category === 'PROFIT' })
  parentId.value = parent?.id || null
  visible.value = true
}

async function save() {
  const payload = { ...form, parent_id: parentId.value, is_leaf: true, sort_no: 0 }
  if (form.id) await api.updateSubject(store.bookId, form.id, payload)
  else await api.createSubject(store.bookId, payload)
  ElMessage.success('已保存')
  visible.value = false
  load()
}

async function remove(row) {
  await ElMessageBox.confirm(`确认删除科目 ${row.code} ${row.name}？`, '删除', { type: 'warning' })
  await api.deleteSubject(store.bookId, row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
watch(() => store.bookId, load)
</script>
