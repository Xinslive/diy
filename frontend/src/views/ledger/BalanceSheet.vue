<template>
  <div class="page">
    <div class="toolbar no-print">
      <!-- 期间控件：这一页以前**没有**，期间完全跟着顶栏走 ——
           而同一组里的明细账、总账都各有一个。用户在这三页之间来回切时，
           「看哪个月」这件事在两页要换地方找，很容易拿上一月的余额表当本月用。
           口径跟那两页一致：页内自选，不去改顶栏的全局期间。 -->
      <el-radio-group v-model="mode" size="default" @change="onModeChange">
        <el-radio-button value="single">本期</el-radio-button>
        <el-radio-button value="range">区间</el-radio-button>
      </el-radio-group>
      <template v-if="mode === 'range'">
        <span class="muted">起</span>
        <el-select v-model="fromId" class="balance-period-select"
                   :disabled="!yearPeriods.length" @change="onFromChange">
          <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id" />
        </el-select>
        <span class="muted">止</span>
      </template>
      <span v-else class="muted">期间</span>
      <el-select v-model="periodId" class="balance-period-select" :disabled="!yearPeriods.length"
                 @change="onPeriodChange">
        <el-option v-for="p in yearPeriods" :key="p.id" :label="p.code" :value="p.id">
          <span class="period-opt">
            <span>{{ p.code }}</span>
            <el-tag v-if="p.locked" size="small" type="info" effect="plain">已结账</el-tag>
          </span>
        </el-option>
      </el-select>
      <el-select v-model="includeUnposted" style="width: 150px" @change="load">
        <el-option label="包含未记账凭证" :value="true" />
        <el-option label="仅已记账凭证" :value="false" />
      </el-select>
      <!-- 「看哪些科目」是同一根轴上的三档，不是两个独立的开关。一级科目和
           明细科目本来就互斥（顶层科目要是同时没有下级，两头都算），摆两个
           复选框就会勾出一个两边都不是的结果，还得让人猜现在是哪个口径。
           三档互斥就用单选，用户永远只需要回答一个问题。 -->
      <el-radio-group v-model="scope" size="default">
        <el-radio-button value="top">仅一级科目</el-radio-button>
        <el-radio-button value="leaf">仅明细科目</el-radio-button>
        <el-radio-button value="all">全部科目</el-radio-button>
      </el-radio-group>
      <span class="spacer" />
      <!-- 三颗动作按钮**整体**换行。工具栏宽度不够时 flex-wrap 会把最后一颗
           甩到第二行，孤零零一个「导出 Excel」看着像是排版坏了。

           「打印本页」去掉了：它打的是屏幕上这张网页表格 —— 没有套打印模板、
           没有页眉页脚、没有纸张设置，与左边那颗「打印预览」（走模板、给
           用户挑模板和纸张）打出来的东西完全不是一回事。两个挨在一起，
           用户很容易点错那个，然后拿着一张没有抬头的纸来问为什么。
           要打当前页的原始表格，浏览器自己的打印（⌘P）就有。 -->
      <div class="toolbar-actions">
        <el-button @click="previewHtml">打印预览</el-button>
        <el-button type="primary" @click="exportXlsx">导出 Excel</el-button>
      </div>
    </div>

    <div class="card">
      <div class="report-head">
        <h2 class="report-title">科目余额表</h2>
        <div class="report-sub">
          {{ store.currentBook?.name }} · {{ periodText }}
        </div>
      </div>

      <div class="balance-check" :class="data.check?.closing_balanced ? 'ok' : 'bad'">
        <span v-if="data.check?.closing_balanced">试算平衡 ✓</span>
        <span v-else>试算不平 ✗ 借 {{ fmt(data.check?.total_closing_debit) }} / 贷 {{ fmt(data.check?.total_closing_credit) }}</span>
        <!-- 口径跟着模式走：区间模式下这个数是**整段**的发生额，
             还叫「本期」的话，用户会拿它跟隔壁月份对，对不上就以为表错了 -->
        <span class="muted">{{ moveLabel }} {{ fmt(data.check?.total_debit) }} / {{ fmt(data.check?.total_credit) }}</span>
      </div>

      <el-table :data="visibleRows" border size="small" class="ledger" v-loading="loading">
        <el-table-column label="编码" width="90" prop="code" />
        <el-table-column label="科目名称" min-width="220">
          <template #default="{ row }">
            <span :style="{ paddingLeft: `${(row.level - 1) * 14}px` }">
              <b v-if="!row.is_leaf">{{ row.name }}</b>
              <span v-else>{{ row.name }}</span>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="期初方向" width="72" align="center" prop="opening_direction" />
        <el-table-column label="期初余额" width="120" align="right">
          <template #default="{ row }">{{ fmt(row.opening) }}</template>
        </el-table-column>
        <el-table-column :label="moveDebitLabel" width="120" align="right">
          <template #default="{ row }">{{ fmt(row.period_debit) }}</template>
        </el-table-column>
        <el-table-column :label="moveCreditLabel" width="120" align="right">
          <template #default="{ row }">{{ fmt(row.period_credit) }}</template>
        </el-table-column>
        <!-- 本年两列在区间模式下**不显示**：
             区间跨年（2026-11 ~ 2027-02）时「本年」是哪个年都不成立 ——
             2027-02 的本年累计只含 2027 年，摆在「区间」表里会被当成整段的数。
             与其给一个语义不对的数，不如不给这一列。 -->
        <el-table-column v-if="!isRange" label="本年借方" width="120" align="right">
          <template #default="{ row }">{{ fmt(row.ytd_debit) }}</template>
        </el-table-column>
        <el-table-column v-if="!isRange" label="本年贷方" width="120" align="right">
          <template #default="{ row }">{{ fmt(row.ytd_credit) }}</template>
        </el-table-column>
        <el-table-column label="期末方向" width="76" align="center" prop="direction" />
        <el-table-column label="期末余额" width="130" align="right">
          <template #default="{ row }"><b>{{ fmt(row.closing) }}</b></template>
        </el-table-column>
      </el-table>
    </div>

    <PrintDialog v-model="printVisible" :book-id="store.bookId" :path="printPath"
                 scope="BALANCE" :params="printParams" title="打印科目余额表" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import * as api from '@/api/ledger'
