<template>
  <div class="page invoice-page">
    <div class="invoice-head">
      <div>
        <h2>发票导入</h2>
        <p>解析电子发票后，按科目规则生成未记账凭证。确认前可逐张核对，原始 PDF 会保留在批次中。</p>
      </div>
      <div class="invoice-head-actions">
        <el-button :disabled="!store.canWrite" @click="loadAll">
          <el-icon><Refresh /></el-icon>刷新
        </el-button>
        <el-button v-if="batch" :disabled="!store.canWrite" @click="newBatch">新建批次</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="invoice-tabs">
      <el-tab-pane label="发票批次" name="batches">
        <el-card shadow="never" class="invoice-config-card">
          <template #header>
            <div class="section-head">
              <span>自动制证配置</span>
              <span class="muted">本单位税号用于自动判断进项/销项；默认结算科目为 1001 库存现金。进项发票不单列税额，价税合计直接计入成本费用</span>
            </div>
          </template>
          <el-form :model="config" inline class="invoice-config-form">
            <el-form-item label="本单位税号">
              <el-input v-model="config.taxpayer_id" :disabled="!store.canWrite" placeholder="用于判断发票类型" style="width: 230px" />
            </el-form-item>
            <el-form-item label="销项税额">
              <el-select v-model="config.output_tax_subject_id" :disabled="!store.canWrite" filterable style="width: 220px">
                <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="进项贷方">
              <el-select v-model="config.input_credit_subject_id" :disabled="!store.canWrite" filterable style="width: 220px">
                <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="销项借方">
              <el-select v-model="config.output_debit_subject_id" :disabled="!store.canWrite" filterable style="width: 220px">
                <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :disabled="!store.canWrite" :loading="savingConfig" @click="saveConfig">
                <el-icon><Check /></el-icon>保存配置
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card v-if="!batch" shadow="never" class="invoice-upload-card">
          <template #header>
            <div class="section-head">
              <span>上传电子发票</span>
            </div>
          </template>
          <el-upload
            drag :auto-upload="false" :show-file-list="false" accept=".pdf"
            :disabled="!store.canWrite" :on-change="onFile"
          >
            <el-icon class="el-icon--upload"><Upload /></el-icon>
            <div class="el-upload__text">把 PDF 拖到此处，或<em>点击选择文件</em></div>
          </el-upload>
          <div class="upload-footer">
            <span class="muted">{{ file?.name || '尚未选择文件' }}</span>
            <el-button type="primary" :disabled="!file || !store.canWrite" :loading="uploading" @click="upload">
              <el-icon><Upload /></el-icon>上传并解析
            </el-button>
          </div>
        </el-card>

        <el-card v-if="!batch && history.length" shadow="never" class="invoice-history-card">
          <template #header><span>处理历史</span></template>
          <el-table :data="history" size="small" border @row-click="loadBatch">
            <el-table-column prop="filename" label="文件" min-width="220" />
            <el-table-column prop="created_at" label="时间" width="180" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }"><el-tag size="small" :type="batchType(row.status)">{{ batchText(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="total" label="发票" width="75" align="center" />
            <el-table-column prop="committed_count" label="已生成凭证" width="105" align="center" />
            <el-table-column prop="message" label="备注" min-width="220" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="科目规则" name="rules">
        <el-card shadow="never">
          <template #header>
            <div class="section-head">
              <span>供应商 / 项目科目规则</span>
              <el-button type="primary" :disabled="!store.canWrite" @click="openRule">
                <el-icon><Plus /></el-icon>新增规则
              </el-button>
            </div>
          </template>
          <el-alert type="info" :closable="false" show-icon class="rule-help">
            同一优先级下取更长关键词；仍然冲突会在预览中标红，不会静默猜科目。未命中规则的发票不能确认生成凭证。
          </el-alert>
          <el-table :data="rules" size="small" border>
            <el-table-column label="匹配对象" width="110">
              <template #default="{ row }">{{ row.match_type === 'SELLER' ? '供应商/客户' : '项目名称' }}</template>
            </el-table-column>
            <el-table-column label="发票类型" width="100">
              <template #default="{ row }">{{ invoiceTypeText(row.invoice_type) }}</template>
            </el-table-column>
            <el-table-column prop="keyword" label="关键词" min-width="180" />
            <el-table-column label="科目" min-width="210">
              <template #default="{ row }">{{ row.subject_code }} {{ row.subject_name }}</template>
            </el-table-column>
            <el-table-column prop="priority" label="优先级" width="85" align="center" />
            <el-table-column label="状态" width="85" align="center">
              <template #default="{ row }"><el-tag size="small" :type="row.active ? 'success' : 'info'">{{ row.active ? '启用' : '停用' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="110" fixed="right">
              <template #default="{ row }">
                <el-button link type="danger" :disabled="!store.canWrite" @click="removeRule(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!rules.length" description="还没有规则，请先新增供应商或项目关键词规则" />
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <template v-if="batch">
      <el-card shadow="never" class="invoice-result-card">
        <template #header>
          <div class="section-head">
            <div class="result-title">
              <span>{{ batch.filename }}</span>
              <el-tag v-if="batch.ocr_used" size="small" type="warning">OCR API 兜底</el-tag>
            </div>
            <div class="result-actions">
              <el-button :disabled="!store.canWrite" :loading="previewing" @click="recalculate">重新计算预览</el-button>
              <el-button type="danger" plain :disabled="!store.canWrite" @click="discard">作废批次</el-button>
              <el-button type="primary" :disabled="!store.canWrite || batch.error_count > 0 || !batch.ready_count" :loading="committing" @click="commit">
                确认生成 {{ batch.ready_count || 0 }} 张凭证
              </el-button>
            </div>
          </div>
        </template>
        <el-alert
          :type="batch.error_count ? 'error' : 'success'" :closable="false" show-icon class="mb"
          :title="`共 ${batch.total} 张发票：${batch.ready_count} 张可生成，${batch.error_count} 张有问题`"
        />
        <el-alert v-if="batch.error_count" type="warning" :closable="false" show-icon class="mb"
                  title="请展开红色发票，修正类型或科目后点击「重新计算预览」；所有异常消除后才能确认。" />

        <el-table :data="batch.invoices" size="small" border row-key="id" :row-class-name="invoiceRowClass">
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="invoice-detail">
                <div class="detail-toolbar">
                  <el-form inline size="small">
                    <el-form-item label="发票类型">
                      <el-select :model-value="invoiceTypeFor(row)" :disabled="!store.canWrite" style="width: 120px" @update:model-value="(v) => setInvoiceType(row, v)">
                        <el-option label="进项" value="INPUT" /><el-option label="销项" value="OUTPUT" />
                      </el-select>
                    </el-form-item>
                    <el-form-item label="税额科目" v-if="invoiceTypeFor(row) === 'OUTPUT'">
                      <el-select :model-value="taxSubjectFor(row)" :disabled="!store.canWrite" filterable style="width: 210px" @update:model-value="(v) => setTaxSubject(row, v)">
                        <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                      </el-select>
                    </el-form-item>
                    <el-form-item v-else label="税额">
                      <span class="muted">不单列，并入第一条明细行</span>
                    </el-form-item>
                    <el-form-item label="结算科目">
                      <el-select :model-value="settlementSubjectFor(row)" :disabled="!store.canWrite" filterable style="width: 210px" @update:model-value="(v) => setSettlementSubject(row, v)">
                        <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                      </el-select>
                    </el-form-item>
                  </el-form>
                </div>
                <div v-if="row.errors?.length" class="invoice-errors">
                  <div v-for="error in row.errors" :key="error">{{ error }}</div>
                </div>
                <el-table :data="row.lines" size="small" border class="line-table">
                  <el-table-column type="index" label="#" width="48" />
                  <el-table-column prop="name" label="发票项目" min-width="260" show-overflow-tooltip />
                  <el-table-column prop="amount" label="不含税金额" width="120" align="right" />
                  <el-table-column prop="tax" label="税额" width="100" align="right" />
                  <el-table-column prop="tax_rate" label="税率" width="80" align="center" />
                  <el-table-column label="入账科目" min-width="230">
                    <template #default="{ row: line, $index }">
                      <el-select :model-value="lineSubjectFor(row, $index)" :disabled="!store.canWrite" filterable clearable style="width: 100%" @update:model-value="(v) => setLineSubject(row, $index, v)">
                        <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
                      </el-select>
                    </template>
                  </el-table-column>
                </el-table>
                <div class="entry-preview" v-if="row.resolution?.entries?.length">
                  <span class="muted">凭证预览：</span>
                  <span v-for="(entry, index) in row.resolution.entries" :key="index" class="entry-chip">
                    {{ entry.direction === 'DEBIT' ? '借' : '贷' }} {{ entry.subject_code }} {{ entry.subject_name }} {{ entry.amount }}
                  </span>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="page_index" label="页码" width="70" align="center">
            <template #default="{ row }">{{ row.page_index + 1 }}</template>
          </el-table-column>
          <el-table-column prop="invoice_no" label="发票号码" min-width="190" />
          <el-table-column prop="issue_date" label="开票日期" width="120" />
          <el-table-column label="类型" width="85" align="center"><template #default="{ row }">{{ invoiceTypeText(row.invoice_type) }}</template></el-table-column>
          <el-table-column label="状态" width="100" align="center"><template #default="{ row }"><el-tag size="small" :type="invoiceType(row.status)">{{ invoiceStatusText(row.status) }}</el-tag></template></el-table-column>
          <el-table-column label="价税合计" width="120" align="right"><template #default="{ row }">{{ row.resolution?.total || row.meta?.total_incl_tax || '' }}</template></el-table-column>
        </el-table>
      </el-card>
    </template>

    <el-dialog v-model="ruleVisible" title="新增发票科目规则" width="520px">
      <el-form :model="ruleForm" label-width="90px">
        <el-form-item label="匹配对象"><el-radio-group v-model="ruleForm.match_type"><el-radio value="SELLER">供应商/客户</el-radio><el-radio value="ITEM">项目名称</el-radio></el-radio-group></el-form-item>
        <el-form-item label="发票类型"><el-select v-model="ruleForm.invoice_type" style="width: 200px"><el-option label="进项和销项" value="BOTH" /><el-option label="仅进项" value="INPUT" /><el-option label="仅销项" value="OUTPUT" /></el-select></el-form-item>
        <el-form-item label="关键词"><el-input v-model="ruleForm.keyword" placeholder="如：办公用品、某某科技有限公司" /></el-form-item>
        <el-form-item label="入账科目"><el-select v-model="ruleForm.subject_id" filterable style="width: 100%"><el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" /></el-select></el-form-item>
        <el-form-item label="优先级"><el-input-number v-model="ruleForm.priority" :min="-1000" :max="1000" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="ruleVisible = false">取消</el-button><el-button type="primary" :loading="savingRule" @click="createRule">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Plus, Refresh, Upload } from '@element-plus/icons-vue'
