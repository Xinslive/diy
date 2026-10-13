<template>
  <div class="page">
    <div class="toolbar no-print">
      <!-- 筛选一律按**全部期间**做：起止期间和「全部期间」那两个下拉撤了，
           顶栏切到哪个月都不再把这一页锁死在那个月 —— 查账的人要的是「全年哪些凭证
           超过十万」，不是「上个月里有哪些」。

           「金额」筛的是**某一条分录**的金额，不是整张凭证的合计：
           只要凭证里有任意一行落在这个区间，整张就被捞出来。 -->
      <el-input v-model="query.keyword" placeholder="凭证号 / 摘要" clearable style="width: 108px" @keyup.enter="load" />
      <el-tooltip content="按分录金额筛选：凭证里有任意一行落在这个区间，整张都会被列出来" placement="top">
        <span class="muted">分录金额</span>
      </el-tooltip>
      <el-input-number v-model="query.min_amount" :controls="false" :min="0" :precision="2"
                       :value-on-clear="null" placeholder="从" style="width: 78px" @keyup.enter="load" />
      <span class="muted">—</span>
      <el-input-number v-model="query.max_amount" :controls="false" :min="0" :precision="2"
                       :value-on-clear="null" placeholder="到" style="width: 78px" @keyup.enter="load" />
      <el-button @click="load">查询</el-button>

      <span class="spacer" />

      <!-- 「记账 / 反记账」撤了：期末结账里的「结账」本来就会把本期未记账的
           全部记上（`_auto_post`），「反结账」原路退回。同一件事两个入口，
           走了一个就忘了另一个，月底对不上账又要从头捋。
           腾出来的宽度正好够把原来塞在「更多」里的两个常用项摆到明面上 ——
           一个只剩一个选项的下拉框，本身就是个没意义的容器。 -->
      <el-button @click="printVouchers">
        <el-icon><Printer /></el-icon>&nbsp;{{ selection.length ? `打印所选（${selection.length}）` : '打印预览' }}
      </el-button>

      <template v-if="store.canWrite">
        <el-button :icon="Sort" @click="router.push('/vouchers/organize')">整理凭证</el-button>
        <el-button :icon="Upload" @click="router.push('/vouchers/import')">导入凭证</el-button>
        <el-button type="primary" @click="$router.push('/vouchers/new')">新增凭证</el-button>
      </template>
    </div>

    <!-- 整理提醒搬到这里来了。原先「断号检查」是整理凭证里的一个「方式」，
         用户想看一眼有没有断号，得先想明白该选哪个整理方式、还得点一次「查看方案」
         —— 想确认一件事却要先做一次可能改数据的操作，这个顺序是反的。
         这里只读、只说一句话，要不要动号由用户自己点「整理凭证」去决定。
         断号和时序不对都会挡住记账和结账，所以一并在这儿讲清楚。 -->
    <el-alert
      v-if="notReady"
      type="warning"
      :closable="false"
      class="no-print"
      style="margin-bottom: 10px"
    >
      <template #title>
        {{ notReady }}
        ，整理好之前这个月不能记账、也不能结账。
        <el-button link type="primary" @click="router.push('/vouchers/organize')">去整理</el-button>
      </template>
    </el-alert>

    <el-alert
      v-if="selection.length"
      type="info"
      :closable="false"
      class="no-print"
      style="margin-bottom: 10px"
      :title="`已选 ${selection.length} 张凭证`"
    />

    <div class="card">
      <el-table
        v-loading="loading"
        :data="rows"
        border
        stripe
        @selection-change="selection = $event"
        @row-dblclick="(row) => open(row)"
      >
        <el-table-column type="selection" width="42" />
        <!-- 日期不许折行：'2026-12-31' 断成两行，扫一列日期全靠猜 -->
        <el-table-column prop="voucher_date" label="日期" width="124" class-name="nowrap" label-class-name="nowrap" />
        <el-table-column prop="voucher_no" label="凭证号" width="95" />
        <el-table-column label="摘要" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">{{ firstSummary(row) }}</template>
        </el-table-column>
        <el-table-column label="借方合计" width="130" align="right">
          <template #default="{ row }"><span class="money">{{ fmt(row.total_debit) }}</span></template>
        </el-table-column>
        <el-table-column label="贷方合计" width="130" align="right">
          <template #default="{ row }"><span class="money">{{ fmt(row.total_credit) }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.status === 'POSTED'" type="success" size="small">已记账</el-tag>
            <el-tag v-else type="warning" size="small">未记账</el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="store.canWrite" label="操作" width="168" fixed="right">
          <template #default="{ row }">
            <!-- 已记账凭证锁定：只能看，改/删都要先反记账 -->
            <el-button link type="primary" @click="open(row)">
              {{ row.status === 'POSTED' ? '查看' : '查看/改' }}
            </el-button>
            <el-button link type="danger" :disabled="isLocked(row)" @click="removeOne(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty><span class="muted">当前条件下没有凭证</span></template>
      </el-table>

      <div style="display: flex; justify-content: flex-end; margin-top: 12px">
        <el-pagination
          layout="total, sizes, prev, pager, next"
          :total="total"
          v-model:current-page="query.page"
          v-model:page-size="query.page_size"
          :page-sizes="[20, 50, 100, 200]"
          @current-change="load"
          @size-change="load"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Printer, Sort, Upload } from '@element-plus/icons-vue'
