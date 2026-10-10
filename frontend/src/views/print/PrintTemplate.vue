<template>
  <div class="page">
    <el-card shadow="never" class="mt">
      <template #header>
        <div class="card-head">
          <span class="title">打印模板</span>
          <span class="hint">
            左边改版式，右边是实时预览 —— 预览和实际打印用的是同一份 HTML，所见即所得。
            在浏览器打印对话框里可以直接「另存为 PDF」。
          </span>
        </div>
      </template>

      <el-form :inline="true" class="toolbar">
        <el-form-item label="打印内容">
          <el-select v-model="scope" style="width: 180px" @change="onScopeChange">
            <el-option v-for="(label, key) in SCOPES" :key="key" :label="label" :value="key" />
          </el-select>
        </el-form-item>
        <el-form-item label="模板">
          <el-select v-model="templateId" style="width: 200px" @change="loadTemplate">
            <el-option label="（内置默认）" :value="0" />
            <el-option v-for="t in listOfScope" :key="t.id" :label="t.name" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button @click="doCopy" :disabled="!templateId"><el-icon><CopyDocument /></el-icon>&nbsp;复制一份</el-button>
          <el-button @click="doCreate"><el-icon><Plus /></el-icon>&nbsp;新建空白</el-button>
          <el-button type="danger" plain :disabled="!templateId" @click="doDelete">
            <el-icon><Delete /></el-icon>&nbsp;删除
          </el-button>
        </el-form-item>
      </el-form>

      <div v-if="!store.canWrite" class="warn">
        <el-alert type="info" :closable="false" show-icon
                  title="当前账套是只读权限，可以预览和打印，但不能保存模板。" />
      </div>
    </el-card>

    <div class="designer">
      <el-card shadow="never" class="panel">
        <template #header><span class="title">版式</span></template>

        <el-form label-width="86px" label-position="left" size="default">
          <el-divider content-position="left">纸张</el-divider>
          <el-form-item label="纸型">
            <el-select v-model="tpl.paper" style="width: 100%" @change="normalize">
              <el-option v-for="p in meta.papers" :key="p.key" :value="p.key"
                         :label="p.label || p.key" />
            </el-select>
          </el-form-item>
          <el-form-item label="方向">
            <el-radio-group v-model="tpl.orientation" @change="normalize">
              <el-radio-button value="portrait">纵向</el-radio-button>
              <el-radio-button value="landscape">横向</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="页边距">
            <div class="margins">
              <div v-for="side in ['top', 'right', 'bottom', 'left']" :key="side">
                <span class="mlabel">{{ SIDE_CN[side] }}边</span>
                <el-input-number v-model="tpl.margins[side]" :min="0" :max="40" :step="1"
                                 size="small" :controls="false" style="width: 100%"
                                 @change="normalize" />
              </div>
            </div>
          </el-form-item>

          <el-divider content-position="left">标题</el-divider>
          <el-form-item label="主标题"><el-input v-model="tpl.title.text" @change="normalize" /></el-form-item>
          <el-form-item label="字号">
            <el-input-number v-model="tpl.title.size" :min="8" :max="40" size="small" @change="normalize" />
            <el-checkbox v-model="tpl.title.bold" class="ml" @change="normalize">加粗</el-checkbox>
          </el-form-item>
          <el-form-item label="副标题"><el-input v-model="tpl.subtitle.text" placeholder="留空则显示期间" @change="normalize" /></el-form-item>

          <el-divider content-position="left">字体</el-divider>
          <el-form-item label="字体">
            <el-input v-model="tpl.font.family" @change="normalize" />
          </el-form-item>
          <el-form-item label="字号">
            <el-input-number v-model="tpl.font.size" :min="6" :max="24" :step="0.5" size="small" @change="normalize" />
            pt
          </el-form-item>

          <el-divider content-position="left">页眉字段</el-divider>
          <el-form-item label="显示项">
            <el-checkbox-group v-model="metaOn" @change="normalize">
              <el-checkbox v-for="(label, key) in META_CN" :key="key" :value="key">{{ label }}</el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item label="页脚">
            <el-input v-model="tpl.footer.text" placeholder="自定义页脚文字" @change="normalize" />
          </el-form-item>
          <el-form-item label="页码">
            <el-checkbox v-model="tpl.footer.show_page_no" @change="normalize">显示页码</el-checkbox>
            <el-input v-model="tpl.footer.format" size="small" class="ml" style="width: 200px"
                      @change="normalize" />
          </el-form-item>

          <el-divider content-position="left">表格</el-divider>
          <el-form-item label="边框"><el-switch v-model="tpl.body.border" @change="normalize" /></el-form-item>
          <el-form-item label="跨页重复表头">
            <el-switch v-model="tpl.body.repeat_header" @change="normalize" />
          </el-form-item>
        </el-form>
      </el-card>

      <el-card shadow="never" class="panel">
        <template #header>
          <div class="card-head">
            <span class="title">列</span>
            <span class="hint">宽度是相对权重，会自动按 100% 归一化</span>
          </div>
        </template>

        <el-table :data="tpl.columns" size="small" border>
          <el-table-column label="字段" width="108">
            <template #default="{ row }">
              <el-select v-model="row.key" size="small" filterable style="width: 100%" @change="normalize">
                <el-option v-for="k in COLUMN_KEYS" :key="k" :label="k" :value="k" />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="表头" width="84">
            <template #default="{ row }">
              <el-input v-model="row.label" size="small" style="width: 100%" @change="normalize" />
            </template>
          </el-table-column>
          <el-table-column label="宽度" width="94">
            <template #default="{ row }">
              <el-input-number v-model="row.width" :min="0.5" :step="0.5" size="small"
                               style="width: 100%" :controls="false" @change="normalize" />
            </template>
          </el-table-column>
          <el-table-column label="对齐" width="76">
            <template #default="{ row }">
              <el-select v-model="row.align" size="small" style="width: 100%" @change="normalize">
                <el-option label="左" value="left" />
                <el-option label="中" value="center" />
                <el-option label="右" value="right" />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column width="52" align="center">
            <template #default="{ $index }">
              <el-button link type="danger" :icon="Delete" @click="tpl.columns.splice($index, 1)" />
            </template>
          </el-table-column>
          <el-table-column width="74" align="center">
            <template #default="{ $index }">
              <el-button link :icon="Top" :disabled="$index === 0" @click="move($index, -1)" />
              <el-button link :icon="Bottom" :disabled="$index === tpl.columns.length - 1" @click="move($index, 1)" />
            </template>
          </el-table-column>
        </el-table>
        <el-button class="mt" :icon="Plus" @click="addColumn">添加列</el-button>

        <div class="tip mt">
          凭证页固定可用字段：<code>line_no / summary / subject / debit / credit</code>；
          余额表：<code>code / name / level / opening_debit …</code>
        </div>
      </el-card>

      <el-card shadow="never" class="panel">
        <template #header>
          <div class="card-head">
            <span class="title">预览</span>
            <el-select v-model="previewTplId" size="small" style="width: 150px">
              <el-option label="默认模板" :value="0" />
              <el-option v-for="t in listOfScope" :key="t.id" :label="t.name" :value="t.id" />
            </el-select>
            <el-button size="small" @click="refreshPreview"><el-icon><Refresh /></el-icon></el-button>
            <el-button size="small" type="primary" @click="doPrint">
              <el-icon><Printer /></el-icon>&nbsp;打印
            </el-button>
          </div>
        </template>

        <el-form :inline="true" class="preview-opts">
          <el-form-item label="期间">
            <el-select v-model="previewPeriodId" size="small" style="width: 130px" @change="refreshPreview">
              <el-option v-for="p in store.periods" :key="p.id" :label="p.code" :value="p.id" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="scope === 'DETAIL_LEDGER'" label="科目">
            <el-select v-model="previewSubjectId" size="small" style="width: 180px" @change="refreshPreview">
              <el-option v-for="s in subjects" :key="s.id" :label="`${s.code} ${s.name}`" :value="s.id" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="scope === 'REPORT'" label="报表">
            <el-select v-model="previewReportId" size="small" style="width: 150px" @change="refreshPreview">
              <el-option v-for="r in reports" :key="r.id" :label="r.name" :value="r.id" />
            </el-select>
          </el-form-item>
        </el-form>

        <iframe ref="frame" class="preview" :srcdoc="previewHtml" title="打印预览"></iframe>
      </el-card>
    </div>

    <div class="footer-bar">
      <el-button type="primary" :loading="saving" :disabled="!store.canWrite || !templateId" @click="save">
        保存模板
      </el-button>
      <el-button @click="loadTemplate(templateId)">放弃改动</el-button>
      <span class="hint">保存后刷新页面即可在「凭证 / 账簿 / 报表」页面用这个模板打印。</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Top, Bottom, Plus, CopyDocument, Refresh, Printer } from '@element-plus/icons-vue'
