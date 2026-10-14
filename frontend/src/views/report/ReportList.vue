<template>
  <div class="page report-list-page">
    <div class="page-head report-head">
      <div>
        <h2 class="page-title">报表</h2>
        <p class="page-subtitle">查看账套报表，或把其他系统的报表模板迁移进来继续使用。</p>
      </div>
      <el-button type="primary" :disabled="!store.canWrite || !store.bookId" @click="showImport = true">
        <el-icon><Upload /></el-icon>
        导入模板
      </el-button>
    </div>

    <div class="card report-card">
      <div class="section-heading">
        <div>
          <h3>报表模板</h3>
          <p>模板结构和公式在系统内共享，打开时按当前账套与会计期间重新计算金额。</p>
        </div>
        <span class="template-count">{{ templates.length }} 张</span>
      </div>

      <el-table v-loading="loading" :data="templates" border class="report-table">
        <el-table-column label="报表名称" min-width="280">
          <template #default="{ row }">
            <router-link :to="`/reports/${row.id}`" class="report-name">{{ row.name }}</router-link>
            <div class="report-meta">
              <el-tag v-if="row.is_builtin" size="small" effect="plain" type="info">内置</el-tag>
              <el-tag v-else-if="row.code?.startsWith('IMPORTED_')" size="small" effect="plain" type="success">已导入</el-tag>
              <span>{{ typeName(row.type) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="来源说明" min-width="320">
          <template #default="{ row }">
            <span class="report-remark">{{ row.remark || '系统内置模板，可直接查看和设计。' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="280" align="right">
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
        <template #empty>
          <el-empty description="还没有报表模板" />
        </template>
      </el-table>

      <div class="migration-tip">
        <el-icon><InfoFilled /></el-icon>
        <span>支持用友 UFO 5.10 / 8.00 的 <code>.rep</code>、<code>.rbk</code> 模板。导入前会先预览行列、公式和科目匹配情况。</span>
      </div>
    </div>

    <ReportImportDialog v-model="showImport" @imported="load" />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, EditPen, InfoFilled, Upload } from '@element-plus/icons-vue'
import * as api from '@/api/report'
import { useSessionStore } from '@/stores/session'
import ReportImportDialog from '@/components/ReportImportDialog.vue'

const templates = ref([])
const loading = ref(false)
const showImport = ref(false)
const store = useSessionStore()
const router = useRouter()

const typeName = type => ({
  BALANCE_SHEET: '资产负债表',
  INCOME_SHEET: '利润表',
  CUSTOM: '自定义报表',
}[type] || '报表')

async function load() {
  loading.value = true
  try {
    templates.value = await api.templates()
  } finally {
    loading.value = false
  }
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
    inputValidator: value => value.trim() ? true : '报表名称不能为空',
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

<style scoped>
.report-head { align-items: flex-start; }
.page-subtitle { margin: 7px 0 0; color: #8b98aa; font-size: 12px; line-height: 1.5; }
.report-head > .el-button { margin-top: 1px; }
.report-head > .el-button :deep(.el-icon) { margin-right: 5px; }
.report-card { padding-top: 18px; }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
.section-heading h3 { margin: 0; color: #33435a; font-size: 15px; font-weight: 600; }
.section-heading p { margin: 6px 0 0; color: #8a97a9; font-size: 12px; }
.template-count { color: #8c9aae; font-size: 12px; white-space: nowrap; }
.report-name { color: var(--cwq-primary); text-decoration: none; font-weight: 500; }
.report-name:hover { text-decoration: underline; }
.report-meta { display: flex; align-items: center; gap: 7px; margin-top: 7px; color: #9aa5b5; font-size: 11px; }
.report-meta :deep(.el-tag) { height: 20px; line-height: 18px; }
.report-remark { color: #7e8da2; font-size: 12px; line-height: 1.5; }
.migration-tip { display: flex; align-items: flex-start; gap: 7px; margin-top: 15px; padding: 11px 13px; border: 1px solid #e1eafb; border-radius: 7px; background: #f6f9ff; color: #7f90aa; font-size: 12px; line-height: 1.6; }
.migration-tip .el-icon { margin-top: 3px; color: #7c9bd3; flex-shrink: 0; }
.migration-tip code { padding: 1px 4px; border-radius: 3px; background: #e9f0fd; color: #617fae; font-family: inherit; }
</style>
