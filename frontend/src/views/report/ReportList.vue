<template>
  <div class="page">
    <div class="page-head"><h2 class="page-title">报表</h2></div>
    <div class="card">
      <p class="muted" style="margin-top:0">
        选择一张报表查看数据，进入设计模式可调整行列和公式。报表名称可在本页或报表详情页修改。
      </p>
      <el-table :data="templates" border style="margin-top:12px">
        <el-table-column label="报表名称" min-width="200">
          <template #default="{ row }">
            <router-link :to="`/reports/${row.id}`" style="color:var(--cwq-primary);text-decoration:none">
              {{ row.name }}
            </router-link>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="260" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openReport(row)">打开</el-button>
            <el-button link type="primary" :disabled="!store.canWrite" @click="renameTemplate(row)">
              <el-icon><EditPen /></el-icon>重命名
            </el-button>
            <el-button link type="danger" :disabled="!store.canWrite || row.is_builtin"
                       :title="row.is_builtin ? '内置报表不能删除' : '删除报表'" @click="removeTemplate(row)">
              <el-icon><Delete /></el-icon>删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, EditPen } from '@element-plus/icons-vue'
import * as api from '@/api/report'
import { useSessionStore } from '@/stores/session'

const templates = ref([])
const store = useSessionStore()
const router = useRouter()

async function load() {
  templates.value = await api.templates()
}

function openReport(row) {
  router.push(`/reports/${row.id}`)
}

async function renameTemplate(row) {
  if (!store.canWrite) return
  const result = await ElMessageBox.prompt('输入新的报表名称', '重命名报表', {
    inputValue: row.name,
    confirmButtonText: '保存',
    cancelButtonText: '取消',
    inputValidator: (value) => value.trim() ? true : '报表名称不能为空',
  }).catch(() => null)
  const name = result?.value?.trim()
  if (!name || name === row.name) return
  await api.updateTemplate(row.id, { name }, store.bookId)
  row.name = name
  ElMessage.success('报表名称已更新')
}

async function removeTemplate(row) {
  if (!store.canWrite || row.is_builtin) return
  const confirmed = await ElMessageBox.confirm(
    `确定删除「${row.name}」？删除后该报表的行、列和公式都会一并删除。`,
    '删除报表', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).then(() => true).catch(() => false)
  if (!confirmed) return
  await api.deleteTemplate(row.id, store.bookId)
  await load()
  ElMessage.success('报表已删除')
}

onMounted(load)
</script>