import * as invoiceApi from '@/api/invoice'
import * as subjectApi from '@/api/subject'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const tab = ref('batches')
const subjects = ref([])
const rules = ref([])
const history = ref([])
const batch = ref(null)
const file = ref(null)
const uploading = ref(false)
const committing = ref(false)
const previewing = ref(false)
const savingConfig = ref(false)
const savingRule = ref(false)
const config = reactive({ taxpayer_id: '', output_tax_subject_id: null, input_credit_subject_id: null, output_debit_subject_id: null })
const overrides = reactive({})
const ruleVisible = ref(false)
const ruleForm = reactive({ match_type: 'SELLER', invoice_type: 'BOTH', keyword: '', subject_id: null, priority: 0, active: true })

const subjectLabel = (s) => `${s.code} ${s.name}`
const invoiceTypeText = (v) => ({ INPUT: '进项', OUTPUT: '销项', UNKNOWN: '未判断' }[v] || v || '未判断')
const invoiceStatusText = (v) => ({ READY: '可生成', REJECTED: '有问题', COMMITTED: '已生成', DISCARDED: '已作废' }[v] || v)
const invoiceType = (v) => ({ READY: 'success', REJECTED: 'danger', COMMITTED: 'success', DISCARDED: 'info' }[v] || 'warning')
const batchText = (v) => ({ PENDING: '待确认', COMMITTED: '已生成', DISCARDED: '已作废' }[v] || v)
const batchType = (v) => ({ PENDING: 'warning', COMMITTED: 'success', DISCARDED: 'info' }[v] || '')

