<template>
  <el-dialog
    v-model="visible"
    title="导入科目表（覆盖当前科目）"
    :width="dialogWidth"
    :close-on-click-modal="false"
    @closed="reset"
  >
    <el-alert type="warning" :closable="false" show-icon class="mb">
      <template #title>这是账套初始化 / 迁移操作</template>
      <div>
        确认后会删除当前账套已有的全部科目，再写入文件中的科目。账套已有凭证或期初余额时，系统会拒绝覆盖。
      </div>
    </el-alert>

    <div v-if="step === 'upload'">
      <el-upload
        drag
        :auto-upload="false"
        :show-file-list="false"
        accept=".xls,.xlsx"
        :on-change="onFile"
        class="upload-box mb"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">把外部科目表拖到此处，或<em>点击选择文件</em></div>
        <template #tip>
          <div class="el-upload__tip">支持用友导出的 .xls：类型、级次、科目编码、科目名称、余额方向</div>
        </template>
      </el-upload>

      <div v-if="file" class="selected-file">
        <el-icon><Document /></el-icon>
        <span>{{ file.name }}</span>
      </div>
    </div>

    <div v-else-if="step === 'preview'">
      <div class="summary-grid">
        <div><span class="muted">文件</span><b>{{ result.filename }}</b></div>
        <div><span class="muted">解析科目</span><b>{{ result.total }} 个</b></div>
        <div><span class="muted">当前科目</span><b>{{ result.current?.subject_count || 0 }} 个</b></div>
        <div><span class="muted">导入编码规则</span><b>{{ result.code_rule || '无法识别' }}</b></div>
      </div>

      <el-alert
        v-if="result.error_count"
        type="error"
        :closable="false"
        show-icon
        class="mb"
        :title="`发现 ${result.error_count} 个问题，修正文件后才能覆盖导入`"
      >
        <ul class="error-list">
          <li v-for="error in visibleErrors" :key="`${error.line}-${error.code}-${error.message}`">
            第 {{ error.line }} 行{{ error.code ? `（${error.code}）` : '' }}：{{ error.message }}
          </li>
        </ul>
        <div v-if="result.errors.length > visibleErrors.length" class="muted">
          还有 {{ result.errors.length - visibleErrors.length }} 个问题未展开。
        </div>
      </el-alert>

      <el-alert v-else-if="!result.current?.can_overwrite" type="error" :closable="false" show-icon class="mb">
        {{ result.blocked_reason }}
      </el-alert>
      <el-alert v-else type="success" :closable="false" show-icon class="mb">
        文件校验通过，将删除当前 {{ result.current?.subject_count || 0 }} 个科目并导入 {{ result.total }} 个科目。
      </el-alert>

      <el-table :data="previewRows" size="small" border max-height="420" class="preview-table">
        <el-table-column prop="line" label="行号" width="64" align="center" />
        <el-table-column prop="code" label="编码" width="118" />
        <el-table-column prop="name" label="科目名称" min-width="250" show-overflow-tooltip />
        <el-table-column prop="level" label="级次" width="64" align="center" />
        <el-table-column label="类别" width="84">
          <template #default="{ row }">{{ CATEGORY_LABEL[row.category] || row.category || '错误' }}</template>
        </el-table-column>
        <el-table-column label="方向" width="64" align="center">
          <template #default="{ row }">{{ row.direction === 'DEBIT' ? '借' : row.direction === 'CREDIT' ? '贷' : '错误' }}</template>
        </el-table-column>
        <el-table-column prop="parent_code" label="上级编码" width="118" />
      </el-table>
      <div v-if="result.total > previewRows.length" class="table-note muted">
        预览前 {{ previewRows.length }} 条，确认时将按文件完整导入 {{ result.total }} 条。
      </div>
    </div>

    <div v-else class="success-state">
      <el-result icon="success" title="科目表已覆盖导入">
        <template #sub-title>
          已删除 {{ committed.deleted }} 个旧科目，导入 {{ committed.created }} 个新科目；编码规则已更新为 {{ committed.code_rule }}。
        </template>
      </el-result>
    </div>

    <template #footer>
      <el-button v-if="step === 'preview'" @click="step = 'upload'">重新选择文件</el-button>
      <el-button v-if="step !== 'done'" @click="visible = false">取消</el-button>
      <el-button v-if="step === 'upload'" type="primary" :disabled="!file" :loading="previewing" @click="preview">
        <el-icon><View /></el-icon>&nbsp;解析并预览
      </el-button>
      <el-button
        v-if="step === 'preview'"
        type="danger"
        :disabled="!canCommit"
        :loading="committing"
        @click="commit"
      >
        <el-icon><Refresh /></el-icon>&nbsp;确认覆盖并导入
      </el-button>
      <el-button v-if="step === 'done'" type="primary" @click="visible = false">完成</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document, Refresh, UploadFilled, View } from '@element-plus/icons-vue'
