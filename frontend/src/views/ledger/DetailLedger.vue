<template>
  <div class="page">
    <div class="ledger-toolbar no-print">
      <div class="ledger-toolbar__row">
        <div class="ledger-toolbar__group ledger-toolbar__group--subject">
          <span class="ledger-toolbar__label">科目</span>
          <SubjectPicker v-model="subjectId" class="ledger-subject-picker"
                         :options="subjects" :allow-add="false" :select-parents="true"
                         placeholder="选择科目" @change="onSubjectChange" />
        </div>
        <div v-if="isComprehensive" class="ledger-toolbar__group ledger-toolbar__group--range">
          <span class="ledger-toolbar__label">子科目范围</span>
          <el-select v-model="fromSubjectId" class="ledger-range-select" @change="onFromSubjectChange">
            <el-option v-for="s in rangeSubjects" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
          <span class="ledger-toolbar__separator">至</span>
          <el-select v-model="toSubjectId" class="ledger-range-select" @change="onToSubjectChange">
            <el-option v-for="s in rangeSubjects" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </div>
      </div>
      <div class="ledger-toolbar__row ledger-toolbar__row--period">
        <div class="ledger-toolbar__group">
          <span class="ledger-toolbar__label">会计期间</span>
          <el-select v-model="fromId" class="ledger-period-select" @change="load">
            <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id" />
          </el-select>
          <span class="ledger-toolbar__separator">至</span>
          <el-select v-model="toId" class="ledger-period-select" @change="load">
            <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id" />
          </el-select>
        </div>
        <el-checkbox v-model="includeUnposted" label="包含未记账凭证" @change="load" />
        <div class="ledger-toolbar__actions">
          <el-button @click="previewHtml">模板预览/打印</el-button>
          <el-button @click="window.print()">打印本页</el-button>
          <el-button type="primary" @click="exportXlsx">导出 Excel</el-button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="report-head">
        <h2 class="report-title">明 细 账</h2>
        <div class="report-sub">
          {{ store.currentBook?.name }} · {{ data.subject?.code }} {{ data.subject?.name }}
          <template v-if="data.subject_range">
            · {{ data.subject_range.from.code }} 至 {{ data.subject_range.to.code }}
          </template>
          <template v-if="periodLabel"> · {{ periodLabel }}</template>
        </div>
      </div>
      <!-- 明细账是唯一逐笔列凭证的账簿，所以只有它的凭证行能点。 -->
      <el-table :data="data.rows" border size="small" v-loading="loading"
                :row-class-name="rowClass" @row-click="openVoucher">
        <el-table-column :label="`${store.currentYear}年`" align="center">
          <el-table-column label="月" width="48" align="center">
            <template #default="{ row }">{{ monthOf(row) }}</template>
          </el-table-column>
          <el-table-column label="日" width="48" align="center">
            <template #default="{ row }">{{ dayOf(row) }}</template>
          </el-table-column>
        </el-table-column>
        <el-table-column label="凭证号" width="95">
          <template #default="{ row }">
            <button v-if="row.voucher_id" type="button" class="vno-link"
                    :title="`打开凭证 ${row.voucher_no}`"
                    @click.stop="openVoucher(row)">{{ row.voucher_no }}</button>
          </template>
        </el-table-column>
        <template v-if="data.comprehensive">
          <el-table-column prop="subject_code" label="科目编码" width="105" />
          <el-table-column label="科目名称" width="180">
            <template #default="{ row }">{{ indentedSubjectName(row) }}</template>
          </el-table-column>
        </template>
        <el-table-column label="摘要" min-width="240"><template #default="{ row }">{{ summaryOf(row) }}</template></el-table-column>
        <el-table-column label="借方" width="120" align="right"><template #default="{ row }">{{ amount(row.debit) }}</template></el-table-column>
        <el-table-column label="贷方" width="120" align="right"><template #default="{ row }">{{ amount(row.credit) }}</template></el-table-column>
        <el-table-column label="方向" width="60" align="center"><template #default="{ row }">{{ row.direction }}</template></el-table-column>
        <el-table-column label="余额" width="130" align="right"><template #default="{ row }"><b>{{ amount(row.balance) }}</b></template></el-table-column>
        <template #empty><span class="muted">请先选择科目</span></template>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import * as api from '@/api/ledger'
