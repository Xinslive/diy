<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2 class="page-title">账套管理</h2>
        <div class="muted">账套是独立的记账单元；公司名只是账套上的一个属性，同一家公司可以开多个账套</div>
      </div>
      <!-- 建账套是管理员的事：自助注册进来的账号不该绕过授权自己开账 -->
      <el-button v-if="store.isAdmin" type="primary" @click="openCreate">新建账套</el-button>
    </div>

    <div class="card" v-loading="loading">
      <el-table :data="store.books" border>
        <el-table-column label="单位名称" prop="company_name" min-width="180" />
        <el-table-column label="账套名称" min-width="180">
          <template #default="{ row }">
            <el-link type="primary" @click="switchTo(row.id)">{{ row.name }}</el-link>
            <el-tag v-if="row.id === store.bookId" size="small" type="success" class="ml">当前</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="年度" prop="year" width="90" align="center" />
        <el-table-column label="启用月" width="90" align="center">
          <template #default="{ row }">{{ row.start_month }}月</template>
        </el-table-column>
        <el-table-column label="已结账" width="120" align="center">
          <template #default="{ row }">
            <span :class="row.closed_periods === 12 ? 'ok' : ''">{{ row.closed_periods }} / 12 期</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="row.status === 'CLOSED' ? 'info' : 'success'" size="small">
              {{ row.status === 'CLOSED' ? '已结转' : '使用中' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="我的权限" width="110" align="center">
          <template #default="{ row }">{{ ROLE[row.role] || row.role }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" align="center">
          <template #default="{ row }">
            <el-button link type="primary" @click="switchTo(row.id)">进入</el-button>
            <el-button link type="primary" @click="goOpenings(row.id)">期初余额</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty
        v-if="!loading && !store.books.length"
        :description="store.isAdmin ? '还没有账套，先新建一个' : '还没有分配到账套，请联系管理员在「用户与授权」里授权'"
      />
    </div>

    <el-dialog v-model="visible" title="新建账套" width="520px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="公司名称" required>
          <el-input v-model="form.company_name" placeholder="如：启明物业管理有限公司" />
          <div class="muted tip">用于报表、凭证页眉的「单位名称」，同一家公司可以开多个账套</div>
        </el-form-item>
        <el-form-item label="账套名称" required>
          <el-input v-model="form.name" placeholder="如：租金账套" />
          <div class="muted tip">同一单位下的业务用途，如租金账套、管理费账套</div>
        </el-form-item>
        <el-form-item label="会计年度" required>
          <el-select v-model="form.year" style="width: 100%">
            <el-option v-for="y in yearOptions" :key="y" :label="y + ' 年'" :value="y" />
          </el-select>
        </el-form-item>
        <el-form-item label="启用月份">
          <el-select v-model="form.start_month" style="width: 100%">
            <el-option v-for="m in 12" :key="m" :label="m + ' 月'" :value="m" />
          </el-select>
          <div class="muted tip">非 1 月启用时，启用月之前的期间不出现在账上</div>
        </el-form-item>
      </el-form>
      <el-alert v-if="newBookId" type="success" :closable="false" class="mb">
        <div>账套已创建，下一步录入<strong>期初余额</strong>（上一年的科目余额表）。</div>
        <el-button size="small" type="primary" class="mt" @click="goOpenings(newBookId)">
          去录期初余额
        </el-button>
        <el-button size="small" class="mt" @click="visible = false">稍后再说</el-button>
      </el-alert>
      <template #footer>
        <el-button @click="visible = false">关闭</el-button>
        <el-button type="primary" :loading="saving" :disabled="!canSubmit" @click="submit">
          创建账套
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const router = useRouter()
const loading = ref(false)
const saving = ref(false)
const visible = ref(false)
const newBookId = ref(0)

const ROLE = { OWNER: '所有者', ACCOUNTANT: '会计', VIEWER: '只读' }
const yearOptions = Array.from({ length: 6 }, (_, i) => new Date().getFullYear() - 1 + i)
const form = reactive({ company_name: '', name: '', year: new Date().getFullYear(), start_month: 1 })

const canSubmit = computed(() => form.company_name.trim() && form.name.trim())

function openCreate() {
  newBookId.value = 0
  form.company_name = ''
  form.name = `${form.year} 账套`
  form.start_month = 1
  visible.value = true
}

async function submit() {
  saving.value = true
  try {
    const book = await api.createBook({ ...form })
    newBookId.value = book.id
    await store.loadBooks()
    ElMessage.success(`账套《${book.company_name} · ${book.name}》已创建，标准科目表已装载`)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '创建失败')
  } finally {
    saving.value = false
  }
}

async function switchTo(id) {
  await store.setBook(id)
  ElMessage.success(`已切换到《${store.currentBook?.name}》`)
  router.push('/vouchers')
}

async function goOpenings(id) {
  if (store.bookId !== id) await store.setBook(id)
  router.push('/openings')
}

onMounted(async () => {
  loading.value = true
  try {
    await store.loadBooks()
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.page-title { margin: 0 0 2px; font-size: 18px; }
.ml { margin-left: 8px; }
.tip { font-size: 12px; margin-top: 2px; }
.mt { margin-top: 8px; }
.mb { margin-bottom: 12px; }
.ok { color: var(--cwq-ok); font-weight: 600; }
</style>
