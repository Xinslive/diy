<template>
  <div class="page">
    <div class="page-head">
      <h2 class="page-title">期末处理</h2>
      <span class="muted">{{ store.currentBook?.company }} · {{ store.currentBook?.name }} {{ store.currentBook?.year }}</span>
    </div>

    <div class="grid">
      <div class="card">
        <h3>会计期间</h3>
        <el-table :data="store.periods" border size="small" highlight-current-row
                  :current-row-key="store.periodId" @current-change="(row) => row && store.setPeriod(row.id)">
          <el-table-column label="期间" width="90" prop="code" />
          <el-table-column label="凭证数" width="80" align="center" prop="vouchers" />
          <el-table-column label="未记账" width="80" align="center" prop="unposted" />
          <el-table-column label="状态" width="100" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.locked" type="success" size="small">已结账</el-tag>
              <el-tag v-else type="warning" size="small">未结账</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="结账人" prop="closed_by" />
        </el-table>
      </div>

      <div class="card actions">
        <h3>当前期间 {{ store.currentPeriod?.code }}</h3>

        <div class="act">
          <div class="act-title">1 · 结转损益</div>
          <p class="muted">把损益类科目余额结转到「本年利润」，生成记账凭证（默认未记账，需手工记账）。</p>
          <el-button type="primary" :disabled="!store.canWrite || store.currentPeriod?.locked" @click="doCarry">执行结转损益</el-button>
        </div>

        <div class="act">
          <div class="act-title">2 · 结账</div>
          <p class="muted">锁定期间。锁定后任何凭证的新增/修改/删除都会被数据库拒绝。</p>
          <el-button type="success" :disabled="!store.canWrite || store.currentPeriod?.locked" @click="doClose">
            结账 {{ store.currentPeriod?.code }}
          </el-button>
        </div>

        <div class="act">
          <div class="act-title">3 · 批量反结账</div>
          <p class="muted">
            从选定期间起，连续解锁到最后一个已结账期间，区间内已记账凭证自动回到未记账。
            结账越靠前影响范围越大，执行前请先看预览。
          </p>
          <el-button type="danger" plain :disabled="!store.canWrite" @click="previewUnclose">查看影响范围</el-button>
        </div>

        <div class="act">
          <div class="act-title">4 · 年度结转</div>
          <p class="muted">结转本年利润并生成下一年度账套，自动结转期初余额与账套授权。</p>
          <el-button :disabled="!store.canWrite" @click="doYearEnd">年度结转</el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()

async function doCarry() {
  const p = store.currentPeriod
  await ElMessageBox.confirm(
    `将 ${p.code} 损益类科目余额结转到「本年利润」，生成记账凭证。损益科目当前有余额时才能执行。`,
    '结转损益', { type: 'info' }
  )
  const r = await api.carryForward(store.bookId, p.id)
  await store.loadPeriods()
  ElMessage.success(`已生成 ${r.vouchers.join('、')}`)
}

async function doClose() {
  const p = store.currentPeriod
  await ElMessageBox.confirm(
    `结账 ${p.code}？结账后该期间凭证不可再改。前面若有未结账期间会一并结账。`,
    '结账', { type: 'warning' }
  )
  const r = await api.closePeriod(store.bookId, p.id)
  await store.loadPeriods()
  const extra = r.auto_closed_periods?.length ? `，顺带结账 ${r.auto_closed_periods.join('、')}` : ''
  ElMessage.success(`${p.code} 已结账${extra}`)
}

async function previewUnclose() {
  const p = store.currentPeriod
  const r = await api.unclosePreview(store.bookId, p.id)
  if (!r.periods.length) return ElMessage.info('所选期间及其之后没有已结账的期间，无需反结账')

  try {
    await ElMessageBox.confirm(
      `将从 ${r.periods[0]} 起连续解锁 ${r.periods.length} 个期间：` +
      `${r.periods.join('、')}。\n\n其中 ${r.will_unpost_vouchers} 张已记账凭证会回到未记账状态，` +
      `改完凭证后需要重新记账。`,
      '批量反结账预览',
      {
        type: 'warning',
        confirmButtonText: '确认反结账',
        cancelButtonText: '取消',
        dangerouslyUseHTMLString: false
      }
    )
  } catch {
    return   // 用户点了取消
  }

  const done = await api.unclose(store.bookId, p.id, true)
  await store.loadPeriods()
  ElMessage.success(`已解锁 ${done.periods.length} 个期间，${done.unposted_vouchers} 张凭证转未记账`)
}

async function doYearEnd() {
  await ElMessageBox.confirm(
    '年度结转会生成下一年度账套并结转期初余额。若本年最后一个月已结账，系统会自动解锁补做结转本年利润再重新结账。',
    '年度结转', { type: 'warning' }
  )
  const r = await api.yearEnd(store.bookId)
  await store.loadBooks()
  ElMessage.success(`已生成 ${r.new_year} 年度账套，结转 ${r.carried_subjects} 个科目期初余额`)
}
</script>

<style scoped>
.grid { display: grid; grid-template-columns: 1.1fr .9fr; gap: 14px; align-items: start; }
h3 { margin: 0 0 12px; font-size: 15px; }
.act { padding: 12px 0; border-bottom: 1px dashed var(--cwq-border); }
.act:last-child { border-bottom: none; }
.act-title { font-weight: 600; margin-bottom: 4px; }
.act p { margin: 0 0 10px; font-size: 12px; line-height: 1.6; }
</style>
