<template>
  <div class="page page-plain">
    <el-card shadow="never" class="mt">
      <template #header>
        <div class="card-head"><span class="title">打印模板</span></div>
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

    <div class="designer" :class="{ voucher: isVoucher }">
      <el-card shadow="never" class="panel p-format">
        <template #header><span class="title">版式</span></template>

        <el-form label-width="86px" label-position="left" size="default">
          <el-divider content-position="left">纸张</el-divider>
          <el-form-item label="纸型">
            <el-select v-model="tpl.paper" style="width: 100%" @change="onPaperChange">
              <el-option v-for="p in meta.papers" :key="p.key" :value="p.key"
                         :label="paperLabel(p.key)" />
            </el-select>
          </el-form-item>
          <el-form-item label="方向">
            <el-radio-group v-model="tpl.orientation" @change="onOrientationChange">
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

          <!-- 凭证的标题/页眉/页脚/框线都在下方「版式编辑」画布里直接改
               （文字、字号、粗细、显隐、框线），这里再放一份就是两套开关互相打架 -->
          <template v-if="!isVoucher">
            <el-divider content-position="left">标题</el-divider>
            <el-form-item label="主标题"><el-input v-model="tpl.title.text" @input="schedulePreview" @change="normalize" /></el-form-item>
            <el-form-item label="字号">
              <el-input-number v-model="tpl.title.size" :min="8" :max="40" size="small" @change="normalize" />
              <el-checkbox v-model="tpl.title.bold" class="ml" @change="normalize">加粗</el-checkbox>
            </el-form-item>
            <el-form-item label="副标题"><el-input v-model="tpl.subtitle.text" placeholder="留空则显示期间"
                         @input="schedulePreview" @change="normalize" /></el-form-item>
          </template>

          <el-divider content-position="left">字体</el-divider>
          <el-form-item label="字体">
            <el-input v-model="tpl.font.family" @input="schedulePreview" @change="normalize" />
          </el-form-item>
          <el-form-item v-if="!isVoucher" label="字号">
            <el-input-number v-model="tpl.font.size" :min="6" :max="24" :step="0.5" size="small" @change="normalize" />
            pt
          </el-form-item>

          <template v-if="!isVoucher">
            <el-divider content-position="left">页眉字段</el-divider>
            <el-form-item label="显示项">
              <el-checkbox-group v-model="metaOn" @change="normalize">
                <el-checkbox v-for="(label, key) in META_CN" :key="key" :value="key">{{ label }}</el-checkbox>
              </el-checkbox-group>
            </el-form-item>
            <el-form-item label="页脚">
              <el-input v-model="tpl.footer.text" placeholder="自定义页脚文字"
                        @input="schedulePreview" @change="normalize" />
            </el-form-item>
            <el-form-item label="页码">
              <el-checkbox v-model="tpl.footer.show_page_no" @change="normalize">显示页码</el-checkbox>
              <el-input v-model="tpl.footer.format" size="small" class="ml" style="width: 200px"
                        @change="normalize" />
            </el-form-item>
          </template>

          <el-divider content-position="left">表格</el-divider>
          <el-form-item v-if="!isVoucher" label="边框">
            <el-switch v-model="tpl.body.border" @change="normalize" />
          </el-form-item>
          <el-form-item label="重复表头">
            <el-switch v-model="tpl.body.repeat_header" @change="normalize" />
          </el-form-item>
        </el-form>
      </el-card>

      <el-card shadow="never" class="panel p-preview">
        <template #header>
          <div class="card-head">
            <span class="title">预览</span>
            <el-tag v-if="previewing" size="small" type="info" effect="plain">渲染中…</el-tag>
            <el-tag v-else-if="dirty" size="small" type="warning" effect="plain">未保存</el-tag>
            <el-button size="small" @click="refreshPreview"><el-icon><Refresh /></el-icon></el-button>
            <el-button size="small" type="primary" @click="doPrint">
              <el-icon><Printer /></el-icon>&nbsp;打印
            </el-button>
          </div>
        </template>

        <el-form v-if="scope === 'DETAIL_LEDGER' || scope === 'REPORT'" :inline="true" class="preview-opts">
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

        <div v-if="previewError" class="preview-err">
          <el-alert type="warning" :closable="false" show-icon :title="previewError" />
        </div>
        <div v-else ref="box" class="preview-box">
          <div ref="stage" class="preview-stage" :style="stageStyle">
            <iframe ref="frame" class="preview" :srcdoc="previewHtml" title="打印预览"
                    :style="frameStyle" @load="measurePreview"></iframe>
          </div>
        </div>
      </el-card>

      <el-card shadow="never" class="panel p-cols">
        <template #header>
          <div class="card-head"><span class="title">列</span></div>
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
              <el-input v-model="row.label" size="small" style="width: 100%"
                        @input="schedulePreview" @change="normalize" />
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
              <el-button link type="danger" :icon="Delete" :disabled="tpl.columns.length <= 1"
                         @click="removeColumn($index)" />
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

        <div class="fields">
          <div class="fields-title">
            可用字段
            <span class="fields-count">{{ FIELD_DOC.length }}</span>
          </div>
          <el-collapse v-model="openFieldGroup" class="fields-body">
            <el-collapse-item v-for="g in FIELD_DOC" :key="g.title" :name="g.title">
              <template #title>
                <span class="fields-group">{{ g.title }}</span>
                <span class="fields-group-sub">{{ g.items.length }}</span>
              </template>
              <dl class="fields-list">
                <template v-for="f in g.items" :key="f.key">
                  <dt><code>{{ f.key }}</code></dt>
                  <dd>{{ f.desc }}</dd>
                </template>
              </dl>
            </el-collapse-item>
          </el-collapse>
        </div>
      </el-card>

      <el-card v-if="isVoucher" shadow="never" class="panel p-layout">
        <template #header>
          <div class="card-head"><span class="title">版式编辑</span></div>
        </template>
        <VoucherLayoutEditor :tpl="tpl" :slots="voucherSlots" @change="onLayoutChange" />
      </el-card>

    </div>

    <div class="footer-bar">
      <el-button type="primary" :loading="saving" :disabled="!store.canWrite || !templateId" @click="save">
        保存模板
      </el-button>
      <el-button @click="loadTemplate(templateId)">放弃改动</el-button>
      <span class="spacer"></span>
      <el-tag v-if="dirty" size="small" type="warning" effect="plain">未保存</el-tag>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Top, Bottom, Plus, CopyDocument, Refresh, Printer } from '@element-plus/icons-vue'
