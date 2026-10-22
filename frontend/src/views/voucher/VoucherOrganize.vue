<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <span class="title">整理凭证</span>
          <span class="hint">
            重新编号前会先给出逐张对照表，确认后再落库。结转损益的凭证固定排在最后两个号。
          </span>
        </div>
      </template>

      <el-form :inline="true" class="toolbar">
        <el-form-item label="方式">
          <el-select v-model="strategy" style="width: 210px">
            <el-option label="按日期升序重排并编号" value="SORT_DATE" />
            <!-- 「按当前顺序」的前提是当前顺序本身就对。时序不对还照着当前顺序压紧号，
                 等于把乱的号段固化成整齐的乱 —— 这种月份不给选。 -->
            <el-option label="按当前顺序重新编号" value="RENUM" :disabled="outOfOrder" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="preview">
            <el-icon><Search /></el-icon>&nbsp;查看方案
          </el-button>
          <el-button
            type="success" :disabled="!plan || plan.total_changed === 0" :loading="applying"
            @click="apply"
          >
            <el-icon><Select /></el-icon>&nbsp;确认执行
          </el-button>
        </el-form-item>
      </el-form>

      <el-alert
        type="warning" :closable="false" show-icon class="mb"
        title="改号会让已经打印出来的纸质凭证与系统对不上。请确认改号方案后再执行。"
      />
      <el-alert
        v-if="outOfOrder" type="error" :closable="false" show-icon class="mb"
        :title="`${check.period} 有凭证号和日期对不上（${orderIssueText}），不能「按当前顺序重新编号」，请改用「按日期升序重排并编号」。整理好之前，这个月也不能记账、不能结账。`"
      />
    </el-card>

    <el-card v-if="plan" shadow="never" class="mt">
      <template #header>
        <div class="card-head">
          <span class="title">整理结果</span>
          <el-tag v-if="plan.total_changed" type="warning">将修改 {{ plan.total_changed }} 张凭证</el-tag>
          <el-tag v-else type="info">无需修改</el-tag>
        </div>
      </template>

      <div v-for="p in plan.periods" :key="p.period_id" class="period-block">
        <div class="period-head">
          <span class="pcode">{{ p.period }}</span>
          <el-tag v-if="p.gaps.length" type="danger" size="small">
            断号 {{ p.gaps.length }} 个：{{ p.gaps.join('、') }}
          </el-tag>
          <el-tag v-else type="success" size="small">号段连续</el-tag>
          <el-tag v-if="p.changed_count" type="warning" size="small">
            {{ p.changed_count }} 张需改号
          </el-tag>
        </div>

        <el-table
          :data="p.changes" size="small" border max-height="420" class="centered"
          :row-class-name="({ row }) => (row.changed ? 'row-changed' : '')"
        >
          <el-table-column label="原凭证号" width="110">
            <template #default="{ row }">
              <span class="mono">{{ row.old_no }}</span>
            </template>
          </el-table-column>
          <el-table-column label="新凭证号" width="110">
            <template #default="{ row }">
              <span :class="row.changed ? 'mono new-no' : 'muted'">{{ row.new_no }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="voucher_date" label="日期" width="110" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'POSTED' ? 'success' : 'info'">
                {{ row.status === 'POSTED' ? '已记账' : '未记账' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="110">
            <template #default="{ row }">
              <span :class="row.source === 'CARRY_FORWARD' ? 'carry' : 'muted'">
                {{ sourceLabel(row.source) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="变化" width="120">
            <template #default="{ row }">
              <el-tag v-if="row.changed" type="warning" size="small">改号</el-tag>
              <el-tag v-else-if="row.status === 'POSTED'" type="info" size="small">已记账·不改号</el-tag>
              <span v-else class="muted">不变</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Select } from '@element-plus/icons-vue'
import * as voucherApi from '@/api/voucher'
import { useUrlConditions } from '@/composables/useUrlConditions'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
/** 默认按日期升序：用户点开这个页面多半就是嫌现在的号跟日期对不上 */
const strategy = ref('SORT_DATE')

/**
 * 整理方式 ↔ URL。
 *
 * 标签栏不做 keep-alive：切走再切回来就是组件重建，条件只活在组件里的话，
 * 回来必然是默认值 —— 那就是「查询参数被重置」这个毛病。所以条件住进 URL，
 * 标签栏记的完整地址带着它，回来还是那一种整理方式。
 */
const { restore } = useUrlConditions({
  strategy: { ref: strategy, type: 'str', def: 'SORT_DATE' },
})
const plan = ref(null)
const loading = ref(false)
const applying = ref(false)
/** 当前范围的「整理就绪」体检：断号 + 时序。决定「按当前顺序重新编号」能不能用。 */
const check = ref(null)
const outOfOrder = computed(() => !!(check.value?.time_out_of_order || []).length)
const orderIssueText = computed(() =>
  (check.value?.time_out_of_order || [])
    .slice(0, 3)
    .map((i) => `${i.prev_no}(${i.prev_date}) → ${i.voucher_no}(${i.voucher_date})`)
    .join('、'))

/** 「人工填制」比「手工」说清楚是谁记的：这张是人在账套里一笔一笔敲出来的 */
function sourceLabel(s) {
  return { MANUAL: '人工填制', IMPORT: '导入', CARRY_FORWARD: '结转损益' }[s] || s
}

/** 只整理顶栏那个期间。「整个账套」这个范围撤了：
 *  改号是会把已经打印出来的纸质凭证作废重印的操作，横跨十二个月一起改，
 *  没人会在看都没看全的时候点确认。范围还是由顶栏的年月决定。 */
function payload() {
  return { period_id: store.periodId, strategy: strategy.value }
}

async function loadCheck() {
  if (!store.bookId || !store.periodId) { check.value = null; return }
  try {
    check.value = await voucherApi.voucherPeriodCheck(store.bookId, store.periodId)
  } catch {
    check.value = null
  }
}

async function preview() {
  loading.value = true
  try {
    plan.value = await voucherApi.organizePreview(store.bookId, payload())
    if (plan.value.total_changed === 0 && plan.value.total_gaps === 0) {
      ElMessage.success('号段连续，无需整理')
    } else if (plan.value.total_gaps > 0 && plan.value.total_changed === 0) {
      ElMessage.warning(`发现 ${plan.value.total_gaps} 个断号`)
    }
  } finally {
    loading.value = false
  }
}

// 打开页面先体检一次；换了期间/账套要重测 —— 上个月的结论不能拿来管这个月
onMounted(() => {
  // 整理方式从 URL 复原（标签栏切回来时带着它）
  restore()
  loadCheck()
})
watch(() => [store.bookId, store.periodId], loadCheck)

async function apply() {
  if (!plan.value?.total_changed) return
  try {
    await ElMessageBox.confirm(
      `将修改 ${plan.value.total_changed} 张凭证的编号。改号后已经打印的纸质凭证会与系统不一致，确定继续吗？`,
      '确认整理凭证',
      { type: 'warning', confirmButtonText: '确认改号', cancelButtonText: '再想想' }
    )
  } catch {
    return
  }
  applying.value = true
  try {
    const res = await voucherApi.organize(store.bookId, payload())
    ElMessage.success(`已整理 ${res.total_changed} 张凭证`)
    await preview()
  } finally {
    applying.value = false
  }
}
</script>

<style scoped>
/* 「范围」撤了之后表单只剩一行，el-form-item 默认那 18px 下边距就在工具条底下
   单独留出一条空白带。表单已经是一行了，外边距换不来任何东西 */
:deep(.toolbar .el-form-item) { margin-bottom: 0; }
.mb { margin-bottom: 12px; }

/* 对照表是要一行一行核的：表头和内容都居中，视线从中间过一遍就能读完一整行 */
:deep(.centered th .cell),
:deep(.centered td .cell) { text-align: center; }
.period-block { margin-bottom: 18px; }
.period-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.pcode { font-weight: 600; font-family: ui-monospace, monospace; }
.muted { color: #999; }
.new-no { color: var(--cwq-primary); font-weight: 600; }
/* 结转损益那几张：它们会被排到号段末尾，标出来才看得出「为什么它排在最后」 */
.carry { color: #b88230; font-weight: 600; }
.mono { font-family: ui-monospace, monospace; }
:deep(.row-changed) { background: #fdf6ec; }
</style>
