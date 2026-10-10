<template>
  <div class="page">
    <div class="page-head">
      <h2 class="page-title">期末处理</h2>
      <span class="muted">
        {{ store.currentBook?.company_name }} · {{ store.currentBook?.name }} {{ store.currentPeriod?.code }}
      </span>
    </div>

    <div class="grid">
      <div class="card">
        <h3>会计期间 {{ store.currentYear }} 年</h3>
        <el-table ref="periodTable" :data="yearPeriods" border size="small" highlight-current-row
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
        <div v-if="store.yearsOfBook(store.currentBook).length > 1" class="years">
          <span class="muted">切年度</span>
          <el-radio-group :model-value="store.currentYear" size="small" @change="store.setYear">
            <el-radio-button v-for="y in store.yearsOfBook(store.currentBook)" :key="y" :value="y">
              {{ y }}
            </el-radio-button>
          </el-radio-group>
        </div>
      </div>

      <div class="card actions">
        <h3>当前期间 {{ store.currentPeriod?.code }}</h3>

        <div class="act">
          <div class="act-title">1 · 结转损益</div>
          <p class="muted">
            把损益类科目本期发生额结转到「本年利润」，生成记账凭证（默认未记账，结账时统一自动记账）。
            本期已有结转凭证时会<b>直接删掉、按最新余额重新生成</b>，不用手动清理。
          </p>
          <div v-if="carryInfo.has" class="carry-state">
            <el-tag type="success" size="small" effect="plain">本期已结转</el-tag>
            <span class="muted">凭证 {{ carryInfo.vouchers.join('、') }}{{ carryInfo.posted ? '（已记账）' : '（未记账）' }}</span>
          </div>
          <el-button type="primary" :loading="carrying" :disabled="!store.canWrite || store.currentPeriod?.locked" @click="doCarry">
            {{ carryInfo.has ? '重新结转损益' : '执行结转损益' }}
          </el-button>
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
          <p class="muted">
            把本年利润转到未分配利润，并在<strong>同一个账套</strong>里追加下一年度的 12 个会计期间，
            本年期末余额自动成为下一年 1 月的期初。科目表和账套授权都不用动。
          </p>
          <el-button :loading="yearEnding" :disabled="!store.canWrite" @click="doYearEnd">
            结转到 {{ store.currentYear + 1 }} 年
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch, onMounted, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const carryInfo = ref({ has: false, vouchers: [], posted: false })
const carrying = ref(false)
const yearEnding = ref(false)
const periodTable = ref(null)

// 期末处理只管当前年度：一次结一个年度，看别的年度先切年度
const yearPeriods = computed(() => store.periodsOfYear(store.currentYear))

// 期间换了年度后，把表格的高亮行滚到当前期间
watch([() => store.periodId, yearPeriods], async () => {
  await nextTick()
  const tbl = periodTable.value
  if (!tbl) return
  const i = yearPeriods.value.findIndex((p) => p.id === store.periodId)
  if (i >= 0) tbl.setCurrentRow(yearPeriods.value[i])
})

/**
 * 结转损益。重复执行是安全的：后端永远「先删旧凭证再重算」。
 * 确认框只在「要删已有结转凭证」时才提示删除清单 ——
 * 删之前必须让人知道删的是哪几张。
 */
async function doCarry() {
  const p = store.currentPeriod
  const already = carryInfo.value.has
  if (already) {
    await ElMessageBox.confirm(
      `${p.code} 已有结转凭证 ${carryInfo.value.vouchers.join('、')}。\n\n` +
      `继续将先删除它们，再按当前损益类科目余额重新生成结转凭证。` +
      `删除不可撤销（会记入操作日志）。`,
      '重新结转损益',
      { type: 'warning', confirmButtonText: '删除并重新结转', cancelButtonText: '取消' }
    )
  } else {
    await ElMessageBox.confirm(
      `将 ${p.code} 损益类科目余额结转到「本年利润」，生成记账凭证。损益科目本期有发生额时才能执行。`,
      '结转损益', { type: 'info' }
    )
  }

  carrying.value = true
  try {
    const r = await api.carryForward(store.bookId, p.id, true)
    await store.loadPeriods()
    await loadCarryInfo()
    if (r.replaced) {
      ElMessage.success(`已删除 ${r.removed.join('、')}，重新生成 ${r.vouchers.join('、')}`)
    } else {
      ElMessage.success(`已生成 ${r.vouchers.join('、')}`)
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '结转失败')
  } finally {
    carrying.value = false
  }
}

/** 本期已有哪些结转凭证 —— 决定按钮是「执行」还是「重新结转」。 */
async function loadCarryInfo() {
  const p = store.currentPeriod
  if (!p) return (carryInfo.value = { has: false, vouchers: [], posted: false })
  try {
    const r = await api.carryForwardInfo(store.bookId, p.id)
    carryInfo.value = r
  } catch {
    carryInfo.value = { has: false, vouchers: [], posted: false }
  }
}

async function doClose() {
  const p = store.currentPeriod
  await ElMessageBox.confirm(
    `结账 ${p.code}？结账后该期间凭证不可再改。前面若有未结账期间会一并结账。`,
    '结账', { type: 'warning' }
  )
  try {
    const r = await api.closePeriod(store.bookId, p.id)
    await store.loadPeriods()
    const extra = r.auto_closed_periods?.length ? `，顺带结账 ${r.auto_closed_periods.join('、')}` : ''
    const posted = r.auto_posted ? `，自动记账 ${r.auto_posted} 张` : ''
    ElMessage.success(`${p.code} 已结账${extra}${posted}`)
  } catch (e) {
    // 结账失败的十有八九是「损益还没结平」，原文里有科目和金额，别吞掉
    ElMessage.error(e?.response?.data?.detail || '结账失败')
    await store.loadPeriods()
  }
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

watch(() => store.periodId, loadCarryInfo)
onMounted(loadCarryInfo)

async function doYearEnd() {
  const from = store.currentYear
  await ElMessageBox.confirm(
    `结转到 ${from + 1} 年？\n\n` +
    `会在本账套内追加 ${from + 1} 年的 12 个会计期间，并把 ${from} 年期末余额写成 ${from + 1} 年 1 月的期初。` +
    `若 ${from} 年还有未结账的期间，会先拒绝并告诉你是哪几个月。`,
    '年度结转', { type: 'warning', confirmButtonText: `结转到 ${from + 1} 年` }
  )
  yearEnding.value = true
  try {
    const r = await api.yearEnd(store.bookId)
    await store.loadBooks()
    await store.loadPeriods()
    store.setYear(r.new_year)
    ElMessage.success(`已追加 ${r.new_year} 年度（${r.first_period} 起 12 期），结转 ${r.carried_subjects} 个科目期初余额`)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '年度结转失败')
  } finally {
    yearEnding.value = false
  }
}
</script>

<style scoped>
.grid { display: grid; grid-template-columns: 1.1fr .9fr; gap: 14px; align-items: start; }
.years { display: flex; align-items: center; gap: 8px; margin-top: 12px; font-size: 12px; }
h3 { margin: 0 0 12px; font-size: 15px; }
.act { padding: 12px 0; border-bottom: 1px dashed var(--cwq-border); }
.act:last-child { border-bottom: none; }
.act-title { font-weight: 600; margin-bottom: 4px; }
.act p { margin: 0 0 10px; font-size: 12px; line-height: 1.6; }
.carry-state { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; font-size: 12px; }
.tip { margin-left: 8px; font-size: 12px; }
</style>
