<template>
  <div class="page">
    <div class="page-head">
      <h2 class="page-title">数据安全</h2>
      <span class="muted">各账套的数据先压缩再备份到阿里云 OSS，按天/周/月分级保留</span>
    </div>

    <!-- ── 一、能不能备：开关与 OSS 参数 ─────────────────────────── -->
    <div class="card">
      <el-form :model="form" label-width="142px" class="ds-form">
        <el-form-item label="定时备份">
          <el-switch v-model="form.enabled" />
          <span class="muted form-hint">
            关掉之后下面这些参数照旧可用，只是每天定时跑的那一趟不再执行。
          </span>
        </el-form-item>

        <el-form-item label="备份范围">
          <el-switch v-model="form.include_system" />
          <span class="muted form-hint">
            除各账套外，另备一份系统数据（用户、账套授权、系统参数、全局打印/报表基线）。
            这些不属于任何一本账，只备账套的话全员账号与权限不在任何一份备份里。
          </span>
        </el-form-item>

        <el-form-item label="Endpoint">
          <el-input v-model="form.endpoint" placeholder="oss-cn-hangzhou.aliyuncs.com" />
          <span class="muted form-hint">
            写区域（<code>oss-cn-hangzhou.aliyuncs.com</code>）而不是裸的
            <code>xxx.oss.aliyuncs.com</code>，后者会解析到内网地址。带不带
            <code>https://</code> 都行。
          </span>
        </el-form-item>

        <el-form-item label="Bucket">
          <el-input v-model="form.bucket" placeholder="acct-backup" />
        </el-form-item>

        <el-form-item label="对象前缀">
          <el-input v-model="form.prefix" placeholder="acct-backup" />
          <span class="muted form-hint">
            备份对象放在 Bucket 下的这个前缀里，清理也只碰这一亩地。
          </span>
        </el-form-item>

        <el-form-item label="AccessKey ID">
          <el-input
            v-model="akIdInput" type="password" show-password autocomplete="off"
            :placeholder="form.access_key_id_set ? `已配置 ${form.access_key_id_hint}` : '粘贴 AccessKey ID'"
          />
          <span class="muted form-hint">
            与 Secret 作为一个凭据对，<b>只在写的时候存在</b>，之后读不出来，只能覆盖重填。
          </span>
        </el-form-item>

        <el-form-item label="AccessKey Secret">
          <el-input
            v-model="akSecretInput" type="password" show-password autocomplete="off"
            :placeholder="form.access_key_secret_set ? `已配置 ${form.access_key_secret_hint}` : '粘贴 AccessKey Secret'"
          />
          <el-button v-if="form.access_key_secret_set" link type="danger" @click="clearKey">清除</el-button>
          <span class="muted form-hint">
            建议用一个<b>只有上传与读取本前缀</b>权限的 AccessKey —— 备份不需要删除 Bucket 的权限。
          </span>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存</el-button>
          <el-button :loading="testing" @click="testConnection">测试连接</el-button>
          <el-button type="success" :loading="running" @click="runNow">立即备份一次</el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- ── 二、备多久：分级保留 ───────────────────────────────────── -->
    <div class="card">
      <h3 class="card-title">保留策略</h3>
      <p class="muted">
        每天备份一次、每周一次、每月一次；一份产物归到它够格的<b>最高那一档</b>，
        所以月初那次是永久档，不必再为同一天补一份 30 天后会被删掉的日备份。
      </p>
      <el-form :model="form" label-width="142px" class="ds-form keep-form">
        <el-form-item label="天备份">
          <el-input-number v-model="form.keep_daily_days" :min="1" :max="3650" controls-position="right" style="width: 150px" />
          <span class="unit">天</span>
          <span class="muted form-hint">默认 30 天。</span>
        </el-form-item>
        <el-form-item label="周备份">
          <el-input-number v-model="form.keep_weekly_days" :min="1" :max="36500" controls-position="right" style="width: 150px" />
          <span class="unit">天</span>
          <span class="muted form-hint">默认 365 天（一年）。</span>
        </el-form-item>
        <el-form-item label="月备份">
          <el-input value="永久保留" disabled />
          <span class="muted form-hint">
            这一档不可改，也不接受任何清理 —— 代码里没有删它的分支，
            而不是靠一个写很大的天数兜着。
          </span>
        </el-form-item>
      </el-form>
    </div>

    <!-- ── 三、到底备没备上 ───────────────────────────────────────── -->
    <div class="card">
      <div class="card-head">
        <h3 class="card-title">备份记录</h3>
        <div class="card-actions">
          <el-radio-group v-model="scopeFilter" size="small" @change="loadRuns">
            <el-radio-button label="all">全部</el-radio-button>
            <el-radio-button label="success">成功</el-radio-button>
            <el-radio-button label="failed">失败</el-radio-button>
          </el-radio-group>
          <el-button size="small" @click="loadRuns">刷新</el-button>
        </div>
      </div>

      <el-table :data="visibleRuns" border size="small" v-loading="loadingRuns" empty-text="还没有备份记录">
        <el-table-column label="完成时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.finished_at || row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="账套" min-width="180" prop="book_label">
          <template #default="{ row }">
            <el-tag v-if="row.scope === 'SYSTEM'" size="small" type="info">系统</el-tag>
            {{ row.book_label || '—' }}
          </template>
        </el-table-column>
        <el-table-column label="档位" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="TIER_TAG[row.tier]">{{ TIER_LABEL[row.tier] }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="80" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'success' ? 'success' : 'danger'">
              {{ row.status === 'success' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="行数" width="90" align="right" prop="row_count" />
        <el-table-column label="大小" width="100" align="right">
          <template #default="{ row }">{{ row.size_bytes ? fmtSize(row.size_bytes) : '—' }}</template>
        </el-table-column>
        <el-table-column label="OSS 对象" min-width="280">
          <template #default="{ row }">
            <code v-if="row.oss_key" class="oss-key">{{ row.oss_key }}</code>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="240">
          <template #default="{ row }">
            <span :class="row.status === 'failed' ? 'err' : 'muted'">{{ row.message || '—' }}</span>
          </template>
        </el-table-column>
      </el-table>

      <p class="muted footnote">
        校验和（SHA256）随每条记录一起存着，可在需要时对下载回来的归档做校验。
        <el-button link type="primary" @click="showChecksums = !showChecksums">
          {{ showChecksums ? '收起校验和' : '查看校验和' }}
        </el-button>
      </p>
      <el-table v-if="showChecksums && visibleRuns.length" :data="visibleRuns" size="small" border class="hash-table">
        <el-table-column label="账套" min-width="160" prop="book_label" />
        <el-table-column label="SHA256" min-width="380">
          <template #default="{ row }"><code class="oss-key">{{ row.checksum || '—' }}</code></template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '@/api/system'
import { formatDateTime } from '@/utils/datetime'

const TIER_LABEL = { daily: '天', weekly: '周', monthly: '月' }
const TIER_TAG = { daily: 'info', weekly: 'warning', monthly: 'success' }

const saving = ref(false)
const testing = ref(false)
const running = ref(false)
const loadingRuns = ref(false)
const runs = ref([])
const scopeFilter = ref('all')
const showChecksums = ref(false)

// AccessKey 只往这两个变量里流，保存完立刻清空，不留在响应式状态里等着被人翻。
const akIdInput = ref('')
const akSecretInput = ref('')

const form = reactive({
  endpoint: '', bucket: '', prefix: 'acct-backup',
  access_key_id_set: false, access_key_id_hint: '',
  access_key_secret_set: false, access_key_secret_hint: '',
  enabled: true, include_system: true,
  keep_daily_days: 30, keep_weekly_days: 365,
})

const visibleRuns = computed(() =>
  scopeFilter.value === 'all' ? runs.value : runs.value.filter((r) => r.status === scopeFilter.value))

// **必须走 formatDateTime，不能自己截断。** 后端把每条连接的会话时区钉在 UTC
// （见 app/core/db.py），接口回来的时间串一律是 UTC。这里要是直接
// `replace('T',' ').slice(0,19)`，显示出来的时间会比实际早 8 小时 ——
// 而计划任务页走的是 formatDateTime（北京时间），于是同一个备份在两个页面上
// 对不上号：计划任务显示「2026-10-25 03:47」，本页显示「2026-10-24 19:47」，
// 看着就像「任务跑了却没产出记录」。
//
// 差的不只是 8 小时，还是**一天**：每日备份的 cron 是北京时间 03:47
// （= UTC 前一天 19:47），所以它在前一页显示 10-25，在本页显示 10-24。
// 照着「成功记录」上的时间去本页找，自然什么都找不到。
const fmtTime = (t) => formatDateTime(t)

function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`
}

async function loadConfig() {
  Object.assign(form, await api.getBackupConfig())
  form.keep_daily_days = form.keep_days.daily
  form.keep_weekly_days = form.keep_days.weekly
}

async function loadRuns() {
  loadingRuns.value = true
  try { runs.value = await api.getBackupRuns(100) } finally { loadingRuns.value = false }
}

async function save() {
  saving.value = true
  try {
    const payload = {
      endpoint: form.endpoint,
      bucket: form.bucket,
      prefix: form.prefix,
      enabled: form.enabled,
      include_system: form.include_system,
      keep_daily_days: form.keep_daily_days,
      keep_weekly_days: form.keep_weekly_days,
    }
    // 两个 AccessKey 留空就是「不改」。后端不接受「空串 = 清空」，
    // 所以这里空值干脆不往上传，天然不会误清已有凭据。
    if (akIdInput.value) payload.access_key_id = akIdInput.value
    if (akSecretInput.value) payload.access_key_secret = akSecretInput.value
    const saved = await api.saveBackupConfig(payload)
    Object.assign(form, saved)
    form.keep_daily_days = saved.keep_days.daily
    form.keep_weekly_days = saved.keep_days.weekly
    akIdInput.value = ''
    akSecretInput.value = ''
    ElMessage.success('数据安全配置已保存')
  } finally { saving.value = false }
}

async function testConnection() {
  // 先把当前输入框里的内容存下去再测 —— 否则测的是磁盘上那份旧的，
  // 刚填完就点「测试」却因为用的是旧参数而失败，是最容易把人绕进去的一种。
  await save()
  testing.value = true
  try {
    const r = await api.testBackupConnection()
    ElMessage.success(r.message || '连接正常')
  } finally { testing.value = false }
}

async function clearKey() {
  await ElMessageBox.confirm(
    '清除后备份会立刻失败（第二天才会被发现）。确定清除 AccessKey 吗？',
    '清除 AccessKey', { type: 'warning', confirmButtonText: '清除', cancelButtonText: '取消' },
  )
  saving.value = true
  try {
    const saved = await api.saveBackupConfig({ clear_access_key: true })
    Object.assign(form, saved)
    akIdInput.value = ''
    akSecretInput.value = ''
    ElMessage.success('AccessKey 已清除')
  } finally { saving.value = false }
}

async function runNow() {
  await ElMessageBox.confirm(
    '现在会为每个账套导出一份压缩快照并上传到 OSS，账套较多时需要几分钟。确定开始吗？',
    '立即备份', { type: 'info', confirmButtonText: '开始', cancelButtonText: '取消' },
  )
  running.value = true
  try {
    const r = await api.triggerBackup()
    ElMessage.success(r.message || '已开始')
    // 备份在后台跑，这张表就是它的进度。轮询几轮就收工 —— 页面开着也不会一直转。
    poll()
  } finally { running.value = false }
}

let timer = null
function poll() {
  clearInterval(timer)
  let tries = 0
  timer = setInterval(async () => {
    tries += 1
    await loadRuns()
    // 最多轮询 40 次（每次 3 秒）。再久也没关系：记录留在表里，刷新页面能看到。
    if (tries >= 40) clearInterval(timer)
  }, 3000)
}

onMounted(() => { loadConfig(); loadRuns() })
</script>

<style scoped>
.ds-form { max-width: 760px; }
.keep-form { margin-top: 12px; }
.form-hint { margin-left: 12px; font-size: 12px; }
.unit { margin-left: 6px; font-size: 13px; color: #64748b; }
.card-title { margin: 0 0 6px; font-size: 15px; }
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.card-head .card-title { margin: 0; }
.card-actions { display: flex; gap: 10px; align-items: center; }
.oss-key { font-size: 12px; color: #475569; word-break: break-all; }
.err { color: #dc2626; font-size: 12px; }
.footnote { margin-top: 10px; font-size: 12px; }
.hash-table { margin-top: 8px; }
</style>
