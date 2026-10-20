<template>
  <div class="login-page">
    <div class="login-box">
      <h1>财务记账系统</h1>
      <p class="muted">多账套 · 凭证 · 账簿 · 报表</p>

      <!-- ------------------------------------------------ 二次验证：等邮件 -->
      <!-- v-if 而不是 v-else 的一层：二次验证进行中时，登录表单整个收起 ——
           留着它等于让人在「等邮件」的半路上又点一次登录（那会再造一个
           挑战、把上一封邮件作废）。 -->
      <div v-if="store.mfa" class="mfa-box">
        <h2>请查收验证邮件</h2>
        <p class="muted">
          我们已经往 <strong>{{ store.mfa.emailHint || '通知邮箱' }}</strong>
          发了一封带验证链接的邮件。点开链接完成验证后，**回到这个窗口**
          点「我已验证」。
        </p>
        <p class="muted countdown">验证链接 {{ mfaLeft }} 秒后过期</p>
        <el-button type="primary" size="large" style="width: 100%"
                   :loading="polling" @click="submitPoll">
          我已验证
        </el-button>
        <el-button size="large" style="width: 100%; margin-top: 10px"
                   :disabled="resendWait > 0" :loading="resending" @click="submitResend">
          {{ resendWait > 0 ? `重新发送（${resendWait} 秒后可用）` : '重新发送验证邮件' }}
        </el-button>
        <el-button link type="info" class="exit-btn" @click="backToLogin">
          退出，重新登录
        </el-button>
      </div>

      <el-tabs v-else v-model="tab" stretch :class="{ 'tabs-single': !gate.showTabs }">
        <!-- ------------------------------------------------ 登录 -->
        <el-tab-pane label="登录" name="login">
          <el-form @submit.prevent="submitLogin">
            <el-form-item>
              <el-input v-model="username" placeholder="用户名" size="large" @keyup.enter="submitLogin" />
            </el-form-item>
            <el-form-item>
              <el-input v-model="password" type="password" placeholder="密码" size="large"
                        show-password @keyup.enter="submitLogin" />
            </el-form-item>
            <el-button type="primary" size="large" style="width: 100%" :loading="busy" @click="submitLogin">
              登录
            </el-button>
          </el-form>
          <!-- 演示账号提示**只在开发构建里出现**。
               它原先是无条件渲染的，于是生产站点 acct.xins.live 的登录页
               公开写着「演示账号 admin / 123456」——等于替攻击者
               念出管理员用户名，还顺带告诉他们这个站点的口令强度
               就是六个数字。import.meta.env.DEV 由 vite 在
               `vite build`（生产构建）时置为 false。

               实测（构建后查 dist）：`isDev` 这个标识符被完全折掉，
               产物里是 `username:""`，即预填也没有了；而那段提示文字
               **仍然留在 chunk 里** —— vue 的模板编译把 `v-if` 编成
               条件表达式，terser 不会把这个常量假分支删掉。
               它不会被渲染（运行时恒为 false），但「产物里搜不到
               演示口令」这句话是不成立的，别据它下结论。要连字面量
               一起消失，得把提示文字挪出组件（例如放进 .env.local
               之类的构建期变量），那是另一件事。 -->
          <p v-if="isDev" class="hint muted">演示账号 admin / 123456</p>
        </el-tab-pane>

        <!-- ------------------------------------------------ 注册 -->
        <!-- v-if 而不是只藏页签：管理员关掉自助注册后，页签栏里那个「注册」
             必须整个消失，而不是留在那儿让人点了撞 403。判定见
             utils/registration.js，后端闸门在 /api/auth/register。 -->
        <el-tab-pane v-if="gate.showRegister" label="注册" name="register">
          <el-form @submit.prevent="submitRegister">
            <el-form-item>
              <el-input v-model="reg.username" placeholder="手机号（11 位）"
                        size="large" @keyup.enter="submitRegister" />
            </el-form-item>
            <el-form-item>
              <el-input v-model="reg.password" type="password" placeholder="密码（至少 8 位）"
                        size="large" show-password @keyup.enter="submitRegister" />
            </el-form-item>
            <el-form-item>
              <el-input v-model="reg.confirm" type="password" placeholder="再输一次密码"
                        size="large" show-password @keyup.enter="submitRegister" />
            </el-form-item>
            <el-button type="primary" size="large" style="width: 100%" :loading="busy" @click="submitRegister">
              注册
            </el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as authApi from '@/api/auth'
