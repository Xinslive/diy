<template>
  <div class="page">
    <div class="toolbar no-print">
      <!-- 起止期间和「全部期间」那两个下拉撤了，期间由顶栏说了算：
           **没填任何筛选条件**时只看顶栏那个月（空表格比跨年几百行好用）；
           **填了任一条件**后放宽到全部期间 —— 查账的人要的是「全年哪些凭证
           超过十万」，这时被顶栏那个月卡着反而是帮倒忙。判据见 hasFilter。

           「金额」筛的是**某一条分录**的金额，不是整张凭证的合计：
           只要凭证里有任意一行落在这个区间，整张就被捞出来。

           「查询」按钮和输入框里的清空「×」都撤了：三个输入框各自绑了回车，
           条件填完按回车就跑。多摆一个按钮，等于把「条件什么时候算改了」摊成
           两个入口 —— 改完点按钮是一种，改完直接回车是另一种，人很容易只记住
           一种，然后对着没反应的列表以为坏了；「×」则只在有内容时冒出来，
           工具栏宽度跟着输入内容变来变去，整排控件左右跳。
           要清空就全选退格。 -->
      <el-input v-model="query.keyword" placeholder="凭证号 / 摘要" title="填完按回车查询"
                style="width: 108px" @keyup.enter="load" />
      <el-tooltip content="按分录金额筛选：凭证里有任意一行落在这个区间，整张都会被列出来" placement="top">
        <span class="muted">分录金额</span>
      </el-tooltip>
      <!-- 金额框要放得下**完整的金额**（用户 2026-10-23：「金额不能显示全」）。
           **不靠加宽，靠把那 30px 的内边距收掉**：el-input-number 两侧各留
           15px 是给加减按钮的，这两个框没有按钮（:controls="false"），
           那 30px 全是浪费 —— 78px 的外宽只剩 48px 字宽，
           而 14px Arial 下一个数字 7.8px，「4421.79」= 50.6px，
           **差 2.6px 就被截掉**，输进去看着像「4421.7」。
           内边距收到 8px 之后，92px 的框给出 76px 字宽，
           够放下七位整数加两位小数（9999999.99 = 74px）；
           框本身只比原来宽 14px，一眼看过去还是原来那个大小
           （用户 2026-10-23：「宽度太大了，这不好看」）。 -->
      <el-input-number v-model="query.min_amount" class="amt-input" :controls="false" :min="0"
                       :precision="2" :value-on-clear="null" placeholder="从"
                       style="width: 92px" @keyup.enter="load" />
      <span class="muted">—</span>
      <el-input-number v-model="query.max_amount" class="amt-input" :controls="false" :min="0"
                       :precision="2" :value-on-clear="null" placeholder="到"
                       style="width: 92px" @keyup.enter="load" />

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
        <!-- 列宽按**日期文本本身**算：实测 '2026-12-31' 在 14px 系统字体下
             占 83px（macOS 的 SF 比 Windows 的 Segoe 更宽，取宽的那个）；
             单元格左右内边距各 12px → 下限 83 + 24 = 107px，
             留 5px 余量（字号/字体差异）→ 112px。
             原来给到 124px，右边空着一截。nowrap 配 overflow:hidden，
             宽度不够会变成省略号（'2026-12-3…'），所以这个下限不能压。 -->
        <el-table-column prop="voucher_date" label="日期" width="112"
                         class-name="nowrap" label-class-name="nowrap">
          <template #header>
            <span class="sort-head" :class="headClass('date_')"
                  @click.stop="toggleSort('date')">日期</span>
          </template>
        </el-table-column>
        <el-table-column prop="voucher_no" label="凭证号" width="95">
          <template #header>
            <span class="sort-head" :class="headClass('no_')"
                  @click.stop="toggleSort('no')">凭证号</span>
          </template>
        </el-table-column>
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
        <!-- 操作列宽按**最宽的那一格**算，不按平均：
             未记账凭证的按钮是「查看/改」（3 个汉字 + 一个半角斜杠 ≈ 50px，
             加按钮内边距约 56px），已记账是「查看」（34px）；
             两个按钮之间有 12px 间距，单元格左右内边距各 12px ——
             所以最宽的一行要 56 + 12 + 34 + 24 = 126px。
             原来给到 168px，右边空出 40 多像素，窄一窄正好把宽度让给
             旁边的「状态」列（那一列本来就被右边这列压住一截）。
             126 是下限，留 6px 余量给不同机器的字体差异：132px。 -->
        <el-table-column v-if="store.canWrite" label="操作" width="132" fixed="right">
          <template #default="{ row }">
            <!-- 已记账凭证锁定：只能看，改/删都要先退回复记账状态（入口在「期末结账 → 反结账」） -->
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

    <!-- 打印不直接出纸：先弹窗选模板和纸型、看预览，确认了才打 -->
    <PrintDialog
      v-model="printVisible"
      :book-id="store.bookId"
      :path="printPath"
      scope="VOUCHER"
      :params="printParams"
      title="打印记账凭证"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Printer, Sort, Upload } from '@element-plus/icons-vue'
