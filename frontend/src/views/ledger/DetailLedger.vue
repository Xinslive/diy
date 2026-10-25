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
          <el-select v-model="fromId" class="ledger-period-select" @change="onPeriodChange('from')">
            <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id" />
          </el-select>
          <span class="ledger-toolbar__separator">至</span>
          <el-select v-model="toId" class="ledger-period-select" @change="onPeriodChange('to')">
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

    <!-- 加载失败**不能**与「没有数据」同形：断网/超时时表格会空着，
         一条提示都没有的话，用户以为这个科目一整年没发生额（审计 §3.25）。 -->
    <el-alert v-if="loadError" type="error" :closable="false" show-icon class="mb"
              :title="`加载失败：${loadError}。请检查网络后重试。`" />

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
      <el-table :data="viewRows" border size="small" v-loading="loading"
                :row-class-name="rowClass" @row-click="openVoucher">
        <el-table-column :label="`${store.currentYear}年`" align="center">
          <el-table-column label="月" width="48" align="center" prop="month" />
          <el-table-column label="日" width="48" align="center" prop="day" />
        </el-table-column>
        <!-- 凭证号是「记 + 3 位数字」（记001），内容本身很窄。
             原来给到 95，标题「凭证号」三个字撑满、左右各空一截，
             而同级的月/日两列各只有 48，横向被这一列拉得松垮。
             收到 72：标题与内容都不折行，还比原来省出 23px 给摘要。 -->
        <el-table-column label="凭证号" width="72">
          <template #default="{ row }">
            <button v-if="row.voucher_id" type="button" class="vno-link"
                    :title="`打开凭证 ${row.voucher_no}`"
                    @click.stop="openVoucher(row)">{{ row.voucher_no }}</button>
          </template>
        </el-table-column>
        <template v-if="data.comprehensive">
          <el-table-column prop="subject_code" label="科目编码" width="105" />
          <!-- 科目名有长有短（「银行存款—中国工商银行基本户」这类），
               固定宽度下只能截断。截断成省略号、悬停出全称，
               与全站其它表格一致（见 VoucherList 的摘要列）。 -->
          <el-table-column label="科目名称" width="180" prop="subjectText" show-overflow-tooltip />
        </template>
        <el-table-column label="摘要" min-width="240" prop="summary" />
        <el-table-column label="借方" width="120" align="right" prop="debitText" />
        <el-table-column label="贷方" width="120" align="right" prop="creditText" />
        <el-table-column label="方向" width="60" align="center" prop="direction" />
        <el-table-column label="余额" width="130" align="right">
          <template #default="{ row }"><b>{{ row.balanceText }}</b></template>
        </el-table-column>
        <template #empty><span class="muted">请先选择科目</span></template>
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
                 scope="DETAIL_LEDGER" :params="printParams" title="打印明细账"
                 :print-ranges="LEDGER_PRINT_RANGES"
                 v-model:range="printRange" v-model:skip-empty="printSkipEmpty" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import * as api from '@/api/ledger'
import * as subjectApi from '@/api/subject'
import { downloadFile } from '@/api/print'
import PrintDialog from '@/components/PrintDialog.vue'
import SubjectPicker from '@/components/SubjectPicker.vue'
import { useLatestLoad } from '@/composables/useLatestLoad'
import { useUrlConditions } from '@/composables/useUrlConditions'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'
import {
  LEDGER_PRINT_RANGES, PRINT_RANGE_CURRENT, SKIP_EMPTY_DEFAULT, ledgerPrintParams,
} from '@/utils/ledgerPrint'

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
/**
 * 每格都调一次的函数，砍到一处算好。
 *
 *  以前月/日/摘要/借贷/余额/科目名全是模板里的函数调用 —— 一个来回刷新、
 * 一个勾选切换，都要按「行数 × 列数」重算一遍，其中科目名还要现拼全角空格。
 * 现在一次性算成视图行，computed 只在数据或页码真的变了时才重算。
 * 同时按页切片：一页 50 行，别把整段明细一次性铺进 DOM。
 */
