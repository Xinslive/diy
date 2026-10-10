<template>
  <el-container class="shell">
    <el-aside width="196px" class="no-print">
      <div class="brand">
        <span class="logo">CWQ</span>
        <span class="name">财务记账</span>
      </div>
      <el-menu :default-active="$route.path" router class="menu">
        <el-menu-item index="/books"><el-icon><Folder /></el-icon>账套管理</el-menu-item>
        <el-menu-item index="/openings"><el-icon><Coin /></el-icon>期初余额</el-menu-item>
        <el-menu-item index="/vouchers"><el-icon><Tickets /></el-icon>凭证</el-menu-item>
        <el-menu-item index="/balance"><el-icon><Grid /></el-icon>科目余额表</el-menu-item>
        <el-menu-item index="/general-ledger"><el-icon><Notebook /></el-icon>总账</el-menu-item>
        <el-menu-item index="/detail-ledger"><el-icon><List /></el-icon>明细账</el-menu-item>
        <el-menu-item index="/reports"><el-icon><DataAnalysis /></el-icon>报表</el-menu-item>
        <el-menu-item index="/periods"><el-icon><Calendar /></el-icon>期末处理</el-menu-item>
        <el-menu-item index="/subjects"><el-icon><Collection /></el-icon>科目表</el-menu-item>
        <el-menu-item index="/print-templates"><el-icon><Printer /></el-icon>打印模板</el-menu-item>
        <el-menu-item index="/audit"><el-icon><Document /></el-icon>操作日志</el-menu-item>
        <el-menu-item index="/users"><el-icon><User /></el-icon>用户与授权</el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header height="52px" class="topbar no-print">
        <el-select
          v-model="store.bookId"
          size="default"
          style="width: 240px"
          @change="onBookChange"
        >
          <el-option
            v-for="b in store.books"
            :key="b.id"
            :label="`${b.company} · ${b.name} ${b.year}`"
            :value="b.id"
          />
        </el-select>

        <el-select v-model="store.periodId" size="default" style="width: 150px" @change="onPeriodChange">
          <el-option v-for="p in store.periods" :key="p.id" :label="p.code" :value="p.id">
            <span>{{ p.code }}</span>
            <el-tag v-if="p.locked" size="small" type="success" style="float: right">已结账</el-tag>
            <el-tag v-else-if="p.unposted > 0" size="small" type="warning" style="float: right">
              未记账 {{ p.unposted }}
            </el-tag>
          </el-option>
        </el-select>

        <span class="spacer" />

        <el-tag v-if="store.currentBook?.role === 'VIEWER'" type="info">只读</el-tag>
        <el-dropdown @command="onCommand">
          <span class="user">
            {{ store.user?.real_name || store.user?.username }}<el-icon><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <el-main>
        <router-view :key="viewKey" />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { ArrowDown, Calendar, Coin, Collection, DataAnalysis, Document, Folder, Grid, List, Notebook, Printer, Tickets, User } from '@element-plus/icons-vue'

const store = useSessionStore()
const route = useRoute()
const router = useRouter()

// 切换账套/期间后让当前页面重新取数
const viewKey = computed(() => `${route.name}-${store.bookId}-${store.periodId}`)

async function onBookChange(id) {
  await store.setBook(id)
}
function onPeriodChange(id) {
  store.setPeriod(id)
}
function onCommand(cmd) {
  if (cmd === 'logout') {
    store.logout()
    router.push('/login')
  }
}

onMounted(async () => {
  if (!store.books.length) await store.loadBooks()
})
watch(() => store.bookId, async (id) => {
  if (id) await store.loadPeriods()
})
</script>

<style scoped>
.shell { height: 100vh; }

.brand {
  display: flex; align-items: center; gap: 8px;
  padding: 14px 16px; border-bottom: 1px solid var(--cwq-border);
}
.logo {
  background: var(--cwq-primary); color: #fff; font-weight: 700;
  padding: 2px 6px; border-radius: 4px; font-size: 12px; letter-spacing: .5px;
}
.name { font-weight: 600; }

.menu { border-right: none; }

.topbar {
  display: flex; align-items: center; gap: 10px;
  background: #fff; border-bottom: 1px solid var(--cwq-border);
}
.topbar .spacer { flex: 1; }
.user { cursor: pointer; display: flex; align-items: center; gap: 4px; color: var(--cwq-text); }
</style>