import VoucherLayoutEditor from '@/components/VoucherLayoutEditor.vue'
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
  voucher: { rows_per_page: 5, split_number: true, fill_rows: true },
  elements: [],
}

/** 一页能放两张凭证的纸型，要和后端 TWO_UP_PAPERS 保持一致。 */
const TWO_UP_PAPERS = ['A4', 'A3', 'A4_LANDSCAPE']
const PAPER_LABEL = {
  A4: 'A4（每页 2 张凭证）',
  A3: 'A3（每页 2 张凭证）',
  A4_LANDSCAPE: 'A4 横向（每页 2 张凭证）',
  A5: 'A5（每页 1 张凭证）',
  B5: 'B5（每页 1 张凭证）',
  CONTINUOUS: '连续纸（每页 1 张凭证）',
}

const meta = ref({ papers: [], defaults: {} })
const scope = ref('VOUCHER')
const templateId = ref(0)
const list = ref([])
const tpl = ref(clone(EMPTY_TPL))
const frame = ref(null)
const stage = ref(null)
const box = ref(null)
const subjects = ref([])
const reports = ref([])
const saving = ref(false)

/** 预览期间跟顶栏走。顶栏已经有一个期间选择器，面板里再放一个只会选出两份不一样的期间。 */
const previewPeriodId = computed(() =>
  store.periodId || store.currentPeriod?.id || store.periods[store.periods.length - 1]?.id || null)