const page = ref(1)
const pageSize = ref(50)
const totalRows = computed(() => data.value.rows.length)
const viewRows = computed(() => {
  const base = data.value.subject?.level || 1
  const start = (page.value - 1) * pageSize.value
  return data.value.rows.slice(start, start + pageSize.value).map((row) => ({
    ...row,
    month: String(row.voucher_date || row.period || '').slice(5, 7),
    day: row.voucher_date ? String(row.voucher_date).slice(8, 10) : '',
    // 累计行的标签由**后端**给（整年 12 月写「本年累计」，其余写「累　　计」，
    // 与总账同一套规则）。以前这里前端硬写「累　　计」，于是屏幕与纸上
    // 在整年那一行对不上 —— 纸上写本年累计、屏幕写累计。
    summary: row.summary || '',
    debitText: row.debit == null ? '' : fmt(row.debit),
    creditText: row.credit == null ? '' : fmt(row.credit),
    balanceText: row.balance == null ? '' : fmt(row.balance),
    subjectText: `${'　'.repeat(Math.max(0, (row.subject_level || 1) - base))}${row.subject_name || ''}`,
  }))
})
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
const selectedSubject = computed(() => subjects.value.find((s) => s.id === subjectId.value))
const rangeSubjects = computed(() => selectedSubject.value?.children || [])
const isComprehensive = computed(() => rangeSubjects.value.length > 0)
const periodLabel = computed(() => {
  const from = data.value.from_period?.slice(5, 7)
  const to = data.value.to_period?.slice(5, 7)
  if (!from || !to) return ''
  return from === to ? `${from}月` : `${from}月 至 ${to}月`
})
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

/** 科目编码 ↔ id。 */
function subjectIdOf(code) { return subjects.value.find((s) => s.code === code)?.id ?? null }
function subjectCodeOf(id) { return subjects.value.find((s) => s.id === id)?.code || '' }
/** 期间编码 ↔ id（编码是「2027-01」这种）。 */
function periodIdOf(code) { return yearPeriods.value.find((p) => p.code === code)?.id ?? null }
function periodCodeOf(id) { return yearPeriods.value.find((p) => p.id === id)?.code || '' }

/**
 * 条件 ↔ URL。
 *
 * 标签栏不做 keep-alive：切走再切回来就是组件重建，条件只活在组件里的话，
 * 回来必然是默认值 —— 那就是「查询参数被重置」这个毛病。所以条件住进 URL，
 * 标签栏记的完整地址带着它，回来照常重新取数（数据不旧），用的还是原来那组条件。
 *
 * **URL 里放编码，不放 id。** 科目表每个年度各有一份（后端迁移 0032），
 * 编码跨年度不变，id 每年都换 —— 放 id 的话，换个年度再点回标签就指空了。
 */
const { restore } = useUrlConditions({
  subject: { type: 'str', get: () => subjectCodeOf(subjectId.value), set: (c) => { const id = subjectIdOf(c); if (id) subjectId.value = id } },
  // 子科目范围只有选了父科目才有意义，末级科目上就不带它
  subFrom: { type: 'str', get: () => (isComprehensive.value ? subjectCodeOf(fromSubjectId.value) : ''), set: (c) => { const id = subjectIdOf(c); if (id) fromSubjectId.value = id } },
  subTo: { type: 'str', get: () => (isComprehensive.value ? subjectCodeOf(toSubjectId.value) : ''), set: (c) => { const id = subjectIdOf(c); if (id) toSubjectId.value = id } },
  from: { type: 'str', get: () => periodCodeOf(fromId.value), set: (c) => { const id = periodIdOf(c); if (id) fromId.value = id } },
  to: { type: 'str', get: () => periodCodeOf(toId.value), set: (c) => { const id = periodIdOf(c); if (id) toId.value = id } },
  unposted: { ref: includeUnposted, type: 'bool', def: true },
})

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

/** 只认最新一趟：期间/科目下拉连点时，旧响应不许覆盖新数据。 */
const { loading, error: loadError, run, isStale } = useLatestLoad('ledger/detail')