import { downloadFile } from '@/api/print'
import PrintDialog from '@/components/PrintDialog.vue'
import { useLatestLoad } from '@/composables/useLatestLoad'
import { useUrlConditions } from '@/composables/useUrlConditions'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const data = ref({ rows: [], check: {} })
const includeUnposted = ref(true)
/** 看哪些科目：三档互斥，默认只看一级。
 *
 *  默认这一档而不是沿用原来的「仅明细科目」，是因为明细级在真实账里是两三千行
 *  （星悦荟商管光一张余额表就是 1300 多行），默认铺满屏谁都看不动；一级科目
 *  六七十行，一眼扫完就知道钱在哪。要下钻的用户自己切「仅明细科目」或「全部科目」。 */
const scope = ref('top')

/** 本页自选的期间。跟明细账 / 总账一个口径：页内自选，不改顶栏的全局期间。
 *  余额表既有单期（默认）也有区间，所以「止期」两种模式共用同一个 ref。 */
const periodId = ref(store.periodId)
const mode = ref('single')
/** 区间起点。默认与止期同一期，切到区间时不会立刻给出一段空区间 */
const fromId = ref(store.periodId)
const yearPeriods = computed(() => store.periodsOfYear(store.currentYear))
const isRange = computed(() => mode.value === 'range')

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
 * 默认值不写进 URL：地址栏只留用户真改过的那些。
 */
const { restore } = useUrlConditions({
  scope: { ref: scope, type: 'str', def: 'top' },
  mode: { ref: mode, type: 'str', def: 'single' },
  period: { type: 'str', get: () => periodCodeOf(periodId.value), set: (c) => { const id = periodIdOf(c); if (id) periodId.value = id } },
  // 本期模式下没有「起」这一说，URL 上就不带它 —— 地址栏上写的是真话
  from: { type: 'str', get: () => (isRange.value ? periodCodeOf(fromId.value) : ''), set: (c) => { const id = periodIdOf(c); if (id) fromId.value = id } },
  unposted: { ref: includeUnposted, type: 'bool', def: true },
})
/** 区间模式下发生额那一列得改名字：字段叫 period_debit，装的是**整段**的发生额 */
const moveDebitLabel = computed(() => (isRange.value ? '区间借方' : '本期借方'))
const moveCreditLabel = computed(() => (isRange.value ? '区间贷方' : '本期贷方'))
const moveLabel = computed(() => (isRange.value ? '区间发生额' : '本期发生额'))
/** 表头用：数据还没回来时至少显示选中的那一期，而不是一片空白 */
const periodCode = computed(
  () => yearPeriods.value.find((p) => p.id === periodId.value)?.code || ''
)
const fromCode = computed(
  () => yearPeriods.value.find((p) => p.id === fromId.value)?.code || ''
)
/** 表头那一句期间。区间缩到单期时按「期间 X」写，不要写成「区间 X ~ X」——
 *  那种写法把同一个期间在页内说了两遍，而顶栏已经选过它了。 */
const periodText = computed(() => {
  const to = data.value.period || periodCode.value
  const from = data.value.from_period || fromCode.value
  if (isRange.value && from && to && from !== to) return `区间 ${from} ~ ${to}`
  return `期间 ${to}`
})

/** 账套、年度或顶栏期间变了以后，原来那一期可能已经不在当前口径里 —— 归位。 */
function alignPeriod() {
  const pool = yearPeriods.value
  if (!pool.length) { periodId.value = null; fromId.value = null; return }
  if (!pool.some((p) => p.id === periodId.value)) {
    periodId.value = pool.some((p) => p.id === store.periodId) ? store.periodId : pool[0].id
  }
  if (!pool.some((p) => p.id === fromId.value)) fromId.value = periodId.value
}

/** 切模式：区间模式下起点默认落在止期上，免得刚切过去就是一段空区间 */
function onModeChange() {
  if (isRange.value && !yearPeriods.value.some((p) => p.id === fromId.value)) {
    fromId.value = periodId.value
  }
  load()
}