import { useSessionStore } from '@/stores/session'
import { registrationView } from '@/utils/registration'

const store = useSessionStore()
const router = useRouter()

const tab = ref('login')
const busy = ref(false)

// 自助注册是否开放。**初值是 false**：接口还没回来之前（或者请求失败），
// 登录页只显示登录。反过来先显示再撤，页面会闪一下，而且网络抖动时可能闪完
// 就留在那儿 —— 一个没人确认过状态的安全相关入口，不该先亮出来。
const registrationEnabled = ref(false)
const gate = computed(() => registrationView(registrationEnabled.value, tab.value))

// 管理员把开关关掉时，正停在「注册」页的人要立刻被拉回登录页，
// 否则他会看到一张没有页签栏的空白注册表单。
watch(() => gate.value.tab, (t) => { if (t !== tab.value) tab.value = t })

// 开发构建才有的便利（预填演示账号 + 显示提示）。
// 生产构建里 isDev 恒为 false，于是：用户名不预填、口令不预填、提示不渲染。
//
// **口令原来无条件预填 `123456`。** 生产站点的登录框打开就是
// 「admin / ••••••」，点一下登录按钮就等于把这套弱口令递出去。
// 就算部署时把口令改掉了，这个预填仍然在替攻击者省掉输入成本，
// 而且任何一次「重置成默认值」都会直接变成一次公网沦陷。
const isDev = import.meta.env.DEV

const username = ref(isDev ? 'admin' : '')
const password = ref('')

// 自助注册的「用户名」就是手机号：11 位大陆号码（1 开头，第二位 3-9）。
//
// 这条**只是前端的约定，不是服务端强制的**：后端 `RegisterIn.username`
// 仍然是 `^[A-Za-z0-9_]{3,32}$`，手机号本来就落在里面，所以以前后不用改
// 接口就能收号码。别把这里的正则当成安全边界 —— 真要只收手机号，得在
// schemas.py 里收紧 pattern，那会连带改掉若干以用户名当标识的测试。
const PHONE_RE = /^1[3-9]\d{9}$/

const reg = reactive({ username: '', password: '', confirm: '' })

const err = (e, fallback) => e?.response?.data?.detail || fallback

// ---- 二次验证：等邮件那一屏 ------------------------------------------------
//
// **「等邮件」不是「登录失败」。** 所以从进这一屏到验证通过，界面上
// **不出现任何一条错误提示** —— 一条红字会让人以为口令错了，转头去
// 重输一遍（那只会再造一个挑战、把上一封邮件作废）。
//
// 倒计时靠一个每秒跳一次的时钟驱动。直接读 Date.now() 的话 Vue 察觉不到
// 时间变了，界面上的数字会一直停在同一个数。
const polling = ref(false)
const resending = ref(false)
const now = ref(Date.now())
let ticker = null

const mfaLeft = computed(() => store.mfaExpiresIn(now.value))
const resendWait = computed(() => store.mfaResendWait(now.value))