import * as api from '@/api/voucher'
import PrintDialog from '@/components/PrintDialog.vue'
import { useUrlConditions } from '@/composables/useUrlConditions'
import { useSessionStore } from '@/stores/session'
import { conditionsBelongTo } from '@/utils/filterScope'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const router = useRouter()
const route = useRoute()
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

/**
 * 条件 ↔ URL。
 *
 * 标签栏不做 keep-alive：切走再切回来就是组件重建，条件只活在组件里的话，
 * 回来必然是默认值 —— 那就是「查询参数被重置」这个毛病。所以条件住进 URL：
 * 标签栏记的完整地址带着它，回来照常重新取数（数据不旧），用的还是原来那组条件。
 *
 * 键名沿用 kw / min / max：凭证详情页（VoucherEdit）也是靠这三个键把条件
 * 带来带去的 —— 键名一改，那条线就断了。
 */
const { restore } = useUrlConditions({
  kw: { type: 'str', get: () => query.keyword, set: (v) => { query.keyword = v } },
  min: { type: 'num', get: () => query.min_amount, set: (v) => { query.min_amount = v } },
  max: { type: 'num', get: () => query.max_amount, set: (v) => { query.max_amount = v } },
  // **这组条件属于哪本账。**
  //
  // 条件住在 URL 里，页面重挂后只认 URL —— 所以「换账套要清条件」必须在
  // **重挂时**判断（组件里的 watch 活不到那一刻：账套一变 MainLayout 的
  // viewKey 就变，整页重挂，watch 一起被停掉）。
  //
  // 那怎么认出「这组条件是上一本账的」？光记住「条件存在过」不够 ——
  // 切走再切回来也重挂，条件得留着。得记住**是谁的**。
  //
  // 没有条件时不写这一项：地址栏里只留用户真填过的那些。
  book: {
    type: 'str',
    get: () => (hasFilter.value ? String(store.bookId) : ''),
    // 账套不是「条件」，只拿来比 —— 不往回写。
    set: () => {}
  },
})

// --------------------------------------------------------------------- 排序
//
// 四个档位（后端 SORTS 一一对应）：凭证号顺序 / 凭证号倒序 / 日期顺序 / 日期倒序。
// **默认凭证号倒序** —— 刚录的那批排在最前面，一天里最后动过账的先看见。
//
// 「凭证号」排序是**先按期间、再按号段**：凭证号只在期间内唯一（记0001 与 记0012
// 分属两个月），跨月比大小没有意义。这条口径由后端保证，前端只管把档位传下去。
const SORT_LABEL = {
  no_desc: '凭证号倒序', no_asc: '凭证号顺序',
  date_desc: '日期倒序', date_asc: '日期顺序'
}
const sort = ref('no_desc')

/** 切换排序列时从升序开始；连续点击当前列时切换升降序。 */
function toggleSort(field) {
  const isCurrentField = sort.value.startsWith(`${field}_`)
  const direction = isCurrentField && sort.value.endsWith('_asc') ? 'desc' : 'asc'
  sort.value = `${field}_${direction}`
  query.page = 1        // 换排序回第一页，否则停在原来那页会看着像「少了几张」
  load()
}

/** 表头高亮：正在按这一列排才点亮，并按升降挂 asc/desc 箭头。 */
function headClass(prefix) {
  if (!sort.value.startsWith(prefix)) return ''
  return {
    on: true,
    asc: sort.value.endsWith('_asc'),
    desc: sort.value.endsWith('_desc')
  }
}

