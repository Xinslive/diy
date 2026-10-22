<template>
  <div class="page">
    <div class="ledger-toolbar no-print">
      <div class="ledger-toolbar__row">
        <div class="ledger-toolbar__group ledger-toolbar__group--subject">
          <span class="ledger-toolbar__label">总账科目</span>
          <!-- 一级科目清单来自后端（它认的就是同一份），不是前端另查一遍科目表 ——
               两处各算一遍「一级」迟早会对不上，对不上时用户只会看到空白。 -->
          <el-select v-model="subjectCode" class="ledger-subject-select"
                     placeholder="选择一级科目" @change="onSubjectChange">
            <el-option v-for="s in subjects" :key="s.id"
                       :label="`${s.code} ${s.name}`" :value="s.code" />
          </el-select>
        </div>
        <div class="ledger-toolbar__group ledger-toolbar__group--scope">
          <span class="ledger-toolbar__label">打印范围</span>
          <el-select v-model="printScope" class="ledger-scope-select">
            <el-option label="本册（全部一级科目）" value="book" />
            <el-option label="仅当前科目" value="current" />
          </el-select>
        </div>
      </div>
      <div class="ledger-toolbar__row ledger-toolbar__row--period">
        <div class="ledger-toolbar__group">
          <span class="ledger-toolbar__label">会计期间</span>
          <el-select v-model="fromId" class="ledger-period-select" @change="onFilterChange">
            <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id" />
          </el-select>
          <span class="ledger-toolbar__separator">至</span>
          <el-select v-model="toId" class="ledger-period-select" @change="onFilterChange">
            <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id" />
          </el-select>
        </div>
        <el-checkbox v-model="includeUnposted" label="包含未记账凭证" @change="onFilterChange" />
        <div class="ledger-toolbar__actions">
          <el-button @click="previewHtml">打印预览</el-button>
          <el-button type="primary" @click="exportXlsx">导出 Excel</el-button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="report-head">
        <!-- 用友的纸上标题就是「{科目名}总账」，屏幕与纸上同一套说法。 -->
        <h2 class="report-title" :class="{ 'report-title--plain': !data.subject }">{{ titleText }}</h2>
        <div class="report-sub">
          {{ store.currentBook?.name }}
          <template v-if="data.subject"> · 科目: {{ data.subject.name }}({{ data.subject.code }})</template>
          <template v-if="periodRangeText"> · {{ periodRangeText }}</template>
        </div>
      </div>
      <!-- 八列，与用友总账逐格对齐：月 / 日 / 凭证号数 / 摘要 / 借方 / 贷方 / 方向 / 余额。
           「凭证号数」「日」两列全程空白 —— 总账只印汇总行，这是用友的固定写法。 -->
      <el-table :data="viewRows" border size="small" v-loading="loading"
                :row-class-name="rowClass">
        <el-table-column :label="`${yearLabel}年`" align="center">
          <el-table-column label="月" width="48" align="center" prop="month" />
          <el-table-column label="日" width="48" align="center" prop="day" />
        </el-table-column>
        <el-table-column label="凭证号数" width="92" align="center" prop="voucher_no" />
        <el-table-column label="摘要" min-width="240" prop="summary" />
        <el-table-column label="借方" width="120" align="right" prop="debitText" />
        <el-table-column label="贷方" width="120" align="right" prop="creditText" />
        <el-table-column label="方向" width="60" align="center" prop="direction" />
        <el-table-column label="余额" width="130" align="right">
          <template #default="{ row }"><b>{{ row.balanceText }}</b></template>
        </el-table-column>
        <template #empty><span class="muted">请先选择一级科目</span></template>
      </el-table>

      <div class="ledger-pager no-print">
        <el-pagination
          layout="total, sizes, prev, pager, next"
          :total="totalRows"
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[20, 50, 100, 200]"
        />
      </div>
    </div>

    <PrintDialog v-model="printVisible" :book-id="store.bookId" :path="printPath"
                 scope="LEDGER" :params="printParams" title="打印总账" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import * as api from '@/api/ledger'
import { downloadFile } from '@/api/print'
import PrintDialog from '@/components/PrintDialog.vue'
import { useLatestLoad } from '@/composables/useLatestLoad'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const data = ref({ rows: [], subjects: [] })

/**
 * 选中的**科目编码**，不是科目 id。
 *
 *  科目表每个年度各有一份（后端迁移 0032）：2026 年的 1001 与 2027 年的 1001
 *  是两行不同的记录，id 也不同。跨年还认同一个科目，只能认编码。
 */
const subjectCode = ref(null)

/** 一级科目清单 —— 来自后端，与它认的是同一份。 */
const subjects = computed(() => data.value.subjects || [])
/** 这份清单属于哪一年 —— 清单里的 id 只在它自己那一年作数。 */
const subjectsYear = computed(() => String(data.value.to_period || '').slice(0, 4))
/** 清单是不是**当前要查的那一年**的。不是的话，它的 id 送过去后端认不出来（404）。 */
const subjectsFresh = computed(() => subjectsYear.value === String(store.currentYear))

