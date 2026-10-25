<template>
  <div class="page">
    <div class="page-head">
      <h2 class="page-title">管理员二次验证</h2>
      <span class="muted">
        管理员登录时除了口令，还要再确认一次身份。<b>这是可选的</b>：默认关闭，
        什么时候该开、什么时候不该开，下面写得很直白。
      </span>
    </div>

    <el-alert
      v-if="!isAdmin" type="info" :closable="false" show-icon class="mb"
      title="只有系统管理员可以查看和修改这一页。"
    />

    <!-- 开关开着但通道没就绪 —— 这是最要紧的那个分支：二次验证的邮件
         发不出去 ⇒ 管理员登不进来 ⇒ 管理面锁死。必须在界面上明说，
         并且劝管理员先把 SMTP 配好。 -->
    <el-alert
      v-if="config.enabled && !config.smtp_ready" type="error" :closable="false" show-icon class="mb"
      title="二次验证已开启，但验证邮件发不出去：SMTP 通道没有就绪。"
      :description="config.channel_error || '请到「邮件通知」配好 SMTP 服务器与收件人。'"
    />

    <el-alert
      v-else-if="!config.enabled" type="info" :closable="false" show-icon class="mb"
      title="二次验证当前是关闭的。"
      description="关闭时管理员只凭口令就能登录全部管理功能。这条路本身就是一条口令走到底。"
    />

    <div class="card">
      <el-form :model="config" label-width="120px" class="mfa-form">
        <el-form-item label="开启二次验证">
          <el-switch
            v-model="enabled"
            :loading="busy"
            :disabled="!isAdmin"
            @change="toggle"
          />
          <span class="muted form-hint">
            打开之后，管理员登录会多一步邮箱确认；改他人口令、授权、删账号、
            写 OSS 凭据、轮换调度密钥，以及<b>改动这个开关本身</b>，都要求
            「刚刚验证过」（{{ config.grace_minutes }} 分钟内）。
          </span>
        </el-form-item>

        <el-form-item label="当前状态">
          <el-tag :type="config.active ? 'success' : 'info'" size="small">
            {{ config.active ? '已开启且生效' : (config.enabled ? '已开启但未生效' : '未开启') }}
          </el-tag>
          <span class="muted form-hint">
            SMTP 通道：{{ config.smtp_ready ? '已就绪' : '未就绪' }} ·
            收件人 {{ config.recipient_count }} 个{{ config.recipient_hint ? `（${config.recipient_hint}）` : '' }}
          </span>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="busy" :disabled="!isAdmin" @click="save">
            保存
          </el-button>
          <el-button :disabled="!isAdmin" @click="load">刷新</el-button>
        </el-form-item>
      </el-form>
    </div>

    <div class="card">
      <div class="card-title">什么时候该开，什么时候不该开</div>
      <ul class="reason-list">
        <li>
          <b>该开</b>：管理面能改<b>别人的</b>口令、能给任何人授权账套、能读全部操作日志、
          能写 OSS 凭据（那个凭据能读走全公司账务数据）。只要这套系统暴露在内网之外，
          口令就是唯一的一道门 —— 而口令是最容易流出的那一种。
        </li>
        <li>
          <b>不该开</b>：<b>邮件通道还没配好</b>。验证链接发不出去，管理员一个都登不进来，
          唯一出口是运维跑脚本改库。系统会拒绝在通道没就绪时打开这个开关，
          但请先把「邮件通知」那一页配好。
        </li>
        <li>
          <b>开着的代价</b>：每次高危操作前要重新验证（默认 {{ config.grace_minutes }} 分钟内有效），
          管理员会多一次点击。邮箱被网关扫描、被手机锁屏预览时验证链接可能失效，
          这时会要求重新发一封 —— 属于可接受的麻烦，不是故障。
        </li>
        <li>
          <b>改这个开关也要刚刚验证过</b>：所以「开着」之后想关掉，<b>刚登录完</b>（那次登录
          带二次验证）就能改。超过验证窗口还想关，走运维脚本
          <code>python scripts/mfa_admin.py --disable</code> —— 那是明确关掉，
          与「假装开着」是两件完全不同的事。
        </li>
      </ul>
      <p class="muted note">
        开关存在库里（不是 .env），改完<b>立即生效</b>，不必重启后端；
        每一个开关都会记进「操作日志」。
      </p>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import * as api from '@/api/system'
import { useSessionStore } from '@/stores/session'
import { errorText } from '@/utils/errorText'

const store = useSessionStore()
const isAdmin = computed(() => store.isAdmin)

const busy = ref(false)
// 开关拨动即保存（与「用户与授权」页上的自助注册开关同一套交互）。
// 失败时回滚到后端确认过的那个状态，而不是停在「没保存上的假象」。
const enabled = ref(false)

const config = ref({
  enabled: false, active: false, grace_minutes: 5,
  smtp_ready: false, recipient_count: 0, recipient_hint: '',
  channel_error: '',
})

async function load() {
  const data = await api.getMfaConfig()
  config.value = { ...config.value, ...data }
  enabled.value = !!data.enabled
}

async function toggle(value) {
  busy.value = true
  try {
    const data = await api.setMfaConfig({ enabled: value })
    config.value = { ...config.value, ...data }
    enabled.value = !!data.enabled
    ElMessage.success(value ? '二次验证已开启' : '二次验证已关闭')
  } catch (e) {
    // 拨回去：让界面回到后端确认过的那个状态。
    enabled.value = !value
    ElMessage.error(errorText(e, '保存失败'))
  } finally {
    busy.value = false
  }
}

async function save() {
  busy.value = true
  try {
    const data = await api.setMfaConfig({ enabled: enabled.value })
    config.value = { ...config.value, ...data }
    enabled.value = !!data.enabled
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error(errorText(e, '保存失败'))
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.mfa-form { max-width: 760px; }
/* .mb / .page-head / .card 这些是全局样式里的，别的页面也是这么用的；
   这里只补本页面特有的。 */
.mb { margin-bottom: 14px; }
.form-hint { margin-left: 12px; font-size: 12px; }
.card-title { font-weight: 600; margin-bottom: 8px; }
.reason-list { margin: 0; padding-left: 20px; line-height: 1.9; color: #475569; }
.note { margin-top: 10px; font-size: 12px; }
</style>
