<template>
  <!-- 计划任务：扩展声明的到点作业 + 系统自带作业，管理员在这里看和开关。 -->
  <div class="page">
    <div class="heading">
      <div><h2>计划任务</h2><p>到点自动执行的作业都在这里：扩展自己声明的（如每日备份、每月结转）和系统自带的。下面能看清现在开还是关、下次什么时候跑、上次跑成了什么样。</p></div>
      <div class="tools"><el-button :loading="loading || runsLoading" @click="refresh">刷新</el-button></div>
    </div>

    <h3 class="section">任务清单</h3>
    <el-table v-loading="loading" :data="tasks" row-key="task_key" class="task-table" empty-text="暂无计划任务">
      <el-table-column label="任务" min-width="235">
        <template #default="{ row }">
          <strong>{{ row.task_name }}</strong>
          <div class="muted">{{ row.plugin_id || '系统自带' }}</div>
          <div class="task-key" :title="row.task_key">{{ row.task_key }}</div>
        </template>
      </el-table-column>
      <el-table-column label="归属" width="118">
        <template #default="{ row }"><el-tag :type="row.owner_type === 'PLUGIN' ? '' : 'warning'" effect="plain">{{ ownerLabel(row.owner_type) }}</el-tag></template>
      </el-table-column>
      <el-table-column label="执行计划" min-width="190">
        <template #default="{ row }">
          <span>{{ scheduleText(row.schedule) }}</span>
          <div v-if="zoneOf(row.schedule)" class="muted">时区 {{ zoneOf(row.schedule) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="下次执行" width="150">
        <template #default="{ row }">{{ timeText(row.next_run_at) }}<div class="muted">{{ row.enabled ? '' : '已停用，不会执行' }}</div></template>
      </el-table-column>
      <el-table-column label="上次执行" width="150">
        <template #default="{ row }">{{ timeText(row.last_run_at) }}</template>
      </el-table-column>
      <el-table-column label="开关" width="150">
        <template #default="{ row }">
          <!-- 只有扩展任务能开关；系统任务只读，不给开关，免得点了必然 404。 -->
          <el-switch v-if="row.owner_type === 'PLUGIN'"
            :model-value="row.enabled"
            :loading="toggling === row.task_key"
            :disabled="busy || row.available === false"
            @change="value => toggle(row, value)" />
          <el-tag v-else type="warning" effect="plain">系统任务 · 只读</el-tag>
          <!-- available=false 不禁用整行：只锁那个开关，并把原因摆出来。 -->
          <div v-if="row.available === false" class="muted reason">{{ row.unavailable_reason || '扩展未安装或当前版本没有这个任务' }}</div>
          <div v-if="row.actionError" class="error-code">{{ row.actionError }}</div>
        </template>
      </el-table-column>
    </el-table>

    <h3 class="section">执行记录</h3>
    <p class="muted lead">最近 {{ runs.length }} 条，按开始时间倒序。扩展任务是一次触发、按持有授权的账号逐个跑，所以每条记录都能展开看是哪个用户跑的、结果如何。</p>
    <el-table v-loading="runsLoading" :data="runs" row-key="run_id" empty-text="还没有执行记录">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="expand">
            <div class="muted">这次触发按用户逐个执行，共 {{ (row.user_results || []).length }} 位用户</div>
            <el-table v-if="(row.user_results || []).length" :data="row.user_results" size="small" empty-text="没有逐用户结果">
              <el-table-column prop="user_id" label="用户 ID" width="110" />
              <el-table-column label="结果" width="130">
                <template #default="scope"><el-tag size="small" effect="plain" :type="statusTagType(scope.row.status)">{{ statusLabel(scope.row.status) }}</el-tag></template>
              </el-table-column>
              <el-table-column label="说明" min-width="260">
                <template #default="scope">
                  <span>{{ scope.row.message || '—' }}</span>
                  <div v-if="scope.row.error_code" class="error-code">{{ scope.row.error_code }}</div>
                </template>
              </el-table-column>
            </el-table>
            <p v-else class="muted">这条记录没有逐用户结果（系统任务通常不是按用户跑的）。</p>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="任务" min-width="235">
        <template #default="{ row }"><strong>{{ taskNameOf(row.task_key) }}</strong><div class="task-key" :title="row.task_key">{{ row.task_key }}</div></template>
      </el-table-column>
      <el-table-column label="结果" width="110">
        <template #default="{ row }"><el-tag :type="statusTagType(row.status)" effect="plain">{{ statusLabel(row.status) }}</el-tag></template>
      </el-table-column>
      <el-table-column label="计划时间" width="150"><template #default="{ row }">{{ timeText(row.scheduled_for) }}</template></el-table-column>
      <el-table-column label="开始 / 结束" width="160">
        <template #default="{ row }">{{ timeText(row.started_at) }}<div class="muted">{{ timeText(row.finished_at) }} · 用时 {{ durationText(row) }}</div></template>
      </el-table-column>
      <el-table-column label="说明" min-width="200">
        <template #default="{ row }"><span>{{ row.message || '—' }}</span><div v-if="row.error_code" class="error-code">{{ row.error_code }}</div></template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import * as api from '@/api/system'
import { formatDateTime } from '@/utils/datetime'

const tasks = ref([]), runs = ref([])
const loading = ref(false), runsLoading = ref(false), busy = ref(false), toggling = ref('')

const WEEK = { 0: '日', 1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '日' }

const ownerLabel = value => (value === 'PLUGIN' ? '扩展任务' : value === 'SYSTEM' ? '系统任务' : String(value ?? '未知'))
const STATUS_LABELS = { SUCCESS: '成功', FAILED: '失败', SKIPPED: '已跳过', RUNNING: '执行中' }
// 未知状态原样显示，不崩也不猜。
const statusLabel = value => STATUS_LABELS[value] || value || '—'
const statusTagType = value => (value === 'SUCCESS' ? 'success' : value === 'FAILED' ? 'danger' : value === 'RUNNING' ? 'warning' : 'info')

// ------------------------------------------------------------------
// 执行计划的中文化
//
// 后端只给结构化字段（cron 五段或 interval 分钟数），页面上要给人看。
// **任何一步认不出来就退回原始 JSON**：这里宁可丑，也不能抛异常把整页带崩。
// ------------------------------------------------------------------
const pad2 = n => String(n).padStart(2, '0')

function weekText(field) {
  return field.split(',').map(part => {
    if (WEEK[part] != null) return `周${WEEK[part]}`
    const range = part.match(/^([0-7])-([0-7])$/)
    if (range) return `周${WEEK[range[1]]}至周${WEEK[range[2]]}`
    const step = part.match(/^\*\/([1-9]\d*)$/)
    if (step) return `每 ${step[1]} 天`
    return null
  }).join('、') || null
}

function dayText(day, month, week) {
  const bits = []
  if (month !== '*') bits.push(`每年 ${month} 月`)
  if (day !== '*') bits.push(`每月 ${day} 号`)
  const w = week === '*' ? null : weekText(week)
  if (w) bits.push(w)
  return bits.join('，') || null
}

/** cron 五段 → 中文短语。认不出返回 null，由调用方降级。 */
function cronText(expression) {
  const parts = String(expression ?? '').trim().split(/\s+/)
  if (parts.length !== 5) return null
  const [minute, hour, day, month, week] = parts
  let when
  if (minute === '*' && hour === '*') when = '每分钟'
  else if (hour === '*') {
    const step = minute.match(/^\*\/([1-9]\d*)$/)
    when = step ? `每 ${step[1]} 分钟` : `每小时第 ${minute} 分钟`
  } else if (/^\d{1,2}$/.test(hour) && /^\d{1,2}$/.test(minute)) {
    const hm = `${pad2(hour)}:${pad2(minute)}`
    when = `每天 ${hm}`
  } else if (/^\d{1,2}-\d{1,2}$/.test(hour) && /^\d{1,2}$/.test(minute)) {
    const [from, to] = hour.split('-')
    when = `每天 ${pad2(from)}:${pad2(minute)} 至 ${pad2(to)}:${pad2(minute)}`
  } else return null
  const days = dayText(day, month, week)
  return days ? `${when}，${days}` : when
}

function scheduleText(schedule) {
  const fallback = () => (schedule == null ? '未登记' : JSON.stringify(schedule))
  if (!schedule || typeof schedule !== 'object') return fallback()
  if (schedule.type === 'cron') return cronText(schedule.expression) || fallback()
  if (schedule.type === 'interval') {
    const m = Number(schedule.minutes)
    return Number.isFinite(m) && m > 0 ? `每 ${m} 分钟` : fallback()
  }
  return fallback()
}

const zoneOf = schedule => (schedule && typeof schedule === 'object' ? schedule.timezone || '' : '')

// ------------------------------------------------------------------
// 时间
// ------------------------------------------------------------------
// 固定显示北京时间，不用 toLocaleString：后者按浏览器时区转换，
// 换个设备打开，同一次执行会显示成另一个时间。
function timeText(iso) {
  return iso ? formatDateTime(iso) : '—'
}

function durationText(row) {
  if (!row.finished_at || !row.started_at) return '—'
  const ms = new Date(row.finished_at) - new Date(row.started_at)
  if (!Number.isFinite(ms) || ms < 0) return '—'
  return ms < 60000 ? `${(ms / 1000).toFixed(1)} 秒` : `${Math.floor(ms / 60000)} 分 ${Math.round((ms % 60000) / 1000)} 秒`
}

const taskNameOf = key => String(key || '').split(':').pop() || key || '—'

// ------------------------------------------------------------------
// 开关失败时的错误文案
//
// 后端 detail 可能是字符串，也可能是 {code, message}。拦截器已经把可读的
// 那部分弹成 toast 了，这里**不再弹一次**，只把 code 补在行上（下面那行红字）。
// 无论如何都不能把 [object Object] 甩到界面上。
// ------------------------------------------------------------------
function detailText(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) return detail.map(d => d?.msg || JSON.stringify(d)).join('；')
  if (detail && typeof detail === 'object') return [detail.code, detail.message].filter(Boolean).join('：') || JSON.stringify(detail)
  return error?.message || '操作失败'
}

async function loadTasks() {
  if (loading.value) return
  loading.value = true
  try {
    tasks.value = await api.schedulerTasks()
  } catch { /* 具体错误由拦截器提示 */ }
  finally { loading.value = false }
}

async function loadRuns() {
  if (runsLoading.value) return
  runsLoading.value = true
  try {
    runs.value = await api.schedulerRuns(50)
  } catch { /* 具体错误由拦截器提示 */ }
  finally { runsLoading.value = false }
}

async function refresh() { await Promise.all([loadTasks(), loadRuns()]) }

async function toggle(row, value) {
  if (busy.value || row.available === false) return
  busy.value = true
  toggling.value = row.task_key
  const before = row.enabled
  row.enabled = value          // 先拨过去，请求回来再以服务端为准
  row.actionError = ''
  try {
    const result = await api.setSchedulerTask(row.plugin_id, row.task_name, value)
    row.enabled = result.enabled
    row.next_run_at = result.next_run_at
    ElMessage.success(value ? '已启用，到点会执行' : '已停用，到点不再执行')
  } catch (error) {
    row.enabled = before        // 弹回去，别让开关停在一个从没保存过的状态
    row.actionError = detailText(error)
    void loadTasks()            // 以服务端为准校准一遍
  } finally {
    busy.value = false
    toggling.value = ''
  }
}

let timer
onMounted(() => {
  void refresh()
  // 执行记录跟着时间往前走，定时补一次；busy 时不打断正在操作的行。
  timer = setInterval(() => { if (!busy.value) void loadRuns() }, 20000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.heading { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 18px; flex-wrap: wrap; }
h2 { margin: 0 0 8px; } p { color: var(--el-text-color-secondary); line-height: 1.7; }
.tools { display: flex; gap: 8px; flex-wrap: wrap; }
.section { margin: 24px 0 10px; font-size: 15px; } .lead { margin: 0 0 12px; }
.task-table { margin-bottom: 4px; }
.muted, .task-key { color: var(--el-text-color-secondary); font-size: 12px; margin-top: 5px; }
.reason { max-width: 240px; }
.task-key, .error-code { font-family: ui-monospace, monospace; }
.error-code { color: var(--el-color-danger); font-size: 12px; margin-top: 5px; }
.expand { padding: 4px 20px 12px 48px; } .expand .el-table { margin-top: 10px; }
</style>