/** 编码 → 当年那一份的 id。清单过期时给 null（宁可不送，也别送个作废的 id）。 */
function idOfCode(code) {
  if (!code || !subjectsFresh.value) return null
  return subjects.value.find((s) => s.code === code)?.id ?? null
}

const titleText = computed(() =>
  data.value.subject ? `${data.value.subject.name}总账` : '总 账')
const yearLabel = computed(() =>
  String(data.value.to_period || store.currentYear || '').slice(0, 4))

/** 表头那一句期间。起止是同一期时按「期间 X」写 —— 写成「X 至 X」就是把
 *  同一个期间在页内说了两遍，而顶栏已经选过它了。 */
const periodRangeText = computed(() => {
  const from = data.value.from_period
  const to = data.value.to_period
  if (!from && !to) return ''
  if (!to || from === to) return `期间 ${from || to}`
  return `${from} 至 ${to}`
})

const fromId = ref(store.periodId)
const toId = ref(store.periodId)
/** 未记账凭证也参与计算 —— 默认开，理由同明细账。 */
const includeUnposted = ref(true)

/**
 * 打印范围：**本册**是账套的全部一级科目、连续打成一册（用友正式账簿的做法）；
 * 「仅当前科目」只打屏幕上正在看的这一个。
 */
const printScope = ref('book')

/**
 * 分页是**纯前端切片** —— 后端接口照旧一次把整段返回，契约不动。
 * 一个科目整年 26 行，切片只是兜住区间拉长时的渲染量。
 */
const page = ref(1)
const pageSize = ref(50)
const totalRows = computed(() => data.value.rows.length)
/**
 * 每格都调一次的函数，砍到一处算好（与明细账同一做法）。
 * 摘要直接用后端给的字：那是**打印件上的那一份**标签（含「累　　计」的
 * 全角空格），屏幕与纸上必须逐字一致，前端另写一套标签迟早对不上。
 */
const viewRows = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return data.value.rows.slice(start, start + pageSize.value).map((row) => ({
    ...row,
    debitText: row.debit == null ? '' : fmt(row.debit),
    creditText: row.credit == null ? '' : fmt(row.credit),
    balanceText: row.balance == null ? '' : fmt(row.balance),
  }))
})
function rowClass({ row }) {
  if (row.kind === 'OPENING') return 'row-opening'
  if (row.kind === 'MONTH_TOTAL') return 'row-month-total'
  if (row.kind === 'YTD_TOTAL') return 'row-ytd-total'
  return ''
}

/** 只认最新一趟：期间/科目下拉连点时，旧响应不许覆盖新数据（见 composables/useLatestLoad）。 */
const { loading, run, isStale } = useLatestLoad('ledger/general')

/** 起止只列顶栏那个年份的期间。列全部年份时，顶栏停在 2027、
 *  下拉里却能翻出 2026-06，翻过去算的是另一本账。 */
const yearPeriods = computed(() => store.periodsOfYear(store.currentYear))

/**
 * 起止必须落在顶栏那个年份里，且**默认是整年**。
 *
 *  总账是**一本年账**：从 1 月的「上年结转」一路排到 12 月的「本年累计」。
 * 默认只取当前一期的话，表里只有四行，「上年结转 / 累计 / 本年累计 /
 *  结转下年」这些行型一个都看不到 —— 那不是用友的总账。
 *
 *  换年份后重置成整年：上一年的区间在新年份里不存在，而年账本来也是按年算的。
 *  同一年内只补位（id 落在池子外才补），用户自己拉窄的区间保留。
 */
let anchoredYear = null
function anchorPeriods() {
  const pool = yearPeriods.value
  if (!pool.length) { fromId.value = null; toId.value = null; return }
  if (anchoredYear !== String(store.currentYear)) {
    anchoredYear = String(store.currentYear)
    fromId.value = pool[0].id
    toId.value = pool[pool.length - 1].id
    return
  }
  if (!pool.some((p) => p.id === fromId.value)) fromId.value = pool[0].id
  if (!pool.some((p) => p.id === toId.value)) toId.value = pool[pool.length - 1].id
}