const fromPeriodId = ref(null)
const previewSubjectId = ref(null)
const previewReportId = ref(null)
const previewHtml = ref('')
const previewError = ref('')
const previewing = ref(false)
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
/** 字段说明：key 必须和 COLUMN_KEYS / 后端渲染器认识的字段一致。 */
const FIELD_DOC = [
  {
    title: '凭证分录',
    items: [
      { key: 'line_no', desc: '分录行号' },
      { key: 'summary', desc: '摘要' },
      { key: 'subject', desc: '会计科目（编码 + 名称）' },
      { key: 'debit', desc: '借方金额' },
      { key: 'credit', desc: '贷方金额' },
    ],
  },
  {
    title: '账簿通用',
    items: [
      { key: 'date', desc: '凭证日期' },
      { key: 'voucher_no', desc: '凭证字号' },
      { key: 'direction', desc: '借贷方向' },
      { key: 'balance', desc: '该科目余额' },
    ],
  },
  {
    title: '科目余额表',
    items: [
      { key: 'code', desc: '科目编码' },
      { key: 'name', desc: '科目名称' },
      { key: 'level', desc: '科目级次' },
      { key: 'opening_debit', desc: '期初借方' },
      { key: 'opening_credit', desc: '期初贷方' },
      { key: 'period_debit', desc: '本期借方发生额' },
      { key: 'period_credit', desc: '本期贷方发生额' },
      { key: 'closing_debit', desc: '期末借方' },
      { key: 'closing_credit', desc: '期末贷方' },
      { key: 'opening', desc: '期初余额' },
      { key: 'closing', desc: '期末余额' },
    ],
  },
  {
    title: '报表',
    items: [
      { key: 'label', desc: '项目名称' },
      { key: 'value0', desc: '本期金额（第 1 列）' },
      { key: 'value1', desc: '本期金额（第 2 列）' },
      { key: 'value2', desc: '本期金额（第 3 列）' },
    ],
  },
]

const COLUMN_KEYS = [
  'line_no', 'summary', 'subject', 'debit', 'credit',
  'date', 'voucher_no', 'direction', 'balance',
  'code', 'name', 'level',
  'opening_debit', 'opening_credit', 'period_debit', 'period_credit',
  'closing_debit', 'closing_credit', 'opening', 'closing',
  'label', 'value0', 'value1', 'value2',
]

const openFieldGroup = ref(['凭证分录'])

/** 各纸型的惯用方向：记账凭证在 A5/B5 上是横向小册子，A4/A3 则是纵向。 */
const DEFAULT_ORIENTATION = {
  A4: 'portrait', A3: 'portrait', A4_LANDSCAPE: 'landscape',
  A5: 'landscape', B5: 'landscape', CONTINUOUS: 'landscape',
}

function onOrientationChange() {
  contentH.value = 0
  normalize()
}

/** 换纸型就把方向带到该纸型的默认值，否则 A5 沿用 A4 的纵向，横向小册子排不出来。 */
function onPaperChange() {
  tpl.value.orientation = DEFAULT_ORIENTATION[tpl.value.paper] || 'portrait'
  contentH.value = 0
  normalize()
}

const listOfScope = computed(() => list.value.filter((t) => t.scope === scope.value))
const isVoucher = computed(() => scope.value === 'VOUCHER')
const voucherSlots = computed(() => (TWO_UP_PAPERS.includes(tpl.value.paper) ? 2 : 1))
function paperLabel(key) {
  return isVoucher.value ? (PAPER_LABEL[key] || key) : key
}

/** 拖拽过程每帧都在改坐标，只重排防抖刷新，不碰列宽归一化。 */
function onLayoutChange() {
  schedulePreview()
}

/** 当前草稿是否与已保存内容不同 —— 用来提示「未保存」，免得以为已经存进去了。 */
const savedSnapshot = ref('')
const dirty = computed(() => !!templateId.value && JSON.stringify(tpl.value) !== savedSnapshot.value)
const metaOn = computed({
  get: () => Object.keys(tpl.value?.meta || {}).filter((k) => tpl.value.meta[k]),
  set: (keys) => {
    for (const k of Object.keys(tpl.value.meta || {})) tpl.value.meta[k] = keys.includes(k)
  },
})

/**
 * 凭证的显隐统一交给画布元素的 visible 开关（后端 _voucher_element_html 只认它）。
 * 老模板把开关存在 meta 里，载入时折进 visible，再把 meta 全部置 true，
 * 免得留下一个界面上已经关不掉的「隐藏」。
 */