onMounted(() => { ticker = setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => { if (ticker) clearInterval(ticker) })

async function submitLogin() {
  if (!username.value || !password.value) return ElMessage.warning('请填写用户名和密码')
  busy.value = true
  try {
    const r = await store.login(username.value, password.value)
    if (r && r.mfa_required) {
      // 二次验证：**没有令牌**（服务端压根没发），切到「等邮件」那一屏。
      // 口令从内存里清掉 —— 它已经在服务端校验过了，留着没用。
      password.value = ''
      return
    }
    router.push('/books')
  } catch (e) {
    ElMessage.error(err(e, '登录失败'))
  } finally {
    busy.value = false
  }
}

/** 点「我已验证」：问一次服务端。**没验证成功不算错误** —— 那只是「还没点链接」。 */
async function submitPoll() {
  polling.value = true
  try {
    const r = await store.pollMfa()
    if (r.status === 'VERIFIED') {
      ElMessage.success('二次验证已完成')
      router.push('/books')
    } else {
      ElMessage.info('还没验证成功，请先点开邮件里的链接')
    }
  } catch (e) {
    ElMessage.error(err(e, '读取验证状态失败'))
  } finally {
    polling.value = false
  }
}

async function submitResend() {
  resending.value = true
  try {
    const r = await store.resendMfa()
    if (r.ok) ElMessage.success('验证邮件已重新发送')
    else if (r.reason === 'too_soon') ElMessage.warning(`请 ${r.wait} 秒后再试`)
  } catch (e) {
    ElMessage.error(err(e, '重新发送失败'))
  } finally {
    resending.value = false
  }
}

/** 退出等待，回到登录表单。**不清令牌** —— 此刻本来就没有令牌。 */
function backToLogin() {
  store.cancelMfa()
  password.value = ''
}

async function submitRegister() {
  // 前端先拦一道：当场提示比等一次往返舒服
  const phone = reg.username.trim()
  if (!PHONE_RE.test(phone))
    return ElMessage.warning('请输入 11 位手机号')
  if (reg.password.length < 8)
    return ElMessage.warning('密码至少 8 位')
  if (reg.password !== reg.confirm)
    return ElMessage.warning('两次输入的密码不一致')

  busy.value = true
  try {
    // 刻意不发 real_name：空姓名由后端兜成「会计」（见 app/api/auth.py
    // 的 register）。默认值放在服务端，脚本和将来的调用方拿到同一个口径。
    await authApi.register({
      username: phone,
      password: reg.password
    })
    ElMessage.success(
      '注册请求已提交，请登录；登录后联系管理员分配账套。' +
      '若您此前已用该号码注册过，请直接用原密码登录。')
    // ⚠️ 后端对「重名」与「成功」返回**完全相同**的响应（审查报告安全域 P2-2：
    // 「重名返回 400、成功返回 201」是一条用户名枚举信道，而登录路径本来
    // 是靠「用户名不存在也走完一次 PBKDF2」堵住枚举的）。
    // 所以前端**不能**说「注册成功」—— 那对重名的人是不成立的；
    // 但也**不能**说「该号码已被注册」—— 那就把枚举信道又开回来了。
    // 这句话对两种情况都成立：它只说「请求已提交」，并提示老用户直接登录。
    // 切回登录页并把用户名带过去，省得再输一遍
    username.value = phone
    password.value = ''
    Object.assign(reg, { username: '', password: '', confirm: '' })
    tab.value = 'login'
  } catch (e) {
    ElMessage.error(err(e, '注册失败'))
  } finally {
    busy.value = false
  }
}
onMounted(async () => {
  try {
    const cfg = await authApi.publicConfig()
    registrationEnabled.value = !!cfg?.registration_enabled
  } catch {
    // 保持 false。登录不受影响 —— 注册入口少一个，总比在没确认过状态时
    // 把一个匿名入口亮出来要好。
    registrationEnabled.value = false
  }
})
</script>

<style scoped>
.login-page {
  height: 100vh; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #eef2ff, #f8fafc 60%, #ecfdf5);
}
.login-box {
  width: 380px; background: #fff; padding: 32px; border-radius: 10px;
  box-shadow: 0 12px 40px rgba(15, 23, 42, .08);
}
h1 { font-size: 22px; margin: 0 0 4px; }
p { margin: 0 0 12px; font-size: 13px; }
.hint { text-align: center; font-size: 12px; margin: 12px 0 0; line-height: 1.6; }
.mfa-box { text-align: center; }
.mfa-box h2 { font-size: 16px; margin: 4px 0 10px; }
.mfa-box p { line-height: 1.8; }
.countdown { font-size: 12px; margin: 0 0 14px; }
.exit-btn { margin-top: 10px; width: 100%; }
:deep(.el-tabs__header) { margin-bottom: 18px; }
/* 关掉自助注册后只剩「登录」一个页签，那一行页签栏没有信息量，
   去掉它，登录表单直接顶上来（见 utils/registration.js 的 showTabs）。 */
.tabs-single :deep(.el-tabs__header) { display: none; }
</style>