async function load() {
  if (!store.bookId) return
  // 期间还没灌完就查，后端会拿**它自己默认的那一期**当上下界 ——
  // 请求里压根没有 from/to，回来的数是另一段区间，页面上看着还像那么回事。
  // 与其先显示一个错的再纠正，不如等 store.periods 到位（下面的 watch 会再触发一次）。
  if (!yearPeriods.value.length) return
  const want = subjectCode.value
  const id = idOfCode(want)
  const d = await run(() => api.general(store.bookId, {
    subject_id: id ?? undefined,
    from_period_id: fromId.value, period_id: toId.value,
    include_unposted: includeUnposted.value,
  }))
  // 过期的那一趟：结果已经到了，但条件早就被用户改掉了 —— 写进去就是
  // 「表格里的数对不上顶栏的期间」。
  if (isStale(d)) return
  data.value = d
  // 送过去的 id 属于**上一年度**的科目表（id 每年都变），后端只能给别的科目。
  // 编码跨年度不变 —— 用新年度的 id 再查一趟，就回到用户原来选的那个科目。
  const back = (d.subjects || []).find((s) => s.code === want)
  if (want && back && back.id !== id) {
    subjectCode.value = want
    return load()
  }
  subjectCode.value = d.subject?.code ?? null
  // 换到行数更少的区间时，原来那页可能已经不存在了 —— 停在越界的页码上
  // 就是一张空表，看着像「这个期间一笔都没有」。
  const maxPage = Math.max(1, Math.ceil(d.rows.length / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
}

/** 条件变了就回第一页：停在原来那页会看着像「科目少了」。 */
function onFilterChange() { page.value = 1; load() }
function onSubjectChange() { page.value = 1; load() }

function previewHtml() {
  printVisible.value = true
}

const printVisible = ref(false)
const printPath = computed(() => `/books/${store.bookId}/print/general-ledger.html`)
/**
 * 只有范围，模板 id 由弹窗自己选 —— 它得让用户在打之前改主意。
 *
 * 「仅当前科目」才带 subject_id：**本册**是账套全部一级科目连续成册，
 * 不带 subject_id 后端就打整册；带了反而只出一个科目。
 * 这个 id 取自当前响应，与打印用的 period_id 是同一份年度的，不会对不上。
 */
const printParams = computed(() => ({
  period_id: toId.value, from_period_id: fromId.value || undefined,
  include_unposted: includeUnposted.value,
  ...(printScope.value === 'current' && data.value.subject?.id
    ? { subject_id: data.value.subject.id } : {}),
}))

function exportXlsx() {
  // 导出与打印共用同一份参数：屏幕上是「起始月 ~ 结束月」这一段，
  // 导出也得是这一段 —— 审计第 7.3 节把「总账导出：区间与页面显示一致」
  // 列为验收项。
  downloadFile(`/books/${store.bookId}/print/general-ledger.xlsx`,
    { ...printParams.value }, '总账.xlsx')
}

onMounted(() => { anchorPeriods(); load() })
// **单一数据源**：账套 + 顶栏年份 + 该年份的期间数，就是「现在该按哪一段查」。
//
//  以前是两个 watch（bookId 一个、年份/期间数一个），而 store.setBook() 是
//  「先改 bookId，再 await loadPeriods() 灌 periods」—— 两条 watch 依次触发，
//  同一份查询发两趟。合成一条之后：换账套时那一趟 load() 会因为
//  yearPeriods 还是空的直接返回（见 load() 开头的注释），真正发出的
//  只有 periods 到位后那一次。
//
//  为什么非得盯着**年份和期间数**、不是只盯年份：
//  store.loadBooks() 先灌 books、再灌 periods，currentYear 会先从 books 的
//  latest_year 变成真值，那一刻 periods 还是空的。只盯年份的话 watcher 只响一次，
//  打在空列表上，之后 periods 到位也不再响 —— 起止就一直停在 null 上。
watch(() => [store.bookId, store.currentYear, store.periods.length], () => {
  anchorPeriods()
  load()
})
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
.ledger-toolbar__group--subject { flex: 0 1 420px; }
.ledger-toolbar__group--scope { flex: 0 1 260px; }
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
.ledger-toolbar :deep(.ledger-subject-select) { width: 280px; }
.ledger-toolbar :deep(.ledger-scope-select) { width: 190px; }
.ledger-toolbar :deep(.ledger-period-select) { width: 132px; }
.ledger-toolbar :deep(.el-checkbox) { margin-right: 0; }

@media (max-width: 760px) {
  .ledger-toolbar { padding: 0 12px; }
  .ledger-toolbar__group--subject,
  .ledger-toolbar__group--scope { flex: 1 1 100%; }
  .ledger-toolbar :deep(.ledger-subject-select) { flex: 1 1 200px; width: auto; }
  .ledger-toolbar__row--period { padding: 10px 0; }
  .ledger-toolbar__actions { margin-left: auto; }
}

.report-head { text-align: center; margin-bottom: 12px; }
.report-title { margin: 0 0 4px; font-size: 19px; }
/* 没选科目时是页面自己的名字「总 账」，那两个字要撑开；选了就变成
   「库存现金总账」这样的账簿名，逐字排开反而松散。 */
.report-title--plain { letter-spacing: 6px; }
.report-sub { font-size: 13px; color: var(--cwq-muted); }
/* 分页靠右，与 VoucherList 一致；no-print 让它不进纸面。 */
.ledger-pager { display: flex; justify-content: flex-end; margin-top: 12px; }

/* 汇总行靠字重区分，不引入新的颜色编码（与明细账同一口径）。 */
:deep(.el-table__row.row-opening > td.el-table__cell),
:deep(.el-table__row.row-month-total > td.el-table__cell),
:deep(.el-table__row.row-ytd-total > td.el-table__cell) {
  font-size: 13px;
  font-weight: 600;
}
:deep(.el-table__row.row-month-total > td.el-table__cell) { background: #f7f9fb; }
</style>
