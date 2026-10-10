<template>
  <div class="page">
    <div class="toolbar no-print">
      <span class="muted">起</span>
      <el-select v-model="fromId" style="width: 120px" @change="load">
        <el-option v-for="p in store.periods" :key="p.id" :label="p.code" :value="p.id" />
      </el-select>
      <span class="muted">止</span>
      <el-select v-model="toId" style="width: 120px" @change="load">
        <el-option v-for="p in store.periods" :key="p.id" :label="p.code" :value="p.id" />
      </el-select>
      <el-checkbox v-model="leafOnly" label="仅明细科目" @change="load" />
      <span class="spacer" />
      <el-button @click="previewHtml">模板预览/打印</el-button>
      <el-button @click="window.print()">打印本页</el-button>
      <el-button type="primary" @click="exportXlsx">导出 Excel</el-button>
    </div>

    <div class="card">
      <div class="report-head">
        <h2 class="report-title">总 账</h2>
        <div class="report-sub">
          {{ store.currentBook?.company }} · {{ store.currentBook?.name }} {{ store.currentBook?.year }}
          · {{ data.from_period }} 至 {{ data.to_period }}
        </div>
      </div>
      <el-table :data="data.rows" border size="small" v-loading="loading">
        <el-table-column label="编码" width="90" prop="code" />
        <el-table-column label="科目名称" min-width="200">
          <template #default="{ row }"><span :style="{ paddingLeft: `${(row.level - 1) * 14}px` }">{{ row.name }}</span></template>
        </el-table-column>
        <el-table-column label="期初方向" width="72" align="center" prop="opening_direction" />
        <el-table-column label="期初余额" width="120" align="right"><template #default="{ row }">{{ fmt(row.opening) }}</template></el-table-column>
        <el-table-column label="本期借方" width="120" align="right"><template #default="{ row }">{{ fmt(row.period_debit) }}</template></el-table-column>
        <el-table-column label="本期贷方" width="120" align="right"><template #default="{ row }">{{ fmt(row.period_credit) }}</template></el-table-column>
        <el-table-column label="本年借方" width="120" align="right"><template #default="{ row }">{{ fmt(row.ytd_debit) }}</template></el-table-column>
        <el-table-column label="本年贷方" width="120" align="right"><template #default="{ row }">{{ fmt(row.ytd_credit) }}</template></el-table-column>
        <el-table-column label="期末余额" width="130" align="right"><template #default="{ row }"><b>{{ fmt(row.closing) }}</b></template></el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import * as api from '@/api/ledger'
import { generalXlsx, openPrint } from '@/api/print'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const data = ref({ rows: [] })
const loading = ref(false)
const fromId = ref(store.periodId)
const toId = ref(store.periodId)
const leafOnly = ref(true)

async function load() {
  if (!store.bookId) return
  loading.value = true
  try {
    data.value = await api.general(store.bookId, {
      from_period_id: fromId.value, to_period_id: toId.value, leaf_only: leafOnly.value
    })
  } finally {
    loading.value = false
  }
}
function previewHtml() {
  openPrint(`/books/${store.bookId}/print/general-ledger.html`, {
    period_id: toId.value, from_period_id: fromId.value || undefined,
  })
}

function exportXlsx() {
  window.open(generalXlsx(store.bookId, { period_id: toId.value }))
}

onMounted(() => { toId.value = store.periodId; load() })
watch(() => store.bookId, load)
</script>

<style scoped>
.report-head { text-align: center; margin-bottom: 12px; }
.report-title { margin: 0 0 4px; font-size: 19px; letter-spacing: 6px; }
.report-sub { font-size: 13px; color: var(--cwq-muted); }
</style>