function migrateVoucherVisibility() {
  if (!isVoucher.value) return
  for (const el of tpl.value.elements) {
    if (el.visible === undefined) el.visible = true
    if (META_CN[el.bind] && tpl.value.meta?.[el.bind] === false) el.visible = false
  }
  Object.keys(tpl.value.meta || {}).forEach((k) => { tpl.value.meta[k] = true })
  tpl.value.show.page_no = true
}

/** 把本地改动对齐到「可提交」的形态：列宽归一化、meta/show 补全 */
function normalize() {
  const total = (tpl.value.columns || []).reduce((s, c) => s + (Number(c.width) || 0), 0) || 1
  tpl.value.columns.forEach((c) => { c.width = round((Number(c.width) || 0) / total * 100) })
  if (!Array.isArray(tpl.value.elements)) tpl.value.elements = []
  if (!tpl.value.voucher) tpl.value.voucher = { rows_per_page: 5, split_number: true, fill_rows: true }
  if (!tpl.value.meta) tpl.value.meta = {}
  Object.keys(META_CN).forEach((k) => { if (!(k in tpl.value.meta)) tpl.value.meta[k] = false })
  if (!tpl.value.show) tpl.value.show = {}
  schedulePreview()
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

function removeColumn(i) {
  if (tpl.value.columns.length <= 1) return   // 至少留一列，否则模板是非法的
  tpl.value.columns.splice(i, 1)
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
  normalize()
  migrateVoucherVisibility()
  // 等列宽归一化完再取快照，否则「刚打开就显示未保存」是假警报
  savedSnapshot.value = JSON.stringify(tpl.value)
}

async function loadPreviewData() {
  subjects.value = await subjectApi.subjectFlat(store.bookId)
  if (!previewSubjectId.value && subjects.value.length) previewSubjectId.value = subjects.value[0].id
  reports.value = await reportApi.templates()
  if (!previewReportId.value && reports.value.length) previewReportId.value = reports.value[0].id
}

/**
 * 预览请求体：带上**当前未保存的草稿**。
 *
 * 这是「改一下右边立刻变」的全部秘密 —— 预览不吃已保存的模板（那要先点保存，
 * 还得刷新），而是把编辑器里这份 JSON 直接发给后端渲染一遍。
 */
function previewPayload() {
  return {
    scope: scope.value,
    content: tpl.value,
    period_id: previewPeriodId.value,
    from_period_id: fromPeriodId.value || undefined,
    subject_id: previewSubjectId.value || undefined,
    report_template_id: previewReportId.value || undefined,
  }
}

/**
 * 正式打印：与预览**同一个范围、同一个期间**，只是模板换成已保存的那份。
 *
 * 打印走 template_id 而不是草稿，是因为打印窗口里的东西可能被人另存为 PDF
 * 发出去 —— 必须对应一条真正存了档的版式，而不是编辑器里还没保存的状态。
 */
function buildTarget() {
  const pid = previewPeriodId.value
  const tp = templateId.value || undefined
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
  const token = ++previewToken
  previewing.value = true
  try {
    const html = await printApi.previewHtml(store.bookId, previewPayload())
    if (token !== previewToken) return              // 过期响应直接丢弃，避免旧结果盖掉新的
    previewHtml.value = html
    previewError.value = ''
    nextTick(fitPreview)
  } catch (e) {
    if (token !== previewToken) return
    const detail = e.response?.data?.detail
    // 草稿处于半成品状态（列全删了、科目没选）是正常编辑过程，
    // 就地显示原因即可，不该弹一个红色报错打断。
    previewError.value = typeof detail === 'string' ? detail : '预览加载失败'
  } finally {
    if (token === previewToken) previewing.value = false
  }
}

/**
 * 防抖刷新。
 *
 * 输入框每敲一个字就发一次请求的话，一个标题就能打出几十个请求；
 * 但完全不等又会让人觉得「不跟手」。250ms 是两者之间的平衡点。
 */
let previewTimer = null
function schedulePreview(delay = 250) {
  clearTimeout(previewTimer)
  previewTimer = setTimeout(refreshPreview, delay)
}

/** 纸型物理尺寸（mm），和后端 PAPERS 保持一致。 */
const PAPER_MM = {
  A4: [210, 297], A3: [297, 420], A5: [148, 210], B5: [176, 250],
  A4_LANDSCAPE: [297, 210], CONTINUOUS: [100, 297],
}
const MM2PX = 96 / 25.4

/** 当前纸张在 96dpi 下的像素尺寸；orientation 为横向时宽高互换。 */
const pagePx = computed(() => {
  const [w, h] = PAPER_MM[tpl.value.paper] || PAPER_MM.A4
  const landscape = tpl.value.orientation === 'landscape'
  return {
    w: Math.round((landscape ? h : w) * MM2PX),
    h: Math.round((landscape ? w : h) * MM2PX),
  }
})

const scale = ref(1)
const contentH = ref(0)
/** 量高留的兜底余量：一像素的取整误差就够顶出一条内部滚动条。 */
const H_PAD = 2
/** 文档内部的尺寸观察者：内容变高时自动把 iframe 补高。 */
let innerRO = null
function disconnectInnerRO() {
  innerRO?.disconnect()
  innerRO = null
}

const frameStyle = computed(() => {
  const { w, h } = pagePx.value
  return {
    width: w + 'px',
    // 多页预览时按内容高度撑开，至少一整页
    height: Math.max(h, contentH.value || h) + 'px',
    transform: `scale(${scale.value})`,
  }
})

const stageStyle = computed(() => {
  const { w, h } = pagePx.value
  return {
    width: Math.round(w * scale.value) + 'px',
    height: Math.round(Math.max(h, contentH.value || h) * scale.value) + 'px',
  }
})

/** 容器宽度变了（窗口缩放、面板重排）就重算缩放比。 */
let ro = null
function fitPreview() {
  const host = box.value
  if (!host) return
  // clientWidth 是「内容 + padding」，真正能放版面的只有扣掉左右 padding 的内容宽。
  // 之前直接用 clientWidth，右侧灰边会被撑没，页面还会溢出面板 8px。
  const cs = getComputedStyle(host)
  const availW = host.clientWidth
    - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0)
  if (availW <= 0) return
  // 宽度不够就缩小；宽度富余则适度放大到 1.4 倍把版面填满，
  // 不再拿高度卡一刀 —— 否则 A5 在宽面板里被压成灰底中间的一小块。
  scale.value = Math.max(0.15, Math.min(1.4, availW / pagePx.value.w))
}

