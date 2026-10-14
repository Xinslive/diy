<template>
  <el-dialog :model-value="modelValue" title="导入报表模板" width="min(1120px, calc(100vw - 40px))"
             class="report-import-dialog" :close-on-click-modal="false" :close-on-press-escape="!busy"
             :show-close="!busy" destroy-on-close @update:model-value="emit('update:modelValue', $event)" @closed="reset">
    <div class="import-stepper" aria-label="导入进度">
      <div v-for="(title, index) in ['选择文件', '预览与匹配', '导入完成']" :key="title"
           class="import-step" :class="{ current: step === index, finished: step > index }">
        <span class="step-number"><el-icon v-if="step > index"><Check /></el-icon><template v-else>{{ index + 1 }}</template></span>
        <span>{{ title }}</span>
      </div>
    </div>

    <div v-if="step === 0" class="choose-files">
      <div class="upload-intro">
        <h3>把原系统的报表模板带过来</h3>
        <p>保留报表项目、行次、左右栏和可转换的取数公式，导入后可继续编辑。</p>
      </div>
      <el-upload ref="uploadRef" drag multiple :auto-upload="false" :show-file-list="false" accept=".rep,.rbk"
                 :limit="10" :on-change="addFile" :on-exceed="onExceed" :disabled="busy" class="template-upload">
        <div class="upload-symbol"><el-icon><Upload /></el-icon></div>
        <div class="upload-title">拖拽模板文件到这里，或<span>选择文件</span></div>
        <div class="upload-description">用友 UFO 5.10 / 8.00 · .rep、.rbk · 单个文件不超过 5 MB</div>
      </el-upload>
      <div class="upload-notes">
        <span><el-icon><DocumentCopy /></el-icon>一次最多 10 个模板</span>
        <span><el-icon><Connection /></el-icon>相同内容的 .rep / .rbk 会自动去重</span>
      </div>
      <div v-if="files.length" class="selected-files">
        <div v-for="file in files" :key="file.uid" class="selected-file">
          <div class="file-format">{{ file.raw.name.split('.').pop().toUpperCase() }}</div>
          <div class="selected-file-copy"><strong>{{ file.raw.name }}</strong><span :class="{ 'file-error': file.error }">{{ file.error || formatSize(file.raw.size) }}</span></div>
          <el-icon v-if="file.parsing" class="is-loading"><Loading /></el-icon>
          <el-button v-else text circle :disabled="busy" :aria-label="`移除 ${file.raw.name}`" @click="removeFile(file.uid)"><el-icon><Close /></el-icon></el-button>
        </div>
      </div>
      <div class="import-scope-note"><el-icon><InfoFilled /></el-icon>仅导入模板结构和公式。模板在系统内共享，金额按打开报表时的账套与期间重新计算。</div>
    </div>

    <div v-else-if="step === 1" class="review-import">
      <aside class="template-file-sidebar" aria-label="待导入文件">
        <div class="sidebar-caption">已解析 {{ parsedFiles.length }} 个文件</div>
        <button v-for="file in parsedFiles" :key="file.uid" class="template-file-tab" :class="{ active: activeUid === file.uid }"
                type="button" @click="activeUid = file.uid">
          <div class="file-tab-top"><el-icon><Document /></el-icon><strong>{{ file.preview.document.name }}</strong></div>
          <div class="file-tab-meta">{{ typeName(file.preview.document.type) }} · {{ file.preview.stats.rows }} 行</div>
          <div class="file-tab-bottom">
            <span v-if="file.duplicate" class="muted">重复文件，跳过</span>
            <span v-else-if="file.preview.existing" class="muted">已经导入</span>
            <template v-else><span class="pending-count" v-if="pendingFor(file)">{{ pendingFor(file) }} 项待检查</span><span v-else class="ready-count">校验通过</span><el-checkbox v-model="file.selected" @click.stop @change="acceptedWarnings = false" aria-label="选择导入此模板" /></template>
          </div>
        </button>
        <div v-for="file in failedFiles" :key="file.uid" class="failed-file"><strong>{{ file.raw.name }}</strong><span>{{ file.error }}</span></div>
      </aside>
      <main v-if="activeFile" class="template-review-main">
        <div class="review-heading">
          <div><span class="review-kicker">{{ activeFile.preview.document.source.format }}</span><h3>{{ typeName(activeFile.preview.document.type) }}</h3></div>
          <el-tag effect="plain" type="info">{{ activeFile.raw.name.split('.').pop().toUpperCase() }} 模板</el-tag>
        </div>
        <el-alert v-if="activeFile.duplicate" :title="`与「${activeFile.duplicate}」内容相同，将自动跳过此文件。`" type="info" :closable="false" show-icon class="review-alert" />
        <el-alert v-else-if="activeFile.preview.existing" title="此模板已导入，已有模板可以直接打开。" type="info" :closable="false" show-icon class="review-alert">
          <el-button link type="primary" @click="openReport(activeFile.preview.existing.id)">打开已有模板</el-button>
        </el-alert>
        <div class="template-name-field"><label for="import-template-name">导入后的名称</label><el-input id="import-template-name" v-model="activeFile.preview.document.name" maxlength="128" :disabled="busy || !!activeFile.duplicate || !!activeFile.preview.existing" /></div>
        <div class="import-metrics">
          <div><strong>{{ activeFile.preview.stats.rows }}</strong><span>项目行</span></div>
          <div><strong>{{ activeFile.preview.stats.cols }}</strong><span>报表列</span></div>
          <div><strong>{{ activeFile.preview.stats.converted_formulas }}</strong><span>已转换公式</span></div>
          <div :class="{ 'has-pending': pendingFor(activeFile) }"><strong>{{ pendingFor(activeFile) }}</strong><span>待检查项</span></div>
        </div>
        <el-tabs v-model="previewTab" class="preview-tabs">
          <el-tab-pane label="结构预览" name="structure">
            <div class="template-preview-scroll">
              <table class="template-preview-table">
                <colgroup><col v-for="col in activeFile.preview.document.cols" :key="col.col_no" :style="{ width: `${col.width}px` }" /></colgroup>
                <thead><tr><th v-for="col in activeFile.preview.document.cols" :key="col.col_no">{{ col.label }}</th></tr></thead>
                <tbody><tr v-for="row in activeFile.preview.document.rows" :key="row.row_no">
                  <td v-for="(col, index) in activeFile.preview.document.cols" :key="col.col_no" :class="previewCellClass(row, col, index)"
                      :title="cellFormula(row.row_no, col.col_no)">
                    <span v-if="col.kind === 'STATIC'" :style="{ paddingLeft: `${sideIndent(row, index) * 8}px` }">{{ sideLabel(row, index) }}</span>
                    <span v-else-if="col.kind === 'LINE_NO'">{{ sideLine(row, index) }}</span>
                    <span v-else-if="issueAt(row.row_no, col.col_no)" class="pending-formula">待配置</span>
                    <span v-else-if="cellFormula(row.row_no, col.col_no)" class="formula-preview"><span class="fx-symbol">ƒx</span>{{ formulaSummary(cellFormula(row.row_no, col.col_no)) }}</span>
                  </td>
                </tr></tbody>
              </table>
            </div>
            <p class="preview-caption">预览显示模板结构与公式，悬停金额格可查看完整公式；导入后再按当前账套计算金额。</p>
          </el-tab-pane>
          <el-tab-pane :label="`科目匹配${missingFor(activeFile) ? ` (${missingFor(activeFile)})` : ''}`" name="subjects">
            <div class="mapping-toolbar"><span>按编码匹配当前账套，可手动选择替代科目。</span><el-checkbox v-model="onlyMissing">仅看未匹配</el-checkbox></div>
            <el-table :data="shownSubjects" max-height="360" size="small" class="mapping-table">
              <el-table-column prop="code" label="原科目编码" width="108" />
              <el-table-column label="关联项目" min-width="140"><template #default="{ row }"><span>{{ subjectLabel(row.code) }}</span></template></el-table-column>
              <el-table-column label="当前账套科目" min-width="240"><template #default="{ row }">
                <el-select v-model="activeFile.subjectMapping[row.code]" filterable clearable placeholder="选择目标科目，或稍后配置" size="small" :disabled="busy" @change="acceptedWarnings = false" style="width:100%">
                  <el-option v-for="subject in activeFile.preview.available_subjects" :key="subject.code" :value="subject.code" :label="`${subject.code} ${subject.name}`" />
                </el-select>
              </template></el-table-column>
              <el-table-column label="状态" width="88"><template #default="{ row }"><el-tag size="small" :type="isMatched(activeFile, row) ? 'success' : 'warning'" effect="plain">{{ isMatched(activeFile, row) ? '已匹配' : '待匹配' }}</el-tag></template></el-table-column>
              <template #empty>所有科目均已匹配</template>
            </el-table>
          </el-tab-pane>
          <el-tab-pane :label="`待配置公式${activeFile.preview.document.issues.length ? ` (${activeFile.preview.document.issues.length})` : ''}`" name="issues">
            <el-empty v-if="!activeFile.preview.document.issues.length" description="所有可读取公式均已转换" :image-size="72" />
            <template v-else>
              <div class="pending-explanation"><el-icon><Warning /></el-icon><span>这些金额格会留空，请在导入后进入「设计报表」补齐公式。</span></div>
              <el-table :data="activeFile.preview.document.issues" max-height="360" size="small">
                <el-table-column prop="source_address" label="原单元格" width="90" />
                <el-table-column prop="label" label="项目" min-width="140" />
                <el-table-column prop="source_formula" label="原内容" min-width="160" show-overflow-tooltip />
                <el-table-column prop="reason" label="处理说明" min-width="220" />
              </el-table>
            </template>
          </el-tab-pane>
        </el-tabs>
        <div v-for="warning in activeFile.preview.warnings" :key="warning" class="formula-scope-note"><el-icon><InfoFilled /></el-icon><span>{{ warning }}</span></div>
      </main>
    </div>

    <div v-else class="import-complete">
      <el-result icon="success" :title="`已导入 ${created.length} 张报表模板`" sub-title="模板已加入报表列表，可打开报表或进入设计模式继续调整。" />
      <div class="created-template-list"><div v-for="template in created" :key="template.id"><div><strong>{{ template.name }}</strong><span v-if="template.pending_formulas || template.missing_subjects">{{ template.missing_subjects }} 个科目、{{ template.pending_formulas }} 个公式待配置</span><span v-else>结构和公式已就绪</span></div><el-button link type="primary" @click="openReport(template.id)">打开报表 <el-icon><ArrowRight /></el-icon></el-button></div></div>
    </div>

    <template #footer>
      <div class="import-dialog-footer">
        <div class="import-footer-copy">
          <template v-if="step === 0"><span>{{ busy ? `正在解析 ${parseProgress} / ${files.length}` : files.length ? `已选择 ${files.length} 个文件` : '选择原系统导出的报表模板' }}</span></template>
          <template v-else-if="step === 1"><el-checkbox v-if="selectedPending" v-model="acceptedWarnings" :disabled="busy">已核对 {{ selectedMissing }} 个未匹配科目、{{ selectedIssues }} 个待配置金额格，导入后继续配置</el-checkbox><span v-else>以新模板导入 · 已选 {{ selectedFiles.length }} 张</span></template>
          <span v-else>模板已保存，金额按当前账套重新计算</span>
        </div>
        <div class="import-footer-actions">
          <template v-if="step === 0"><el-button :disabled="busy" @click="close">取消</el-button><el-button type="primary" :disabled="!files.length" :loading="busy" @click="parseFiles">{{ busy ? '解析中' : '解析并预览' }}<el-icon v-if="!busy"><ArrowRight /></el-icon></el-button></template>
          <template v-else-if="step === 1"><el-button :disabled="busy" @click="backToFiles">重新选择</el-button><el-button type="primary" :loading="busy" :disabled="!canImport" @click="commit">确认导入 {{ selectedFiles.length }} 张</el-button></template>
          <template v-else><el-button @click="reset">继续导入</el-button><el-button type="primary" @click="close">完成</el-button></template>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowRight, Check, Close, Connection, Document, DocumentCopy, InfoFilled, Loading, Upload, Warning } from '@element-plus/icons-vue'
