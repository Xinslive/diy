<template>
  <!-- 根节点跟其余 14 个页面一样用 .page：它带 16px 内边距，
       页面内容才不会紧贴侧边栏。原先这里写的是没有样式的
       plugin-manager，整页内容直接顶到侧边栏边上。 -->
  <div class="page">
    <div class="heading">
      <div><h2>扩展管理</h2><p>安装、运行状态和使用授权分别管理。安装后需重启服务，且默认没有可使用的用户。</p></div>
      <div class="tools"><el-button :loading="loading" @click="load">刷新状态</el-button><el-button :loading="busy" @click="openDiagnostics">恢复 / 诊断</el-button><el-button type="primary" :disabled="!enabled || busy" title="上传一个全新的扩展包；已安装的扩展要换版本，请在下方列表里点「升级」" @click="chooseFile()">安装扩展</el-button></div>
    </div>
    <el-alert v-if="!enabled" title="扩展平台未开启。请由运维配置受信任签名公钥及运行目录，再开启 PLUGIN_ENABLED 并重启服务。" type="info" :closable="false" show-icon />
    <el-alert v-else title="扩展在服务进程内运行。只安装经审查、由受信任发布者签名的代码；签名与扫描不能隔离恶意代码。" type="warning" :closable="false" show-icon />
    <input ref="fileInput" type="file" accept=".cwp" hidden @change="upload" />
    <el-table v-loading="loading" :data="plugins" row-key="id" class="plugin-table" empty-text="尚未安装扩展">
      <el-table-column label="扩展" min-width="220"><template #default="{ row }"><strong>{{ row.name }}</strong><div class="muted">{{ row.id }} · {{ row.version }}</div><div class="digest" :title="row.digest">{{ row.digest.slice(0, 16) }}</div></template></el-table-column>
      <el-table-column label="运行状态" min-width="175"><template #default="{ row }"><el-tag :type="statusType(row)">{{ row.deleted ? '已卸载，表保留' : statusLabel(row.runtime_status) }}</el-tag><div class="muted">期望：{{ row.desired_enabled ? '启用' : '禁用' }}</div><div v-if="row.error_code" class="error-code">{{ row.error_code }}</div></template></el-table-column>
      <el-table-column label="使用授权" width="135"><template #default="{ row }"><span>{{ row.user_count }} 人</span><el-button link type="primary" :disabled="row.deleted" @click="openUsers(row)">管理用户</el-button></template></el-table-column>
      <el-table-column label="业务表" width="100"><template #default="{ row }"><el-button link type="primary" @click="openTables(row)">{{ row.table_count }} 张</el-button></template></el-table-column>
      <el-table-column label="操作" min-width="255"><template #default="{ row }"><div class="row-tools" v-if="!row.deleted"><el-button size="small" :disabled="busy || !enabled" @click="toggle(row)">{{ row.desired_enabled ? '禁用' : '启用' }}</el-button><el-button size="small" :disabled="busy || !enabled || row.runtime_status !== 'DISABLED'" :title="upgradeHint(row)" @click="chooseFile(row)">升级</el-button><el-button size="small" type="danger" plain :disabled="busy || row.runtime_status !== 'DISABLED'" @click="remove(row)">卸载</el-button></div><el-button v-else size="small" type="danger" plain :disabled="busy || !row.table_count" @click="cleanup(row)">清理保留表</el-button><div v-if="row.runtime_status === 'PENDING_RESTART'" class="muted">等待运维重启全部 worker</div></template></el-table-column>
    </el-table>

    <el-dialog v-model="usersDialog" :title="`${target?.name || ''} · 使用授权`" width="700px" destroy-on-close>
      <p>只有以下名单中的用户可以使用此扩展。管理员也需单独授权；变更在下一次请求生效，无需重启。</p>
      <div class="selected-users"><el-tag v-for="user in selected.values()" :key="user.id" closable @close="selectUser(user, false)">{{ user.real_name || user.username }}<span v-if="!user.active">（已停用）</span></el-tag><span v-if="!selected.size" class="muted">尚未授权任何用户</span></div>
      <el-form @submit.prevent="searchUsers"><el-input v-model="query" clearable placeholder="搜索用户名或姓名" aria-label="搜索可授权用户" @keyup.enter="searchUsers"><template #append><el-button @click="searchUsers">搜索</el-button></template></el-input></el-form>
      <el-table v-loading="usersLoading" :data="eligible" row-key="id" empty-text="没有匹配的启用用户">
        <el-table-column label="授权" width="65"><template #default="{ row }"><el-checkbox :model-value="selected.has(row.id)" :aria-label="`授权 ${row.username}`" @change="value => selectUser(row, value)" /></template></el-table-column>
        <el-table-column prop="username" label="用户名" /><el-table-column prop="real_name" label="姓名" />
      </el-table>
      <el-pagination v-model:current-page="page" :page-size="30" :total="total" layout="prev, pager, next, total" @current-change="searchUsers(false)" />
      <template #footer><el-button @click="usersDialog = false">取消</el-button><el-button type="primary" :loading="busy" @click="saveUsers">保存授权名单</el-button></template>
    </el-dialog>

    <el-dialog v-model="tablesDialog" :title="`${target?.name || ''} · 业务表`" width="850px">
      <p v-if="backupPath" class="backup-path">最近的已校验备份：{{ backupPath }}</p>
      <el-table v-loading="tablesLoading" :data="tableRows" row-key="name" empty-text="没有保留的业务表">
        <el-table-column type="expand"><template #default="{ row }"><pre class="metadata">{{ JSON.stringify(row, null, 2) }}</pre></template></el-table-column>
        <el-table-column prop="name" label="表名" min-width="260" /><el-table-column label="范围" width="100"><template #default="{ row }">{{ row.scope === 'book' ? '账套' : '全局' }}</template></el-table-column><el-table-column prop="row_count" label="记录数" width="110" />
      </el-table>
      <p class="muted">卸载保留业务表。升级、卸载与清理前会生成并校验备份；恢复操作由运维按对应版本执行。</p>
    </el-dialog>

    <!-- 恢复 / 诊断：安装中断后的现场、安装尝试记录、备份区清理与密钥封禁。 -->
    <el-dialog v-model="diagDialog" title="恢复 / 诊断" width="820px" destroy-on-close>
      <p class="muted">「恢复」会处理安装中断留下的现场：已提交迁移的继续完成，未提交的隔离保留证据。正在运行的插件代码不会被远程停掉。</p>
      <div class="row-tools">
        <el-button size="small" :loading="diagBusy" @click="reconcile">执行恢复</el-button>
        <el-button size="small" :loading="diagBusy" @click="loadAttempts">刷新尝试记录</el-button>
        <el-button size="small" :loading="diagBusy" @click="pruneBackups">清理备份与隔离区</el-button>
      </div>
      <el-alert v-if="diagMessage" :title="diagMessage" type="info" :closable="false" show-icon class="diag-alert" />
      <el-table v-loading="diagLoading" :data="attempts" row-key="attempt_id" empty-text="没有安装尝试记录" class="attempt-table">
        <el-table-column prop="plugin_id" label="扩展" min-width="180" />
        <el-table-column prop="operation" label="操作" width="80" />
        <el-table-column prop="status" label="状态" width="110" />
        <el-table-column label="错误码" min-width="150"><template #default="{ row }">{{ row.error_code || '—' }}</template></el-table-column>
        <el-table-column prop="created_at" label="时间" min-width="180" />
      </el-table>
      <el-divider content-position="left">签名密钥封禁</el-divider>
      <p class="muted">撤销后，这把密钥签发的新包装不进来，已安装的插件在下次重启 worker 时也会被拒绝加载。运行中的代码需要重启才会停。</p>
      <div class="row-tools">
        <el-input v-model="revokeKeyId" placeholder="密钥标识（key_id）" style="max-width: 260px" />
        <el-button size="small" type="danger" plain :loading="diagBusy" :disabled="!revokeKeyId" @click="revokeKey">封禁并停用</el-button>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '@/plugins/api'