/**
 * 预览文档必须**永远不自己滚动**：外层 .preview-box 才是唯一的滚动容器。
 *
 * iframe 上那个 scrolling="no" 是 HTML5 早就废弃的属性，Chrome 根本不认 ——
 * 只要文档比 iframe 高出一丁点，iframe 内部就会自己冒出一条滚动条。滚轮悬在
 * 预览上时先被这条「几乎滚不动」的滚动条吃掉，表现就是预览滑不动；而外层同时
 * 也有滚动条，于是同时出现两条，滑哪条都别扭。
 * 真正管用的是往文档里插一条 overflow 规则。
 */
function lockInnerScroll(d) {
  const host = d.head || d.documentElement
  if (!host || d.getElementById('__vle_lock')) return
  const st = d.createElement('style')
  st.id = '__vle_lock'
  st.textContent = 'html,body{overflow:hidden!important;}'
  host.appendChild(st)
}

/** 量「排版后内容真正占多高」的下沿，不受 iframe 视口高影响。 */
function readContentBottom(d, w) {
  const body = d.body
  if (!body) return 0
  // body 自身的下沿兜住「溢出 body 的内容」（绝对定位、超长子表格）。
  let bottom = body.getBoundingClientRect().bottom
  for (const node of body.children) {
    const r = node.getBoundingClientRect()
    const mb = parseFloat(w.getComputedStyle(node).marginBottom) || 0
    bottom = Math.max(bottom, r.bottom + mb)
  }
  return bottom
}

/**
 * iframe 是 about:srcdoc，同源可读：量出真实内容高度，好让多页预览不被裁掉。
 *
 * 量的是**最后一个纸页的下边缘**，不是 documentElement.scrollHeight ——
 * scrollHeight 永远不小于视口高，而视口高又是我们设进去的，拿它定高会变成
 * 「越量越高」的死循环（实测一张 A5 横向被撑到 2795px，实际只有 1 页 559px）。
 */