import * as printApi from '@/api/print'
import * as reportApi from '@/api/report'
import * as subjectApi from '@/api/subject'
import { useSessionStore } from '@/stores/session'

function clone(o) {
  return JSON.parse(JSON.stringify(o))
}

const store = useSessionStore()
// 骨架：首屏渲染发生在模板数据回来之前，表单里到处是 tpl.margins[side] 这类深层访问，
// 所以必须给一份完整默认值，否则 setup 阶段就会抛 "reading 'top'"。
const EMPTY_TPL = {
  paper: 'A4',
  orientation: 'portrait',
  margins: { top: 10, right: 10, bottom: 10, left: 10 },
  title: { text: '', size: 16, bold: true, align: 'center' },
  subtitle: { text: '', size: 11, align: 'center' },
  font: { family: '宋体', size: 10 },
  show: { period: true, voucher_no: true, date: true, attachments: true, remark: true, page_no: true },
  meta: { company: true, book: true, period: true, accountant: true, checker: true, cashier: true,
          voucher_no: true, date: true, attachments: true, remark: true },
  columns: [{ key: 'summary', label: '摘要', width: 40, align: 'left', indent: 0 }],
  footer: { text: '', show_page_no: true, format: '第 {page} 页　共 {total} 页' },
  body: { repeat_header: true, border: true },
}