import * as api from '@/api/subject'
import { useSessionStore } from '@/stores/session'
import { CATEGORY_LABEL } from '@/utils/subjectCode'

const props = defineProps({ modelValue: { type: Boolean, default: false } })
const emit = defineEmits(['update:modelValue', 'imported'])
const store = useSessionStore()

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})
const step = ref('upload')
const file = ref(null)
const result = ref({ errors: [], subjects: [], total: 0, current: {} })
const committed = ref({ deleted: 0, created: 0, code_rule: '' })
const previewing = ref(false)
const committing = ref(false)

const visibleErrors = computed(() => (result.value.errors || []).slice(0, 12))
const previewRows = computed(() => (result.value.subjects || []).slice(0, 250))
const canCommit = computed(() => !!file.value && result.value.ok && result.value.current?.can_overwrite)

/**
 * 宽度跟着步骤走，不是一个宽度用到底。
 *
 * 上传步只有一句警告、一个拖拽框和一个文件名，铺开 1080px 的话中间全是空白，
 * 框越宽越像没做完 —— 收到 560px，和本项目其它表单弹窗（420/520px）一个体量。
 *
 * 预览步反过来必须宽：那 7 列里除「科目名称」外都是定宽，64+118+64+84+64+118
 * 已经占掉 512px，「科目名称」最少还要 250px，再加弹窗两侧各 16px 内边距，
 * 低于 800px 就得横向滚动。所以这一步给到 880px，余量全给科目名称。
 */
const dialogWidth = computed(() =>
  step.value === 'preview' ? 'min(880px, calc(100vw - 32px))' : 'min(560px, calc(100vw - 32px))')

function onFile(uploadFile) {
  const name = uploadFile.raw?.name || uploadFile.name || ''
  if (!/\.xlsx?$/i.test(name)) {
    ElMessage.warning('请选择 .xls 或 .xlsx 科目表')
    return
  }
  file.value = uploadFile.raw
}

async function preview() {
  if (!file.value) return
  previewing.value = true
  try {
    result.value = await api.previewSubjectImport(store.bookId, file.value)
    step.value = 'preview'
  } finally {
    previewing.value = false
  }
}

async function commit() {
  try {
    await ElMessageBox.confirm(
      `确认覆盖当前科目表？这会删除 ${result.value.current.subject_count} 个旧科目并导入 ${result.value.total} 个科目。该操作只适用于没有凭证和期初余额的初始化/迁移账套。`,
      '确认覆盖导入',
      { type: 'warning', confirmButtonText: '确认覆盖', cancelButtonText: '返回检查' }
    )
  } catch {
    return
  }
  committing.value = true
  try {
    committed.value = await api.importSubjects(store.bookId, file.value)
    step.value = 'done'
    emit('imported')
  } finally {
    committing.value = false
  }
}

function reset() {
  step.value = 'upload'
  file.value = null
  result.value = { errors: [], subjects: [], total: 0, current: {} }
  committed.value = { deleted: 0, created: 0, code_rule: '' }
}
</script>

<style scoped>
.mb { margin-bottom: 14px; }
.upload-box { width: 100%; }
/* 拖拽框：Element Plus 默认上下各留 40px、图标 67px，一块空地就有近 200px 高。
   弹窗收窄之后这块空白更扎眼 —— 收到 26px、图标缩到 44px，
   够放一句「拖到这里」就行，不必摆一个可以站人的广场。 */
.upload-box :deep(.el-upload-dragger) { padding: 26px 16px; border-radius: 8px; }
.upload-box :deep(.el-upload-dragger .el-icon--upload) { margin-bottom: 10px; font-size: 44px; line-height: 40px; }
.selected-file { display: flex; align-items: center; gap: 8px; padding: 10px 12px; color: #334155; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.summary-grid > div { display: flex; flex-direction: column; gap: 4px; min-width: 0; padding: 10px 12px; border: 1px solid #e5e7eb; background: #fff; }
.summary-grid b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.error-list { max-height: 150px; overflow: auto; margin: 4px 0 0; padding-left: 20px; line-height: 1.7; }
.preview-table { margin-top: 8px; }
.table-note { padding-top: 8px; font-size: 12px; }
.success-state { padding: 16px 0 4px; }
@media (max-width: 720px) {
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