function onFile(value) { file.value = value.raw }
function ensureOverride(row) {
  if (!overrides[row.id]) overrides[row.id] = { line_subjects: {} }
  overrides[row.id].line_subjects ||= {}
  return overrides[row.id]
}
function invoiceTypeFor(row) { return overrides[row.id]?.invoice_type || row.invoice_type || 'UNKNOWN' }
function lineSubjectFor(row, index) {
  return overrides[row.id]?.line_subjects?.[String(index)] ?? row.resolution?.line_resolution?.[index]?.subject_id ?? null
}
function taxSubjectFor(row) {
  // 进项不单列税额（小微企业不做进项抵扣），只有销项才需要税额科目。
  if (invoiceTypeFor(row) !== 'OUTPUT') return null
  return overrides[row.id]?.tax_subject_id || config.output_tax_subject_id
}
function settlementSubjectFor(row) {
  const input = invoiceTypeFor(row) === 'INPUT'
  return overrides[row.id]?.settlement_subject_id || (input ? config.input_credit_subject_id : config.output_debit_subject_id)
}
function setInvoiceType(row, value) { ensureOverride(row).invoice_type = value }
function setTaxSubject(row, value) { ensureOverride(row).tax_subject_id = value }
function setSettlementSubject(row, value) { ensureOverride(row).settlement_subject_id = value }
function setLineSubject(row, index, value) { ensureOverride(row).line_subjects[String(index)] = value }
function invoiceRowClass({ row }) { return row.status === 'REJECTED' ? 'invoice-row-bad' : '' }

