<template>
  <div class="page">
    <div class="page-head">
      <h2 class="page-title">操作日志</h2>
      <span class="muted">凭证号可回收复用，因此删除/修改都留了完整内容快照</span>
    </div>
    <!-- 加载失败**不能**与「没有日志」同形：断网/超时时表格会空着，
         一条提示都没有的话，用户以为这段时间真的一条操作都没有
         （审计 N3 / §3.25）。 -->
    <el-alert v-if="loadError" type="error" :closable="false" show-icon class="mb"
              :title="`加载失败：${loadError}。请检查网络后重试。`" />
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
import { useLatestLoad } from '@/composables/useLatestLoad'
import { useSessionStore } from '@/stores/session'
import { formatDateTime } from '@/utils/datetime'

const store = useSessionStore()
const logs = ref([])
/**
 * 只认最新一趟（见 composables/useLatestLoad.js 的文件头那份清单）。
 *
 * 这一页的触发口有两个：切账套（watch store.bookId）与页面自己的加载。
 * 切账套那一次靠 MainLayout 的 viewKey 整页重挂兜底就够了；
 * 但**页内**再触发一次加载时（连点、watch 与 onMounted 撞在一起），
 * 组件还活着 —— 旧响应写进来，表格里就是另一批日志。
 * 所以这里接上守卫：这是「页内加载点」的样板。
 */
const { loading, error: loadError, run, isStale } = useLatestLoad('system/audit')
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
  let d
  try {
    d = await run(() => api.auditLogs(store.bookId, 200))
  } catch {
    // 加载失败**不是**「没有日志」：这两件事在页面上长得一模一样。
    // 以前这里一声不吭：run() 把真错照原样往外抛，这里没人接 →
    // 变成一个没人管的 promise rejection，页面上 logs 保持旧值（空表）、
    // 零提示，用户于是以为这段时间真的一条操作都没有（审计 N3 / §3.25）。
    //
    // 拦截器已经弹过提示了，这里只要**接住这个错**：页面上 logs 保持原样，
    // 顶部由 loadError 挂一条「加载失败」。
    return
  }
  // 过期的那一趟：条件早被用户改掉了，写进去就是「表格里是另一批日志」
  if (isStale(d)) return
  logs.value = d
}
onMounted(load)
watch(() => store.bookId, load)
</script>

<style scoped>
.mb { margin-bottom: 12px; }
.snap { max-height: 460px; overflow: auto; font-size: 12px; background: #f8fafc; padding: 12px; border-radius: 4px; }
</style>
