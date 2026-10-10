<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <span class="title">用户与账套授权</span>
          <span class="hint">权限落在数据库的「用户 × 账套」授权表上，前端按钮只是提示，真正拦截在后端。</span>
        </div>
      </template>

      <el-alert
        v-if="!isAdmin" type="info" :closable="false" show-icon class="mb"
        title="只有系统管理员（admin）可以增删用户和调整授权。"
      />

      <div class="row mb">
        <el-button type="primary" :disabled="!isAdmin" @click="openCreate">
          <el-icon><Plus /></el-icon>&nbsp;新建用户
        </el-button>
      </div>

      <el-table :data="users" border size="default">
        <el-table-column prop="username" label="用户名" width="130" />
        <el-table-column prop="real_name" label="姓名" width="130" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.active ? 'success' : 'info'">
              {{ row.active ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="账套授权" min-width="420">
          <template #default="{ row }">
            <template v-if="row.books.length">
              <el-tag
                v-for="b in row.books" :key="b.book_id" size="small" class="mr"
                :type="ROLE_TAG[b.role]"
              >
                {{ bookLabel(b.book_id) }} · {{ ROLE_CN[b.role] }}
              </el-tag>
            </template>
            <span v-else class="muted">未授权任何账套</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" align="center">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="!isAdmin" @click="openBooks(row)">授权</el-button>
            <el-button link type="primary" :disabled="!isAdmin" @click="openReset(row)">重置密码</el-button>
            <el-button link type="danger" :disabled="!isAdmin || row.username === 'admin'"
                       @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建用户 -->
    <el-dialog v-model="dlgUser" title="新建用户" width="520px">
      <el-form :model="userForm" label-width="86px">
        <el-form-item label="用户名"><el-input v-model="userForm.username" /></el-form-item>
        <el-form-item label="姓名"><el-input v-model="userForm.real_name" /></el-form-item>
        <el-form-item label="初始密码">
          <el-input v-model="userForm.password" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
      </el-form>
      <el-divider content-position="left">账套授权</el-divider>
      <el-table :data="bookRows" size="small" border max-height="280">
        <el-table-column prop="label" label="账套" min-width="230" />
        <el-table-column label="角色" width="180">
          <template #default="{ row }">
            <el-select v-model="userForm.books[row.id].role" size="small" style="width: 100%">
              <el-option v-for="r in ROLES" :key="r" :label="ROLE_CN[r]" :value="r" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="授权" width="80" align="center">
          <template #default="{ row }">
            <el-checkbox v-model="userForm.books[row.id].on" />
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="dlgUser = false">取消</el-button>
        <el-button type="primary" :loading="busy" @click="saveUser">创建</el-button>
      </template>
    </el-dialog>

    <!-- 调整授权 -->
    <el-dialog v-model="dlgBooks" :title="`调整授权 · ${target?.real_name || target?.username || ''}`" width="520px">
      <el-table :data="bookRows" size="small" border max-height="320">
        <el-table-column prop="label" label="账套" min-width="230" />
        <el-table-column label="角色" width="180">
          <template #default="{ row }">
            <el-select v-model="grant.books[row.id].role" size="small" style="width: 100%"
                       :disabled="!grant.books[row.id].on">
              <el-option v-for="r in ROLES" :key="r" :label="ROLE_CN[r]" :value="r" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="授权" width="80" align="center">
          <template #default="{ row }"><el-checkbox v-model="grant.books[row.id].on" /></template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="dlgBooks = false">取消</el-button>
        <el-button type="primary" :loading="busy" @click="saveBooks">保存</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码 -->
    <el-dialog v-model="dlgReset" title="重置密码" width="420px">
      <el-form label-width="80px">
        <el-form-item label="用户">
          <span>{{ target?.real_name || target?.username }}</span>
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="newPassword" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlgReset = false">取消</el-button>
        <el-button type="primary" :loading="busy" @click="savePassword">重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import * as sysApi from '@/api/system'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const users = ref([])
const books = ref([])
const busy = ref(false)

const ROLES = ['OWNER', 'ACCOUNTANT', 'VIEWER']
const ROLE_CN = { OWNER: '负责人', ACCOUNTANT: '会计', VIEWER: '只读' }
const ROLE_TAG = { OWNER: 'danger', ACCOUNTANT: 'primary', VIEWER: 'info' }

const isAdmin = computed(() => store.user?.username === 'admin')

const dlgUser = ref(false)
const dlgBooks = ref(false)
const dlgReset = ref(false)
const target = ref(null)
const newPassword = ref('')
const userForm = reactive({ username: '', real_name: '', password: '', books: {} })
const grant = reactive({ books: {} })

const bookRows = computed(() =>
  books.value.map((b) => ({ id: b.id, label: `${b.company_name} · ${b.name} ${b.year}` }))
)
const bookLabel = (id) => {
  const b = books.value.find((x) => x.id === id)
  return b ? `${b.company_name} · ${b.name} ${b.year}` : `#${id}`
}

function blankGrant() {
  const g = {}
  books.value.forEach((b) => { g[b.id] = { on: false, role: 'ACCOUNTANT' } })
  return g
}

async function load() {
  users.value = await sysApi.users()
  books.value = await sysApi.allBooks()
}

function openCreate() {
  Object.assign(userForm, {
    username: '', real_name: '', password: '', books: blankGrant(),
  })
  dlgUser.value = true
}

async function saveUser() {
  if (!userForm.username || userForm.password.length < 6) {
    ElMessage.warning('用户名不能为空，密码至少 6 位')
    return
  }
  busy.value = true
  try {
    await sysApi.createUser({
      username: userForm.username,
      real_name: userForm.real_name,
      password: userForm.password,
      books: Object.entries(userForm.books)
        .filter(([, v]) => v.on)
        .map(([bookId, v]) => ({ book_id: Number(bookId), role: v.role })),
    })
    ElMessage.success('已创建')
    dlgUser.value = false
    await load()
  } finally {
    busy.value = false
  }
}

function openBooks(row) {
  target.value = row
  grant.books = blankGrant()
  row.books.forEach((b) => { grant.books[b.book_id] = { on: true, role: b.role } })
  dlgBooks.value = true
}

async function saveBooks() {
  busy.value = true
  try {
    await sysApi.setUserBooks(target.value.id, Object.entries(grant.books)
      .filter(([, v]) => v.on)
      .map(([bookId, v]) => ({ book_id: Number(bookId), role: v.role })))
    ElMessage.success('授权已保存')
    dlgBooks.value = false
    await load()
  } finally {
    busy.value = false
  }
}

function openReset(row) {
  target.value = row
  newPassword.value = ''
  dlgReset.value = true
}

async function savePassword() {
  if (newPassword.value.length < 6) {
    ElMessage.warning('密码至少 6 位')
    return
  }
  await sysApi.updateUser(target.value.id, { password: newPassword.value })
  ElMessage.success('密码已重置')
  dlgReset.value = false
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`确定删除用户「${row.username}」吗？其授权会一并删除。`, '删除用户', { type: 'warning' })
  } catch {
    return
  }
  await sysApi.deleteUser(row.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>

<style scoped>
.row { display: flex; gap: 10px; }
.mb { margin-bottom: 14px; }
.mt { margin-top: 14px; }
.mr { margin-right: 6px; }
.muted { color: #999; }
</style>
