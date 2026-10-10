<template>
  <div class="login-page">
    <div class="login-box">
      <h1>财务记账系统</h1>
      <p class="muted">多账套 · 凭证 · 账簿 · 报表</p>
      <el-form @submit.prevent="submit">
        <el-form-item>
          <el-input v-model="username" placeholder="用户名" size="large" @keyup.enter="submit" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" placeholder="密码" size="large" show-password @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="loading" @click="submit">
          登录
        </el-button>
      </el-form>
      <p class="hint muted">演示账号 admin / 123456</p>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const router = useRouter()
const username = ref('admin')
const password = ref('123456')
const loading = ref(false)

async function submit() {
  if (!username.value || !password.value) return
  loading.value = true
  try {
    await store.login(username.value, password.value)
    router.push('/vouchers')
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  height: 100vh; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #eef2ff, #f8fafc 60%, #ecfdf5);
}
.login-box {
  width: 360px; background: #fff; padding: 32px; border-radius: 10px;
  box-shadow: 0 12px 40px rgba(15, 23, 42, .08);
}
h1 { font-size: 22px; margin: 0 0 4px; }
p { margin: 0 0 20px; font-size: 13px; }
.hint { text-align: center; font-size: 12px; margin: 16px 0 0; }
</style>
