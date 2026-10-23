<template>
  <div class="page">
    <!-- **不摆页头**（用户 2026-10-23 定的口径）：标题与那句说明都去掉。
         这一页开着就只该看见两张卡片 —— 卡片自己带着标题（「基本资料」
         「修改密码」），页面叫什么，标签栏上已经写着「个人资料」了，
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
  </div>
</template>

<script setup>
import { reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as authApi from '@/api/auth'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const router = useRouter()

const profile = reactive({ real_name: '' })
const pwd = reactive({ current_password: '', new_password: '', confirm: '' })
const savingName = ref(false)
const savingPwd = ref(false)

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
</script>

<style scoped>
.pf-form { max-width: 620px; }
</style>