const meta = ref({ papers: [], defaults: {} })
const scope = ref('VOUCHER')
const templateId = ref(0)
const list = ref([])
const tpl = ref(clone(EMPTY_TPL))
const frame = ref(null)
const subjects = ref([])
const reports = ref([])
const saving = ref(false)

const previewTplId = ref(0)
const previewPeriodId = ref(store.periodId)
const fromPeriodId = ref(null)
const previewSubjectId = ref(null)
const previewReportId = ref(null)
const previewHtml = ref('')
let previewToken = 0

const SCOPES = {
  VOUCHER: '记账凭证', LEDGER: '总账', DETAIL_LEDGER: '明细账',
  BALANCE: '科目余额表', REPORT: '报表',
}
const SIDE_CN = { top: '上', right: '右', bottom: '下', left: '左' }
const META_CN = {
  company: '单位', book: '账套', period: '期间', accountant: '制单',
  checker: '审核', cashier: '出纳', voucher_no: '凭证号', date: '日期',
  attachments: '附单据', remark: '备注',
}
const COLUMN_KEYS = [
  'line_no', 'summary', 'subject', 'debit', 'credit',
  'date', 'voucher_no', 'direction', 'balance',
  'code', 'name', 'level',
  'opening_debit', 'opening_credit', 'period_debit', 'period_credit',
  'closing_debit', 'closing_credit', 'opening', 'closing',
  'label', 'value0', 'value1', 'value2',
]

const listOfScope = computed(() => list.value.filter((t) => t.scope === scope.value))
const metaOn = computed({
  get: () => Object.keys(tpl.value?.meta || {}).filter((k) => tpl.value.meta[k]),
  set: (keys) => {
    for (const k of Object.keys(tpl.value.meta || {})) tpl.value.meta[k] = keys.includes(k)
  },
})

/** 把本地改动对齐到「可提交」的形态：列宽归一化、meta/show 补全 */
function normalize() {
  const total = (tpl.value.columns || []).reduce((s, c) => s + (Number(c.width) || 0), 0) || 1
  tpl.value.columns.forEach((c) => { c.width = round((Number(c.width) || 0) / total * 100) })
  if (!tpl.value.meta) tpl.value.meta = {}
  Object.keys(META_CN).forEach((k) => { if (!(k in tpl.value.meta)) tpl.value.meta[k] = false })
  if (!tpl.value.show) tpl.value.show = {}
  refreshPreview()
}

const round = (n) => Math.round(n * 10000) / 10000

function move(i, d) {
  const arr = tpl.value.columns
  const j = i + d
  if (j < 0 || j >= arr.length) return
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
  normalize()
}

function addColumn() {
  tpl.value.columns.push({ key: 'summary', label: '新列', width: 10, align: 'left', indent: 0 })
  normalize()
}

async function loadTemplates() {
  list.value = await printApi.templates(store.bookId)
}

async function onScopeChange() {
  const hit = listOfScope.value.find((t) => t.is_default) || listOfScope.value[0]
  templateId.value = hit?.id || 0
  await loadTemplate(templateId.value)
}

async function loadTemplate(id) {
  const base = id
    ? (await printApi.templateDetail(store.bookId, id)).content
    : clone(meta.value.defaults[scope.value] || EMPTY_TPL)
  tpl.value = clone({ ...clone(EMPTY_TPL), ...base })
  previewTplId.value = id
  normalize()
}

async function loadPreviewData() {
  subjects.value = await subjectApi.subjectFlat(store.bookId)
  if (!previewSubjectId.value && subjects.value.length) previewSubjectId.value = subjects.value[0].id
  reports.value = await reportApi.templates()
  if (!previewReportId.value && reports.value.length) previewReportId.value = reports.value[0].id
  if (!store.periods.find((p) => p.id === previewPeriodId.value)) {
    previewPeriodId.value = store.currentPeriod?.id || store.periods[store.periods.length - 1]?.id
  }
}

