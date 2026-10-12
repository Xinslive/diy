<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-select v-model="subjectId" filterable placeholder="选择科目" style="width: 320px" @change="load">
        <el-option v-for="s in subjects" :key="s.id" :label="`${s.code} ${s.name}`" :value="s.id" />
      </el-select>
      <span class="muted">起</span>
      <el-select v-model="fromId" style="width: 110px" @change="load">
        <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id" />
      </el-select>
      <span class="muted">止</span>
      <el-select v-model="toId" style="width: 110px" @change="load">
        <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id" />
      </el-select>
      <el-checkbox v-model="includeUnposted" label="包含未记账凭证" @change="load" />
      <span class="spacer" />
      <el-button @click="previewHtml">模板预览/打印</el-button>
      <el-button @click="window.print()">打印本页</el-button>
    </div>

    <div class="card">
      <div class="report-head">
        <h2 class="report-title">明 细 账</h2>
        <div class="report-sub">
          {{ store.currentBook?.company_name }} · {{ data.subject?.code }} {{ data.subject?.name }}
          · {{ data.from_period }} 至 {{ data.to_period }}
        </div>
      </div>
      <!-- 明细账是唯一逐笔列凭证的账簿，所以只有它的行能点。
           期初余额 / 本期合计 两行没有 voucher_id，rowClassName / rowClick 都会跳过。 -->
      <el-table :data="data.rows" border size="small" v-loading="loading"
                :row-class-name="rowClass" @row-click="openVoucher">
        <el-table-column label="日期" width="105"><template #default="{ row }">{{ row.voucher_date || '' }}</template></el-table-column>
        <el-table-column label="凭证号" width="95">
          <template #default="{ row }">
            <button v-if="row.voucher_id" type="button" class="vno-link"
                    :title="`打开凭证 ${row.voucher_no}`"
                    @click.stop="openVoucher(row)">{{ row.voucher_no }}</button>
          </template>
        </el-table-column>
        <el-table-column label="摘要" min-width="240"><template #default="{ row }">{{ row.summary || '' }}</template></el-table-column>
        <el-table-column label="借方" width="120" align="right"><template #default="{ row }">{{ fmt(row.debit) }}</template></el-table-column>
        <el-table-column label="贷方" width="120" align="right"><template #default="{ row }">{{ fmt(row.credit) }}</template></el-table-column>
        <el-table-column label="方向" width="60" align="center"><template #default="{ row }">{{ row.direction }}</template></el-table-column>
        <el-table-column label="余额" width="130" align="right"><template #default="{ row }"><b>{{ fmt(row.balance) }}</b></template></el-table-column>
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
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const router = useRouter()

/** 这一行有没有对应的凭证可点。
 *  期初余额和本期合计是算出来的行，没有 voucher_id —— 给它们挂 pointer
 *  样式的话，用户点下去没反应，比不可点更让人恼火。 */
function clickable(row) { return !!row.voucher_id }
function rowClass({ row }) { return clickable(row) ? 'row-voucher' : '' }
function openVoucher(row) {
  if (!clickable(row)) return
  router.push({ name: 'voucher-edit', params: { id: row.voucher_id } })
}
const subjects = ref([])
const subjectId = ref(null)
const fromId = ref(store.periodId)
const toId = ref(store.periodId)
const data = ref({ rows: [] })
const loading = ref(false)
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
      subject_id: subjectId.value, from_period_id: fromId.value, to_period_id: toId.value,
      include_unposted: includeUnposted.value
    })
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  toId.value = store.periodId
  anchorPeriods()
  subjects.value = await subjectApi.subjectFlat(store.bookId)
  if (subjects.value.length) subjectId.value = subjects.value[0].id
  load()
})
watch(() => store.bookId, async () => {
  anchorPeriods()
  subjects.value = await subjectApi.subjectFlat(store.bookId)
  if (subjects.value.length) subjectId.value = subjects.value[0].id
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
    include_unposted: includeUnposted.value,
  })
}
</script>

<style scoped>
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

/* 整行可点：悬停给一点反馈，不然只有凭证号变蓝，看不出整行都认。 */
:deep(.el-table__row.row-voucher) { cursor: pointer; }
:deep(.el-table__row.row-voucher:hover > td.el-table__cell) { background: #f2f7ff !important; }

/* 印到纸上就没有「能点」这回事了，蓝字只剩浪费墨水和看不清。
   明细账是要归档的账簿，凭证号该是黑字。 */
@media print {
  .vno-link { color: inherit !important; text-decoration: none !important; }
}
</style>
