<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <span class="title">整理凭证</span>
          <span class="hint">
            断号检查只体检不改数据；重新编号前会先给出逐张对照表，确认后再落库。
          </span>
        </div>
      </template>

      <el-form :inline="true" class="toolbar">
        <el-form-item label="范围">
          <el-radio-group v-model="scope">
            <el-radio-button value="period">当前期间 {{ store.currentPeriod?.code }}</el-radio-button>
            <el-radio-button value="book">整个账套</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="方式">
          <el-select v-model="strategy" style="width: 210px">
            <el-option label="断号检查（只体检）" value="GAPS" />
            <el-option label="按当前顺序重新编号" value="RENUM" />
            <el-option label="按日期升序重排并编号" value="SORT_DATE" />
            <el-option label="按日期降序重排并编号" value="SORT_DATE_DESC" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="includeSystem">包含系统凭证（结转损益）</el-checkbox>
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
        v-if="strategy === 'GAPS'"
        type="info" :closable="false" show-icon class="mb"
        title="断号检查不会修改任何数据，只列出缺失的凭证号。"
      />
      <el-alert
        v-else type="warning" :closable="false" show-icon class="mb"
        title="改号会让已经打印出来的纸质凭证与系统对不上。请确认改号方案后再执行。"
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
          <el-tag v-else-if="strategy === 'GAPS'" type="success" size="small">号段连续</el-tag>
          <el-tag v-if="p.changed_count" type="warning" size="small">
            {{ p.changed_count }} 张需改号
          </el-tag>
        </div>

        <el-table
          v-if="strategy !== 'GAPS'" :data="p.changes" size="small" border max-height="420"
          :row-class-name="({ row }) => (row.changed ? 'row-changed' : '')"
        >
          <el-table-column prop="voucher_no" label="原凭证号" width="110" />
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
              <span class="muted">{{ sourceLabel(row.source) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="变化" width="90">
            <template #default="{ row }">
              <el-tag v-if="row.changed" type="warning" size="small">改号</el-tag>
              <span v-else class="muted">不变</span>
            </template>
          </el-table-column>
        </el-table>

        <el-table
          v-else :data="p.changes" size="small" border max-height="420"
        >
          <el-table-column prop="voucher_no" label="凭证号" width="110" />
          <el-table-column prop="voucher_date" label="日期" width="120" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'POSTED' ? 'success' : 'info'">
                {{ row.status === 'POSTED' ? '已记账' : '未记账' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="120">
            <template #default="{ row }"><span class="muted">{{ sourceLabel(row.source) }}</span></template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Select } from '@element-plus/icons-vue'
import * as voucherApi from '@/api/voucher'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const scope = ref('period')
const strategy = ref('GAPS')
const includeSystem = ref(false)
const plan = ref(null)
const loading = ref(false)
const applying = ref(false)

function sourceLabel(s) {
  return { MANUAL: '手工', IMPORT: '导入', CARRY_FORWARD: '结转损益' }[s] || s
}

function payload() {
  return {
    period_id: scope.value === 'period' ? store.periodId : null,
    strategy: strategy.value,
    include_system: includeSystem.value,
  }
}

async function preview() {
  loading.value = true
  try {
    plan.value = await voucherApi.organizePreview(store.bookId, payload())
    if (plan.value.total_changed === 0 && plan.value.total_gaps === 0) {
      ElMessage.success('号段连续，无需整理')
    } else if (strategy.value === 'GAPS' && plan.value.total_gaps > 0) {
      ElMessage.warning(`发现 ${plan.value.total_gaps} 个断号`)
    }
  } finally {
    loading.value = false
  }
}

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
.period-block { margin-bottom: 18px; }
.period-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.pcode { font-weight: 600; font-family: ui-monospace, monospace; }
.muted { color: #999; }
.new-no { color: var(--cwq-primary); font-weight: 600; }
.mono { font-family: ui-monospace, monospace; }
:deep(.row-changed) { background: #fdf6ec; }
</style>
