<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-select v-model="subjectId" filterable placeholder="选择科目" style="width: 320px" @change="load">
        <el-option v-for="s in subjects" :key="s.id" :label="`${s.code} ${s.name}`" :value="s.id" />
      </el-select>
      <span class="muted">起</span>
      <el-select v-model="fromId" style="width: 110px" @change="load">
        <el-option v-for="p in store.periods" :key="p.id" :label="p.code" :value="p.id" />
      </el-select>
      <span class="muted">止</span>
      <el-select v-model="toId" style="width: 110px" @change="load">
        <el-option v-for="p in store.periods" :key="p.id" :label="p.code" :value="p.id" />
      </el-select>
      <span class="spacer" />
      <el-button @click="previewHtml">模板预览/打印</el-button>
      <el-button @click="window.print()">打印本页</el-button>
    </div>

    <div class="card">
      <div class="report-head">
        <h2 class="report-title">明 细 账</h2>
        <div class="report-sub">
          {{ store.currentBook?.company }} · {{ data.subject?.code }} {{ data.subject?.name }}
          · {{ data.from_period }} 至 {{ data.to_period }}
        </div>
      </div>
      <el-table :data="data.rows" border size="small" v-loading="loading">
        <el-table-column label="日期" width="105"><template #default="{ row }">{{ row.voucher_date || '' }}</template></el-table-column>
        <el-table-column label="凭证号" width="95"><template #default="{ row }">{{ row.kind === 'ENTRY' ? row.voucher_no : '' }}</template></el-table-column>
        <el-table-column label="摘要" min-width="240"><template #default="{ row }">{{ row.summary || '' }}</template></el-table-column>
        <el-table-column label="借方" width="120" align="right"><template #default="{ row }">{{ fmt(row.debit) }}</template></el-table-column>
        <el-table-column label="贷方" width="120" align="right"><template #default="{ row }">{{ fmt(row.credit) }}</template></el-table-column>
        <el-table-column label="方向" width="60" align="center"><template #default="{ row }">{{ row.direction }}</template></el-table-column>
        <el-table-column label="余额" width="130" align="right"><template #default="{ row }"><b>{{ fmt(row.balance) }}</b></template></el-table-column>
        <template #empty><span class="muted">请先选择科目</span></template>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import * as api from '@/api/ledger'
import * as subjectApi from '@/api/subject'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const subjects = ref([])
const subjectId = ref(null)
const fromId = ref(store.periodId)
const toId = ref(store.periodId)
const data = ref({ rows: [] })
const loading = ref(false)

async function load() {
  if (!store.bookId || !subjectId.value) return
  loading.value = true
  try {
    data.value = await api.detail(store.bookId, {
      subject_id: subjectId.value, from_period_id: fromId.value, to_period_id: toId.value
    })
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  toId.value = store.periodId
  subjects.value = await subjectApi.subjectFlat(store.bookId)
  if (subjects.value.length) subjectId.value = subjects.value[0].id
  load()
})
watch(() => store.bookId, async () => {
  subjects.value = await subjectApi.subjectFlat(store.bookId)
  if (subjects.value.length) subjectId.value = subjects.value[0].id
  load()
})
function previewHtml() {
  openPrint(`/books/${store.bookId}/print/detail-ledger.html`, {
    subject_id: subjectId.value, period_id: store.periodId, from_period_id: fromId.value || undefined,
  })
}
</script>

<style scoped>
.report-head { text-align: center; margin-bottom: 12px; }
.report-title { margin: 0 0 4px; font-size: 19px; letter-spacing: 6px; }
.report-sub { font-size: 13px; color: var(--cwq-muted); }
</style>
