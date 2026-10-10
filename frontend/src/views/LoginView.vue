<template>
  <div class="login-page">
    <div class="login-box">
      <h1>财务记账系统</h1>
      <p class="muted">多账套 · 凭证 · 账簿 · 报表</p>

      <el-tabs v-model="tab" stretch>
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
          <p class="hint muted">演示账号 admin / 123456</p>
        </el-tab-pane>

        <!-- ------------------------------------------------ 注册 -->
        <el-tab-pane label="注册" name="register">
          <el-form @submit.prevent="submitRegister">
            <el-form-item>
              <el-input v-model="reg.username" placeholder="用户名（3-32 位字母、数字或下划线）"
                        size="large" @keyup.enter="submitRegister" />
            </el-form-item>
            <el-form-item>
              <el-input v-model="reg.real_name" placeholder="姓名（选填）" size="large" />
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
          <p class="hint muted">
            注册只开账号，账套需要管理员在「用户与授权」里分配。
          </p>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as authApi from '@/api/auth'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const router = useRouter()

const tab = ref('login')
const busy = ref(false)

const username = ref('admin')
const password = ref('123456')

const reg = reactive({ username: '', real_name: '', password: '', confirm: '' })

const err = (e, fallback) => e?.response?.data?.detail || fallback

async function submitLogin() {
  if (!username.value || !password.value) return ElMessage.warning('请填写用户名和密码')
  busy.value = true
  try {
    await store.login(username.value, password.value)
    router.push('/books')
  } catch (e) {
    ElMessage.error(err(e, '登录失败'))
  } finally {
    busy.value = false
  }
}

async function submitRegister() {
  // 前端先拦一道：后端 pydantic 也会校验，但当场提示比等一次往返舒服
  if (!/^[A-Za-z0-9_]{3,32}$/.test(reg.username))
    return ElMessage.warning('用户名需为 3-32 位字母、数字或下划线')
  if (reg.password.length < 8)
    return ElMessage.warning('密码至少 8 位')
  if (reg.password !== reg.confirm)
    return ElMessage.warning('两次输入的密码不一致')

  busy.value = true
  try {
    await authApi.register({
      username: reg.username,
      password: reg.password,
      real_name: reg.real_name.trim()
    })
    ElMessage.success('注册成功，请登录；登录后联系管理员分配账套')
    // 切回登录页并把用户名带过去，省得再输一遍
    username.value = reg.username
    password.value = ''
    Object.assign(reg, { username: '', real_name: '', password: '', confirm: '' })
    tab.value = 'login'
  } catch (e) {
    ElMessage.error(err(e, '注册失败'))
  } finally {
    busy.value = false
  }
}
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
:deep(.el-tabs__header) { margin-bottom: 18px; }
</style>
