<template>
  <div class="page">
    <div class="page-head">
      <h2 class="page-title">操作日志</h2>
      <span class="muted">凭证号可回收复用，因此删除/修改都留了完整内容快照</span>
    </div>
    <div class="card">
      <el-table :data="logs" border size="small" v-loading="loading">
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作人" width="90" prop="username" />
        <el-table-column label="动作" width="90">
          <template #default="{ row }"><el-tag size="small" :type="typeOf(row.action)">{{ row.action }}</el-tag></template>
        </el-table-column>
        <el-table-column label="对象" width="120">
          <template #default="{ row }">{{ row.entity }}#{{ row.entity_id }}</template>
        </el-table-column>
        <el-table-column label="说明" min-width="260" prop="detail" />
        <el-table-column label="快照" width="80" align="center">
          <template #default="{ row }">
            <el-button v-if="row.before || row.after" link type="primary" @click="show(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="snapVisible" title="内容快照" width="720px">
      <pre class="snap">{{ snapshotText }}</pre>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'
import { formatDateTime } from '@/utils/datetime'

const store = useSessionStore()
const logs = ref([])
const loading = ref(false)
const snapVisible = ref(false)
const snapshot = ref(null)

const typeOf = (a) => ({ CREATE: 'success', UPDATE: 'warning', DELETE: 'danger', POST: 'success',
  UNPOST: 'info', CLOSE: 'success', UNCLOSE: 'danger', YEAR_END: 'primary',
  // 拆分凭证：一张拆成两张，是**改**也是**建**，所以单独给一个色 ——
  // 不然它落到默认的 info，混在一堆日志里认不出来。
  SPLIT: 'primary' }[a] || 'info')
const snapshotText = computed(() => JSON.stringify(snapshot.value, null, 2))
function show(row) { snapshot.value = { before: row.before, after: row.after }; snapVisible.value = true }

async function load() {
  if (!store.bookId) return
  loading.value = true
  try { logs.value = await api.auditLogs(store.bookId, 200) } finally { loading.value = false }
}
onMounted(load)
watch(() => store.bookId, load)
</script>

<style scoped>
.snap { max-height: 460px; overflow: auto; font-size: 12px; background: #f8fafc; padding: 12px; border-radius: 4px; }
</style>
