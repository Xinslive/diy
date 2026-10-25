<template>
  <!-- 高危动作撞到「近期二次验证」闸门时弹出的引导。

       **为什么需要它**：闸门扩到 13 条受管控写路由之后，管理员在令牌
       超过宽限期时碰这些动作，只会收到一条 403 —— 界面上没有「去重新
       验证」的入口，撞死路，而且没有任何提示告诉他该做什么（审计 §3.2）。

       **自动重试**：验证通过之后，触发它的那次请求会被**自动重发一遍**
       （拦截器负责），管理员不用重新点一次那个按钮。 -->
  <el-dialog
    v-model="visible"
    title="该操作需要邮箱二次验证"
    width="440px"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
  >
    <div class="mra">
      <p class="mra-lead">
        出于安全考虑，这个操作需要管理员<strong>在近期完成邮箱二次验证</strong>。
        验证通过后，刚才那个操作会<strong>自动重试</strong>，不用重新点一遍。
      </p>

      <!-- 还没发信：只说明为什么要发这一封。 -->
      <template v-if="phase === 'idle'">
        <p class="mra-hint">点下面的按钮，验证邮件会发到通知邮箱里。</p>
      </template>

      <!-- 已发信：把「去哪儿、点什么、多久过期」一次说清。 -->
      <template v-else>
        <div class="mra-mail">
          <span class="mra-mail-label">验证链接已发送到</span>
          <span class="mra-mail-addr">{{ store.mfaReauth?.emailHint || '通知邮箱' }}</span>
        </div>

        <ol class="mra-steps">
          <li v-for="(step, i) in STEPS" :key="step">
            <span class="mra-step-n">{{ i + 1 }}</span>
            <span class="mra-step-text">{{ step }}</span>
          </li>
        </ol>

        <p v-if="left > 0" class="mra-expire" :class="{ warn: left <= 60 }">
          <el-icon><Clock /></el-icon>
          <span>验证链接 {{ left }} 秒后过期</span>
        </p>
      </template>

      <p v-if="errorText" class="mra-error">{{ errorText }}</p>

      <div class="mra-actions">
        <el-button v-if="phase === 'idle'" type="primary" :loading="busy" @click="send">
          发送验证邮件
        </el-button>
        <el-button v-else type="primary" :loading="polling" @click="pollNow">
          我已验证
        </el-button>

        <el-button
          v-if="phase !== 'idle'"
          :disabled="resendWait > 0"
          :loading="resending"
          @click="resend"
        >
          {{ resendWait > 0 ? `重新发送（${resendWait} 秒后可用）` : '重新发送' }}
        </el-button>

        <el-button :disabled="busy" @click="cancel">取消该操作</el-button>
      </div>
    </div>
  </el-dialog>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Clock } from '@element-plus/icons-vue'
import { useSessionStore } from '@/stores/session'
import { errorText as pickErrorText } from '@/utils/errorText'

const store = useSessionStore()

/** 编号是**顺序**不是并列**：先收信、再回本页，两件事有先后。 */
const STEPS = [
  '打开通知邮箱里的验证邮件',
  '点击邮件中的验证链接',
  '回到本页，验证会自动通过'
]

const visible = ref(false)
const busy = ref(false)
const polling = ref(false)
const resending = ref(false)
/** 本地记的错误（发信失败之类）—— 与 store 里那份分开：这份是**这一屏**的。 */
const localError = ref('')
/** 每秒跳一次的时钟，让倒计时真的会走（getter 里读 Date.now() 是不会触发的）。 */
const now = ref(Date.now())

const phase = computed(() => store.mfaReauth?.phase || 'idle')
const left = computed(() => store.mfaReauthExpiresIn(now.value))
const resendWait = computed(() => store.mfaReauthResendWait(now.value))
const errorText = computed(() => localError.value || store.mfaReauth?.error || '')

// store 里出现「待重新验证」⇒ 把对话框打开。
// 关掉对话框的唯一途径是「取消该操作」（见 cancel）—— 所以这里
// **不监听** visible 变 false 去取消流程：那会把「误点遮罩」也当成取消。
watch(() => store.mfaReauth, (v) => {
  if (v) {
    visible.value = true
    localError.value = ''
  }
}, { deep: true })