import * as subjectApi from '@/api/subject'
import { downloadFile, openPrint } from '@/api/print'
import SubjectPicker from '@/components/SubjectPicker.vue'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const router = useRouter()

/** 只有凭证行能点；期初余额、月合计和累计行都是计算行。 */
function clickable(row) { return !!row.voucher_id }
function rowClass({ row }) {
  if (clickable(row)) return 'row-voucher'
  if (row.kind === 'OPENING') return 'row-opening'
  if (row.kind === 'MONTH_TOTAL') return 'row-month-total'
  if (row.kind === 'YTD_TOTAL') return 'row-ytd-total'
  return ''
}
function summaryOf(row) {
  return row.kind === 'YTD_TOTAL' ? '累\u3000\u3000计' : row.summary || ''
}
function monthOf(row) {
  const date = row.voucher_date || row.period || ''
  return String(date).slice(5, 7)
}
function dayOf(row) { return row.voucher_date ? String(row.voucher_date).slice(8, 10) : '' }
function amount(value) { return value == null ? '' : fmt(value) }
function openVoucher(row) {
  if (!clickable(row)) return
  router.push({ name: 'voucher-edit', params: { id: row.voucher_id } })
}
const subjects = ref([])
const subjectId = ref(null)
const fromSubjectId = ref(null)
const toSubjectId = ref(null)
const fromId = ref(store.periodId)
const toId = ref(store.periodId)
const data = ref({ rows: [] })
const loading = ref(false)
const selectedSubject = computed(() => subjects.value.find((s) => s.id === subjectId.value))
const rangeSubjects = computed(() => selectedSubject.value?.children || [])
const isComprehensive = computed(() => rangeSubjects.value.length > 0)
const periodLabel = computed(() => {
  const from = data.value.from_period?.slice(5, 7)
  const to = data.value.to_period?.slice(5, 7)
  if (!from || !to) return ''
  return from === to ? `${from}月` : `${from}月 至 ${to}月`
})
function indentedSubjectName(row) {
  const depth = Math.max(0, (row.subject_level || 1) - (data.value.subject?.level || 1))
  return `${'　'.repeat(depth)}${row.subject_name || ''}`
}
/** 未记账凭证也参与计算 —— 默认开。
 *
 *  月底集中记账之前，账上几乎全是未记账凭证；默认排除的话，
 *  明细账翻过去是「这个月一笔都没有」，看着像账丢了。
 *  真要核已入账的数据时再把这个勾去掉。 */
const includeUnposted = ref(true)

/** 起止只列**顶栏那个年份**的期间。
 *
 *  列全部年份时，顶栏停在 2027、这个下拉里却能翻出 2026-06 ——
 *  两处对不上，而且翻到别的年份算出来的数跟顶栏说的不是一本账。
 */
const yearPeriods = computed(() => store.periodsOfYear(store.currentYear))

/** 起止必须落在顶栏那个年份里；落空了就回到顶栏当前那一期。
 *
 *  顶栏换年份时原来那个期间 id 不在新列表里，el-select 会显示成空白或裸 id，
 *  传出去就是一个不属于当前年份的下界/上界 —— 页面看着正常，账算的是另一年。 */
function anchorPeriods() {
  const pool = yearPeriods.value
  if (!pool.length) { fromId.value = null; toId.value = null; return }
  const cur = pool.some((p) => p.id === store.periodId) ? store.periodId : pool[0].id
  if (!pool.some((p) => p.id === fromId.value)) fromId.value = cur
  if (!pool.some((p) => p.id === toId.value)) toId.value = cur
}