/** 返回 [path, params]，交给 fetchPrintHtml / openPrint */
function buildTarget() {
  const pid = previewPeriodId.value
  const tp = previewTplId.value || undefined
  const b = store.bookId
  switch (scope.value) {
    case 'LEDGER':
      return [`/books/${b}/print/general-ledger.html`,
              { period_id: pid, template_id: tp, from_period_id: fromPeriodId.value || undefined }]
    case 'DETAIL_LEDGER':
      return [`/books/${b}/print/detail-ledger.html`,
              { period_id: pid, template_id: tp, subject_id: previewSubjectId.value }]
    case 'BALANCE':
      return [`/books/${b}/print/balance.html`, { period_id: pid, template_id: tp }]
    case 'REPORT':
      return [`/books/${b}/print/report.html`,
              { period_id: pid, print_template_id: tp, template_id: previewReportId.value }]
    default:
      return [`/books/${b}/print/vouchers.html`, { period_id: pid, template_id: tp }]
  }
}

async function refreshPreview() {
  if (!store.bookId) return
  const [path, params] = buildTarget()
  const token = ++previewToken
  try {
    const html = await printApi.fetchPrintHtml(path, params)
    if (token === previewToken) previewHtml.value = html   // 丢弃过期响应
  } catch {
    if (token === previewToken) previewHtml.value = '<p style="padding:20px;color:#999">预览加载失败</p>'
  }
}

function doPrint() {
  const [path, params] = buildTarget()
  printApi.openPrint(path, params)
}

async function save() {
  saving.value = true
  try {
    await printApi.updateTemplate(store.bookId, templateId.value, { content: tpl.value })
    ElMessage.success('模板已保存')
    await loadTemplates()
  } finally {
    saving.value = false
  }
}

async function doCopy() {
  const t = await printApi.copyTemplate(store.bookId, templateId.value)
  ElMessage.success(`已复制为「${t.name}」`)
  await loadTemplates()
  templateId.value = t.id
  await loadTemplate(t.id)
}

async function doCreate() {
  const { value: name } = await ElMessageBox.prompt('新模板名称', '新建打印模板', {
    inputValue: `${SCOPES[scope.value]} 模板`,
    inputValidator: (v) => (v && v.trim() ? true : '名称不能为空'),
  })
  const t = await printApi.createTemplate(store.bookId, {
    scope: scope.value,
    name: name.trim(),
    content: clone(tpl.value),
    is_default: false,
  })
  await loadTemplates()
  templateId.value = t.id
  await loadTemplate(t.id)
}

async function doDelete() {
  try {
    await ElMessageBox.confirm('删除后无法恢复，确定删除该打印模板吗？', '删除模板', { type: 'warning' })
  } catch {
    return
  }
  await printApi.deleteTemplate(store.bookId, templateId.value)
  ElMessage.success('已删除')
  await loadTemplates()
  templateId.value = listOfScope.value[0]?.id || 0
  await loadTemplate(templateId.value)
}

onMounted(async () => {
  meta.value = await printApi.templateMeta(store.bookId)
  await loadTemplates()
  await loadPreviewData()
  templateId.value = listOfScope.value.find((t) => t.is_default)?.id || listOfScope.value[0]?.id || 0
  await loadTemplate(templateId.value)
})

watch(() => store.bookId, async () => {
  if (store.bookId) {
    await loadTemplates()
    await loadPreviewData()
    await loadTemplate(templateId.value)
  }
})
</script>

<style scoped>
.designer {
  display: grid; grid-template-columns: 286px 408px minmax(420px, 1fr);
  gap: 12px; align-items: start;
}
@media (max-width: 1280px) {
  .designer { grid-template-columns: 1fr; }
}
.panel :deep(.el-card__body) { max-height: 76vh; overflow: auto; padding: 14px 12px; }
.panel :deep(.el-table .cell) { padding: 0 4px; }
.panel :deep(.el-form-item) { margin-bottom: 12px; }
.preview {
  width: 100%; height: 68vh; border: 1px solid var(--cwq-border);
  border-radius: 4px; background: #fff;
}
.margins { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 8px; width: 100%; }
.margins > div { display: flex; align-items: center; gap: 5px; }
.margins .mlabel { font-size: 12px; color: #888; width: 28px; flex: none; }
.footer-bar {
  position: sticky; bottom: 0; display: flex; gap: 10px; align-items: center;
  background: #fff; border: 1px solid var(--cwq-border); border-radius: 4px;
  padding: 10px 14px; margin-top: 12px;
}
.mt { margin-top: 12px; }
.ml { margin-left: 10px; }
.tip { font-size: 12px; color: #888; line-height: 1.8; }
code { background: #f5f5f5; padding: 1px 4px; border-radius: 3px; }
</style>