let timer = null
function startTimer() {
  if (timer) return
  timer = setInterval(() => {
    now.value = Date.now()
    // 自动轮询：**验证通过之后要自动重试原动作**，管理员不该盯着屏幕
    // 手动点「我已验证」。4 秒一次：服务端最短间隔是 2 秒，10 分钟的
    // 链接也就 150 次，远在 300 次的预算之内。
    if (phase.value === 'sent') void poll(false)
  }, 1000)
  timer?.unref?.()
}
function stopTimer() {
  if (timer) { clearInterval(timer); timer = null }
}
startTimer()
onUnmounted(stopTimer)

/**
 * 发一封验证邮件。
 *
 * 失败（通道没配、开关没开……）时**把原因留在这一屏里** ——
 * 只弹一个一闪而过的 toast 的话，管理员根本来不及读。
 */
async function send() {
  busy.value = true
  localError.value = ''
  try {
    await store.startMfaReauth()
  } catch (e) {
    localError.value = pickErrorText(e, '发送验证邮件失败')
  } finally {
    busy.value = false
  }
}

/**
 * 轮询一次挑战。
 *
 * @param {boolean} manual 是不是管理员手动点的「我已验证」
 *   （手动的那次要把 loading 态显示出来；自动轮询不闪按钮）
 */
async function poll(manual) {
  if (polling.value) return
  polling.value = true
  if (manual) busy.value = false
  try {
    const r = await store.pollMfaReauth()
    if (r.status === 'VERIFIED') {
      // 裁决交给 store：它会让拦截器去**自动重试**那个被拦下的动作。
      store.finishMfaReauth()
      visible.value = false
      ElMessage.success('二次验证已通过，正在继续刚才的操作')
    } else if (r.status === 'FAILED') {
      localError.value = r.error || '验证失败，请重新发送验证邮件'
    }
  } catch (e) {
    localError.value = pickErrorText(e, '验证失败')
  } finally {
    polling.value = false
  }
}

function pollNow() { void poll(true) }

/** 重新发送（链接丢了 / 没收到）。 */
async function resend() {
  resending.value = true
  localError.value = ''
  try {
    const r = await store.resendMfaReauth()
    if (!r.ok && r.reason === 'too_soon') {
      localError.value = `发送太频繁，请 ${r.wait} 秒后再试`
    } else if (!r.ok) {
      localError.value = '重新发送失败，请再试一次'
    }
  } catch (e) {
    localError.value = pickErrorText(e, '重新发送失败')
  } finally {
    resending.value = false
  }
}

/**
 * 取消：**原动作没有被执行**。
 *
 * 这句话要明确告诉管理员 —— 他刚才点的那个按钮（建号 / 改收件人 /
 * 签发令牌……）**没有**生效。闷声关掉对话框会让人以为已经做完了。
 */
function cancel() {
  visible.value = false
  store.cancelMfaReauth()
  ElMessage.warning('已取消该操作：它需要邮箱二次验证，未执行')
}
</script>

<style scoped>
.mra { display: flex; flex-direction: column; gap: 12px; }
.mra-lead { margin: 0; font-size: 13px; line-height: 1.8; color: #606266; }
.mra-hint { margin: 0; font-size: 13px; color: var(--cwq-muted); }

.mra-mail { display: flex; align-items: baseline; gap: 8px; }
.mra-mail-label { font-size: 12px; color: var(--cwq-muted); }
.mra-mail-addr { font-size: 14px; font-weight: 600; color: var(--cwq-text); }

.mra-steps { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; }
.mra-steps li { display: flex; align-items: flex-start; gap: 8px; font-size: 13px; line-height: 1.7; color: #606266; }
.mra-step-n {
  flex: 0 0 auto;
  width: 18px; height: 18px;
  border-radius: 50%;
  background: var(--cwq-primary, #409eff);
  color: #fff;
  font-size: 12px;
  line-height: 18px;
  text-align: center;
}
.mra-step-text { flex: 1; }

.mra-expire { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 12px; color: var(--cwq-muted); }
.mra-expire.warn { color: var(--cwq-warn); }

.mra-error {
  margin: 0;
  padding: 8px 10px;
  border-radius: 4px;
  background: var(--el-color-danger-light-9, #fef0f0);
  color: var(--el-color-danger, #f56c6c);
  font-size: 13px;
  line-height: 1.7;
  /* 失败原因**常驻**：默认 3 秒的 toast 根本来不及读。 */
}

.mra-actions { display: flex; flex-direction: column; gap: 10px; }
.mra-actions :deep(.el-button) { width: 100%; margin-left: 0; }
</style>
