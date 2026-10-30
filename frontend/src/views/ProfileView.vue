<template>
  <div class="page">
    <!-- **不摆页头**（用户 2026-10-23 定的口径）：标题与那句说明都去掉。
         这一页开着就只该看见几张卡片 —— 卡片自己带着标题（「基本资料」
         「修改密码」「MCP 密钥」），页面叫什么，标签栏上已经写着「个人资料」了，
         再摆一遍是重复。 -->

    <!-- ── 一、基本资料 ─────────────────────────────────────────── -->
    <div class="card">
      <div class="card-head">
        <span class="title">基本资料</span>
      </div>
      <el-form :model="profile" label-width="96px" class="pf-form">
        <el-form-item label="账号">
          <!-- **只读，不给改**：账号是登录标识，也是审计里认人的依据
               （审计里的 entity_id、告警邮件的收件人都按它写）。
               改它会让「这条记录是谁做的」在事后对不上，所以这一页只放开姓名。 -->
          <el-input :model-value="store.user?.username" disabled />
        </el-form-item>

        <el-form-item label="姓名">
          <el-input
            v-model="profile.real_name"
            maxlength="64"
            placeholder="例如：黄纪幸"
            @keyup.enter="saveName"
          />
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="savingName" @click="saveName">保存姓名</el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- ── 二、修改密码 ─────────────────────────────────────────── -->
    <div class="card">
      <div class="card-head">
        <span class="title">修改密码</span>
      </div>

      <el-form :model="pwd" label-width="96px" class="pf-form" @submit.prevent>
        <el-form-item label="当前密码">
          <el-input
            v-model="pwd.current_password"
            type="password"
            show-password
            autocomplete="current-password"
            placeholder="现在正在用的密码"
          />
        </el-form-item>

        <el-form-item label="新密码">
          <el-input
            v-model="pwd.new_password"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="至少 6 位"
          />
        </el-form-item>

        <el-form-item label="确认新密码">
          <el-input
            v-model="pwd.confirm"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="再输一遍"
          />
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="savingPwd" @click="savePassword">
            修改密码
          </el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- ── 三、MCP 密钥 ─────────────────────────────────────────── -->
    <div class="card">
      <div class="card-head">
        <span class="title">MCP 密钥</span>
      </div>

      <div class="row mb">
        <!-- 「刷新」是次要动作（列表在进页面时就加载了），**不给它主按钮** ——
             一排两个主按钮会让人不知道该按哪个。 -->
        <el-button :loading="loadingTokens" @click="loadTokens">
          刷新
        </el-button>
        <el-button type="primary" @click="openCreate">生成密钥</el-button>
        <el-button v-if="mcpTokens.length" @click="deleteAll">全部删除</el-button>
      </div>

      <el-table v-if="mcpTokens.length" :data="mcpTokens" border size="default">
        <el-table-column prop="name" label="用途" min-width="160" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">
              {{ statusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <!-- 「这枚密钥能读什么」要看得见：数量在明面上，账套名放在悬浮里。
             数量是给人判断「是不是我以为的那本账」的第一眼，名字要展开才看。 -->
        <el-table-column label="可读账套" width="110">
          <template #default="{ row }">
            <el-tooltip :content="bookNames(row)" placement="top">
              <span>{{ row.books.length }} 个</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="最后使用" width="170">
          <template #default="{ row }">
            <span v-if="row.last_used_at">{{ formatDateTime(row.last_used_at) }}</span>
            <span v-else class="muted">从未使用</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <!-- **每一行都给「删除」**，包括已经失效的那几枚：
                 处置就是删掉记录 —— 留着一条再也用不了、也删不掉的旧记录，
                 只会让这一屏越看越乱。 -->
            <el-button
              link
              type="danger"
              @click="deleteOne(row)"
            >删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-else description="还没有 MCP 密钥" :image-size="72" />
    </div>

    <!-- ── 生成密钥 ─────────────────────────────────────────────── -->
    <el-dialog v-model="createVisible" title="生成 MCP 密钥" width="460px">
      <el-form :model="mcpForm" label-width="88px">
        <el-form-item label="用途">
          <el-input
            v-model="mcpForm.name"
            maxlength="64"
            placeholder="例如：Claude Desktop · 财务问答"
          />
        </el-form-item>
        <el-form-item label="当前密码">
          <el-input
            v-model="mcpForm.current_password"
            type="password"
            show-password
            autocomplete="current-password"
            placeholder="生成一枚永久密钥，要先确认是你本人"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="createToken">生成</el-button>
      </template>
    </el-dialog>

    <!-- ── 明文只在这里出现一次 ─────────────────────────────────── -->
    <el-dialog
      v-model="plainVisible"
      title="密钥已生成"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-input :model-value="plainToken" readonly class="plain-input">
        <template #append>
          <el-button @click="copyToken">复制</el-button>
        </template>
      </el-input>
      <p class="plain-note">
        这是唯一一次显示完整密钥。关掉之后找不回来 —— 服务器上只存着它的摘要。
      </p>
      <template #footer>
        <el-button type="primary" @click="plainVisible = false">我已保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as authApi from '@/api/auth'
import * as mcpApi from '@/api/mcp'
import { useSessionStore } from '@/stores/session'
import { formatDateTime } from '@/utils/datetime'

const store = useSessionStore()
const router = useRouter()

const profile = reactive({ real_name: '' })
const pwd = reactive({ current_password: '', new_password: '', confirm: '' })
const savingName = ref(false)
const savingPwd = ref(false)

const mcpTokens = ref([])
const loadingTokens = ref(false)
const createVisible = ref(false)
const creating = ref(false)
const plainVisible = ref(false)
const plainToken = ref('')
const mcpForm = reactive({ name: '', current_password: '' })

/**
 * 姓名：跟着 store 里的值走。
 *
 * **为什么是 watch 而不是 onMounted 读一次**：``refreshMe`` 是 MainLayout
 * 在 onMounted 里 await 的，而**子组件的 mounted 先于父组件** —— 那一刻
 * store 里的 user 可能还是登录那一刻的快照，甚至是空的。直接读一次就可能
 * 填出一个空框（改一个没显示出来的字段是没法改的）。
 *
 * watch + immediate 覆盖两种情况：值已经在 store 里（立刻填上）与值还没到
 * （到了就填）。
 */
watch(() => store.user?.real_name,
  (v) => { if (v != null) profile.real_name = v },
  { immediate: true })

/**
 * 改姓名。
 *
 * **成功就更新本地那份 user**（``applyProfile``）：顶栏那个名字是 store 里的
 * 副本，不更新的话「明明改了，界面没动」—— 而这个人刚刚才在界面上
 * 亲手改的。
 */
async function saveName() {
  const name = (profile.real_name || '').trim()
  savingName.value = true
  try {
    const data = await authApi.updateProfile({ real_name: name })
    store.applyProfile(data)
    profile.real_name = data?.real_name ?? name
    ElMessage.success('姓名已保存')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    savingName.value = false
  }
}

/**
 * 改密码。
 *
 * **改完必须重新登录**：服务端把该账号所有在用令牌都作废了（``token_version``），
 * 这一次的会话也一样。留着令牌不管的话，下一个请求会撞 401 —— 那时候人
 * 已经站在登录页上了，却不知道自己为什么被踢出去。
 *
 * 弹窗**只在这一刻出现**（不是页面上常驻的一条说明）：它要说的正是
 * 「刚刚发生了什么」，说完就要人点「去登录」。
 */
async function savePassword() {
  if (pwd.new_password !== pwd.confirm) {
    ElMessage.error('两次输入的新密码不一致')
    return
  }
  savingPwd.value = true
  try {
    await authApi.changePassword({
      current_password: pwd.current_password,
      new_password: pwd.new_password
    })
    await ElMessageBox.alert(
      '密码已修改。这个账号所有在用令牌都已作废（包括这一次的），请用新密码重新登录。',
      '请重新登录',
      { confirmButtonText: '去登录' }
    ).catch(() => {})
    // **只清本地**：令牌此刻已经作废了，再发一条 /auth/logout 只会拿到 401。
    // 那条留痕请求的语义是「人离开了」，而这次离开的原因是「密码变了」——
    // 这一条审计改密码时已经记过了，不必再记一次登出。
    store.logout({ remote: false })
    router.push('/login')
  } catch (e) {
    // 服务端的失败文案是给人看的（当前密码不正确 / 密码太短 / 连错太多要等），
    // 直接原样显示，别自己再翻译一遍 —— 翻译一遍就会丢掉「等几秒」这类信息。
    ElMessage.error(e?.response?.data?.detail || '修改失败')
  } finally {
    savingPwd.value = false
    pwd.current_password = ''
    pwd.new_password = ''
    pwd.confirm = ''
  }
}

// --------------------------------------------------------------------- MCP 密钥

async function loadTokens() {
  loadingTokens.value = true
  try {
    mcpTokens.value = await mcpApi.listMyTokens()
  } catch (e) {
    // **失败就当空列表**：这一屏是「补充信息」，拉不到时不该把整页卡住。
    // 错误提示由 http 拦截器统一发（见 api/client.js），这里不重复弹一遍。
    mcpTokens.value = []
  } finally {
    loadingTokens.value = false
  }
}

function openCreate() {
  mcpForm.name = ''
  mcpForm.current_password = ''
  createVisible.value = true
}

/**
 * 生成一枚。
 *
 * **明文只在这一次响应里回来** —— 所以成功后立刻弹另一个框把它显示出来，
 * 人抄走就完事。**不能只弹一句「生成成功」**：那样密钥就永远找不回来了
 * （服务器上只存摘要），等于生成了一枚谁也用不了的密钥。
 *
 * **失败时对话框不关**：错的是「当前密码」，人改了再点一次就行；关掉对话框
 * 的话，那句「当前密码不正确」就只留在一条转瞬即逝的提示里。
 */
async function createToken() {
  const name = (mcpForm.name || '').trim()
  if (!name) {
    ElMessage.error('请填写密钥用途')
    return
  }
  creating.value = true
  try {
    const data = await mcpApi.createMyToken({
      name,
      current_password: mcpForm.current_password,
    })
    createVisible.value = false
    plainToken.value = data.token
    plainVisible.value = true
    await loadTokens()
  } catch (e) {
    // 失败留在原地（见上面的说明）；文案由拦截器统一发。
  } finally {
    creating.value = false
  }
}

/** 复制明文。剪贴板不可用（http 页面 / 旧浏览器）时把明文选中让人手动拷。 */
async function copyToken() {
  try {
    await navigator.clipboard.writeText(plainToken.value)
    ElMessage.success('已复制')
  } catch (e) {
    ElMessage.warning('复制不了，请手动选中上面的密钥复制')
  }
}

/**
 * 删掉一枚。
 *
 * **必须先问一句**：MCP 密钥**永不过期**，处置是它唯一的安全阀 —— 删错了
 * （比如 agent 其实还在用）就是当场断线，而「再生成一枚」并不能把旧的找回来。
 *
 * **而且删掉就是删掉了**：这一行连同它的记录一起消失，找不回来（管理面那个
 * 「吊销」才是保留记录的那种处置）。
 */
async function deleteOne(row) {
  try {
    await ElMessageBox.confirm(
      `删除「${row.name}」之后，用它的 agent 立刻就连不上了，这一条记录也找不回来。确定删除吗？`,
      '删除 MCP 密钥',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch (e) {
    return   // 取消
  }
  try {
    await mcpApi.deleteMyToken(row.id)
    ElMessage.success('已删除')
    await loadTokens()
  } catch (e) {
    // 文案由拦截器统一发。
  }
}

/**
 * 一次性删掉全部。
 *
 * 这是「改密后的标准处置」—— MCP 密钥**不随改密自动失效**（这是产品决定，
 * 见架构文档 §11.15：把取舍摆在明面上，而不是让它悄悄发生）。所以怀疑密钥
 * 泄露、换了密码、离职交接，都走这一条。
 *
 * **任何状态的行都删** —— 包括已经失效的那几枚（留着也没用）。
 */
async function deleteAll() {
  try {
    await ElMessageBox.confirm(
      `将删掉你名下全部 ${mcpTokens.value.length} 枚 MCP 密钥，用它们的所有 agent 都会立刻连不上。确定吗？`,
      '全部删除',
      { type: 'warning', confirmButtonText: '全部删除', cancelButtonText: '取消' },
    )
  } catch (e) {
    return   // 取消
  }
  try {
    const data = await mcpApi.deleteAllMyTokens()
    ElMessage.success(`已删除 ${data.deleted} 枚`)
    await loadTokens()
  } catch (e) {
    // 文案由拦截器统一发。
  }
}

function statusText(status) {
  return {
    active: '有效',
    grace: '宽限期内',
    // **不说「已吊销」** —— 那是管理面那个动词，本人这一侧的处置只有
    // 「删除」。这一栏说的是**眼下的事实**（这枚密钥还能不能用）。
    revoked: '已失效',
    disabled: '已停用',
  }[status] || status
}

function statusType(status) {
  return {
    active: 'success',
    grace: 'warning',
    revoked: 'info',
    disabled: 'info',
  }[status] || 'info'
}

/** 这枚密钥能读哪些账套 —— 悬浮里展开写清楚。 */
function bookNames(row) {
  if (!row.books?.length) return '没有可访问的账套'
  return row.books.map(b => `${b.company_name} · ${b.name}`).join('\n')
}

onMounted(loadTokens)
</script>

<style scoped>
.pf-form { max-width: 620px; }
.row { display: flex; gap: 8px; align-items: center; }
.mb { margin-bottom: 12px; }
.plain-input { margin-bottom: 10px; }
.plain-note { margin: 0; font-size: 13px; color: #64748b; line-height: 1.6; }
</style>
