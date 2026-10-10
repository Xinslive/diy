<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-select v-model="includeUnposted" style="width: 150px" @change="load">
        <el-option label="仅已记账凭证" :value="false" />
        <el-option label="含未记账凭证" :value="true" />
      </el-select>
      <el-checkbox v-model="onlyLeaf" label="仅显示明细科目" @change="load" />
      <span class="spacer" />
      <el-button @click="previewHtml">模板预览/打印</el-button>
      <el-button @click="doPrint">打印本页</el-button>
      <el-button type="primary" @click="exportXlsx">导出 Excel</el-button>
    </div>

    <div class="card">
      <div class="report-head">
        <h2 class="report-title">科目余额表</h2>
        <div class="report-sub">
          {{ store.currentBook?.company_name }} · {{ store.currentBook?.name }} {{ store.currentBook?.year }}
          · 期间 {{ data.period }}
        </div>
      </div>

      <div class="balance-check" :class="data.check?.closing_balanced ? 'ok' : 'bad'">
        <span v-if="data.check?.closing_balanced">试算平衡 ✓</span>
        <span v-else>试算不平 ✗ 借 {{ fmt(data.check?.total_closing_debit) }} / 贷 {{ fmt(data.check?.total_closing_credit) }}</span>
        <span class="muted">本期发生额 {{ fmt(data.check?.total_debit) }} / {{ fmt(data.check?.total_credit) }}</span>
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
        <el-table-column label="本期借方" width="120" align="right">
          <template #default="{ row }">{{ fmt(row.period_debit) }}</template>
        </el-table-column>
        <el-table-column label="本期贷方" width="120" align="right">
          <template #default="{ row }">{{ fmt(row.period_credit) }}</template>
        </el-table-column>
        <el-table-column label="本年借方" width="120" align="right">
          <template #default="{ row }">{{ fmt(row.ytd_debit) }}</template>
        </el-table-column>
        <el-table-column label="本年贷方" width="120" align="right">
          <template #default="{ row }">{{ fmt(row.ytd_credit) }}</template>
        </el-table-column>
        <el-table-column label="期末方向" width="76" align="center" prop="direction" />
        <el-table-column label="期末余额" width="130" align="right">
          <template #default="{ row }"><b>{{ fmt(row.closing) }}</b></template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import * as api from '@/api/ledger'
import { downloadFile, openPrint } from '@/api/print'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const data = ref({ rows: [], check: {} })
const loading = ref(false)
const includeUnposted = ref(false)
const onlyLeaf = ref(true)

const visibleRows = computed(() => (onlyLeaf.value ? data.value.rows.filter((r) => r.is_leaf) : data.value.rows))

async function load() {
  if (!store.bookId || !store.periodId) return
  loading.value = true
  try {
    data.value = await api.balance(store.bookId, {
      period_id: store.periodId, include_unposted: includeUnposted.value
    })
  } finally {
    loading.value = false
  }
}

function previewHtml() {
  openPrint(`/books/${store.bookId}/print/balance.html`, {
    period_id: store.periodId, include_unposted: includeUnposted.value,
  })
}

function exportXlsx() {
  downloadFile(`/books/${store.bookId}/print/balance.xlsx`,
    { period_id: store.periodId, include_unposted: includeUnposted.value }, '科目余额表.xlsx')
}
function doPrint() { window.print() }

onMounted(load)
watch(() => [store.bookId, store.periodId], load)
</script>

<style scoped>
.report-head { text-align: center; margin-bottom: 10px; }
.report-title { margin: 0 0 4px; font-size: 19px; letter-spacing: 4px; }
.report-sub { font-size: 13px; color: var(--cwq-muted); }
.ledger :deep(.cell) { padding: 4px 8px; }
.balance-check { margin-bottom: 10px; }
</style>