async function load() {
  if (!store.bookId || !subjectId.value) return
  // 期间还没灌完就查，后端会拿**它自己默认的那一期**当上下界 ——
  // 请求里压根没有 from/to，回来的数是另一段区间，页面上看着还像那么回事。
  // 与其先显示一个错的再纠正，不如等 store.periods 到位（下面的 watch 会再触发一次）。
  if (!yearPeriods.value.length) return
  loading.value = true
  try {
    data.value = await api.detail(store.bookId, {
      subject_id: subjectId.value, from_period_id: fromId.value, period_id: toId.value,
      from_subject_id: isComprehensive.value ? fromSubjectId.value : undefined,
      to_subject_id: isComprehensive.value ? toSubjectId.value : undefined,
      include_unposted: includeUnposted.value
    })
  } finally {
    loading.value = false
  }
}

function resetSubjectRange() {
  fromSubjectId.value = rangeSubjects.value[0]?.id ?? null
  toSubjectId.value = rangeSubjects.value.at(-1)?.id ?? null
}

function onSubjectChange() {
  resetSubjectRange()
  load()
}

function onFromSubjectChange() {
  const start = rangeSubjects.value.findIndex((s) => s.id === fromSubjectId.value)
  const end = rangeSubjects.value.findIndex((s) => s.id === toSubjectId.value)
  if (start > end) toSubjectId.value = fromSubjectId.value
  load()
}

function onToSubjectChange() {
  const start = rangeSubjects.value.findIndex((s) => s.id === fromSubjectId.value)
  const end = rangeSubjects.value.findIndex((s) => s.id === toSubjectId.value)
  if (end < start) fromSubjectId.value = toSubjectId.value
  load()
}

async function loadSubjects() {
  const tree = await subjectApi.subjectTree(store.bookId, { include_inactive: true })
  const flatten = (nodes, parentId = null) => nodes.flatMap((node) => {
    const item = { ...node, parent_id: parentId }
    return [item, ...flatten(node.children || [], node.id)]
  })
  subjects.value = flatten(tree)
  const preferred = subjects.value.find((s) => s.is_leaf && s.active)
  subjectId.value = preferred?.id ?? subjects.value[0]?.id ?? null
  resetSubjectRange()
}

onMounted(async () => {
  toId.value = store.periodId
  anchorPeriods()
  await loadSubjects()
  load()
})
watch(() => store.bookId, async () => {
  anchorPeriods()
  await loadSubjects()
  load()
})
// 年份一变，起止跟着重新落在新年份里。
// 也顺带接住「页面比 store.periods 先加载完」那一路：periods 到位后 currentYear
// 才从 0 变成真值，这时才谈得上「只列这一年」。
// 盯着**年份和期间数**，不是只盯年份：
// store.loadBooks() 先灌 books、再灌 periods，currentYear 会先从 books 的
// latest_year 变成真值，那一刻 periods 还是空的。只盯年份的话 watcher 只响一次，
// 打在空列表上，之后 periods 到位也不再响 —— 起止就一直停在 null 上。
watch(() => [store.currentYear, store.periods.length], () => { anchorPeriods(); load() })

function previewHtml() {
  openPrint(`/books/${store.bookId}/print/detail-ledger.html`, {
    subject_id: subjectId.value, period_id: toId.value, from_period_id: fromId.value || undefined,
    from_subject_id: isComprehensive.value ? fromSubjectId.value : undefined,
    to_subject_id: isComprehensive.value ? toSubjectId.value : undefined,
    include_unposted: includeUnposted.value,
  })
}

function exportXlsx() {
  downloadFile(`/books/${store.bookId}/print/detail-ledger.xlsx`, {
    subject_id: subjectId.value,
    period_id: toId.value,
    from_period_id: fromId.value || undefined,
    from_subject_id: isComprehensive.value ? fromSubjectId.value : undefined,
    to_subject_id: isComprehensive.value ? toSubjectId.value : undefined,
    include_unposted: includeUnposted.value,
  }, '明细账.xlsx')
}
</script>