async function loadAll() {
  if (!store.bookId) return
  const [cfg, flat, ruleRows, batches] = await Promise.all([
    invoiceApi.getConfig(store.bookId), subjectApi.subjectFlat(store.bookId), invoiceApi.listRules(store.bookId), invoiceApi.listBatches(store.bookId)
  ])
  Object.assign(config, cfg)
  subjects.value = flat
  rules.value = ruleRows
  history.value = batches
}
async function saveConfig() {
  savingConfig.value = true
  try { Object.assign(config, await invoiceApi.saveConfig(store.bookId, { ...config })); ElMessage.success('发票制证配置已保存') } finally { savingConfig.value = false }
}
async function upload() {
  uploading.value = true
  try { batch.value = await invoiceApi.uploadBatch(store.bookId, file.value); await loadAll(); ElMessage.success(`解析完成：${batch.value.total} 张发票`) } finally { uploading.value = false }
}
async function loadBatch(row) {
  batch.value = await invoiceApi.getBatch(store.bookId, row.id || row)
  tab.value = 'batches'
}
function newBatch() { batch.value = null; file.value = null; Object.keys(overrides).forEach(key => delete overrides[key]) }
function overridePayload() { return JSON.parse(JSON.stringify(overrides)) }
async function recalculate() {
  previewing.value = true
  try { batch.value = await invoiceApi.previewBatch(store.bookId, batch.value.id, overridePayload()); ElMessage.success('预览已重新计算') } finally { previewing.value = false }
}
async function commit() {
  await ElMessageBox.confirm(`确认根据 ${batch.value.ready_count} 张发票生成未记账凭证吗？生成后仍可在凭证页面修改。`, '确认生成凭证', { type: 'warning' })
  committing.value = true
  try { await invoiceApi.commitBatch(store.bookId, batch.value.id); ElMessage.success('凭证已生成'); await loadAll(); batch.value = null } finally { committing.value = false }
}
async function discard() {
  await ElMessageBox.confirm('作废后本批次不会生成凭证，但原始 PDF 仍保留在处理历史中。确认作废吗？', '作废批次', { type: 'warning' })
  await invoiceApi.discardBatch(store.bookId, batch.value.id)
  ElMessage.success('批次已作废'); newBatch(); await loadAll()
}
function openRule() { Object.assign(ruleForm, { match_type: 'SELLER', invoice_type: 'BOTH', keyword: '', subject_id: null, priority: 0, active: true }); ruleVisible.value = true }
async function createRule() {
  if (!ruleForm.keyword.trim() || !ruleForm.subject_id) { ElMessage.warning('请填写关键词并选择科目'); return }
  savingRule.value = true
  try { await invoiceApi.createRule(store.bookId, { ...ruleForm }); rules.value = await invoiceApi.listRules(store.bookId); ruleVisible.value = false; ElMessage.success('规则已保存') } finally { savingRule.value = false }
}
async function removeRule(row) {
  await ElMessageBox.confirm(`确认删除规则「${row.keyword}」吗？`, '删除规则', { type: 'warning' })
  await invoiceApi.deleteRule(store.bookId, row.id); rules.value = await invoiceApi.listRules(store.bookId)
}