/** 起点往前挪时，止期可能被甩到起点之前 —— 那算出来是一段倒着的区间，
 *  期初比期末还晚，数字能看不能信。直接把止期一起带过去。 */
function onFromChange() {
  const from = yearPeriods.value.find((p) => p.id === fromId.value)
  const to = yearPeriods.value.find((p) => p.id === periodId.value)
  if (from && to && from.ord > to.ord) periodId.value = fromId.value
  load()
}

/** 止期往前挪到起期之前时，把起期一起带过去 —— 与 onFromChange 一个道理。
 *  倒置的区间后端直接 400，屏幕上只剩一条红提示，表还是上一份数据。 */
function onPeriodChange() {
  if (isRange.value) {
    const from = yearPeriods.value.find((p) => p.id === fromId.value)
    const to = yearPeriods.value.find((p) => p.id === periodId.value)
    if (from && to && from.ord > to.ord) fromId.value = periodId.value
  }
  load()
}

/** 只认最新一趟：顶栏期间连点时，旧响应不许覆盖新数据（见 composables/useLatestLoad）。 */
const { loading, run, isStale } = useLatestLoad('ledger/balance')

/** 过滤是纯前端的事：接口一次就把整棵树都给了，切档位不必重新取数。
 *  （原来那个复选框绑的是 @change="load"，每拨一次都白跑一趟全表。） */
const visibleRows = computed(() => {
  const rows = data.value.rows
  if (scope.value === 'top') return rows.filter((r) => r.level === 1)
  if (scope.value === 'leaf') return rows.filter((r) => r.is_leaf)
  return rows
})

async function load() {
  if (!store.bookId || !periodId.value) return
  // 单期不带 from_period_id：后端据此分叉，多传一个空值会变成「一段空区间」
  const d = await run(() => api.balance(store.bookId, {
    period_id: periodId.value,
    ...(isRange.value ? { from_period_id: fromId.value } : {}),
    include_unposted: includeUnposted.value
  }))
  // 过期的那一趟：条件早就被改掉了，写进去就是「表里的数对不上选中的期间」。
  if (isStale(d)) return
  data.value = d
}

function previewHtml() {
  printVisible.value = true
}

const printVisible = ref(false)
const printPath = computed(() => `/books/${store.bookId}/print/balance.html`)
/** 只有范围，模板 id 由弹窗自己选 —— 它得让用户在打之前改主意。
 *  区间**必须**一起传过去：屏幕上一段、纸上单期，用户只会以为纸上那份是对的。 */
const printParams = computed(() => ({
  period_id: periodId.value,
  ...(isRange.value ? { from_period_id: fromId.value } : {}),
  include_unposted: includeUnposted.value,
  // 范围必须一起带过去：屏幕上「仅一级科目」、打出来却是 1253 行明细，
  // 用户不会以为导出坏了，只会把纸上那一份当成对的 —— 余额表正是交给
  // 客户和审计的纸面数。后端 filter_balance_rows 是同一份过滤判据。
  subject_scope: scope.value,
}))

function exportXlsx() {
  downloadFile(`/books/${store.bookId}/print/balance.xlsx`, { ...printParams.value },
    isRange.value
      ? `科目余额表_${fromCode.value}_${periodCode.value}.xlsx`
      : `科目余额表_${periodCode.value}.xlsx`)
}

onMounted(async () => {
  alignPeriod()
  // 条件从 URL 复原：标签栏切走再切回来时，标签记的完整地址带着它
  restore()
  // URL 里那份期间若不在顶栏这个年份里，这一轮把它归位
  alignPeriod()
  await load()
})
// 顶栏换账套 / 换年度：先归位再取数，否则会用新账套的 id 去查旧期间。
//
//  **还得盯住 periods 的长度**，不能只盯年份 —— 总账和明细账早就踩过这个坑：
//  store.loadBooks() 先灌 books、再灌 periods，currentYear 会先从 books 的
//  latest_year 变成真值，而那一刻 periods 还是空的；alignPeriod 于是把 periodId
//  归成 null，之后 periods 到位也不再有人叫它归位。直接打开本页（刷新、按地址
//  进）就正好落在这个中间态上：期间显示「请选择」、表里空无一物、试算不平。
watch(() => [store.bookId, store.currentYear, store.periods.length], async () => {
  alignPeriod()
  await load()
})
watch(() => store.periodId, alignPeriod)
watch([periodId, fromId], load)
</script>

<style scoped>
.report-head { text-align: center; margin-bottom: 10px; }
.report-title { margin: 0 0 4px; font-size: 19px; letter-spacing: 4px; }
.report-sub { font-size: 13px; color: var(--cwq-muted); }
.ledger :deep(.cell) { padding: 4px 8px; }
.balance-check { margin-bottom: 10px; }
.balance-period-select { width: 130px; }
.toolbar-actions { display: flex; gap: 8px; }
/* 已结账那一期仍然可选 —— 结了账的月照样要能查，只是标出来 */
.period-opt { display: flex; align-items: center; }
.period-opt .el-tag { margin-left: auto; }
</style>