import * as api from '@/api/voucher'
import { openPrint } from '@/api/print'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const route = useRoute()
const router = useRouter()
const rows = ref([])
const total = ref(0)
const loading = ref(false)
const selection = ref([])
/** 顶栏那个期间的「整理就绪」体检结果。干净就是 null，不占地方。 */
const check = ref(null)
/** 这张凭证能不能改/删：期间已结账、或本身已记账，都锁死。 */
function isLocked(row) {
  return row.status === 'POSTED' || !!store.currentPeriod?.locked
}

const query = reactive({
  keyword: '', min_amount: null, max_amount: null,
  page: 1, page_size: 50
})

/**
 * 什么时候看全账套、什么时候只看顶栏那个月：
 *   **一个筛选条件都没填** → 就看顶栏定下的那个月（空表格比跨年 200 行好用）；
 *   **填了任一条件** → 放宽到全部期间（要问的是「全年哪些凭证含 16」，
 *     这时候被顶栏那个月卡着反而是帮倒忙）。
 * 两头都跟用户说的一致，不是拍脑袋定的中间值。
 */
const hasFilter = computed(() =>
  !!(query.keyword || query.min_amount !== null || query.max_amount !== null))

/** 传给后端的查询参数：条件栏照旧，期间按 hasFilter 决定给不给。 */
function queryParams() {
  const p = { ...query }
  if (!hasFilter.value) {
    p.from_period_id = store.periodId || undefined
    p.to_period_id = store.periodId || undefined
  }
  return p
}

/**
 * 列表条件要跟着凭证页走：进去、翻前一张后一张、点返回，都得是同一批结果。
 * 只带用户**真正填过的**条件，期间不写进去 —— 期间是顶栏说了算，
 * 写死了用户把顶栏换成 11 月，返回时列表还停在 12 月，这就成了另一个毛病。
 */
function filterQuery() {
  const q = {}
  if (query.keyword) q.kw = query.keyword
  if (query.min_amount !== null) q.min = String(query.min_amount)
  if (query.max_amount !== null) q.max = String(query.max_amount)
  return q
}

/** 页面刷新 / 从凭证页返回时，靠 URL 把上次的条件捡回来。 */
function restoreFilterFromUrl() {
  const q = route.query || {}
  query.keyword = q.kw || ''
  query.min_amount = q.min === undefined || q.min === '' ? null : Number(q.min)
  query.max_amount = q.max === undefined || q.max === '' ? null : Number(q.max)
}

function firstSummary(row) {
  const s = row.entries.find((e) => e.summary)?.summary
  return s || row.remark || '—'
}

