<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-select v-model="query.from_period_id" placeholder="起" clearable style="width: 108px" @change="follow = false">
        <el-option v-for="p in store.periods" :key="p.id" :label="p.code" :value="p.id" />
      </el-select>
      <el-select v-model="query.to_period_id" placeholder="止" clearable style="width: 108px" @change="follow = false">
        <el-option v-for="p in store.periods" :key="p.id" :label="p.code" :value="p.id" />
      </el-select>
      <el-button text type="primary" :disabled="!follow" @click="showAllPeriods">全部期间</el-button>
      <el-input v-model="query.keyword" placeholder="凭证号 / 摘要" clearable style="width: 180px" @keyup.enter="load" />
      <el-select v-model="query.status" placeholder="状态" clearable style="width: 120px">
        <el-option label="未记账" value="UNPOSTED" />
        <el-option label="已记账" value="POSTED" />
      </el-select>
      <el-select v-model="query.source" placeholder="来源" clearable style="width: 130px">
        <el-option label="手工" value="MANUAL" />
        <el-option label="导入" value="IMPORT" />
        <el-option label="结转损益" value="CARRY_FORWARD" />
      </el-select>
      <el-button @click="load">查询</el-button>

      <span class="spacer" />

      <el-dropdown @command="onMore" v-if="store.canWrite">
        <el-button><el-icon><Operation /></el-icon>&nbsp;更多<el-icon><ArrowDown /></el-icon></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="organize" :icon="Sort">整理凭证</el-dropdown-item>
            <el-dropdown-item command="import" :icon="Upload">从 Excel 导入</el-dropdown-item>
            <el-dropdown-item command="sort" :icon="Sort" divided>按科目重排选中凭证的分录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>

      <el-button @click="printVouchers">
        <el-icon><Printer /></el-icon>&nbsp;打印预览
      </el-button>

      <template v-if="store.canWrite">
        <el-tooltip :content="postBlockedTip" placement="top" :disabled="!postBlocked">
          <el-button type="primary" :disabled="!selection.length || postBlocked" @click="post">记账</el-button>
        </el-tooltip>
        <el-button :disabled="!selection.length" @click="unpost">反记账</el-button>
        <el-button type="danger" plain :disabled="!selection.length" @click="removeAll">删除</el-button>
        <el-button type="primary" @click="$router.push('/vouchers/new')">新增凭证</el-button>
      </template>
    </div>

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
        <el-table-column type="selection" width="42" :selectable="() => store.canWrite" />
        <el-table-column prop="voucher_date" label="日期" width="105" />
        <el-table-column prop="voucher_no" label="凭证号" width="95" />
        <el-table-column label="摘要" min-width="230" show-overflow-tooltip>
          <template #default="{ row }">{{ firstSummary(row) }}</template>
        </el-table-column>
        <el-table-column label="科目行数" width="80" align="center">
          <template #default="{ row }">{{ row.entries.length }}</template>
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
        <el-table-column label="来源" width="92" align="center">
          <template #default="{ row }">
            <span v-if="row.source === 'CARRY_FORWARD'" class="muted">结转损益</span>
            <span v-else-if="row.source === 'IMPORT'" class="muted">导入</span>
            <span v-else class="muted">手工</span>
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
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, Operation, Printer, Sort, Upload } from '@element-plus/icons-vue'
import * as api from '@/api/voucher'
import { openPrint } from '@/api/print'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const router = useRouter()
const rows = ref([])
const total = ref(0)
const loading = ref(false)
const selection = ref([])
/** 这张凭证能不能改/删：期间已结账、或本身已记账，都锁死。 */
function isLocked(row) {
  return row.status === 'POSTED' || !!store.currentPeriod?.locked
}

/** 记账门槛：只有上一期结账了，本期才允许记账。
 *  ord 在账套内跨年度连续，所以 1 月的上一期自动就是上年 12 月。
 *  这里跟后端 post_vouchers 是同一条规矩，只是提前告诉用户，省得点了才报错。 */