import * as api from '@/api/report'
import { useSessionStore } from '@/stores/session'

const props = defineProps({ modelValue: Boolean })
const emit = defineEmits(['update:modelValue', 'imported'])
const store = useSessionStore()
const router = useRouter()
const uploadRef = ref(null)
const step = ref(0)
const files = ref([])
const busy = ref(false)
const activeUid = ref(null)
const previewTab = ref('structure')
const onlyMissing = ref(false)
const acceptedWarnings = ref(false)
const parseProgress = ref(0)
const created = ref([])
const typeName = type => ({ BALANCE_SHEET: '资产负债表', INCOME_SHEET: '利润表' }[type] || '自定义报表')
const formatSize = size => size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB` : `${(size / (1024 * 1024)).toFixed(1)} MB`
const parsedFiles = computed(() => files.value.filter(file => file.preview))
const failedFiles = computed(() => files.value.filter(file => file.error && !file.preview))
const activeFile = computed(() => parsedFiles.value.find(file => file.uid === activeUid.value))
const selectedFiles = computed(() => parsedFiles.value.filter(file => file.selected && !file.duplicate && !file.preview.existing))
const isMatched = (file, subject) => !!file.subjectMapping[subject.code] || subject.matched
const missingFor = file => file.preview.subjects.filter(subject => !isMatched(file, subject)).length
const pendingFor = file => missingFor(file) + file.preview.document.issues.length
const selectedMissing = computed(() => selectedFiles.value.reduce((sum, file) => sum + missingFor(file), 0))
const selectedIssues = computed(() => selectedFiles.value.reduce((sum, file) => sum + file.preview.document.issues.length, 0))
const selectedPending = computed(() => selectedMissing.value + selectedIssues.value)
const canImport = computed(() => selectedFiles.value.length > 0 && selectedFiles.value.every(file => file.preview.document.name.trim()) && (!selectedPending.value || acceptedWarnings.value))
const shownSubjects = computed(() => activeFile.value?.preview.subjects.filter(subject => !onlyMissing.value || !isMatched(activeFile.value, subject)) || [])
const cellLookup = computed(() => Object.fromEntries((activeFile.value?.preview.document.cells || []).map(cell => [`${cell.row_no}:${cell.col_no}`, cell.formula])))
const issueLookup = computed(() => new Set((activeFile.value?.preview.document.issues || []).map(issue => `${issue.row_no}:${issue.col_no}`)))
const issueAt = (row, col) => issueLookup.value.has(`${row}:${col}`)
const rightSide = index => activeFile.value?.preview.document.cols.length === 8 && index >= 4
const sideLabel = (row, index) => rightSide(index) ? row.style.label_right : row.label
const sideLine = (row, index) => rightSide(index) ? row.style.line_right : row.style.line_left
const sideIndent = (row, index) => rightSide(index) ? row.style.indent_right : row.indent
function previewCellClass(row, col, index) {
  const side = rightSide(index) ? 'right' : 'left'
  return { 'preview-fixed': col.kind === 'STATIC', 'preview-line': col.kind === 'LINE_NO', 'preview-shaded': (row.style[`kind_${side}`] || row.style.kind) === 'SHADE', 'preview-bold': side === 'right' ? row.style.bold_right : row.bold }
}
function cellFormula(row, col) {
  const formula = cellLookup.value[`${row}:${col}`] || ''
  return formula.replace(/科目\("([^"\\]+)",/g, (match, code) => `科目("${activeFile.value?.subjectMapping[code] || code}",`)
}
function formulaSummary(formula) {
  const subjects = [...formula.matchAll(/科目\("([^"]+)","([^"]+)"/g)]
  if (subjects.length) return `${subjects[0][1]} · ${subjects[0][2]}${subjects.length > 1 ? ` 等 ${subjects.length} 项` : ''}`
  return formula
}
function subjectLabel(code) {
  const cell = activeFile.value?.preview.document.cells.find(cell => cell.formula.includes(`科目("${code}",`))
  const row = activeFile.value?.preview.document.rows.find(row => row.row_no === cell?.row_no)
  return cell && row ? (cell.col_no >= 5 ? row.style.label_right : row.label) : '—'
}
function addFile(uploadFile) {
  if (files.value.some(file => file.uid === uploadFile.uid)) return
  if (!/\.(rep|rbk)$/i.test(uploadFile.raw.name)) {
    uploadRef.value?.handleRemove(uploadFile)
    return ElMessage.warning('请选择 .rep 或 .rbk 报表模板')
  }
  if (uploadFile.raw.size > 5 * 1024 * 1024) {
    uploadRef.value?.handleRemove(uploadFile)
    return ElMessage.warning('单个模板最大 5 MB')
  }
  files.value.push({ uid: uploadFile.uid, raw: uploadFile.raw, preview: null, error: '', selected: true, parsing: false, duplicate: '', subjectMapping: {} })
}
function onExceed() { ElMessage.warning('一次最多选择 10 个模板文件') }
function removeFile(uid) {
  const uploadFile = uploadRef.value?.uploadFiles?.find(file => file.uid === uid)
  if (uploadFile) uploadRef.value.handleRemove(uploadFile)
  files.value = files.value.filter(file => file.uid !== uid)
}
async function parseFiles() {
  if (busy.value || !files.value.length) return
  busy.value = true
  parseProgress.value = 0
  const fingerprints = new Map()
  try {
    for (const file of files.value) {
      file.parsing = true
      file.error = ''
      file.preview = null
      file.duplicate = ''
      try {
        file.preview = await api.previewTemplateImport(file.raw, store.bookId)
        const fingerprint = file.preview.document.source.fingerprint
        file.duplicate = fingerprints.get(fingerprint) || ''
        if (!file.duplicate) fingerprints.set(fingerprint, file.raw.name)
        file.selected = !file.duplicate && !file.preview.existing
        file.subjectMapping = Object.fromEntries(file.preview.subjects.filter(subject => subject.matched).map(subject => [subject.code, subject.code]))
      } catch (error) {
        file.error = typeof error.response?.data?.detail === 'string' ? error.response.data.detail : '解析失败，请检查文件或重试'
      } finally {
        file.parsing = false
        parseProgress.value++
      }
    }
    if (parsedFiles.value.length) {
      activeUid.value = (selectedFiles.value[0] || parsedFiles.value[0]).uid
      step.value = 1
    }
  } finally { busy.value = false }
}
async function commit() {
  if (busy.value || !canImport.value) return
  busy.value = true
  try {
    const documents = selectedFiles.value.map(file => ({ ...file.preview.document, subject_mapping: Object.fromEntries(Object.entries(file.subjectMapping).filter(([code, target]) => target && code !== target)) }))
    const result = await api.importTemplates(documents, store.bookId, acceptedWarnings.value)
    created.value = result.created
    step.value = 2
    emit('imported', result.created)
  } catch { /* API 拦截器展示原因，保留预览和映射以便重试。 */ }
  finally { busy.value = false }
}
function backToFiles() { step.value = 0; acceptedWarnings.value = false; previewTab.value = 'structure'; uploadRef.value?.clearFiles() }
function close() { emit('update:modelValue', false) }
function openReport(id) { close(); router.push(`/reports/${id}`) }
function reset() {
  if (busy.value) return
  step.value = 0; files.value = []; activeUid.value = null; created.value = []
  previewTab.value = 'structure'; acceptedWarnings.value = false; onlyMissing.value = false
  uploadRef.value?.clearFiles()
}
</script>

<style scoped>
.import-stepper { display:flex; align-items:center; gap:64px; padding:2px 0 22px; border-bottom:1px solid var(--cwq-border); margin-bottom:22px; }
.import-step { display:flex; align-items:center; gap:9px; color:#8b95a5; font-size:13px; }
.step-number { width:24px; height:24px; display:flex; align-items:center; justify-content:center; border:1px solid #dce2eb; border-radius:50%; font-size:12px; }
.import-step.current { color:var(--cwq-primary); font-weight:600; }.current .step-number { background:var(--cwq-primary); border-color:var(--cwq-primary); color:white; }.finished { color:#15803d; }.finished .step-number { background:#f0fdf4; border-color:#bbf7d0; }
.choose-files { max-width:770px; margin:0 auto; padding:5px 0 10px; }.upload-intro { margin-bottom:22px; }.upload-intro h3 { margin:0 0 8px; font-size:18px; font-weight:600; color:#273449; }.upload-intro p { margin:0; line-height:1.6; color:#768397; }
.template-upload :deep(.el-upload) { width:100%; }.template-upload :deep(.el-upload-dragger) { padding:32px 20px; border-radius:10px; border-color:#cad5e4; background:#fbfcff; }.template-upload :deep(.el-upload-dragger:hover) { border-color:var(--cwq-primary); }
.upload-symbol { width:48px; height:48px; display:flex; align-items:center; justify-content:center; margin:0 auto 16px; background:#edf3ff; border:1px solid #dae6ff; color:var(--cwq-primary); border-radius:12px; font-size:24px; }.upload-title { font-size:15px; font-weight:500; color:#34425a; }.upload-title span { color:var(--cwq-primary); margin-left:4px; }.upload-description { font-size:12px; color:#8390a3; margin-top:9px; }.upload-notes { display:flex; flex-wrap:wrap; gap:20px; margin-top:13px; color:#8390a3; font-size:12px; }.upload-notes span { display:flex; gap:5px; align-items:center; }
.selected-files { border:1px solid #e8edf4; border-radius:8px; margin-top:23px; max-height:220px; overflow:auto; }.selected-file { display:flex; align-items:center; gap:12px; padding:11px 13px; }.selected-file + .selected-file { border-top:1px solid #eef1f6; }.file-format { display:flex; width:40px; height:40px; align-items:center; justify-content:center; font-size:10px; font-weight:700; color:#6478a1; background:#f1f5fc; border:1px solid #e3eaf5; border-radius:6px; }.selected-file-copy { flex:1; min-width:0; }.selected-file-copy strong,.selected-file-copy span { display:block; }.selected-file-copy strong { font-size:13px; font-weight:500; overflow-wrap:anywhere; }.selected-file-copy span { margin-top:4px; font-size:12px; color:#95a0b1; }.selected-file-copy .file-error { color:#dc2626; }
.import-scope-note { display:flex; gap:7px; align-items:flex-start; margin-top:23px; font-size:12px; line-height:1.7; color:#8390a3; }.import-scope-note .el-icon { margin-top:4px; flex-shrink:0; }
.review-import { display:grid; grid-template-columns:218px minmax(0,1fr); gap:24px; min-height:470px; }.template-file-sidebar { border-right:1px solid #e8edf4; padding-right:16px; max-height:570px; overflow:auto; }.sidebar-caption { font-size:12px; color:#95a0b1; margin:0 0 12px 8px; }.template-file-tab { width:100%; text-align:left; margin:0 0 8px; padding:13px 10px; border:1px solid transparent; background:transparent; border-radius:8px; cursor:pointer; font:inherit; color:inherit; }.template-file-tab.active { background:#eff5ff; border-color:#dce8fe; }.file-tab-top { display:flex; gap:7px; align-items:flex-start; }.file-tab-top .el-icon { margin-top:2px; color:#7594c8; flex-shrink:0; }.file-tab-top strong { font-size:12px; line-height:1.55; font-weight:500; overflow-wrap:anywhere; }.file-tab-meta { color:#91a0b3; font-size:11px; margin:7px 0 0 21px; }.file-tab-bottom { display:flex; justify-content:space-between; align-items:center; margin:7px 0 0 21px; font-size:11px; }.file-tab-bottom :deep(.el-checkbox) { height:20px; }.pending-count { color:#b77922; }.ready-count { color:#329467; }.failed-file { padding:10px; border-top:1px solid #e8edf4; font-size:11px; }.failed-file strong,.failed-file span { display:block; font-weight:400; overflow-wrap:anywhere; }.failed-file span { color:#dc2626; margin-top:5px; line-height:1.5; }
.template-review-main { min-width:0; }.review-heading { display:flex; align-items:center; justify-content:space-between; margin-bottom:17px; }.review-kicker { font-size:11px; color:#8a9bb1; }.review-heading h3 { margin:5px 0 0; font-size:17px; color:#2c3d55; font-weight:600; }.review-alert { margin-bottom:13px; }.template-name-field { display:flex; gap:12px; align-items:center; }.template-name-field label { white-space:nowrap; font-size:12px; color:#8090a7; }.import-metrics { display:flex; gap:0; background:#f7f9fc; border:1px solid #edf1f6; border-radius:8px; margin:17px 0 11px; padding:13px 8px; }.import-metrics>div { width:25%; padding-left:16px; }.import-metrics>div + div { border-left:1px solid #e5ebf3; }.import-metrics strong { display:block; color:#354963; font-size:20px; font-weight:600; font-variant-numeric:tabular-nums; }.import-metrics span { display:block; color:#96a2b5; font-size:11px; margin-top:3px; }.import-metrics .has-pending strong { color:#b77922; }.preview-tabs :deep(.el-tabs__item) { font-size:12px; }
.template-preview-scroll { overflow:auto; max-height:345px; border:1px solid #e4eaf3; border-radius:6px; }.template-preview-table { width:100%; min-width:620px; table-layout:fixed; border-collapse:collapse; font-size:11px; }.template-review-main:has(.template-preview-table col:nth-child(8)) .template-preview-table { min-width:1090px; }.template-preview-table th { position:sticky; top:0; z-index:1; background:#f6f8fc; color:#697d99; padding:10px 6px; border-bottom:1px solid #e4eaf3; font-weight:500; }.template-preview-table th + th,.template-preview-table td + td { border-left:1px solid #edf1f6; }.template-preview-table td { height:30px; padding:7px 7px; border-bottom:1px solid #edf1f6; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#4b607c; }.template-preview-table .preview-fixed { color:#3f526b; }.template-preview-table .preview-line { color:#92a0b3; text-align:center; }.template-preview-table .preview-shaded { background:#f3f6fa; }.template-preview-table .preview-bold { font-weight:600; }.formula-preview { color:#8a9bb3; font-size:10px; }.fx-symbol { color:#7295c7; margin-right:4px; }.pending-formula { color:#b98532; font-size:10px; }.preview-caption { color:#98a5b8; font-size:11px; line-height:1.5; margin:10px 0 0; }.mapping-toolbar { display:flex; justify-content:space-between; align-items:center; font-size:11px; color:#8c9aae; margin-bottom:10px; gap:10px; }.mapping-toolbar :deep(.el-checkbox__label) { font-size:11px; }.pending-explanation,.formula-scope-note { display:flex; align-items:flex-start; gap:6px; font-size:11px; line-height:1.6; }.pending-explanation { color:#b27c2d; margin:4px 0 14px; }.pending-explanation .el-icon,.formula-scope-note .el-icon { margin-top:3px; flex-shrink:0; }.formula-scope-note { color:#8c9aae; margin-top:12px; }
.import-dialog-footer { display:flex; justify-content:space-between; align-items:center; gap:20px; text-align:left; border-top:1px solid #e8edf4; padding-top:16px; }.import-footer-copy { color:#92a0b2; font-size:12px; min-width:0; }.import-footer-copy :deep(.el-checkbox) { height:auto; white-space:normal; align-items:flex-start; }.import-footer-copy :deep(.el-checkbox__input) { margin-top:2px; }.import-footer-copy :deep(.el-checkbox__label) { font-size:11px; line-height:1.6; }.import-footer-actions { display:flex; gap:8px; flex-shrink:0; }.import-footer-actions .el-button + .el-button { margin-left:0; }.import-footer-actions .el-icon { margin-left:5px; }
.import-complete { max-width:690px; margin:auto; }.import-complete :deep(.el-result) { padding:12px 0 24px; }.created-template-list { border:1px solid #e8edf4; border-radius:8px; margin-bottom:24px; }.created-template-list>div { display:flex; justify-content:space-between; align-items:center; padding:14px 17px; gap:20px; }.created-template-list>div + div { border-top:1px solid #edf1f6; }.created-template-list strong { display:block; font-weight:500; font-size:13px; }.created-template-list span { display:block; font-size:11px; color:#95a0b2; margin-top:5px; }
@media (max-width:800px) { .review-import { grid-template-columns:1fr; gap:12px; }.template-file-sidebar { max-height:150px; border-right:0; border-bottom:1px solid #e8edf4; padding-right:0; }.template-file-tab { max-width:210px; display:inline-block; margin-right:8px; vertical-align:top; }.import-stepper { gap:20px; }.import-dialog-footer { flex-wrap:wrap; gap:12px; }.import-footer-actions { margin-left:auto; }.upload-notes { gap:10px; } }
</style>
