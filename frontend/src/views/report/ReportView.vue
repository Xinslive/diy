<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-select v-model="templateId" style="width: 200px" @change="onTemplateChange">
        <el-option v-for="t in allTemplates" :key="t.id" :label="t.name" :value="t.id" />
      </el-select>
      <el-checkbox v-model="editMode" label="编辑公式" @change="load" />
      <span class="spacer" />
      <el-button v-if="editMode" type="success" @click="saveAll">保存公式</el-button>
      <el-button @click="previewHtml">模板预览/打印</el-button>
      <el-button @click="window.print()">打印本页</el-button>
      <el-button type="primary" @click="exportXlsx">导出 Excel</el-button>
    </div>

    <div class="card">
      <div class="report-head">
        <h2 class="report-title">{{ template?.name }}</h2>
        <div class="report-sub">
          {{ store.currentBook?.company }} · {{ store.currentBook?.name }} {{ store.currentBook?.year }}
          · 期间 {{ data.period }}
        </div>
      </div>

      <el-alert v-if="data.errors?.length" type="error" :closable="false" style="margin-bottom:10px">
        <div v-for="(e, i) in data.errors" :key="i">{{ e }}</div>
      </el-alert>

      <table class="report-table" v-loading="loading">
        <thead>
          <tr>
            <th class="c-label" style="width:260px">项目</th>
            <th v-for="c in valueCols" :key="c.col_no" :style="{ width: `${c.width}px` }">{{ c.label }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in data.rows" :key="row.row_no" :class="{ bold: row.bold }">
            <td class="c-label" :style="{ paddingLeft: `${8 + row.indent * 16}px` }">{{ row.label }}</td>
            <td
              v-for="cell in rowCells(row)"
              :key="cell.col_no"
              class="c-num"
              :class="{ editable: editMode, error: cell.error }"
              :title="cell.formula || ''"
              @click="editMode && onCellClick(row, cell)"
            >
              <span v-if="cell.error" class="tag-bad">!</span>
              {{ cell.value !== null && cell.value !== undefined ? fmt(cell.value) : '' }}
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 公式编辑 -->
      <el-dialog v-model="formulaVisible" title="编辑单元格公式" width="560px" class="no-print">
        <div class="muted" style="margin-bottom:6px">
          {{ editing?.row?.label }} · {{ editing?.label }}
        </div>
        <el-input v-model="editingFormula" placeholder='例：科目("1001","期末",{{本期}})' />
        <div class="fn-list">
          <div v-for="f in FN_HELP" :key="f.sig" class="fn">
            <code>{{ f.sig }}</code><span class="muted">{{ f.desc }}</span>
          </div>
        </div>
        <template #footer>
          <el-button @click="formulaVisible = false">取消</el-button>
          <el-button type="primary" @click="applyFormula">应用</el-button>
        </template>
      </el-dialog>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as api from '@/api/report'
import { downloadFile, openPrint } from '@/api/print'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const route = useRoute()
const router = useRouter()

const allTemplates = ref([])
const templateId = ref(Number(route.params.templateId || 0))
const template = ref(null)
const data = ref({ rows: [], cols: [], errors: [] })
const loading = ref(false)
const editMode = ref(false)
const formulaVisible = ref(false)
const editing = ref(null)
const editingFormula = ref('')
const dirty = reactive({})

// STATIC 列（科目行标题）在左边单独一列渲染，表格里不再重复一遍，
// 否则表头会比表体多一列，数字全错位。
const valueCols = computed(() => data.value.cols.filter((c) => c.kind !== 'STATIC'))

function rowCells(row) {
  const ids = new Set(valueCols.value.map((c) => c.col_no))
  return row.cells.filter((c) => ids.has(c.col_no))
}

const FN_HELP = [
  { sig: '科目("1001","期末",{{本期}})', desc: '取科目余额 / 发生额；口径：期初、期末余额、借方累计、贷方累计、本期借方、本期贷方、本期金额、本年累计' },
  { sig: '合计(B2:B8)', desc: '单元格区域求和' },
  { sig: '上期(B10)', desc: '同一行的上一列' },
  { sig: 'IF(B10>0,B10,0)', desc: '条件判断' },
  { sig: '四舍五入(B10,2)', desc: '四舍五入到指定位数' },
  { sig: 'B9+B16', desc: '单元格加减；支持 + - * / 与比较运算' },
  { sig: '{{本期}} {{上期}} {{年初}}', desc: '期间占位符' }
]

function onCellClick(row, cell) {
  editing.value = { row, colNo: cell.col_no, label: cell.label }
  editingFormula.value = dirty[`${row.row_no}:${cell.col_no}`] ?? cell.formula ?? ''
  formulaVisible.value = true
}

function applyFormula() {
  dirty[`${editing.value.row.row_no}:${editing.value.colNo}`] = editingFormula.value
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

async function load(keepDirty = false) {
  if (!templateId.value || !store.bookId || !store.periodId) return
  loading.value = true
  try {
    const tpl = allTemplates.value.find((t) => t.id === templateId.value)
    template.value = tpl
    const res = await api.render(templateId.value, {
      book_id: store.bookId, period_id: store.periodId
    })
    data.value = res
    if (editMode.value && keepDirty) {
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

.fn-list { margin-top: 14px; max-height: 240px; overflow: auto; font-size: 12px; }
.fn { display: flex; gap: 10px; padding: 3px 0; align-items: baseline; }
.fn code { background: #f3f4f6; padding: 1px 5px; border-radius: 3px; white-space: nowrap; }
</style>
