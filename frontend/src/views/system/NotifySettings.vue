<template>
  <div class="page">
    <div class="page-head">
      <h2 class="page-title">邮件通知</h2>
      <span class="muted">
        账号锁定、定时任务失败、备份失败时，系统会往下面配置的邮箱发一封告警。改完即生效，不必重启。
      </span>
    </div>

    <el-alert
      v-if="!form.enabled" type="warning" :closable="false" show-icon class="mb"
      title="通知当前处于关闭状态：配置还在，但系统不会真的发信。"
    />

    <div class="card">
      <el-form :model="form" label-width="110px" class="notify-form">
        <el-form-item label="接收告警">
          <el-switch v-model="form.enabled" :disabled="!isAdmin" />
          <span class="muted form-hint">
            关掉之后配置仍然保留，随时可以再打开。告警发不出去**不会**影响业务本身。
          </span>
        </el-form-item>

        <el-form-item label="收件邮箱">
          <el-input
            v-model="form.recipients" type="textarea" :rows="2"
            placeholder="admin@example.com，ops@example.com"
            :disabled="!isAdmin"
          />
          <span class="muted form-hint">
            多个地址用逗号或换行分隔。写错的地址会被跳过（不会拖累其它地址），
            当前有效收件人 {{ form.recipient_count }} 个。收件人之间互相看不到
            对方的地址（第一个明送，其余密送）。
          </span>
        </el-form-item>

        <el-divider content-position="left">SMTP 服务器</el-divider>

        <el-form-item label="服务器">
          <el-input v-model="form.host" placeholder="smtp.exmail.qq.com" :disabled="!isAdmin" />
        </el-form-item>

        <el-form-item label="端口">
          <el-input-number v-model="form.port" :min="1" :max="65535" :step="1" controls-position="right" style="width: 140px" :disabled="!isAdmin" />
          <span class="muted form-hint">465 = 隐式 TLS，587 = STARTTLS，25 = 明文</span>
        </el-form-item>

        <el-form-item label="加密方式">
          <el-radio-group v-model="securityMode" :disabled="!isAdmin">
            <el-radio value="ssl">SSL（465，握手即加密）</el-radio>
            <el-radio value="tls">STARTTLS（587，握手后升级）</el-radio>
            <el-radio value="none">不加密（仅限内网网关）</el-radio>
          </el-radio-group>
          <span class="muted form-hint">
            两者只能选一个。同时开会让服务器在最远的一次连接上直接报错，而报错现场极难复现。
          </span>
        </el-form-item>

        <el-form-item label="登录账号">
          <el-input v-model="form.username" placeholder="notice@example.com" :disabled="!isAdmin" />
        </el-form-item>

        <el-form-item label="登录口令">
          <el-input
            v-model="passwordInput" type="password" show-password autocomplete="off"
            :placeholder="form.password_set ? '留空表示不修改' : '填写 SMTP 登录口令'"
            :disabled="!isAdmin"
          />
          <span class="muted form-hint">
            出于安全考虑，已配置的口令不再回显，也找不回来 —— 只能覆盖重填。
          </span>
        </el-form-item>

        <el-form-item label="发件人">
          <el-input v-model="form.from_address" placeholder="留空则用登录账号" :disabled="!isAdmin" />
        </el-form-item>

        <el-form-item label="超时">
          <el-input-number v-model="form.timeout_seconds" :min="1" :max="60" :step="1" controls-position="right" style="width: 140px" :disabled="!isAdmin" />
          <span class="unit">秒</span>
          <span class="muted form-hint">发不出去必须很快地失败 —— 通知是旁路，不该拖住业务。</span>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="saving" :disabled="!isAdmin" @click="save">保存</el-button>
          <el-button :loading="testing" :disabled="!isAdmin" @click="sendTest">发送测试邮件</el-button>
          <el-button v-if="form.password_set" :disabled="!isAdmin" @click="clearPassword">清除口令</el-button>
        </el-form-item>
      </el-form>
    </div>

    <div class="card">
      <div class="card-title">会发哪些通知</div>
      <ul class="event-list">
        <li><b>账号锁定</b> —— 连续登录失败触发锁定时，发出账号名、来源 IP、锁多久、第几轮。</li>
        <li><b>定时任务失败</b> —— 计划任务运行失败时发出任务名、错误码与错误信息。</li>
        <li><b>计划任务没跑</b> —— 任务到点却没人来领时发出（通常是外部调度服务没在调 tick）。</li>
        <li><b>备份失败</b> —— 账套备份失败时发出。备份是出事后的退路，它失败通常发生在没人盯着的凌晨。</li>
        <li><b>备份脚本失败</b> —— systemd 定时备份（本机 .sql / OSS）失败时发出，附退出码。</li>
      </ul>
      <p class="muted note">
        这几件事也都会写进「操作日志」。区别在于：操作日志要人去翻，邮件是当时就到。
        邮件发不出去时不影响业务本身，只在服务端日志里留一条记录。
      </p>
      <p class="muted note">
        同一类告警短时间内发得太多时会自动限流（防止有人恶意刷告警把邮箱灌满）。
        被限流的那几条不会单独发信，但会在下一封通知里补一句「另有 N 条未单独通知」——
        完整记录始终在「操作日志」里。
      </p>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '@/api/system'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const isAdmin = computed(() => store.isAdmin)