function measurePreview() {
  try {
    const d = frame.value?.contentDocument
    if (!d) return
    lockInnerScroll(d)
    disconnectInnerRO()
    const w = d.defaultView

    const apply = () => {
      const h = Math.max(pagePx.value.h, Math.ceil(readContentBottom(d, w)) + H_PAD)
      if (h !== contentH.value) contentH.value = h
    }
    apply()

    // srcdoc 一换就重新加载文档，而 contentH 还停在上一版的高度：新内容更高时，
    // 在量到之前 iframe 是偏矮的，内部就会闪出一条滚动条（改版式时最容易撞上）。
    // 盯住文档内容尺寸，任何原因导致变高都立刻补齐，滚轮就永远只有一个去处。
    if (w.ResizeObserver) {
      innerRO = new w.ResizeObserver(apply)
      innerRO.observe(d.body)
    }
  } catch { /* 跨源或尚未就绪，保持一整页高度即可 */ }
  nextTick(fitPreview)
}

onBeforeUnmount(() => { ro?.disconnect(); ro = null; disconnectInnerRO() })

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
    await loadTemplate(templateId.value)      // 重新取回服务端归一化后的内容
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
  if (box.value && window.ResizeObserver) {
    ro = new ResizeObserver(() => fitPreview())
    ro.observe(box.value)
  }
  meta.value = await printApi.templateMeta(store.bookId)
  await loadTemplates()
  await loadPreviewData()
  templateId.value = listOfScope.value.find((t) => t.is_default)?.id || listOfScope.value[0]?.id || 0
  await loadTemplate(templateId.value)
})

// 顶栏切期间，预览跟着走 —— 期间不再是面板里的独立控件了
watch(() => previewPeriodId.value, () => schedulePreview(0))

watch(() => store.bookId, async () => {
  if (store.bookId) {
    await loadTemplates()
    await loadPreviewData()
    await loadTemplate(templateId.value)
  }
})
</script>

<style scoped>
/* ── 三栏设计器 ──────────────────────────────────────────────
   版式 / 列 / 预览 三个面板同高同起，版式编辑在凭证范围下独占整行。
   align-items:stretch 让卡片底部对齐，右侧不会参差成阶梯状。 */
.designer {
  --gap: 12px;
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr) 520px;
  gap: var(--gap);
  align-items: stretch;
}
.designer.voucher { grid-template-columns: 300px minmax(0, 1fr) 520px; }
.designer.voucher .p-format { grid-column: 1; grid-row: 1; }
.designer.voucher .p-preview { grid-column: 2; grid-row: 1; }
.designer.voucher .p-cols    { grid-column: 3; grid-row: 1; }
.designer.voucher .p-layout  { grid-column: 1 / -1; grid-row: 2; }

@media (max-width: 1400px) {
  .designer, .designer.voucher { grid-template-columns: 300px minmax(0, 1fr); }
  .designer .p-format, .designer.voucher .p-format { grid-column: 1; grid-row: 1; }
  .designer .p-cols,    .designer.voucher .p-cols    { grid-column: 2; grid-row: 1; }
  .designer .p-preview, .designer.voucher .p-preview { grid-column: 1 / -1; grid-row: 2; }
  .designer.voucher .p-layout { grid-column: 1 / -1; grid-row: 3; }
}
@media (max-width: 1100px) {
  .designer, .designer.voucher { grid-template-columns: 1fr; }
  .designer .p-format, .designer .p-cols, .designer .p-preview,
  .designer.voucher .p-format, .designer.voucher .p-cols,
  .designer.voucher .p-layout, .designer.voucher .p-preview {
    grid-column: 1; grid-row: auto;
  }
}