/** 传给后端的查询参数：条件栏照旧，期间按 hasFilter 决定给不给。 */
function queryParams() {
  const p = { ...query, sort: sort.value }
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

function firstSummary(row) {
  const s = row.entries.find((e) => e.summary)?.summary
  return s || row.remark || '—'
}

async function load() {
  if (!store.bookId) return
  // 切账套的**那一瞬间**期间还在路上（store 已经把上一个账套的期间清干净了）。
  // 这时 periodId 是 0，不带期间查就是「把整本账的凭证全拉一遍」——
  // 多余的一趟，外加一帧「列表突然变成全年」的闪烁。等期间到位再查。
  //
  // 反过来，**拿着旧账套的期间 id 查新账套**更糟：后端会回「起始期间不存在」。
  // 以前正是在这儿出的错（store 以为账套没换，旧期间一直留着）。
  if (!store.periodsReady) return
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

const printVisible = ref(false)
const printPath = computed(() => `/books/${store.bookId}/print/vouchers.html`)
/**
 * 打印范围：勾选了就只打勾选的，没勾选就打整个期间。
 * 「打印所选」和「打印预览」共用这一份范围 —— 两个按钮指的是同一件事的两种范围，
 * 不该各自算一遍，否则两边的凭证数会悄悄对不上。
 */
const printParams = computed(() => {
  const params = { period_id: store.periodId }
  if (selection.value.length) {
    params.voucher_ids = selection.value.map((voucher) => voucher.id).join(',')
  }
  return params
})

function printVouchers() {
  printVisible.value = true
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
        ? `凭证 ${row.voucher_no} 已记账，不能删除；请到「期末结账」反结账后再删`
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
  // 条件从 URL 复原：页面刷新、从凭证页返回、从标签栏切回来，走的都是这一条。
  //
  // 但**换过账套**的时候不捡：凭证号、金额都是**这一本账**里的检索，
  // 拿到另一本账上去查，命中的是别的账的凭证 —— 看着像「这个账套的凭证少了」。
  //
  // 判断只能落在**重挂时**：账套一变 MainLayout 的 viewKey 就变，整页重挂，
  // 组件里的 watch 活不到那一刻（重挂会把 watch 一起停掉）。
  // 认出「条件是谁的」靠 URL 上的 book —— 条件在 URL 里，归属也得在 URL 里。
  //
  // 换月不清条件：条件填了就放宽到全部期间（见 hasFilter），
  // 月份本来就不参与条件，犯不着为它作废。
  if (!conditionsBelongTo(route.query, store.bookId)) {
    // restore([]) = 「什么都不捡，但把自动同步打开」——
    // 于是 URL 上那份残留也被一并抹掉（不然下次重挂又把它捡回来）。
    clearFilter()
    restore([])
  } else {
    restore()
  }
  load()
})
/** 条件栏清空。URL 上残留的条件由 useUrlConditions 一并抹掉（不然一刷新它又回来了）。 */
function clearFilter() {
  query.keyword = ''
  query.min_amount = null
  query.max_amount = null
}

/**
 * 一趟查询的触发口：账套 + 顶栏期间 + **期间表本身**。
 *
 * 「期间表本身」这一项不能少：切账套时 store 会把旧期间清掉、换成新账套的，
 * 而新账套那一期的 id 未必跟旧的不同（两个账套的第一期很可能都是
 * 「2026-01」，id 甚至可能撞上）—— 那一刻 periodId 不变，
 * 只盯 periodId 的 watch 就不响，列表会一直是空的。
 * 盯住 periods 的长度，期间一到位就有人叫醒这一页。
 *
 * 原来这里是两条 watch（一条盯账套、一条盯期间），各自调 load()：
 * 切账套时账套变、期间也变，两条都响 → 同一份查询发两趟。
 * 合成一条之后，一趟查询只有一个触发口。
 *
 * 这里**不管条件清不清**：那是 onMounted 的事（条件住在 URL 里，
 * 归属也在 URL 里，只能在重挂时判）。
 */
watch(() => [store.bookId, store.periodId, store.periods.length], () => {
  query.page = 1
  // 断号体检只对当前账套当前那期有意义，换了账套就得重算。
  check.value = null
  load()
})
</script>

<style scoped>
/* 金额筛选框的内边距：Element Plus 给 el-input-number 两侧各留 15px
   （那是给加减按钮的），这两个框没有按钮 —— 不收掉的话，框宽里有 30px
   是空的，金额长一点就露不全。收到 8px 之后 92px 的框能给出 76px 字宽。 */
.toolbar .amt-input :deep(.el-input__wrapper) { padding-left: 8px; padding-right: 8px; }

/* 日期不许折行：「2026-12-31」断成两行，扫一列日期全靠猜 */
:deep(.el-table .nowrap .cell) { white-space: nowrap; }

/* 可点的排序列：手型 + 悬停底色，让人看出这是个开关而不是纯标题。
   当前正在按这一列排时给它主色小箭头 —— 四档循环光靠文字说不清自己在哪一档。 */
.sort-head {
  cursor: pointer;
  user-select: none;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 4px;
  margin: -1px -4px;
  border-radius: 3px;
}
.sort-head:hover { background: var(--el-fill-color-light); }
.sort-head.on { color: var(--el-color-primary); font-weight: 600; }
.sort-head.on::after {
  content: '';
  border: 4px solid transparent;
}
.sort-head.on.asc::after { border-bottom-color: currentColor; margin-bottom: -3px; }
.sort-head.on.desc::after { border-top-color: currentColor; margin-top: -3px; }

</style>
