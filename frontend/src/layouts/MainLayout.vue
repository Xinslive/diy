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
        <el-menu-item v-if="store.isAdmin" index="/audit"><el-icon><Document /></el-icon>操作日志</el-menu-item>
        <el-menu-item v-if="store.isAdmin" index="/users"><el-icon><User /></el-icon>用户与授权</el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header height="52px" class="topbar no-print">
        <!-- 只显示账套名：公司名只是账套的属性，切账套时不需要看 -->
        <el-select
          v-model="store.bookId"
          size="default"
          style="width: 180px"
          @change="onBookChange"
        >
          <el-option
            v-for="b in store.books"
            :key="b.id"
            :label="b.name"
            :value="b.id"
          />
        </el-select>

        <!-- 年份和月份拆成两个下拉：
             账套跨多年，一个下拉塞 24 个月份，鼠标得划过半个屏幕。
             拆开后「哪一年」最多几个选项，「几月」固定 12 个，两下点完。 -->
        <el-select v-model="selYear" size="default" style="width: 104px" @change="onYearChange">
          <el-option v-for="y in availableYears" :key="y" :label="`${y} 年`" :value="y" />
        </el-select>
        <el-select v-model="selMonth" size="default" style="width: 122px" @change="onMonthChange">
          <el-option v-for="p in monthsOfYear" :key="p.id" :label="`${p.month} 月`" :value="p.month">
            <!-- 选项行本身是 .el-select-dropdown__item，display 是 list-item，
                 不是 flex。所以对齐不能指望 float 或 margin-left:auto：
                 float 会把标签提出行内流、跟月份文字上下错开一小截。
                 自己套一层 flex 才能既贴右又上下居中。 -->
            <span class="period-opt">
              <span>{{ p.month }} 月</span>
              <el-tag v-if="p.locked" size="small" type="success">已结账</el-tag>
              <el-tag v-else-if="p.unposted > 0" size="small" type="warning">未记账 {{ p.unposted }}</el-tag>
            </span>
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
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { ArrowDown, Calendar, Coin, Collection, DataAnalysis, Document, Folder, Grid, List, Notebook, Printer, Tickets, User } from '@element-plus/icons-vue'

const store = useSessionStore()
const route = useRoute()
const router = useRouter()

// 切换账套/期间后让当前页面重新取数
const viewKey = computed(() => `${route.name}-${store.bookId}-${store.periodId}`)

// 年份下拉：这套账有哪几年，最新的排最前
const availableYears = computed(() =>
  [...new Set(store.periods.map((p) => p.year))].sort((a, b) => b - a)
)

// 当前看的是哪一年、哪一月 —— 直接从正在看的期间反推，
// 不另存一份，免得两边对不上
const selYear = computed(() => store.currentPeriod?.year ?? store.currentYear)
const selMonth = computed(() => store.currentPeriod?.month)

// 月份下拉：只列这一年真实存在的期间（启用月份可能不是 1 月）
const monthsOfYear = computed(() =>
  store.periods
    .filter((p) => p.year === selYear.value)
    .sort((a, b) => a.month - b.month)
)

async function onBookChange(id) {
  await store.setBook(id)
}
// 切年份：落在该年「正在用的」那个期间（没结账的最后一期，没凭证的取最多那张）
function onYearChange(y) {
  store.setYear(y)
}
// 切月份：直接落到那一个月
function onMonthChange(m) {
  const target = store.periods.find((p) => p.year === selYear.value && p.month === m)
  if (target) store.setPeriod(target.id)
}
function onCommand(cmd) {
  if (cmd === 'logout') {
    store.logout()
    router.push('/login')
  }
}

onMounted(async () => {
  await store.refreshMe()
  // loadBooks 内部一定会跟一次 loadPeriods，这里不再单独 watch bookId ——
  // 否则一次切账套会打两遍接口，期间下拉还会闪一次空列表
  if (!store.books.length || !store.periods.length) await store.loadBooks()
})
</script>

<style scoped>
.shell { height: 100vh; }

/* 下拉选项自己排版：文字在左、标记贴右，且上下严格居中对齐 */
.period-opt { display: flex; align-items: center; }
.period-opt .el-tag { margin-left: auto; }

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