async function load() {
  if (!store.bookId || !subjectId.value) return
  // 期间还没灌完就查，后端会拿**它自己默认的那一期**当上下界 ——
  // 请求里压根没有 from/to，回来的数是另一段区间，页面上看着还像那么回事。
  // 与其先显示一个错的再纠正，不如等 store.periods 到位（下面的 watch 会再触发一次）。
  if (!yearPeriods.value.length) return
  let d
  try {
    d = await run(() => api.detail(store.bookId, {
      subject_id: subjectId.value, from_period_id: fromId.value, period_id: toId.value,
      from_subject_id: isComprehensive.value ? fromSubjectId.value : undefined,
      to_subject_id: isComprehensive.value ? toSubjectId.value : undefined,
      include_unposted: includeUnposted.value
    }))
  } catch (e) {
    // 加载失败**不是**「没有数据」：这两件事在页面上长得一模一样。
    // 拦截器已经弹过提示了，这里只要别让它变成一个没人接的 rejection ——
    // 页面上 data 保持原样，顶部由 loadError 挂一条「加载失败」。
    // 以前这里一声不吭：断网/超时后表格空着、零提示，
    // 用户以为这个科目一整年没发生额（审计 §3.25）。
    return
  }
  // 过期的那一趟：条件早就被改掉了，写进去就是「表里的数对不上顶栏的期间」。
  if (isStale(d)) return
  data.value = d
  // 换到行数更少的区间时，原来那页可能已经不存在了 —— 停在越界的页码上
  // 就是一张空表，看着像「这个期间一笔都没有」。
  const maxPage = Math.max(1, Math.ceil(d.rows.length / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
}

/** 条件变了就回第一页：停在原来那页会看着像「明细少了」。 */
function onFilterChange() { page.value = 1; load() }

/**
 * 起止倒置时把另一头一起带过去。
 *
 * 后端对倒置区间直接 400（「起始期间不能晚于结束期间」），屏幕上只剩一条红提示，
 * 表还是上一份数据 —— 用户根本不知道自己刚点的是什么。
 * 用户动的是哪一头，就让哪一头说了算，另一头跟上去。
 * （科目余额表那边是同一套做法，见 BalanceSheet.onFromChange。）
 */
function onPeriodChange(which) {
  const from = yearPeriods.value.find((p) => p.id === fromId.value)
  const to = yearPeriods.value.find((p) => p.id === toId.value)
  if (from && to && from.ord > to.ord) {
    if (which === 'from') toId.value = fromId.value
    else fromId.value = toId.value
  }
  onFilterChange()
}

function resetSubjectRange() {
  fromSubjectId.value = rangeSubjects.value[0]?.id ?? null
  toSubjectId.value = rangeSubjects.value.at(-1)?.id ?? null
}

/**
 * 子科目范围只**补位**，不覆盖已经定好的那一段。
 *
 *  用户自己拉过的那一段（从 URL 带回来的）得留着；
 *  不属于当前科目的（换了科目、换了年度）才补回整段。
 */
function alignSubjectRange() {
  const kids = rangeSubjects.value
  if (!kids.length) { fromSubjectId.value = null; toSubjectId.value = null; return }
  if (!kids.some((s) => s.id === fromSubjectId.value)) fromSubjectId.value = kids[0].id
  if (!kids.some((s) => s.id === toSubjectId.value)) toSubjectId.value = kids[kids.length - 1].id
}

function onSubjectChange() {
  resetSubjectRange()
  page.value = 1; load()
}

function onFromSubjectChange() {
  const start = rangeSubjects.value.findIndex((s) => s.id === fromSubjectId.value)
  const end = rangeSubjects.value.findIndex((s) => s.id === toSubjectId.value)
  if (start > end) toSubjectId.value = fromSubjectId.value
  page.value = 1; load()
}

function onToSubjectChange() {
  const start = rangeSubjects.value.findIndex((s) => s.id === fromSubjectId.value)
  const end = rangeSubjects.value.findIndex((s) => s.id === toSubjectId.value)
  if (end < start) fromSubjectId.value = toSubjectId.value
  page.value = 1; load()
}

async function loadSubjects() {
  // **年度必带**：科目表每个年度各有一份（后端迁移 0032）
  const tree = await subjectApi.subjectTree(
    store.bookId, { include_inactive: true, year: store.currentYear })
  const flatten = (nodes, parentId = null) => nodes.flatMap((node) => {
    const item = { ...node, parent_id: parentId }
    return [item, ...flatten(node.children || [], node.id)]
  })
  subjects.value = flatten(tree)
  /** 默认落在 **1001 库存现金**。
   *
   *  翻明细账十有八九是先看「钱搁在哪儿」，1001 是这张表的头一条。
   *  以前取的是「第一个启用末级」—— 那取决于科目表怎么排，翻开就看到一个
   *  莫名其妙的末级科目（截图里是 1007），还得先换一趟才看得到账。 */
  const preferred = subjects.value.find((s) => s.code === '1001' && s.active)
    ?? subjects.value.find((s) => s.is_leaf && s.active)
    ?? subjects.value[0]
  subjectId.value = preferred?.id ?? null
  resetSubjectRange()
}

/** 科目树是**按账套**的：当前这份 subjects 是哪个账套的。
 *  换账套必须重拉 —— 否则选的是上一本账的科目，明细账查的是外账套的数。 */
let subjectsBookId = 0

onMounted(async () => {
  toId.value = store.periodId
  anchorPeriods()
  subjectsBookId = store.bookId
  await loadSubjects()
  // 条件从 URL 捡回来：标签栏切走再切回来时，标签记的完整地址带着它。
  // 科目 id 要等科目树到位才认得出来，所以这一步排在 loadSubjects 之后。
  restore()
  // 期间必须落在顶栏那个年份里（URL 里那份可能是上一年留下的）
  anchorPeriods()
  // 子科目范围跟着复原后的科目走；用户自己拉过的那一段保留
  alignSubjectRange()
  load()
})
// **单一数据源**：账套 + 顶栏年份 + 该年份的期间数。
//
//  以前是两个 watch：bookId 一个（里面还 await loadSubjects()）、
//  年份/期间数一个。store.setBook() 是「先改 bookId、再 await loadPeriods()
//  灌 periods」，两条依次触发，同一份查询发两趟 —— 这页更重，因为它那条
//  bookId 的 watch 里还夹着一次科目树请求。
//
//  合成一条之后，「换账套要重拉科目树」这条语义一条没丢：靠 subjectsBookId
//  认出这是换账套了才重拉，期间/年份变化不重拉。
watch(() => [store.bookId, store.currentYear, store.periods.length], async () => {
  anchorPeriods()
  if (subjectsBookId !== store.bookId) {
    subjectsBookId = store.bookId
    await loadSubjects()
    // 换账套时期间多半还在路上（setBook 是先改 bookId 再灌 periods）。
    // 这时 load() 会因为 yearPeriods 还是空的自己返回，等下面那条 watch
    // 到位再发真正的那一趟 —— 别在这里把同一份查询发两遍。
    if (!yearPeriods.value.length) return
  }
  load()
})

function previewHtml() {
  printVisible.value = true
}

const printVisible = ref(false)
const printPath = computed(() => `/books/${store.bookId}/print/detail-ledger.html`)
/**
 * 打印范围：**本册**是全账套的一级科目、连续打成一册（用友明细账的做法，
 * 与总账同一册结构 —— 用户 2026-10-22 批注 3）；**仅当前科目**只打屏幕上
 * 正在看的那一个。
 *
 * **这个设置住在打印预览弹窗里**（用户 2026-10-22 批注 1、2）：工具栏上
 * 原来那个下拉已经删掉。状态仍然放在这一页 —— 页面上那个「导出 Excel」
 * 要的是同一份范围。
 *
 * **默认是「仅当前科目」**（用户 2026-10-22 批注 4）：明细账是**逐笔**的账，
 * 整册打出来是全账套几百个一级科目、动辄几百页纸；用户走到明细账这一页，
 * 心里要的通常就是屏幕上正在看的那一个科目。总账那边默认仍是「本册」。
 */
const printRange = ref(PRINT_RANGE_CURRENT)
/** 「不打印无发生额科目」，默认打开（用户 2026-10-22 批注 2）。 */
const printSkipEmpty = ref(SKIP_EMPTY_DEFAULT)
/**
 * 打印与导出共用的那份参数（拼装规则见 ``utils/ledgerPrint.js``）。
 *
 * 「仅当前科目」时带上屏幕上的那一个：父科目就带上起止子科目（综合明细账
 * 可以只框其中一段），末级科目就只带它自己。
 */
const printParams = computed(() => ledgerPrintParams({
  range: printRange.value,
  base: {
    period_id: toId.value, from_period_id: fromId.value || undefined,
    include_unposted: includeUnposted.value,
  },
  subject: {
    subject_id: subjectId.value,
    from_subject_id: isComprehensive.value ? fromSubjectId.value : undefined,
    to_subject_id: isComprehensive.value ? toSubjectId.value : undefined,
  },
  skipEmpty: printSkipEmpty.value,
}))

function exportXlsx() {
  // 导出与打印共用同一份参数：屏幕上是「起始月 ~ 结束月」这一段，
  // 导出也得是这一段 —— 审计第 7.3 节把「明细账导出：区间与页面显示一致」
  // 列为验收项。
  downloadFile(`/books/${store.bookId}/print/detail-ledger.xlsx`,
    { ...printParams.value }, '明细账.xlsx')
}
</script>

<style scoped>

/* 加载失败横幅与工具条之间的距离（el-alert 默认贴着上面那一块）。 */
.mb { margin-bottom: 12px; }
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
/* 分页靠右，与 VoucherList 一致；no-print 让它不进纸面。 */
.ledger-pager { display: flex; justify-content: flex-end; margin-top: 12px; }

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
