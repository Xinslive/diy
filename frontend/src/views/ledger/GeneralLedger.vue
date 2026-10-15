<template>
  <div class="page">
    <div class="toolbar no-print">
      <span class="muted">起</span>
      <el-select v-model="fromId" style="width: 120px" @change="onFilterChange">
        <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id" />
      </el-select>
      <span class="muted">止</span>
      <el-select v-model="toId" style="width: 120px" @change="onFilterChange">
        <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id" />
      </el-select>
      <el-checkbox v-model="leafOnly" label="仅明细科目" @change="onFilterChange" />
      <el-checkbox v-model="includeUnposted" label="包含未记账凭证" @change="onFilterChange" />
      <span class="spacer" />
      <el-button @click="previewHtml">打印预览</el-button>
      <el-button type="primary" @click="exportXlsx">导出 Excel</el-button>
    </div>

    <div class="card">
      <div class="report-head">
        <h2 class="report-title">总 账</h2>
        <div class="report-sub">
          {{ store.currentBook?.company_name }} · {{ store.currentBook?.name }} {{ store.currentPeriod?.code }}
          · {{ data.from_period }} 至 {{ data.to_period }}
        </div>
      </div>
      <el-table :data="pageRows" border size="small" v-loading="loading">
        <el-table-column label="编码" width="90" prop="code" />
        <el-table-column label="科目名称" min-width="200">
          <template #default="{ row }"><span :style="{ paddingLeft: `${(row.level - 1) * 14}px` }">{{ row.name }}</span></template>
        </el-table-column>
        <el-table-column label="期初方向" width="72" align="center" prop="opening_direction" />
        <el-table-column label="期初余额" width="120" align="right"><template #default="{ row }">{{ fmt(row.opening) }}</template></el-table-column>
        <el-table-column label="本期借方" width="120" align="right"><template #default="{ row }">{{ fmt(row.period_debit) }}</template></el-table-column>
        <el-table-column label="本期贷方" width="120" align="right"><template #default="{ row }">{{ fmt(row.period_credit) }}</template></el-table-column>
        <el-table-column label="本年借方" width="120" align="right"><template #default="{ row }">{{ fmt(row.ytd_debit) }}</template></el-table-column>
        <el-table-column label="本年贷方" width="120" align="right"><template #default="{ row }">{{ fmt(row.ytd_credit) }}</template></el-table-column>
        <el-table-column label="期末余额" width="130" align="right"><template #default="{ row }"><b>{{ fmt(row.closing) }}</b></template></el-table-column>
      </el-table>

      <div class="pager no-print">
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
const data = ref({ rows: [] })
const fromId = ref(store.periodId)
const toId = ref(store.periodId)
const leafOnly = ref(true)
/** 未记账凭证也参与计算 —— 默认开，理由同明细账。 */
const includeUnposted = ref(true)

/** 只认最新一趟：期间下拉连点时，旧响应不许覆盖新数据（见 composables/useLatestLoad）。 */
const { loading, run, isStale } = useLatestLoad('ledger/general')

/**
 * 分页是**纯前端切片** —— 后端接口照旧一次把整段返回，契约不动。
 *
 * 一行一个科目，科目一多就整屏铺开；每次切期间都要重渲染几百行，
 * 而这页切期间是主要动作。切片只影响渲染量，不影响任何数字。
 */
const page = ref(1)
const pageSize = ref(50)
const totalRows = computed(() => data.value.rows.length)
const pageRows = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return data.value.rows.slice(start, start + pageSize.value)
})
/** 换条件就回第一页：停在原来那页会看着像「科目少了」。 */
function rewindPage() { page.value = 1 }

/** 起止只列顶栏那个年份的期间。列全部年份时，顶栏停在 2027、
 *  下拉里却能翻出 2026-06，翻过去算的是另一本账。 */
const yearPeriods = computed(() => store.periodsOfYear(store.currentYear))

/** 起止必须落在顶栏那个年份里；落空了就回到顶栏当前那一期。
 *  换年份后原来那个 id 不在新列表里，el-select 会显示成空白或裸 id，
 *  而传出去的就是一个不属于当前年份的下界/上界。 */
function anchorPeriods() {
  const pool = yearPeriods.value
  if (!pool.length) { fromId.value = null; toId.value = null; return }
  const cur = pool.some((p) => p.id === store.periodId) ? store.periodId : pool[0].id
  if (!pool.some((p) => p.id === fromId.value)) fromId.value = cur
  if (!pool.some((p) => p.id === toId.value)) toId.value = cur
}

async function load() {
  if (!store.bookId) return
  // 期间还没灌完就查，后端会拿**它自己默认的那一期**当上下界 ——
  // 请求里压根没有 from/to，回来的数是另一段区间，页面上看着还像那么回事。
  // 与其先显示一个错的再纠正，不如等 store.periods 到位（下面的 watch 会再触发一次）。
  if (!yearPeriods.value.length) return
  const d = await run(() => api.general(store.bookId, {
    from_period_id: fromId.value, period_id: toId.value, leaf_only: leafOnly.value,
    include_unposted: includeUnposted.value
  }))
  // 过期的那一趟：结果已经到了，但条件早就被用户改掉了 —— 写进去就是
  // 「表格里的数对不上顶栏的期间」。
  if (isStale(d)) return
  data.value = d
  // 换到行数更少的区间时，原来那页可能已经不存在了 —— 停在越界的页码上
  // 就是一张空表，看着像「这个期间一个科目都没有」。
  const maxPage = Math.max(1, Math.ceil(d.rows.length / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
}
/** 条件变了就回第一页：停在原来那页会看着像「科目少了」。 */
function onFilterChange() { rewindPage(); load() }
function previewHtml() {
  printVisible.value = true
}

const printVisible = ref(false)
const printPath = computed(() => `/books/${store.bookId}/print/general-ledger.html`)
/** 只有范围，模板 id 由弹窗自己选 —— 它得让用户在打之前改主意。 */
const printParams = computed(() => ({
  period_id: toId.value, from_period_id: fromId.value || undefined,
  leaf_only: leafOnly.value, include_unposted: includeUnposted.value,
}))

function exportXlsx() {
  // from_period_id 必须一起传。页面显示的是「起始月 ~ 结束月」这一段，
  // 导出却只给了 period_id（= 结束月），后端就按「从最早期间到该月」
  // 导 —— 屏幕上是一页，下载下来是全账，数字对不上，用户只会以为导出坏了。
  // 审计第 7.3 节把「总账导出：区间与页面显示一致」列为验收项。
  //
  // 与上面 printParams 的口径保持一致：那边是 from_period_id || undefined。
  downloadFile(`/books/${store.bookId}/print/general-ledger.xlsx`,
    { period_id: toId.value, from_period_id: fromId.value || undefined,
      leaf_only: leafOnly.value, include_unposted: includeUnposted.value }, '总账.xlsx')
}

onMounted(() => { toId.value = store.periodId; anchorPeriods(); load() })
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
watch(() => [store.bookId, store.currentYear, store.periods.length], () => { anchorPeriods(); load() })
</script>

<style scoped>
.report-head { text-align: center; margin-bottom: 12px; }
.report-title { margin: 0 0 4px; font-size: 19px; letter-spacing: 6px; }
.report-sub { font-size: 13px; color: var(--cwq-muted); }
/* 分页靠右，与 VoucherList 一致；no-print 让它不进纸面。 */
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