import { http } from '@/api/client'

const plugins = ref([]), enabled = ref(false), loading = ref(false), busy = ref(false)
const fileInput = ref(null), upgradeTarget = ref(null), target = ref(null)
const usersDialog = ref(false), usersLoading = ref(false), eligible = ref([]), selected = reactive(new Map())
const query = ref(''), page = ref(1), total = ref(0)
const tablesDialog = ref(false), tablesLoading = ref(false), tableRows = ref([]), backupPath = ref('')
const diagDialog = ref(false), diagBusy = ref(false), diagLoading = ref(false), diagMessage = ref('')
const attempts = ref([]), revokeKeyId = ref('')
const labels = { READY: '可用', DISABLED: '已禁用', PENDING_RESTART: '等待重启', LOADING: '正在加载', FAILED: '加载失败', ORPHANED: '需要检查' }
const statusLabel = value => labels[value] || value
const statusType = row => row.runtime_status === 'READY' ? 'success' : ['FAILED', 'ORPHANED'].includes(row.runtime_status) ? 'danger' : 'info'
async function load() {
  if (loading.value) return
  loading.value = true
  try { const result = await api.listPlugins(); plugins.value = result.plugins; enabled.value = result.enabled } catch { /* API 拦截器显示具体错误 */ }
  finally { loading.value = false }
}
function chooseFile(row = null) { upgradeTarget.value = row; fileInput.value.value = ''; fileInput.value.click() }
/** 升级按钮为什么灰着 —— 宿主要求换包前旧代码先停下来（停用 + 重启）。
 *  不说清楚的话，用户只会看到一只按不动的按钮，不知道下一步该做什么。 */