async function load() {
  if (!store.bookId) return
  loading.value = true
  try {
    const data = await api.queryVouchers(store.bookId, queryParams())
    rows.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
  loadCheck()
}

/**
 * 断号和时序只跟「期间」有关，跟筛选条件无关：条件栏筛的是跨年的一堆凭证，
 * 而断号说的是号段本身连不连得上。所以这里固定查顶栏那个期间，不跟着条件走。
 * 体检是只读的，失败就当没提醒 —— 列表该显示的还得显示。
 */
async function loadCheck() {
  if (!store.bookId || !store.periodId) { check.value = null; return }
  try {
    const r = await api.voucherPeriodCheck(store.bookId, store.periodId)
    check.value = r
  } catch {
    check.value = null
  }
}

/** 一句话说清这个月差在哪儿：断号几个、哪几处时序不对。 */
const notReady = computed(() => {
  const r = check.value
  if (!r) return ''
  const parts = []
  if (r.gaps?.length) parts.push(`凭证号有 ${r.gaps.length} 个断号：${r.gaps.join('、')}`)
  if (r.time_out_of_order?.length) {
    parts.push(`有 ${r.time_out_of_order.length} 处凭证号和日期对不上：`
      + r.time_out_of_order.slice(0, 3)
        .map((i) => `${i.prev_no}→${i.voucher_no}`).join('、'))
  }
  return parts.length ? `${r.period} ${parts.join('；')}` : ''
})

function printVouchers() {
  const params = { period_id: store.periodId }
  if (selection.value.length) {
    params.voucher_ids = selection.value.map((voucher) => voucher.id).join(',')
  }
  openPrint(`/books/${store.bookId}/print/vouchers.html`, params)
}

function open(row) {
  router.push({ name: 'voucher-edit', params: { id: row.id }, query: filterQuery() })
}

/**
 * 确认框是会 reject 的：人一点「取消」就带着 'cancel' 抛出来。
 *
 *  原来直接 `await ElMessageBox.confirm(...)`，点取消 → 一个未处理的 rejection
 *  冒到控制台，控制台一片红，还把后面真正的报错一起淹掉 —— 看着像页面崩了。
 * 取消本来就是一个正常的决定，不该当异常。
 */
async function removeOne(row) {
  if (isLocked(row)) {
    return ElMessage.warning(
      row.status === 'POSTED'
        ? `凭证 ${row.voucher_no} 已记账，不能删除；请先「反记账」`
        : `${row.voucher_no} 所在期间已结账，不能删除`)
  }
  const ok = await ElMessageBox.confirm(`确认删除凭证 ${row.voucher_no}？`, '删除凭证', { type: 'warning' })
    .then(() => true)
    .catch(() => false)
  if (!ok) return
  await api.deleteVoucher(store.bookId, row.id)
  ElMessage.success('已删除')
  load()
}
onMounted(() => {
  query.page = 1
  restoreFilterFromUrl()
  load()
})
watch(() => store.bookId, () => {
  query.page = 1
  selection.value = []
  load()
})
/** 条件栏清空，顺带把 URL 上残留的条件也抹掉（不然一刷新它又回来了）。 */
function clearFilter() {
  query.keyword = ''
  query.min_amount = null
  query.max_amount = null
  if (route.query && Object.keys(route.query).length) {
    router.replace({ name: 'vouchers' })
  }
}

/**
 * 顶栏换月 → 条件栏清空，重新按新的那个月取。
 * 早先的写法是「有条件就当没看见，换了月也不动」：条件是**全账套**的，
 * 月份是**单月**的，两个范围叠在一起时结果既不是这个月、也不是那个月，
 * 用户看着只会以为账丢了。月份一变，条件就该作废。
 */
watch(() => store.periodId, () => {
  query.page = 1
  clearFilter()
  check.value = null
  load()
})
</script>

<style scoped>
/* 日期不许折行：「2026-12-31」断成两行，扫一列日期全靠猜 */
:deep(.el-table .nowrap .cell) { white-space: nowrap; }
</style>
