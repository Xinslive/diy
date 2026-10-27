<template>
  <div class="page flow-page">
    <div class="flow-head">
      <div>
        <h2>流水对账</h2>
        <p>
          上传网银导出的对账单，把它绑到具体的现金科目（1002 下面的子科目）上；
          流水可以自动生成凭证，发票的结算科目也会自动跟到对应账户。
        </p>
      </div>
      <div class="flow-head-actions">
        <el-button :disabled="!store.canWrite" @click="loadAll">
          <el-icon><Refresh /></el-icon>刷新
        </el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="flow-tabs">
      <!-- ============================================================ 对账单 -->
      <el-tab-pane label="对账单" name="statements">
        <el-card shadow="never" class="flow-upload-card">
          <template #header>
            <div class="section-head">
              <span>上传对账单</span>
              <span class="muted">
                支持 Excel（xls/xlsx）、网页版表格、CSV 与 PDF；
                格式按**文件内容**识别，不看扩展名
              </span>
            </div>
          </template>
          <el-form :model="uploadForm" label-width="92px" class="flow-upload-form">
            <el-row :gutter="'0 28'">
              <el-col :xs="24" :sm="10">
                <el-form-item label="现金科目">
                  <el-select
                    v-model="uploadForm.account_subject_id"
                    :disabled="!store.canWrite"
                    filterable clearable :persistent="false"
                    placeholder="这份流水记到哪个账户"
                  >
                    <el-option
                      v-for="s in cashSubjects"
                      :key="s.id"
                      :label="subjectLabel(s)"
                      :value="s.id"
                    />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="14">
                <el-form-item label="对账单">
                  <el-upload
                    :auto-upload="false"
                    :show-file-list="false"
                    accept=".xls,.xlsx,.csv,.html,.htm,.pdf"
                    :disabled="!store.canWrite"
                    :on-change="onFile"
                  >
                    <el-button :disabled="!store.canWrite">
                      <el-icon><Upload /></el-icon>选择文件
                    </el-button>
                  </el-upload>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item>
              <el-button
                type="primary"
                :disabled="!store.canWrite || !canUpload"
                :loading="uploading"
                @click="doUpload"
              >
                <el-icon><Check /></el-icon>上传并解析
              </el-button>
              <span v-if="pickedFile" class="muted picked">{{ pickedFile.name }}</span>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never">
          <template #header>
            <div class="section-head">
              <span>对账单台账</span>
              <span class="muted">
                「差额」= 对账单期末余额 − 账面余额（含未记账）；
                差额能被未入账流水解释时，下面会写清楚
              </span>
            </div>
          </template>
          <el-table :data="statements" v-loading="loading" empty-text="还没有上传过对账单">
            <el-table-column label="账户" min-width="170">
              <template #default="{ row }">
                <div>{{ row.account_subject_code }} {{ row.account_subject_name }}</div>
                <div class="muted small">{{ row.account_name || row.account_no || '—' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="文件" min-width="180">
              <template #default="{ row }">
                <div>{{ row.filename }}</div>
                <div class="muted small">{{ row.container_label }} · {{ row.row_count }} 行</div>
              </template>
            </el-table-column>
            <el-table-column label="期间" width="150">
              <template #default="{ row }">
                {{ row.period_start || '—' }} ~ {{ row.period_end || '—' }}
              </template>
            </el-table-column>
            <el-table-column label="收支合计" width="180">
              <template #default="{ row }">
                <div>收 {{ money(row.income_total) }}</div>
                <div>付 {{ money(row.expense_total) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="期末余额" width="120">
              <template #default="{ row }">{{ money(row.closing_balance) }}</template>
            </el-table-column>
            <el-table-column label="账面余额" width="120">
              <template #default="{ row }">
                <span v-if="row.reconcile?.available">{{ money(row.reconcile.book_balance) }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="差额" width="150">
              <template #default="{ row }">
                <span v-if="row.reconcile?.available" :class="diffClass(row.reconcile)">
                  {{ money(row.reconcile.difference) }}
                </span>
                <span v-else class="muted small">{{ row.reconcile?.reason || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="150">
              <template #default="{ row }">
                <el-tag :type="parseType(row.parse_status)" size="small">
                  {{ parseText(row.parse_status) }}
                </el-tag>
                <div v-if="row.pending_count" class="muted small">
                  {{ row.pending_count }} 行待确认科目
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="230" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="openRows(row)">查看流水</el-button>
                <el-button link type="primary" :disabled="!store.canWrite" @click="doReparse(row)">
                  重新解析
                </el-button>
                <el-button link type="primary" @click="doDownload(row)">下载原件</el-button>
                <el-button link type="danger" :disabled="!store.canWrite" @click="doDelete(row)">
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <!-- 解析诊断 -->
        <el-card v-if="current" shadow="never" class="flow-diag-card">
          <template #header>
            <div class="section-head">
              <span>解析诊断 · {{ current.filename }}</span>
              <span class="muted">
                认错了列不要紧：在这里手工指定一次，并「记住这个格式」，
                下次同一家银行的文件自动套用
              </span>
            </div>
          </template>
          <div class="diag-grid">
            <div><span class="muted">容器</span> {{ current.container_label }}</div>
            <div>
              <span class="muted">表头行</span>
              {{ current.diagnosis?.header_row != null ? `第 ${current.diagnosis.header_row + 1} 行` : '—' }}
            </div>
            <div><span class="muted">指纹</span> {{ current.diagnosis?.fingerprint || '—' }}</div>
            <div><span class="muted">解析</span> {{ parseText(current.parse_status) }}</div>
          </div>
          <div v-if="current.parse_message" class="diag-notes">
            <div v-for="(note, i) in noteList" :key="i" class="diag-note">{{ note }}</div>
          </div>
          <div class="diag-mapping">
            <span class="muted">列映射</span>
            <el-tag v-for="(col, field) in current.diagnosis?.column_mapping || {}"
                    :key="field" size="small" class="mapping-tag">
              {{ fieldLabel(field) }} = 第 {{ Number(col) + 1 }} 列
            </el-tag>
          </div>

          <el-collapse v-model="diagPanels" class="diag-collapse">
            <el-collapse-item name="grid" title="网格预览（前 20 行）与手工指定列">
              <el-table :data="gridRows" size="small" border>
                <el-table-column type="index" width="46" />
                <el-table-column
                  v-for="(col, index) in gridColumns"
                  :key="index"
                  :label="`第 ${index + 1} 列`"
                  min-width="120"
                >
                  <template #default="{ row }">
                    <div class="grid-cell">{{ row[index] }}</div>
                    <el-select
                      v-model="manualMapping[index]"
                      size="small"
                      placeholder="（不认）"
                      clearable
                      class="mapping-select"
                    >
                      <el-option
                        v-for="f in FIELD_OPTIONS"
                        :key="f.value"
                        :label="f.label"
                        :value="f.value"
                      />
                    </el-select>
                  </template>
                </el-table-column>
              </el-table>
              <div class="diag-actions">
                <el-button
                  type="primary"
                  :disabled="!store.canWrite || !manualMappingCount"
                  :loading="reparsing"
                  @click="applyManualMapping"
                >
                  按指定重新解析
                </el-button>
                <el-button
                  :disabled="!store.canWrite || !manualMappingCount"
                  @click="rememberFormat"
                >
                  记住这个格式
                </el-button>
                <span class="muted small">
                  手工指定的列会**整体替换**自动映射，不做逐字段合并
                </span>
              </div>
            </el-collapse-item>
            <el-collapse-item name="profiles" title="已记住的格式画像">
              <el-table :data="profiles" size="small" empty-text="还没有记住任何格式">
                <el-table-column label="指纹" prop="fingerprint" width="200" />
                <el-table-column label="列映射" min-width="320">
                  <template #default="{ row }">
                    <el-tag
                      v-for="(col, field) in row.column_mapping"
                      :key="field"
                      size="small"
                      class="mapping-tag"
                    >
                      {{ fieldLabel(field) }} = 第 {{ Number(col) + 1 }} 列
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="命中" width="80" prop="hit_count" />
                <el-table-column label="操作" width="80">
                  <template #default="{ row }">
                    <el-button link type="danger" :disabled="!store.canWrite" @click="doDeleteProfile(row)">
                      删除
                    </el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-collapse-item>
          </el-collapse>
        </el-card>
      </el-tab-pane>

      <!-- ============================================================ 流水明细 -->
      <el-tab-pane label="流水明细" name="rows">
        <el-card shadow="never">
          <template #header>
            <div class="section-head">
              <span>流水明细</span>
              <span class="muted">
                未入账、方向已知、对方科目已定的流水才会生成凭证；
                「待确认」的先在下面指定对方科目
              </span>
            </div>
          </template>
          <div class="flow-rows-toolbar">
            <el-select v-model="activeStatementId" placeholder="选择对账单" class="flow-select" @change="loadRows">
              <el-option
                v-for="s in statements"
                :key="s.id"
                :label="`${s.filename}（${s.period_start || '?'} ~ ${s.period_end || '?'}）`"
                :value="s.id"
              />
            </el-select>
            <el-button :disabled="!activeStatementId" :loading="loadingRows" @click="loadRows">
              <el-icon><Refresh /></el-icon>刷新流水
            </el-button>
            <el-button
              type="primary"
              :disabled="!store.canWrite || !canCommit(plan)"
              :loading="committing"
              @click="doCommit"
            >
              生成流水凭证
            </el-button>
            <span v-if="plan" class="muted">
              可生成 {{ plan.candidate_count }} 条
              <template v-if="plan.pending_count">，待确认科目 {{ plan.pending_count }} 条</template>
            </span>
          </div>

          <el-table :data="rows" v-loading="loadingRows" empty-text="先选一份对账单">
            <el-table-column label="#" width="56" prop="seq" />
            <el-table-column label="日期" width="110">
              <template #default="{ row }">{{ row.tx_date || '—' }}</template>
            </el-table-column>
            <el-table-column label="摘要" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.summary || '—' }}</template>
            </el-table-column>
            <el-table-column label="用途" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">{{ row.purpose || '—' }}</template>
            </el-table-column>
            <el-table-column label="对方户名" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ row.counterparty_name || '—' }}</template>
            </el-table-column>
            <el-table-column label="收入" width="120" align="right">
              <template #default="{ row }">
                <span v-if="row.direction === 'IN'">{{ money(row.amount) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="支出" width="120" align="right">
              <template #default="{ row }">
                <span v-if="row.direction === 'OUT'">{{ money(row.amount) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="余额" width="120" align="right">
              <template #default="{ row }">{{ money(row.balance) }}</template>
            </el-table-column>
            <el-table-column label="方向" width="110">
              <template #default="{ row }">
                <el-tag v-if="row.direction !== 'UNKNOWN'" size="small" :type="row.direction === 'IN' ? 'success' : 'warning'">
                  {{ row.direction === 'IN' ? '收入' : '支出' }}
                </el-tag>
                <template v-else>
                  <el-select
                    :model-value="row.direction"
                    size="small"
                    placeholder="指定方向"
                    class="flow-direction"
                    :disabled="!store.canWrite"
                    @change="(v) => doSetDirection(row, v)"
                  >
                    <el-option label="收入" value="IN" />
                    <el-option label="支出" value="OUT" />
                  </el-select>
                </template>
              </template>
            </el-table-column>
            <el-table-column label="对方科目" width="220">
              <template #default="{ row }">
                <el-select
                  :model-value="row.plan_subject_id"
                  size="small"
                  filterable clearable :persistent="false"
                  placeholder="待确认"
                  class="flow-subject"
                  :disabled="!store.canWrite || row.status !== 'OPEN'"
                  @change="(v) => doSetSubject(row, v)"
                >
                  <el-option
                    v-for="s in subjects"
                    :key="s.id"
                    :label="subjectLabel(s)"
                    :value="s.id"
                  />
                </el-select>
                <div v-if="row.plan_status === 'RULE'" class="muted small">规则命中</div>
                <div v-else-if="row.plan_status === 'MANUAL'" class="muted small">人工指定</div>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="120">
              <template #default="{ row }">
                <el-tag size="small" :type="stateType(row.voucher_state)">
                  {{ stateText(row.voucher_state) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120" fixed="right">
              <template #default="{ row }">
                <el-button
                  link
                  type="danger"
                  :disabled="!store.canWrite || row.status !== 'OPEN'"
                  @click="doIgnore(row)"
                >
                  {{ row.status === 'IGNORED' ? '恢复' : '忽略' }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============================================================ 科目规则 -->
      <el-tab-pane label="流水科目规则" name="rules">
        <el-card shadow="never">
          <template #header>
            <div class="section-head">
              <span>流水科目规则</span>
              <span class="muted">
                按摘要 / 用途 / 对方户名里的关键词定对方科目；
                优先级大的赢，同级取更长的关键词
              </span>
            </div>
          </template>
          <el-form :inline="true" :model="ruleForm" class="flow-rule-form">
            <el-form-item label="匹配列">
              <el-select v-model="ruleForm.match_field" style="width: 120px">
                <el-option label="摘要" value="SUMMARY" />
                <el-option label="用途" value="PURPOSE" />
                <el-option label="对方户名" value="CP_NAME" />
              </el-select>
            </el-form-item>
            <el-form-item label="关键词">
              <el-input v-model="ruleForm.keyword" placeholder="如：运费" style="width: 160px" />
            </el-form-item>
            <el-form-item label="对方科目">
              <el-select v-model="ruleForm.subject_id" filterable clearable :persistent="false" style="width: 220px">
                <el-option v-for="s in subjects" :key="s.id" :label="subjectLabel(s)" :value="s.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="优先级">
              <el-input-number v-model="ruleForm.priority" :step="1" style="width: 110px" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :disabled="!store.canWrite" :loading="savingRule" @click="saveRule">
                新增规则
              </el-button>
            </el-form-item>
          </el-form>

          <el-table :data="rules" empty-text="还没有规则 —— 没有规则的流水会标成「待确认」">
            <el-table-column label="匹配列" width="110">
              <template #default="{ row }">{{ row.match_label }}</template>
            </el-table-column>
            <el-table-column label="关键词" prop="keyword" min-width="160" />
            <el-table-column label="对方科目" min-width="200">
              <template #default="{ row }">{{ row.subject_code }} {{ row.subject_name }}</template>
            </el-table-column>
            <el-table-column label="优先级" prop="priority" width="90" />
            <el-table-column label="启用" width="90">
              <template #default="{ row }">{{ row.active ? '是' : '否' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button link type="danger" :disabled="!store.canWrite" @click="doDeleteRule(row)">
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Refresh, Upload } from '@element-plus/icons-vue'
import * as api from '@/api/bankstatement'
import * as subjectApi from '@/api/subject'
import { reportNumber } from '@/utils/format'
import {
  FIELD_OPTIONS, canCommit, diffClass, fieldLabel, settlementSourceText,
  settlementSourceType, toColumnMapping, voucherStateText, voucherStateType,
} from './flow_state'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const tab = ref('statements')

const statements = ref([])
const subjects = ref([])
const rules = ref([])
const profiles = ref([])
const rows = ref([])
const plan = ref(null)
const current = ref(null)
const activeStatementId = ref(null)

const loading = ref(false)
const loadingRows = ref(false)
const uploading = ref(false)
const reparsing = ref(false)
const committing = ref(false)
const savingRule = ref(false)
const pickedFile = ref(null)
const diagPanels = ref([])
const manualMapping = reactive({})

const uploadForm = reactive({ account_subject_id: null })
const ruleForm = reactive({
  match_field: 'SUMMARY', keyword: '', subject_id: null, priority: 0, active: true,
})

const subjectLabel = (s) => `${s.code} ${s.name}`

/** 现金类科目：1001 库存现金、1002 银行存款、1012 其他货币资金。 */
const cashSubjects = computed(() => subjects.value.filter(
  (s) => ['1001', '1002', '1012'].some((p) => String(s.code || '').startsWith(p)),
))

const canUpload = computed(() => !!pickedFile.value && !!uploadForm.account_subject_id)

/**
 * 金额显示：**走 ``utils/format`` 的 ``reportNumber``**，
 * 不在这里另写一份格式化。
 *
 * ``reportNumber`` 处理的是后端给来的十进制字符串（它给的就是两位小数的
 * 字符串），全程按字符串处理，一个浮点都不经过 —— 18 位整数在双精度里
 * 已经不是原来那个数了。这里若自己用 ``toLocaleString`` 走一遍 Number，
 * 同一个金额在流水页与凭证页就会显示成两个数。
 */
const money = (v) => {
  if (v === '' || v === null || v === undefined) return '—'
  const text = reportNumber(v)
  return text === '' ? '—' : text
}

const parseText = (v) => ({ OK: '解析正常', PARTIAL: '解析有疑点', FAILED: '解析失败' }[v] || v || '—')
const parseType = (v) => ({ OK: 'success', PARTIAL: 'warning', FAILED: 'danger' }[v] || 'info')

const stateText = voucherStateText
const stateType = voucherStateType

const noteList = computed(() => {
  const notes = current.value?.diagnosis?.notes || []
  const msg = current.value?.parse_message
  if (msg && !notes.includes(msg)) return [msg, ...notes]
  return notes
})

const gridRows = computed(() => current.value?.diagnosis?.grid_preview || [])
const gridColumns = computed(() => {
  const rows = gridRows.value
  const width = rows.reduce((max, r) => Math.max(max, r.length), 0)
  return Array.from({ length: width }, (_, i) => i)
})

const manualMappingCount = computed(() => Object.keys(manualMapping).filter(
  (k) => manualMapping[k],
).length)

async function loadAll() {
  loading.value = true
  try {
    const [list, flat, ruleList, profileList] = await Promise.all([
      api.listStatements(store.bookId),
      subjectApi.subjectFlat(store.bookId, { year: store.currentYear }),
      api.listRules(store.bookId),
      api.listProfiles(store.bookId),
    ])
    statements.value = list || []
    subjects.value = flat || []
    rules.value = ruleList || []
    profiles.value = profileList || []
    if (current.value) {
      const again = statements.value.find((s) => s.id === current.value.id)
      if (again) current.value = again
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

function onFile(file) {
  pickedFile.value = file.raw
}

async function doUpload() {
  if (!canUpload.value) return
  uploading.value = true
  try {
    const created = await api.uploadStatement(
      store.bookId, pickedFile.value, uploadForm.account_subject_id)
    pickedFile.value = null
    uploadForm.account_subject_id = null
    await loadAll()
    current.value = statements.value.find((s) => s.id === created.id) || null
    resetManualMapping()
    ElMessage.success(`已解析 ${created.row_count} 行`)
    tab.value = 'statements'
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '上传失败')
  } finally {
    uploading.value = false
  }
}

async function openRows(statement) {
  if (!statement) return
  current.value = statement
  activeStatementId.value = statement.id
  tab.value = 'rows'
  await loadRows()
}

async function loadRows() {
  if (!activeStatementId.value) return
  loadingRows.value = true
  try {
    rows.value = await api.listRows(store.bookId, activeStatementId.value)
    plan.value = await api.planVouchers(store.bookId, activeStatementId.value)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '加载流水失败')
  } finally {
    loadingRows.value = false
  }
}

async function doReparse(statement) {
  try {
    await ElMessageBox.confirm(
      '重新解析会按最新的规则重跑一遍（已生成凭证的行不会被重建）。继续？',
      '重新解析', { type: 'warning' })
  } catch {
    return
  }
  reparsing.value = true
  try {
    const updated = await api.reparse(store.bookId, statement.id, {})
    await loadAll()
    current.value = statements.value.find((s) => s.id === updated.id) || current.value
    resetManualMapping()
    ElMessage.success('已重新解析')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '重新解析失败')
  } finally {
    reparsing.value = false
  }
}

async function doDownload(statement) {
  try {
    await api.downloadOriginal(store.bookId, statement.id, statement.filename)
  } catch (e) {
    ElMessage.error(e?.message || '下载失败')
  }
}

async function doDelete(statement) {
  try {
    await ElMessageBox.confirm(
      `删除「${statement.filename}」？它解析出来的流水会一起删掉。`,
      '删除对账单', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api.deleteStatement(store.bookId, statement.id)
    if (current.value?.id === statement.id) current.value = null
    await loadAll()
    ElMessage.success('已删除')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

async function doSetDirection(row, direction) {
  try {
    await api.setRowDirection(store.bookId, row.id, direction)
    await loadRows()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '指定方向失败')
  }
}

async function doSetSubject(row, subjectId) {
  try {
    await api.patchRow(store.bookId, row.id, { plan_subject_id: subjectId || null })
    await loadRows()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '设置对方科目失败')
  }
}

async function doIgnore(row) {
  const next = row.status === 'IGNORED' ? 'OPEN' : 'IGNORED'
  try {
    await api.patchRow(store.bookId, row.id, { status: next })
    await loadRows()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

async function doCommit() {
  if (!activeStatementId.value) return
  committing.value = true
  try {
    const result = await api.commitVouchers(store.bookId, activeStatementId.value)
    const failed = result.failures?.length || 0
    if (failed) {
      ElMessage.warning(`生成了 ${result.created_count} 张凭证，${failed} 条没生成（见流水明细里的原因）`)
    } else {
      ElMessage.success(`生成了 ${result.created_count} 张凭证`)
    }
    await loadRows()
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '生成凭证失败')
  } finally {
    committing.value = false
  }
}

async function saveRule() {
  if (!ruleForm.keyword || !ruleForm.subject_id) {
    ElMessage.warning('关键词与对方科目都要填')
    return
  }
  savingRule.value = true
  try {
    await api.createRule(store.bookId, { ...ruleForm })
    ruleForm.keyword = ''
    ruleForm.subject_id = null
    await loadAll()
    ElMessage.success('已新增规则')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '新增规则失败')
  } finally {
    savingRule.value = false
  }
}

async function doDeleteRule(row) {
  try {
    await api.deleteRule(store.bookId, row.id)
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '删除规则失败')
  }
}

async function doDeleteProfile(row) {
  try {
    await api.deleteProfile(store.bookId, row.id)
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '删除格式失败')
  }
}

function resetManualMapping() {
  Object.keys(manualMapping).forEach((k) => { delete manualMapping[k] })
}

async function applyManualMapping() {
  if (!current.value) return
  const mapping = toColumnMapping(manualMapping)
  reparsing.value = true
  try {
    const updated = await api.reparse(store.bookId, current.value.id, {
      column_mapping: mapping,
    })
    await loadAll()
    current.value = statements.value.find((s) => s.id === updated.id) || current.value
    ElMessage.success('已按指定的列重新解析')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '重新解析失败')
  } finally {
    reparsing.value = false
  }
}

async function rememberFormat() {
  if (!current.value) return
  const mapping = toColumnMapping(manualMapping)
  try {
    await api.saveProfile(store.bookId, {
      fingerprint: current.value.diagnosis?.fingerprint,
      column_mapping: mapping,
    })
    await loadAll()
    ElMessage.success('已记住这个格式')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存格式失败')
  }
}

onMounted(() => {
  loadAll()
})
</script>

<style scoped>
.flow-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}
.flow-head h2 { margin: 0 0 4px; font-size: 20px; }
.flow-head p { margin: 0; color: var(--el-text-color-secondary); max-width: 760px; line-height: 1.6; }
.section-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}
.section-head .muted { color: var(--el-text-color-secondary); font-size: 12px; }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.picked { margin-left: 12px; font-size: 12px; }
.flow-upload-form { max-width: 900px; }
.flow-rows-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.flow-select { width: 340px; }
.flow-direction { width: 92px; }
.flow-subject { width: 200px; }
.diag-grid {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  font-size: 13px;
}
.diag-notes { margin: 8px 0; }
.diag-note {
  font-size: 13px;
  color: var(--el-color-warning-dark-2);
  line-height: 1.7;
}
.diag-mapping { margin: 10px 0; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.mapping-tag { margin-right: 4px; }
.diag-collapse { margin-top: 8px; }
.grid-cell {
  font-size: 12px;
  color: var(--el-text-color-regular);
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mapping-select { width: 100%; margin-top: 4px; }
.diag-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.diff-ok { color: var(--el-color-success); }
.diff-warn { color: var(--el-color-warning); }
.diff-bad { color: var(--el-color-danger); }
</style>