function upgradeHint(row) {
  if (row.runtime_status === 'DISABLED') return '上传新版本安装包'
  if (row.desired_enabled) return '要先点「禁用」并重启服务：旧代码还在跑的时候不能换包'
  return '等运维重启服务后，这个按钮才可点'
}
async function upload(event) {
  const file = event.target.files?.[0]
  if (!file) return
  if (!file.name.endsWith('.cwp')) { ElMessage.warning('请选择 .cwp 扩展包'); return }
  busy.value = true
  try { const result = await api.uploadPlugin(file, upgradeTarget.value?.id); ElMessage.success(result.status === 'disabled' ? '升级完成，扩展保持禁用，请检查授权后启用并重启' : '安装完成，等待重启；请单独设置使用授权'); await load() } catch { /* 已显示请求错误 */ }
  finally { busy.value = false }
}
async function toggle(row) {
  busy.value = true
  try { await api.pluginAction(row.id, row.desired_enabled ? 'disable' : 'enable'); ElMessage.success('已保存期望状态，请重启全部 API worker'); await load() } catch { /* 已显示请求错误 */ }
  finally { busy.value = false }
}
async function openUsers(row) {
  target.value = row; selected.clear(); query.value = ''; page.value = 1; usersDialog.value = true; usersLoading.value = true
  try { const result = await api.listPluginUsers(row.id); for (const user of result.users) selected.set(user.id, user); await searchUsers() } catch { usersDialog.value = false }
  finally { usersLoading.value = false }
}
function selectUser(user, value) { if (value) selected.set(user.id, user); else selected.delete(user.id) }
async function searchUsers(reset = true) {
  if (reset !== false) page.value = 1
  usersLoading.value = true
  try { const result = await api.listEligibleUsers(query.value, page.value); eligible.value = result.users; total.value = result.total } catch { /* 已显示请求错误 */ }
  finally { usersLoading.value = false }
}
async function saveUsers() {
  busy.value = true
  try { await api.savePluginUsers(target.value.id, [...selected.keys()]); ElMessage.success('授权名单已生效'); usersDialog.value = false; await load() } catch { /* 已显示请求错误 */ }
  finally { busy.value = false }
}
async function openTables(row) {
  target.value = row; tableRows.value = []; backupPath.value = row.backup_path || ''; tablesDialog.value = true; tablesLoading.value = true
  try { const result = await api.pluginTables(row.id); tableRows.value = result.tables; backupPath.value = result.backup_path } catch { /* 已显示请求错误 */ }
  finally { tablesLoading.value = false }
}
async function remove(row) {
  try { await ElMessageBox.confirm(`卸载「${row.name}」会撤销全部使用授权，保留业务表并先生成备份。`, '卸载扩展', { type: 'warning', confirmButtonText: '备份并卸载', cancelButtonText: '取消' }) } catch { return }
  busy.value = true
  try { await api.removePlugin(row.id); ElMessage.success('扩展已卸载，业务表已保留'); await load() } catch { /* 已显示请求错误 */ }
  finally { busy.value = false }
}
async function cleanup(row) {
  try { await ElMessageBox.prompt(`此操作会删除「${row.name}」保留的业务表。请输入完整扩展 id：${row.id}`, '备份并清理业务表', { type: 'warning', inputValidator: value => value === row.id || '扩展 id 不匹配', confirmButtonText: '备份并清理', cancelButtonText: '取消' }) } catch { return }
  busy.value = true
  try { await api.cleanupPlugin(row.id); ElMessage.success('已备份并清理保留表'); await load() } catch { /* 已显示请求错误 */ }
  finally { busy.value = false }
}

