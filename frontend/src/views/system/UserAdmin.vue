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

      <!-- 自助注册开关。放这一页是因为它回答的就是「谁能建号」，
           与上面的用户表是同一件事的两端：管理员开号，或者放开门让本人开。
           关掉时登录页上的「注册」页签整个消失（前端只藏入口，
           真正的闸门在 /api/auth/register，见 backend/app/api/auth.py）。 -->
      <div class="row mb reg-row">
        <span class="title">允许自助注册</span>
        <el-switch v-model="registration" :loading="regBusy" :disabled="!isAdmin"
                   @change="toggleRegistration" />
        <span class="hint">
          {{ registration ? '任何人都能用手机号自行建号，建号后仍需管理员分配账套' : '关闭，登录页不显示注册入口' }}
        </span>
      </div>

      <!-- 登录防护的形态：把「所有请求算作同一个来源」这件事**显式化**。
           平时它是隐形的（代码是 fail-closed 的，配错的后果不是报错而是
           误伤），没人知道就没人能修。 -->
      <el-alert
        v-if="protection && protection.source_bucketing && protection.source_bucketing.shared_bucket"
        type="warning" :closable="false" show-icon class="mb"
        title="登录的 IP 节流当前是「全站一个桶」"
        :description="protection.source_bucketing.shared_bucket_reason +
          ' 改法：' + (protection.source_bucketing.fix_hint || '')"
      />

      <el-table :data="users" border size="default">
        <el-table-column prop="username" label="账号" width="130" />
        <el-table-column prop="real_name" label="姓名" width="130" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.active ? 'success' : 'info'">
              {{ row.active ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <!-- 锁定状态。递进式锁定下这一列要说清三件事：锁没锁、第几轮
             （决定下次锁多久）、还剩多久 —— 只显示「已锁定」的话，
             管理员不知道该等还是该解。 -->
        <el-table-column label="锁定" width="160">
          <template #default="{ row }">
            <el-tag v-if="row.locked" size="small" type="danger">
              第 {{ row.lock_level }} 轮 · {{ leftText(row) }}
            </el-tag>
            <span v-else-if="row.failed_logins" class="muted">
              已错 {{ row.failed_logins }} 次
            </span>
            <span v-else class="muted">—</span>
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
        <el-table-column label="操作" width="290" align="center">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="!isAdmin" @click="openBooks(row)">授权</el-button>
            <el-button link type="primary" :disabled="!isAdmin" @click="openReset(row)">重置密码</el-button>
            <!-- 解锁只在真的锁着时才可点：按钮常驻但置灰，比「有时突然
                 多出一个按钮」好 —— 管理员一眼就知道这个位置管这件事。 -->
            <el-button link type="warning" :disabled="!isAdmin || !row.locked"
                       @click="unlock(row)">解锁</el-button>
            <el-button link type="danger" :disabled="!isAdmin || row.username === 'admin'"
                       @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建用户 -->
    <el-dialog v-model="dlgUser" title="新建用户" width="520px">
      <el-form :model="userForm" label-width="86px">
        <el-form-item label="账号">
          <el-input v-model="userForm.username" :maxlength="USERNAME_MAX" show-word-limit />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="userForm.real_name" :maxlength="REAL_NAME_MAX" show-word-limit />
        </el-form-item>
        <el-form-item label="初始密码">
          <el-input v-model="userForm.password" type="password" show-password
                    :placeholder="`至少 ${PASSWORD_MIN} 位`" />
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
          <el-input v-model="newPassword" type="password" show-password
                    :placeholder="`至少 ${PASSWORD_MIN} 位`" />
        </el-form-item>
      </el-form>
      <!-- 重置密码 = 撤销该账号的全部在用令牌（审计 P2-01）。
           这条以前不做，于是「密码泄露后重置密码」根本挡不住旧令牌。
           代价是这个人要重新登录一次 —— 得让人看见，不然会以为是系统坏了。 -->
      <el-alert
        type="warning" :closable="false" show-icon
        :title="resetHint" class="mb"
      />
      <template #footer>
        <el-button @click="dlgReset = false">取消</el-button>
        <el-button type="primary" :loading="busy" @click="savePassword">重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import * as sysApi from '@/api/system'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const router = useRouter()
const users = ref([])
const books = ref([])
const busy = ref(false)

// 自助注册开关。开关拨动即保存，没有「保存」按钮 —— 一个开关多一次提交就多
// 一次「我以为改了其实没保存」的机会。失败时回滚到后端的真实值。
const registration = ref(false)
const regBusy = ref(false)

// 登录防护的当前形态。**读不到就当作没有提示**（不是「没有风险」）——
// 拿不到状态时不该在界面上断言任何事。
const protection = ref(null)

const ROLES = ['OWNER', 'ACCOUNTANT', 'VIEWER']
const ROLE_CN = { OWNER: '负责人', ACCOUNTANT: '会计', VIEWER: '只读' }
const ROLE_TAG = { OWNER: 'danger', ACCOUNTANT: 'primary', VIEWER: 'info' }

// 入参约束 —— **与后端同一套口径**（backend/app/api/system.py）。
//
// 为什么前端也要写一遍：后端的 422 说的是「字段太长」，而管理员看到的是
// 一句「数据库内部错误」或者一个看不懂的校验数组（审计 P2-6）。
// **前端先挡住并说人话**，后端那份是最后一道网。
const USERNAME_MAX = 64        // 与 sys_user.username 的列宽一致
const REAL_NAME_MAX = 64       // 与 sys_user.real_name 的列宽一致
const PASSWORD_MIN = 6         // 与后端 PASSWORD_MIN_LENGTH 一致
const USERNAME_MIN = 2

// 账号的形状约束：不能有空白与控制字符（换行会写进审计日志与告警邮件，
// 把一行撑成两行 —— 看起来像多了一条记录）。
const USERNAME_BAD = new RegExp('[\\s\\x00-\\x1f\\x7f]')

// 密码策略：**只有长度**（与后端 app/core/security.py 同一把尺）。
//
// **不再要求两类字符**（用户 2026-10-23 定的口径）：全部数字也认。
// 真正兜底的是登录失败锁定与 PBKDF2 的成本，不是字符的花样；要求「两类」
// 拦下的是随手敲出来的数字串，而那种密码本来就不该被禁止 —— 逼人为了凑
// 花样写一个自己记不住的密码，净损失。
function passwordProblem(password) {
  if (!password) return '密码不能为空'
  if (password.length < PASSWORD_MIN) return `密码至少 ${PASSWORD_MIN} 位`
  return ''
}

function usernameProblem(username) {
  if (!username) return '账号不能为空'
  if (username.length < USERNAME_MIN) return `账号至少 ${USERNAME_MIN} 位`
  if (username.length > USERNAME_MAX) return `账号最长 ${USERNAME_MAX} 位`
  if (USERNAME_BAD.test(username)) {
    return '账号不能包含空格、换行或控制字符（它会写进审计日志与告警邮件）'
  }
  return ''
}

const isAdmin = computed(() => store.isAdmin)

const dlgUser = ref(false)
const dlgBooks = ref(false)
const dlgReset = ref(false)
const target = ref(null)
const newPassword = ref('')
const userForm = reactive({ username: '', real_name: '', password: '', books: {} })
const grant = reactive({ books: {} })

// **账套一律只写账套名，不写单位名**（用户 2026-10-22 批注：「不显示单位名」）。
//
// 单位名（公司名）是**建账套时填的抬头**，不是账套的地址：全站顶上的账套
// 选择框早就是只写账套名（见 MainLayout），Excel 导出的抬头也不带单位名
// （ARCHITECTURE.md §8「Excel 导出的抬头：一律不带单位名」），而授权表里
// 认的也是**账套**。
// 授权这一列要回答的是「这个人能碰哪本账」，写成「启明物业管理有限公司 ·
// 启明物业 · 负责人」把答案埋在一串抬头里 —— 管理员扫一眼标签反而看不清
// 是哪本账，也把一列撑得横着拖。
//
// 真认不出是哪一个时（同名账套），退回到 `#id` —— 那才是唯一能区分的东西。
const bookRows = computed(() =>
  // 年度是账套的属性，账套本身不再按年度拆开，所以这里不带年份
  books.value.map((b) => ({ id: b.id, label: b.name }))
)
const bookLabel = (id) => {
  const b = books.value.find((x) => x.id === id)
  return b ? b.name : `#${id}`
}

function blankGrant() {
  const g = {}
  books.value.forEach((b) => { g[b.id] = { on: false, role: 'ACCOUNTANT' } })
  return g
}

async function load() {
  users.value = await sysApi.users()
  books.value = await sysApi.allBooks()
  try {
    const reg = await sysApi.getRegistration()
    registration.value = !!reg?.registration_enabled
  } catch {
    // 读不到就当作关闭：这一行控制的是一个安全相关开关，
    // 显示成「开」而后端其实关着，比反过来更危险。
    registration.value = false
  }
  try {
    protection.value = await sysApi.getLoginProtection()
  } catch {
    // 拿不到就**什么都不显示** —— 界面不该在没确认过状态时断言任何事。
    protection.value = null
  }
}

async function toggleRegistration(value) {
  regBusy.value = true
  try {
    const reg = await sysApi.setRegistration(value)
    registration.value = !!reg?.registration_enabled
    ElMessage.success(value ? '已开放自助注册' : '已关闭自助注册')
  } catch (e) {
    // 拨回去：让界面回到后端确认过的那个状态，而不是停在没保存上的假象。
    registration.value = !value
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    regBusy.value = false
  }
}

function openCreate() {
  Object.assign(userForm, {
    username: '', real_name: '', password: '', books: blankGrant(),
  })
  dlgUser.value = true
}

async function saveUser() {
  // 前端先挡并说人话。后端那份是最后一道网（审计 P2-6/P2-7：
  // 超长与畸形输入在后端是 422，但把 422 原样弹给管理员并不能告诉她怎么改）。
  const nameProblem = usernameProblem(userForm.username)
  if (nameProblem) {
    ElMessage.warning(nameProblem)
    return
  }
  if (userForm.real_name.length > REAL_NAME_MAX) {
    ElMessage.warning(`姓名最长 ${REAL_NAME_MAX} 位`)
    return
  }
  const pwdProblem = passwordProblem(userForm.password)
  if (pwdProblem) {
    ElMessage.warning(pwdProblem)
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
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '创建失败')
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

const resetHint = computed(() =>
  target.value?.id === store.user?.id
    ? '重置后你自己的登录状态也会失效，需要用新密码重新登录。'
    : `重置后「${target.value?.real_name || target.value?.username || ''}」在所有设备上的登录状态会立即失效，需要用新密码重新登录。`
)

async function savePassword() {
  // **与建号同一个下限**（后端两条路径共用一套判据，审计 P2-1）。
  const pwdProblem = passwordProblem(newPassword.value)
  if (pwdProblem) {
    ElMessage.warning(pwdProblem)
    return
  }
  const isSelf = target.value?.id === store.user?.id
  try {
    await sysApi.updateUser(target.value.id, { password: newPassword.value })
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '重置失败')
    return
  }
  ElMessage.success(
    isSelf
      ? '密码已重置。你的登录状态已失效，正在跳转到登录页…'
      : '密码已重置，该账号已登录的会话已全部失效'
  )
  dlgReset.value = false
  if (isSelf) {
    // 自己重置自己的密码 = 自己的令牌也被撤销了。与其等下一次请求撞 401
    // 再被全局拦截器踢出去，不如当场说清楚。
    store.logout()
    router.push({ name: 'login' })
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`确定删除用户「${row.username}」吗？其授权会一并删除。`, '删除用户', { type: 'warning' })
  } catch {
    return
  }
  try {
    await sysApi.deleteUser(row.id)
  } catch (e) {
    // **把后端的话原样透出来**：删不掉时最常见的原因是「这是最后一个系统
    // 管理员」—— 那句话自带处置动作（先建一个管理员），比一句「删除失败」
    // 有用得多。
    ElMessage.error(e?.response?.data?.detail || '删除失败')
    return
  }
  ElMessage.success('已删除')
  await load()
}

// 锁定剩余时间的显示。
//
// **不做倒计时**：每秒刷新一次会让整张表一直在重绘，而管理员看一眼
// 「还剩 12 分钟」已经够判断「是等一等还是解掉」。真要精确到秒，
// 那一行信息的密度反而盖过了它要回答的问题。
//
// 剩余秒数是**后端算好的**（core/lockout.py::lock_summary）—— 前端不拿
// locked_until 去和浏览器时钟比，那会是一次口径分裂（而且用的是浏览器
// 时区，与服务端的 UTC 口径对不上）。
function leftText(row) {
  const sec = row.locked_left_seconds || 0
  if (sec <= 0) return '已到期'
  const min = Math.ceil(sec / 60)
  if (min < 60) return `剩 ${min} 分钟`
  return `剩 ${(min / 60).toFixed(1)} 小时`
}

async function unlock(row) {
  await sysApi.unlockUser(row.id)
  ElMessage.success(`已解除「${row.username}」的锁定`)
  await load()
}

onMounted(load)
</script>

<style scoped>
.row { display: flex; gap: 10px; align-items: center; }
.reg-row { gap: 12px; }
.mb { margin-bottom: 14px; }
.mt { margin-top: 14px; }
.mr { margin-right: 6px; }
.muted { color: #999; }
</style>