/* 面板：内部滚动，卡片外框始终等高 */
.panel { display: flex; flex-direction: column; min-width: 0; }
.p-cols { max-width: 520px; }
.panel :deep(.el-card__header) {
  background: #fff; padding: 10px 14px;
  border-bottom: 1px solid var(--cwq-border);
}
.panel :deep(.el-card__body) {
  flex: 1; min-height: 0; max-height: 72vh;
  overflow: auto; padding: 14px;
}
.card-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.card-head .title { font-weight: 600; font-size: 14px; color: var(--cwq-text); }
.panel :deep(.el-form-item) { margin-bottom: 12px; }
.panel :deep(.el-form-item__content) { min-width: 0; }
.panel :deep(.el-input), .panel :deep(.el-select) { min-width: 0; max-width: 100%; }
.panel :deep(.el-table .cell) { padding: 0 4px; }

/* ── 预览 ─────────────────────────────────────────────────
   iframe 按纸张真实像素宽度渲染，再整块 scale 缩放进容器，
   右边缘不再被裁掉（A4 793px 塞进窄栏时尤其明显）。 */
.p-preview :deep(.el-card__body) {
  overflow: hidden; display: flex; flex-direction: column; min-height: 0;
}
.preview-box {
  /* 不写 flex:1：那样无论内容多少都撑满整张卡片。
     flex-shrink:1 保留，内容超过可用高度时照常收缩并滚动。 */
  flex: 0 1 auto; min-height: 0; overflow: auto;
  padding: 10px 8px;
  background: #f5f6f8;
  border-radius: 4px;
}
.preview-stage {
  margin: 0 auto;
  background: #fff;
  box-shadow: 0 1px 6px rgba(0, 0, 0, .12);
  transform-origin: top left;
}
.preview {
  display: block;
  border: 0;
  transform-origin: top left;
  background: #fff;
}
.preview-err {
  flex: 1; min-height: 0; display: flex; align-items: flex-start; padding-top: 24px;
}
.preview-opts :deep(.el-form-item) { margin-bottom: 10px; }

/* ── 底部保存条 ────────────────────────────────────────────
   悬浮在内容之上：纯不透明白底 + 阴影，否则下层面板会透上来。 */
.footer-bar {
  position: sticky; bottom: 0; z-index: 30;
  display: flex; gap: 10px; align-items: center;
  background: #fff;                       /* 不透明 */
  border: 1px solid var(--cwq-border);
  border-radius: 6px;
  box-shadow: 0 -2px 10px rgba(0, 0, 0, .10);
  padding: 10px 14px;
  margin-top: 12px;
}
.footer-bar .spacer { flex: 1; }

/* ── 零散 ─────────────────────────────────────────────────── */
.margins { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 8px; width: 100%; }
.margins > div { display: flex; align-items: center; gap: 5px; }
.margins .mlabel { font-size: 12px; color: #888; width: 28px; flex: none; }

.fields {
  margin-top: 12px; padding-top: 10px;
  border-top: 1px dashed var(--cwq-border);
}
.fields-title {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; font-weight: 600; color: var(--cwq-text);
  padding-bottom: 8px;
}
.fields-count {
  font-size: 11px; font-weight: 400; color: var(--cwq-muted);
  background: #f0f2f5; border-radius: 10px; padding: 1px 7px;
}
.fields-body { border-top: none; }
.fields-body :deep(.el-collapse-item__header) {
  height: 34px; line-height: 34px; font-size: 13px; padding: 0 4px;
  border-bottom-color: transparent;
}
.fields-body :deep(.el-collapse-item__wrap) { border-bottom: none; }
.fields-body :deep(.el-collapse-item__content) { padding-bottom: 6px; }
.fields-group { font-weight: 500; }
.fields-group-sub { margin-left: 6px; font-size: 11px; color: var(--cwq-muted); }
.fields-list {
  display: grid; grid-template-columns: minmax(96px, auto) 1fr;
  gap: 4px 10px; margin: 0; padding: 2px 4px 0;
}
.fields-list dt { margin: 0; }
.fields-list dd { margin: 0; font-size: 12px; color: #606266; line-height: 20px; }
.fields-list code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px; white-space: nowrap;
}

.page-plain { background: #fff; }
.mt { margin-top: 12px; }
.ml { margin-left: 10px; }
.toolbar :deep(.el-form-item) { margin-bottom: 0; }
code { background: #f5f5f5; padding: 1px 4px; border-radius: 3px; }
</style>