// —— 恢复 / 诊断 ——
// 安装中断后管理员需要三件事：把现场处理掉、看清每次安装尝试的结果、
// 把不再被引用的备份与隔离目录清掉。管理接口在 /api/system/plugins/* 下。
async function openDiagnostics() {
  diagDialog.value = true
  diagMessage.value = ''
  await loadAttempts()
}
async function loadAttempts() {
  diagLoading.value = true
  try { attempts.value = (await http.get('/system/plugins/attempts')).data.attempts } catch { attempts.value = [] }
  finally { diagLoading.value = false }
}
async function reconcile() {
  diagBusy.value = true
  try {
    const result = (await http.post('/system/plugins/reconcile')).data
    const done = (result.completed || []).length, held = (result.quarantined || []).length, bad = (result.unavailable || []).length
    diagMessage.value = `恢复完成：向前完成 ${done} 项，隔离保留 ${held} 项，仍需人工处理 ${bad} 项。`
    ElMessage.success('恢复已执行')
    await loadAttempts()
    await load()
  } catch { /* 已显示请求错误 */ }
  finally { diagBusy.value = false }
}
async function pruneBackups() {
  diagBusy.value = true
  try {
    const result = (await http.post('/system/plugins/maintenance/prune', { force: true })).data
    diagMessage.value = `已清理 ${result.removed.length} 个不再被引用的备份或隔离目录。`
    ElMessage.success('备份区已清理')
  } catch { /* 已显示请求错误 */ }
  finally { diagBusy.value = false }
}
async function revokeKey() {
  diagBusy.value = true
  try {
    const result = (await http.post('/system/plugins/revoke-key', { key_id: revokeKeyId.value.trim() })).data
    revokeKeyId.value = ''
    diagMessage.value = `密钥 ${result.key_id} 已封禁；受影响的扩展：${result.disabled_plugins.join('、') || '无'}。请重启全部 worker 让停用生效。`
    ElMessage.success('密钥已封禁')
    await load()
  } catch { /* 已显示请求错误 */ }
  finally { diagBusy.value = false }
}
let timer
onMounted(() => { void load(); timer = setInterval(() => { if (!busy.value) void load() }, 15000) })
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.heading { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 18px; flex-wrap: wrap; }
h2 { margin: 0 0 8px; } p { color: var(--el-text-color-secondary); line-height: 1.7; }
.tools, .row-tools { display: flex; gap: 8px; flex-wrap: wrap; } .row-tools :deep(.el-button) { margin-left: 0; }
.plugin-table { margin-top: 20px; } .muted, .digest { color: var(--el-text-color-secondary); font-size: 12px; margin-top: 5px; }
.digest, .error-code, .metadata { font-family: ui-monospace, monospace; } .error-code { color: var(--el-color-danger); font-size: 12px; margin-top: 5px; }
.selected-users { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 20px; } .el-pagination { margin-top: 16px; }
.metadata { white-space: pre-wrap; max-height: 320px; overflow: auto; padding: 16px; font-size: 12px; } .backup-path { overflow-wrap: anywhere; }
.attempt-table { margin-top: 14px; } .diag-alert { margin-top: 14px; }
</style>
