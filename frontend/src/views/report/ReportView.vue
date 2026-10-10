<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-select v-model="templateId" style="width: 200px" @change="onTemplateChange">
        <el-option v-for="t in allTemplates" :key="t.id" :label="t.name" :value="t.id" />
      </el-select>
      <el-select v-model="bookId" style="width: 200px" placeholder="复制为新模板到账套">
        <el-option v-for="b in store.books" :key="b.id" :label="b.name" :value="b.id" />
      </el-select>
      <el-button @click="forkTemplate" :disabled="!bookId">另存为新模板</el-button>
      <el-checkbox v-model="designMode" label="设计模式" @change="onDesignToggle" />
      <span class="spacer" />
      <el-button v-if="designMode" type="success" :disabled="!dirtyCount" @click="saveAll">
        保存公式 <template v-if="dirtyCount">（{{ dirtyCount }}）</template>
      </el-button>
      <el-button @click="previewHtml">模板预览/打印</el-button>
      <el-button @click="exportXlsx">导出 Excel</el-button>
    </div>

    <el-alert v-if="designMode" type="info" :closable="false" class="mb no-print">
      设计模式：左侧可增删行、调整缩进与加粗/隐藏，列头可增删列、列标签直接改。
      任何结构变动都会自动改写全表公式的单元格引用（例如在第 10 行插入后
      <code>合计(B11:B15)</code> 自动变成 <code>合计(B12:B16)</code>），但仍建议保存前核对一眼。
    </el-alert>

    <div class="card">
      <div class="report-head">
        <h2 class="report-title">{{ template?.name }}</h2>
        <div class="report-sub">
          {{ store.currentBook?.company_name }} · {{ store.currentBook?.name }} {{ store.currentBook?.year }}
          · 期间 {{ data.period }}
          <span v-if="hiddenRowCount" class="muted">（{{ hiddenRowCount }} 行已隐藏，仅设计模式可见）</span>
        </div>
      </div>

      <el-alert v-if="data.errors?.length" type="error" :closable="false" class="mb">
        <div v-for="(e, i) in data.errors" :key="i">{{ e }}</div>
      </el-alert>

      <table class="report-table" v-loading="loading">
        <thead>
          <tr>
            <th class="c-gutter no-print" v-if="designMode">行</th>
            <th class="c-label" :style="{ width: `${labelWidth}px` }">
              项目
              <span class="addr no-print">{{ staticLetter }}</span>
            </th>
            <th v-for="c in valueCols" :key="c.col_no" class="c-head" :style="{ width: `${c.width}px` }">
              <div class="head-edit no-print">
                <el-button link size="small" :title="`在 ${c.letter} 列左侧插入`" @click="insertCol(c.col_no)">⊕</el-button>
                <el-button link size="small" :title="`删除 ${c.letter} 列`" @click="removeCol(c)">✕</el-button>
              </div>
              <el-input v-if="designMode" v-model="c.label" size="small" class="col-label"
                        @change="renameCol(c, $event)" />
              <template v-else>{{ c.label }}</template>
              <div class="addr no-print">{{ c.letter }}</div>
              <div class="col-meta no-print">
                <el-select v-if="designMode" :model-value="c.kind" size="small"
                           @change="changeColKind(c, $event)">
                  <el-option v-for="(v, k) in COL_KINDS" :key="k" :label="v" :value="k" />
                </el-select>
              </div>
            </th>
            <th class="c-gutter no-print" v-if="designMode">
              <el-button link size="small" title="在末尾加一行" @click="insertRow(lastRowNo + 1)">＋行</el-button>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in visibleRows" :key="row.row_no"
              :class="{ bold: row.bold, 'is-hidden': row.hidden }">
            <td v-if="designMode" class="c-gutter no-print">
              <div class="gutter">
                <span class="addr">{{ row.row_no }}</span>
                <el-button link size="small" title="在上面插入一行" @click="insertRow(row.row_no)">⊕</el-button>
                <el-button link size="small" title="删除本行" @click="removeRow(row)">✕</el-button>
                <el-button link size="small" :title="row.bold ? '取消加粗' : '加粗'"
                           :type="row.bold ? 'primary' : ''" @click="toggleRow(row, 'bold')">B</el-button>
                <el-button link size="small" :title="row.hidden ? '取消隐藏' : '隐藏（不参与打印）'"
                           :type="row.hidden ? 'warning' : ''" @click="toggleRow(row, 'hidden')">◉</el-button>
                <el-button link size="small" title="减少缩进" :disabled="row.indent === 0"
                           @click="toggleRow(row, 'indent', -1)">⇤</el-button>
                <el-button link size="small" title="增加缩进" @click="toggleRow(row, 'indent', +1)">⇥</el-button>
              </div>
            </td>
            <td class="c-label" :style="{ paddingLeft: `${8 + row.indent * 16}px` }">
              <el-input v-if="designMode" v-model="row.label" size="small" class="row-label"
                        @change="renameRow(row, $event)" />
              <template v-else>{{ row.label }}</template>
            </td>
            <td
              v-for="cell in rowCells(row)"
              :key="cell.col_no"
              class="c-num"
              :class="{ editable: designMode, error: cell.error, dirty: isDirty(row.row_no, cell.col_no) }"
              :title="cell.formula || ''"
              @click="designMode && onCellClick(row, cell)"
            >
              <span v-if="cell.error" class="tag-bad">!</span>
              <span v-if="cell.addr" class="addr no-print">{{ cell.addr }}</span>
              {{ cell.value !== null && cell.value !== undefined ? fmt(cell.value) : '' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 公式编辑 -->
    <el-dialog v-model="formulaVisible" title="编辑单元格公式" width="620px" class="no-print">
      <div class="edit-head">
        <span class="badge">{{ editing?.addr }}</span>
        <span>{{ editing?.row?.label }} · {{ editing?.label }}</span>
        <span class="muted">当前值 {{ editing?.value ? fmt(editing.value) : '（空）' }}</span>
      </div>
      <el-input v-model="editingFormula" placeholder='例：科目("1001","期末余额",{{本期}})' />
      <div class="fn-list">
        <div class="fn-title">可用写法</div>
        <div v-for="f in FN_HELP" :key="f.sig" class="fn">
          <code>{{ f.sig }}</code><span class="muted">{{ f.desc }}</span>
        </div>
      </div>
      <div v-if="recentAddrs.length" class="fn-list">
        <div class="fn-title">本表可引用</div>
        <div class="fn-list">
          <code v-for="a in recentAddrs" :key="a.addr" class="ref">{{ a.addr }}={{ fmt(a.value) }}</code>
        </div>
      </div>
      <template #footer>
        <el-button @click="formulaVisible = false">取消</el-button>
        <el-button v-if="editing?.formula" @click="editingFormula = ''; applyFormula()">清空</el-button>
        <el-button type="primary" @click="applyFormula">应用</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '@/api/report'
import { downloadFile, openPrint } from '@/api/print'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const route = useRoute()
const router = useRouter()

const allTemplates = ref([])
const templateId = ref(Number(route.params.templateId || 0))
// 默认当前账套，否则「另存为新模板」一进来就是禁用状态
const bookId = ref(store.bookId || 0)
const template = ref(null)
const data = ref({ rows: [], cols: [], errors: [] })
const loading = ref(false)
const designMode = ref(false)
const formulaVisible = ref(false)
const editing = ref(null)
const editingFormula = ref('')
const dirty = reactive({})

const labelWidth = 260
const lastRowNo = computed(() => Math.max(0, ...data.value.rows.map((r) => r.row_no)))

/** 列号 -> 字母（A=1, B=2 …）。公式里写的就是这个，界面上必须看得见。 */
function letterOf(colNo) {
  let n = colNo, s = ''
  while (n > 0) { const r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = Math.floor((n - 1) / 26) }
  return s
}

const COL_KINDS = {
  BALANCE_CLOSING: '期末余额',
  BALANCE_OPENING: '年初余额',
  PERIOD_AMOUNT: '本期金额',
  YTD_AMOUNT: '本年累计',
  PERIOD_DEBIT: '本期借方',
  PERIOD_CREDIT: '本期贷方'
}

// STATIC 列（科目行标题）在左边单独一列渲染，表格里不再重复一遍，
// 否则表头会比表体多一列，数字全错位。
const staticCol = computed(() => data.value.cols.find((c) => c.kind === 'STATIC'))
const staticLetter = computed(() => (staticCol.value ? staticLetterOf(staticCol.value) : 'A'))
const valueCols = computed(() => data.value.cols.filter((c) => c.kind !== 'STATIC'))

function staticLetterOf(c) { return letterOf(c.col_no) }

// 设计模式要看得到隐藏行（否则没法取消隐藏），查看模式则不显示
const hiddenRowCount = computed(() => data.value.rows.filter((r) => r.hidden).length)
const visibleRows = computed(() =>
  designMode.value ? data.value.rows : data.value.rows.filter((r) => !r.hidden))

function rowCells(row) {
  const ids = new Set(valueCols.value.map((c) => c.col_no))
  return row.cells
    .filter((c) => ids.has(c.col_no))
    .map((c) => ({ ...c, addr: letterOf(c.col_no) + row.row_no, dirtyKey: `${row.row_no}:${c.col_no}` }))
}

const isDirty = (rowNo, colNo) => dirty[`${rowNo}:${colNo}`] !== undefined
const dirtyCount = computed(() => Object.keys(dirty).length)

const FN_HELP = [
  { sig: '科目("1001","期末余额",{{本期}})', desc: '取科目余额/发生额；口径：期初、期末余额、借方累计、贷方累计、本期借方、本期贷方、本期金额、本年累计' },
  { sig: '合计(B2:B8)', desc: '单元格区域求和' },
  { sig: '上期(B10)', desc: '同一行的上一列' },
  { sig: 'IF(B10>0,B10,0)', desc: '条件判断' },
  { sig: '四舍五入(B10,2)', desc: '四舍五入到指定位数' },
  { sig: 'B9+B16', desc: '单元格加减；支持 + - * / 与比较运算' },
  { sig: '{{本期}} {{上期}} {{年初}}', desc: '期间占位符' }
]

/** 当前各格的取值，做成可点击的速查表，省得靠猜。 */
const recentAddrs = computed(() => {
  const out = []
  for (const row of data.value.rows) {
    for (const c of rowCells(row)) {
      if (c.value !== null && c.value !== undefined && c.value !== '') {
        out.push({ addr: c.addr, value: c.value })
      }
    }
  }
  return out.slice(0, 60)
})

function onDesignToggle() {
  if (designMode.value) load()
}

// ------------------------------------------------------------------ 单元格

function onCellClick(row, cell) {
  editing.value = { row, colNo: cell.col_no, label: cell.label, addr: cell.addr,
                    value: cell.value, formula: cell.formula }
  editingFormula.value = dirty[cell.dirtyKey] ?? cell.formula ?? ''
  formulaVisible.value = true
}

function applyFormula() {
  dirty[editing.value.row.row_no + ':' + editing.value.colNo] = editingFormula.value
  formulaVisible.value = false
  load(true)
}

async function saveAll() {
  const cells = Object.entries(dirty).map(([k, formula]) => {
    const [row_no, col_no] = k.split(':').map(Number)
    return { row_no, col_no, formula }
  })
  if (!cells.length) return ElMessage.info('没有改动')
  await api.saveCells(templateId.value, cells, store.bookId)
  ElMessage.success('公式已保存')
  dirtyKeysReset()
  load()
}

function dirtyKeysReset() {
  Object.keys(dirty).forEach((k) => delete dirty[k])
}

// ------------------------------------------------------------------ 行操作

async function insertRow(at) {
  await api.addRow(templateId.value, { row_no: at, label: '新项目' }, store.bookId)
  await load()
  ElMessage.success(`已在第 ${at} 行插入，下方公式引用已自动顺延`)
}

async function removeRow(row) {
  await ElMessageBox.confirm(
    `删除「${row.label}」这一行？下方行的公式引用会自动上收，相关单元格公式也会一起删除。`,
    '删除行', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).catch(() => Promise.reject())
  const r = await api.deleteRow(templateId.value, row.row_no, store.bookId)
  await load()
  ElMessage.success(`已删除，${r.formulas_shifted} 处引用已同步修正`)
}

async function renameRow(row, label) {
  if (label === row.label) return
  await api.updateRow(templateId.value, row.row_no, { label }, store.bookId)
  row.label = label
}

async function toggleRow(row, field, delta = null) {
  const payload = {}
  if (field === 'indent') payload.indent = Math.max(0, (row.indent || 0) + delta)
  else payload[field] = !row[field]
  await api.updateRow(templateId.value, row.row_no, payload, store.bookId)
  await load()
}

// ------------------------------------------------------------------ 列操作

async function insertCol(at) {
  const r = await api.addCol(templateId.value, { col_no: at, label: '新列' }, store.bookId)
  await load()
  ElMessage.success(`已插入 ${r.letter} 列，${r.formulas_shifted} 处引用已自动改写`)
}

async function removeCol(col) {
  await ElMessageBox.confirm(
    `删除「${col.label}」(${col.letter}) 整列？本列所有单元格公式会一并删除，引用它的公式会自动前移。`,
    '删除列', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).catch(() => Promise.reject())
  const r = await api.deleteCol(templateId.value, col.col_no, store.bookId)
  await load()
  ElMessage.success(`已删除，${r.formulas_shifted} 处引用已同步修正`)
}

async function renameCol(col, label) {
  if (label === col.label) return
  await api.updateCol(templateId.value, col.col_no, { label }, store.bookId)
  col.label = label
}

async function changeColKind(col, kind) {
  await api.updateCol(templateId.value, col.col_no, { kind }, store.bookId)
  col.kind = kind
  await load()
}

async function forkTemplate() {
  const { value } = await ElMessageBox.prompt('新模板名称', '另存为新模板',
    { inputValue: `${template.value?.name ?? '报表'} 副本` }).catch(() => ({ value: null }))
  if (!value) return
  const t = await api.copyTemplate(templateId.value, value, bookId.value)
  // 必须重新拉列表：新模板不在 allTemplates 里时 el-select 找不到对应 option，
  // 下拉框会直接把 id 显示出来（看着像个 bug，其实是没刷新）
  allTemplates.value = await api.templates()
  ElMessage.success('已复制，正在打开新模板')
  router.replace(`/reports/${t.id}`)
  templateId.value = t.id
  load()
}

// ------------------------------------------------------------------ 数据

async function load(keepDirty = false) {
  if (!templateId.value || !store.bookId || !store.periodId) return
  loading.value = true
  try {
    const tpl = allTemplates.value.find((t) => t.id === templateId.value)
    template.value = tpl
    const res = await api.render(templateId.value, {
      book_id: store.bookId, period_id: store.periodId
    })
    // 列字母挂在列上，行编辑按钮要用
    res.cols = res.cols.map((c) => ({ ...c, letter: letterOf(c.col_no) }))
    data.value = res
    if (designMode.value && keepDirty) {
      for (const row of res.rows) {
        // 键必须用 cell.col_no（1 基），跟 onCellClick 存进去的一致。
        // 用数组下标 ci（0 基）会永远匹配不上，编辑中的公式就"丢了"
        row.cells.forEach((cell) => {
          const key = `${row.row_no}:${cell.col_no}`
          if (dirty[key] !== undefined) {
            cell.formula = dirty[key]
            cell.value = '（待保存后重算）'
          }
        })
      }
    }
  } finally {
    loading.value = false
  }
}

function onTemplateChange(id) {
  router.replace(`/reports/${id}`)
  dirtyKeysReset()
  load()
}
function previewHtml() {
  openPrint(`/books/${store.bookId}/print/report.html`, {
    template_id: templateId.value, period_id: store.periodId,
  })
}
function exportXlsx() {
  downloadFile(`/books/${store.bookId}/print/report.xlsx`,
    { template_id: templateId.value, period_id: store.periodId }, '报表.xlsx')
}

watch(() => store.bookId, (v) => { if (v && !bookId.value) bookId.value = v })

onMounted(async () => {
  allTemplates.value = await api.templates()
  if (!templateId.value && allTemplates.value.length) templateId.value = allTemplates.value[0].id
  load()
})
watch(() => [store.bookId, store.periodId], () => load())
</script>
<style scoped>
.report-head { text-align: center; margin-bottom: 14px; }
.report-title { margin: 0 0 4px; font-size: 19px; letter-spacing: 6px; }
.report-sub { font-size: 13px; color: var(--cwq-muted); }

.report-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.report-table th, .report-table td { border: 1px solid #dcdfe6; padding: 6px 8px; }
.report-table th { background: #f7f8fa; font-weight: 600; text-align: center; }
.report-table td.c-num { text-align: right; font-variant-numeric: tabular-nums; }
.report-table td.c-label { text-align: left; white-space: nowrap; }
.report-table tr.bold td { font-weight: 700; }
.report-table td.editable { cursor: cell; }
.report-table td.editable:hover { background: #eff6ff; outline: 1px dashed var(--cwq-primary); outline-offset: -1px; }
.report-table td.error { background: #fef2f2; color: var(--cwq-danger); }
.report-table td.dirty { background: #fffbeb; }
.report-table tr.is-hidden td { opacity: .45; }

/* 地址角标：设计模式下每个格子都标 B9 这样的坐标，
   用户写公式时需要看见自己在改哪一格 */
.addr {
  font-size: 10px; color: var(--cwq-muted); font-weight: 400;
  font-variant-numeric: tabular-nums;
}
td.c-num .addr { float: right; margin-left: 6px; }
th.c-head .addr { margin-top: 2px; }
th.c-label .addr { margin-left: 4px; }

/* 列头的增删按钮走常规流：绝对定位会被 label 输入框压住，点不到也看不见 */
.report-table th.c-head { vertical-align: top; }
.head-edit { display: flex; justify-content: flex-end; gap: 2px; margin-bottom: 2px; }
.col-label { width: 100%; }
.col-meta { margin-top: 4px; }
.col-meta :deep(.el-select) { width: 100%; }

th.c-gutter, td.c-gutter { background: #fbfbfc; width: 1%; white-space: nowrap; }
.gutter { display: flex; align-items: center; gap: 0; }
.gutter .addr { min-width: 18px; }
.row-label { width: 100%; }

.edit-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.badge {
  font-family: ui-monospace, monospace; font-weight: 700; background: var(--cwq-primary);
  color: #fff; padding: 1px 8px; border-radius: 4px;
}
.fn-list { margin-top: 14px; max-height: 240px; overflow: auto; font-size: 12px; }
.fn-title { font-weight: 600; margin-bottom: 4px; color: var(--cwq-muted); }
.fn { display: flex; gap: 10px; padding: 3px 0; align-items: baseline; }
.fn code { background: #f3f4f6; padding: 1px 5px; border-radius: 3px; white-space: nowrap; }
code.ref { display: inline-block; margin: 2px 4px; background: #f3f4f6; padding: 1px 5px; border-radius: 3px; }
.mb { margin-bottom: 12px; }
</style>