const saving = ref(false)
const testing = ref(false)
// 口令只往这一个变量里流，保存完立刻清空，不留在响应式状态里等着被人翻。
const passwordInput = ref('')

const form = reactive({
  host: '', port: 25, username: '', from_address: '',
  use_ssl: false, use_tls: true, timeout_seconds: 10,
  recipients: '', recipient_count: 0, enabled: true,
  password_set: false, password_hint: '',
})

// 加密方式用**单选**而不是两个开关：两个开关能同时打开，而「同时开」正是
// 最容易配错、报错现场又最难复现的那个组合。映射到后端的 use_ssl/use_tls
// 两个字段时只有一个会为真。
const securityMode = computed({
  get() {
    if (form.use_ssl) return 'ssl'
    if (form.use_tls) return 'tls'
    return 'none'
  },
  set(value) {
    form.use_ssl = value === 'ssl'
    form.use_tls = value === 'tls'
  },
})

async function load() {
  Object.assign(form, await api.getNotifyConfig())
}

async function save() {
  saving.value = true
  try {
    const payload = {
      host: form.host,
      port: form.port,
      username: form.username,
      from_address: form.from_address,
      use_ssl: form.use_ssl,
      use_tls: form.use_tls,
      timeout_seconds: form.timeout_seconds,
      recipients: form.recipients,
      enabled: form.enabled,
    }
    // 口令留空就是「不改」。后端不接受「空串 = 清空」，所以这里
    // 空值干脆不往上传，天然不会误清。
    if (passwordInput.value) payload.password = passwordInput.value
    Object.assign(form, await api.saveNotifyConfig(payload))
    passwordInput.value = ''
    ElMessage.success('邮件通知配置已保存')
  } finally { saving.value = false }
}

async function clearPassword() {
  await ElMessageBox.confirm(
    '清除后系统将无法登录 SMTP 服务器，告警邮件会发不出去（业务本身不受影响）。确定清除吗？',
    '清除登录口令', { type: 'warning', confirmButtonText: '清除', cancelButtonText: '取消' },
  )
  saving.value = true
  try {
    Object.assign(form, await api.saveNotifyConfig({ clear_password: true }))
    passwordInput.value = ''
    ElMessage.success('登录口令已清除')
  } finally { saving.value = false }
}

async function sendTest() {
  testing.value = true
  try {
    const result = await api.testNotify()
    ElMessage.success(`测试邮件已发送给 ${result.recipients} 个收件人`)
  } catch (e) {
    // 失败原因由后端给出（连接、认证、收件人被拒…），原样透出比
    // 「发送失败」四个字有用得多。
    //
    // **常驻显示，不自动消失。** 这条文案是配 SMTP 的人**唯一的**诊断
    // 渠道，而后端给的是一句带处置方向的长句（「服务器不支持 STARTTLS…
    // 465 请选 SSL、587 请选 STARTTLS…」）。默认 3 秒的提示根本来不及读
    // —— 而没读到就等于没有这条提示。
    //
    // （全局响应拦截器也会弹一次同样的文案。那是全站既有的行为
    // —— 许多页面都靠它兜底，所以这里不去动它。）
    ElMessage.error(e?.response?.data?.detail || '测试发送失败',
      { duration: 0, showClose: true })
  } finally { testing.value = false }
}

onMounted(load)
</script>

<style scoped>
.notify-form { max-width: 760px; }
/* .mb 不在全局样式里（别的页面都是各自 scoped 定义），这里补一个。 */
.mb { margin-bottom: 14px; }
.form-hint { margin-left: 12px; font-size: 12px; }
.unit { margin-left: 6px; font-size: 13px; color: #64748b; }
.card-title { font-weight: 600; margin-bottom: 8px; }
.event-list { margin: 0; padding-left: 20px; line-height: 1.9; color: #475569; }
.note { margin-top: 10px; font-size: 12px; }
</style>