const prevPeriod = computed(() => {
  const cur = store.currentPeriod
  if (!cur) return null
  return store.periods.find((p) => p.ord === cur.ord - 1) || null
})
const postBlocked = computed(() => !!prevPeriod.value && !prevPeriod.value.locked)
const postBlockedTip = computed(() =>
  postBlocked.value
    ? `请先在「期末处理」结账 ${prevPeriod.value.code}，${store.currentPeriod.code} 才能记账`
    : ''
)

const query = reactive({
  keyword: '', status: '', source: '',
  // 默认跟随顶栏选中的月份：进凭证页看到的就是「现在正在记的那个月」，
  // 不用每次都重新拉一遍日期范围
  from_period_id: null, to_period_id: null,
  page: 1, page_size: 50
})
// 顶栏期间变化时是否自动跟着走；用户手动改过起止或点了「全部期间」就停止跟随
const follow = ref(true)

function showAllPeriods() {
  follow.value = false
  query.to_period_id = null
  query.from_period_id = null
  query.page = 1
  load()
}

/** 把凭证范围对齐到顶栏选中的月份。 */
function syncToCurrentPeriod({ reload = true } = {}) {
  if (!follow.value || !store.periodId) return
  query.from_period_id = store.periodId
  query.to_period_id = store.periodId
  query.page = 1
  if (reload) load()
}

function firstSummary(row) {
  const s = row.entries.find((e) => e.summary)?.summary
  return s || row.remark || '—'
}

async function load() {
  if (!store.bookId) return
  loading.value = true
  try {
    const data = await api.queryVouchers(store.bookId, { ...query })
    rows.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function printVouchers() {
  const p = query.from_period_id || store.periodId
  openPrint(`/books/${store.bookId}/print/vouchers.html`, {
    period_id: p, status: query.status || undefined,
  })
}

async function onMore(cmd) {
  if (cmd === 'organize') return router.push('/vouchers/organize')
  if (cmd === 'import') return router.push('/vouchers/import')
  if (cmd === 'sort') {
    if (!selection.value.length) return ElMessage.warning('请先选中凭证')
    try {
      await ElMessageBox.confirm(
        `将按科目编码重新排列这 ${selection.value.length} 张凭证内的分录顺序，确定吗？`,
        '重排分录顺序', { type: 'warning' })
    } catch { return }
    for (const v of selection.value) await api.sortEntries(store.bookId, v.id)
    ElMessage.success('已按科目编码重排分录')
    await load()
  }
}

function open(row) {
  router.push(`/vouchers/${row.id}`)
}

async function post() {
  const r = await api.postVouchers(store.bookId, selection.value.map((x) => x.id))
  ElMessage.success(`已记账 ${r.posted} 张`)
  load()
}
async function unpost() {
  const r = await api.unpostVouchers(store.bookId, selection.value.map((x) => x.id))
  ElMessage.success(`已反记账 ${r.unposted} 张`)
  load()
}
async function removeOne(row) {
  if (isLocked(row)) {
    return ElMessage.warning(
      row.status === 'POSTED'
        ? `凭证 ${row.voucher_no} 已记账，不能删除；请先「反记账」`
        : `${row.voucher_no} 所在期间已结账，不能删除`)
  }
  await ElMessageBox.confirm(`确认删除凭证 ${row.voucher_no}？删除后凭证号可被后续凭证复用。`, '删除凭证', { type: 'warning' })
  await api.deleteVoucher(store.bookId, row.id)
  ElMessage.success('已删除')
  load()
}
async function removeAll() {
  const posted = selection.value.filter((v) => v.status === 'POSTED')
  if (posted.length) {
    return ElMessage.warning(
      `选中的 ${posted.length} 张凭证已记账，不能删除：${posted.slice(0, 5).map((v) => v.voucher_no).join('、')}`)
  }
  await ElMessageBox.confirm(`确认删除选中的 ${selection.value.length} 张凭证？`, '批量删除', { type: 'warning' })
  const r = await api.batchDelete(store.bookId, selection.value.map((x) => x.id))
  ElMessage.success(`已删除 ${r.deleted} 张`)
  load()
}

onMounted(() => {
  query.page = 1
  syncToCurrentPeriod()
})
watch(() => store.bookId, () => {
  query.page = 1
  selection.value = []
  syncToCurrentPeriod()
})
// 顶栏切月份，凭证列表跟着切
watch(() => store.periodId, () => syncToCurrentPeriod())
</script>
