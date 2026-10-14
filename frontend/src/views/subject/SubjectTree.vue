<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-input v-model="keyword" placeholder="按编码或名称筛选" clearable style="width: 240px" />
      <span class="muted">科目编码规则 {{ rule }}</span>
      <span class="spacer" />
      <el-button :disabled="!store.canWrite" @click="importVisible = true">
        <el-icon><Upload /></el-icon>&nbsp;导入并覆盖
      </el-button>
      <el-button type="primary" :disabled="!store.canWrite" @click="openCreate()">新增科目</el-button>
    </div>

    <div class="card">
      <el-table :data="tree" row-key="id" border default-expand-all
                :tree-props="{ children: 'children' }" v-loading="loading">
        <el-table-column label="编码" width="120" prop="code" />
        <el-table-column label="科目名称" min-width="240" prop="name" />
        <el-table-column label="类别" width="90">
          <template #default="{ row }">{{ CATEGORY_LABEL[row.category] || row.category }}</template>
        </el-table-column>
        <el-table-column label="余额方向" width="80" align="center">
          <template #default="{ row }">{{ row.direction === 'DEBIT' ? '借' : '贷' }}</template>
        </el-table-column>
        <el-table-column v-if="store.canWrite" label="操作" width="190" align="center">
          <template #default="{ row }">
            <el-button link type="primary" @click="openCreate(row)">下级</el-button>
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <SubjectFormDialog v-model="visible" :tree="tree" :parent-id="parentId" />
    <SubjectEditDialog v-model="editVisible" :subject="editingSubject" @saved="load" />
    <SubjectImportDialog v-model="importVisible" @imported="onImported" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload } from '@element-plus/icons-vue'
import * as api from '@/api/subject'
import SubjectFormDialog from '@/components/SubjectFormDialog.vue'
import SubjectEditDialog from '@/components/SubjectEditDialog.vue'
import SubjectImportDialog from '@/components/SubjectImportDialog.vue'
import { useSessionStore } from '@/stores/session'
import { CATEGORY_LABEL } from '@/utils/subjectCode'

const store = useSessionStore()
const tree = ref([])
const loading = ref(false)
const keyword = ref('')
const visible = ref(false)
const parentId = ref(null)
const editVisible = ref(false)
const editingSubject = ref(null)
const importVisible = ref(false)

const rule = computed(() => store.currentBook?.subject_code_rule || '4-2-2-2')

async function load() {
  if (!store.bookId) return
  loading.value = true
  try { tree.value = await api.subjectTree(store.bookId) } finally { loading.value = false }
}

function openCreate(parent = null) {
  parentId.value = parent?.id || null
  visible.value = true
}

function openEdit(subject) {
  editingSubject.value = subject
  editVisible.value = true
}

async function onImported() {
  await store.loadBooks()
  await load()
  ElMessage.success('科目表已覆盖导入')
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