<style scoped>
.ledger-toolbar {
  display: flex;
  flex-direction: column;
  margin-bottom: 12px;
  padding: 0 16px;
  background: #fff;
  border: 1px solid var(--cwq-border);
  border-radius: 6px;
}
.ledger-toolbar__row {
  display: flex;
  min-height: 56px;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px 18px;
}
.ledger-toolbar__row + .ledger-toolbar__row {
  border-top: 1px solid var(--cwq-border);
}
.ledger-toolbar__group {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.ledger-toolbar__group--subject { flex: 0 1 380px; }
.ledger-toolbar__group--range { flex: 1 1 500px; }
.ledger-toolbar__label {
  flex: 0 0 auto;
  color: var(--cwq-muted);
  font-size: 12px;
  white-space: nowrap;
}
.ledger-toolbar__separator {
  color: var(--cwq-muted);
  font-size: 12px;
}
.ledger-toolbar__actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
}
.ledger-toolbar :deep(.ledger-subject-picker) {
  box-sizing: border-box;
  flex: 1 1 280px;
  width: 280px;
  height: 32px;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  background: var(--el-bg-color);
}
.ledger-toolbar :deep(.ledger-subject-picker:focus-within) {
  border-color: var(--el-color-primary);
}
.ledger-toolbar :deep(.ledger-range-select) { width: 185px; }
.ledger-toolbar :deep(.ledger-period-select) { width: 132px; }
.ledger-toolbar :deep(.el-checkbox) { margin-right: 0; }

@media (max-width: 760px) {
  .ledger-toolbar { padding: 0 12px; }
  .ledger-toolbar__group--subject,
  .ledger-toolbar__group--range { flex: 1 1 100%; }
  .ledger-toolbar :deep(.ledger-subject-picker) { flex-basis: 200px; }
  .ledger-toolbar :deep(.ledger-range-select) { flex: 1 1 150px; width: auto; }
  .ledger-toolbar__row--period { padding: 10px 0; }
  .ledger-toolbar__actions { margin-left: auto; }
}

.report-head { text-align: center; margin-bottom: 12px; }
.report-title { margin: 0 0 4px; font-size: 19px; letter-spacing: 6px; }
.report-sub { font-size: 13px; color: var(--cwq-muted); }

/* 凭证号做成按钮但长得像字 —— 用户要的是「一列凭证号」，
   不是「一列按钮」。 */
.vno-link {
  padding: 0; border: 0; background: none; cursor: pointer;
  font: inherit; color: var(--cwq-primary); text-align: left;
}
.vno-link:hover { text-decoration: underline; }
.vno-link:focus-visible { outline: 2px solid var(--cwq-primary); outline-offset: 1px; border-radius: 2px; }

/* 汇总行靠字重区分凭证行，不引入新的颜色编码。 */
:deep(.el-table__row.row-opening > td.el-table__cell),
:deep(.el-table__row.row-month-total > td.el-table__cell),
:deep(.el-table__row.row-ytd-total > td.el-table__cell) {
  font-size: 13px;
  font-weight: 600;
}
:deep(.el-table__row.row-month-total > td.el-table__cell) { background: #f7f9fb; }

/* 整行可点：悬停给一点反馈，不然只有凭证号变蓝，看不出整行都认。 */
:deep(.el-table__row.row-voucher) { cursor: pointer; }
:deep(.el-table__row.row-voucher:hover > td.el-table__cell) { background: #f2f7ff !important; }

/* 印到纸上就没有「能点」这回事了，蓝字只剩浪费墨水和看不清。
   明细账是要归档的账簿，凭证号该是黑字。 */
@media print {
  .vno-link { color: inherit !important; text-decoration: none !important; }
}
</style>