onMounted(loadAll)
</script>

<style scoped>
.invoice-page { padding: 22px 24px 36px; }
.invoice-head, .section-head, .upload-footer, .result-actions, .result-title { display: flex; align-items: center; }
.invoice-head { justify-content: space-between; gap: 18px; margin-bottom: 14px; }
.invoice-head h2 { margin: 0; color: var(--cwq-chrome-strong); font-size: 22px; font-weight: 600; }
.invoice-head p { margin: 6px 0 0; color: var(--cwq-muted); font-size: 13px; }
.invoice-head-actions, .result-actions { gap: 8px; }
.invoice-tabs :deep(.el-tabs__content) { overflow: visible; }
.invoice-config-card, .invoice-upload-card, .invoice-history-card, .invoice-result-card { margin-bottom: 14px; }
.section-head { justify-content: space-between; gap: 14px; font-weight: 600; }
.section-head .muted { font-size: 12px; font-weight: 400; }
.invoice-config-form { margin-bottom: -18px; }
.invoice-upload-card :deep(.el-upload-dragger) { padding: 26px 0; }
.upload-footer { justify-content: space-between; margin-top: 14px; }
.result-title { gap: 10px; }
.mb { margin-bottom: 12px; }
.rule-help { margin-bottom: 12px; }
.invoice-detail { padding: 2px 10px 12px 44px; }
.detail-toolbar { padding: 4px 0 8px; border-bottom: 1px solid #edf0f4; }
.detail-toolbar :deep(.el-form-item) { margin-bottom: 5px; }
.invoice-errors { margin: 10px 0; padding: 8px 10px; border-left: 3px solid #d93025; background: #fff5f4; color: #b42318; font-size: 12px; line-height: 1.8; }
.line-table { margin-top: 8px; }
.entry-preview { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; align-items: center; font-size: 12px; }
.entry-chip { padding: 4px 7px; border: 1px solid #dfe6ef; background: #f8fafc; color: #344054; }
:deep(.invoice-row-bad) { background: #fff5f4; }
@media (max-width: 900px) {
  .invoice-head { align-items: flex-start; flex-direction: column; }
  .section-head { align-items: flex-start; flex-direction: column; }
  .result-actions { width: 100%; flex-wrap: wrap; }
  .invoice-detail { padding-left: 8px; }
}
</style>
