<template>
  <!-- --sb-w 跟着折叠状态变：侧栏是一整条通栏的固定列，
       顶栏/标签栏/内容统统从它右边开始，所以这个宽度就是左边那条留白。 -->
  <div class="shell" :class="{ 'sb-off': tabs.sidebarCollapsed }">
    <!-- 收起时不是整条消失，而是收成一条图标带（220px ⇄ 60px）。
         菜单项还在、位置也不跳，只是没了文字；靠 title 提示是什么。 -->
    <aside class="sidebar no-print">
      <div class="sb-head" :title="shownModule?.title">
        <el-icon><component :is="shownModule?.icon" /></el-icon>
        <span class="sb-label">{{ shownModule?.title }}</span>
      </div>
      <ul class="sb-list">
        <!-- role + tabindex + title 三样都要：收起后只剩图标，
             没有可访问名的话读屏用户会听到一串「图像」，完全不知道是哪个菜单。
             tabindex 则是让它能靠键盘走到 —— 鼠标能点的，键盘也得能到。 -->
        <li v-for="it in shownModule?.items || []" :key="it.name"
            role="button" tabindex="0" :aria-label="it.title" :title="it.title"
            :aria-current="it.name === activeKey ? 'page' : null"
            :class="{ active: it.name === activeKey }"
            @click="router.push({ name: it.name })"
            @keydown.enter="router.push({ name: it.name })">
          <el-icon><component :is="it.icon" /></el-icon><span class="sb-label">{{ it.title }}</span>
        </li>
      </ul>
    </aside>

    <div class="main">
    <!-- ── 顶部：折叠按钮 + 模块菜单 + 全局账套/期间 + 账号 ─────────── -->
    <header class="topbar no-print">
      <button class="icon-btn" :title="tabs.sidebarCollapsed ? '展开菜单' : '收起菜单'" @click="tabs.toggleSidebar()">
        <el-icon><Fold v-if="!tabs.sidebarCollapsed" /><Expand v-else /></el-icon>
      </button>

      <nav class="modules">
        <button
          v-for="m in nav"
          :key="m.key"
          class="module"
          :class="{ active: m.key === activeModule }"
          @click="onModuleClick(m)"
        >
          <el-icon><component :is="m.icon" /></el-icon>{{ m.title }}
        </button>
      </nav>

      <div class="spacer" />

      <!-- 账套/期间是全局上下文，跟着顶栏走。菜单挪到左边之后这里成了
           顶栏唯一的「我现在在看哪本账」提示，不能再往侧栏里塞。 -->
      <el-select v-model="store.bookId" class="ctx-select" @change="onBookChange">
        <el-option v-for="b in store.books" :key="b.id" :label="b.name" :value="b.id" />
      </el-select>
      <el-select v-model="selYear" class="ctx-select ctx-year" @change="onYearChange">
        <el-option v-for="y in availableYears" :key="y" :label="`${y} 年`" :value="y" />
      </el-select>
      <el-select
        v-model="selMonth"
        class="ctx-select ctx-year"
        popper-class="period-popper"
        @change="onMonthChange"
      >
        <el-option v-for="p in monthsOfYear" :key="p.id" :label="`${p.month} 月`" :value="p.month">
          <span class="period-opt">
            <span>{{ p.month }} 月</span>
            <el-tag v-if="p.locked" size="small" type="success">已结账</el-tag>
          </span>
        </el-option>
      </el-select>

      <el-tag v-if="store.currentBook?.role === 'VIEWER'" type="info" class="ctx-tag">只读</el-tag>
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
    </header>

    <!-- ── 标签栏：打开过的页面，可关闭、可左右翻 ───────────────────── -->
    <div class="tabbar no-print">
      <button class="tb-btn" :disabled="!tabs.canClosePrev" title="关闭上一页" @click="closePrev">
        <el-icon><ArrowLeft /></el-icon>
      </button>
      <button class="tb-btn" :disabled="!tabs.canClosePrev" title="关闭下一页" @click="closeNext">
        <el-icon><ArrowRight /></el-icon>
      </button>
      <button class="tb-btn" title="回到首页" @click="router.push('/books')">
        <el-icon><HomeFilled /></el-icon>
      </button>

      <div class="tb-scroll">
        <!-- 标签本身要能聚焦、关闭按钮要是真 button：
             一律用 div + 图标的话，关闭按钮在无障碍树里根本不存在，
             读屏用户跳不到，纯图标按钮也点不中（只有 8×8 的 svg 命中区）。 -->
        <div v-for="t in tabs.opened" :key="t.key" class="tb-tab" :class="{ active: t.key === activeKey }"
             role="tab" :aria-selected="t.key === activeKey" tabindex="0"
             @click="router.push(t.path)"
             @keydown.enter="router.push(t.path)">
          <span>{{ t.title }}</span>
          <button v-if="tabs.opened.length > 1" type="button" class="tb-close"
                  :aria-label="`关闭 ${t.title}`" title="关闭" @click.stop="closeTab(t.key)">
            <el-icon><Close /></el-icon>
          </button>
        </div>
      </div>

      <el-dropdown class="tb-more" @command="onTabCommand">
        <button class="tb-btn" title="标签操作"><el-icon><DArrowRight /></el-icon></button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="others" :disabled="!tabs.canClosePrev">关闭其他</el-dropdown-item>
            <el-dropdown-item command="all" :disabled="!tabs.canClosePrev">关闭全部</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>

      <main class="content">
        <router-view :key="viewKey" />
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { useTabsStore } from '@/stores/tabs'
import { landingOf, menuKeyOf, visibleNav } from '@/router/nav'
import { pluginEntries } from '@/plugins/runtime'
import {
  ArrowDown, ArrowLeft, ArrowRight, Close, DArrowRight, Expand, Fold, HomeFilled,
} from '@element-plus/icons-vue'

const store = useSessionStore()
const tabs = useTabsStore()
const route = useRoute()
const router = useRouter()

const nav = computed(() => visibleNav(store.isAdmin, pluginEntries.value))

// 宿主页面按账套/期间重新取数；插件保留自己选择的目标账套和未保存预览。
const viewKey = computed(() => route.meta.pluginId
  ? `plugin-${route.meta.pluginId}`
  : `${route.name}-${store.bookId}-${store.periodId}`)

/** 当前标签页 / 侧栏高亮的 key —— 凭证三种路由共用一个入口，所以不能直接用 route.name */
const activeKey = computed(() => menuKeyOf(route.name))

/** 当前该亮哪个顶部模块：以当前路由在菜单里的归属为准，而不是记住上次点的那个。
 *  记住会出岔子：手敲地址栏进 /users 时顶部还亮着「凭证处理」，侧栏也跟着空。 */
const activeModuleObj = computed(
  () => nav.value.find((m) => m.items.some((it) => it.name === activeKey.value)) || null
)
const activeModule = ref('')

/** 侧栏显示哪个模块 —— 跟顶部高亮的是同一个。
 *  侧栏如果自己去按 route 推，点了顶部「凭证处理」却还列着「科目余额表」，
 *  顶部亮着一个模块、侧边列着另一个模块的菜单，两处对不上。 */
const shownModule = computed(
  () => nav.value.find((m) => m.key === activeModule.value) || activeModuleObj.value
)

/**
 * 点顶部模块：换侧栏，并在跨模块时落到这个模块的入口页
 * （凭证处理 → 凭证，账簿报表 → 明细账，基础资料 → 账套管理）。
 *
 * 已经在这个模块里就**只换侧栏不动页面** —— 正看着科目余额表时点一下
 * 「账簿报表」被弹回明细账，手上的位置就白费了。
 */
function onModuleClick(m) {
  activeModule.value = m.key
  if (activeModuleObj.value?.key === m.key) return
  router.push({ name: landingOf(m) })
}

// 只在**路由本身**变了的时候，把顶部模块拉回当前页所属的模块。
// 不能反过来把 activeModule 绑死在点击上：手敲地址栏进 /users 时，
// 顶部必须跟着亮「系统管理」，不能停在用户上次点的「凭证处理」。
watch(
  activeModuleObj,
  (m) => { if (m) activeModule.value = m.key },
  { immediate: true }
)

// 年份下拉：这套账有哪几年，最新的排最前
const availableYears = computed(() =>
  [...new Set(store.periods.map((p) => p.year))].sort((a, b) => b - a)
)

// 当前看的是哪一年、哪一月 —— 直接从正在看的期间反推，不另存一份
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
function onYearChange(y) {
  store.setYear(y)
}
function onMonthChange(m) {
  const target = store.periods.find((p) => p.year === selYear.value && p.month === m)
  if (target) store.setPeriod(target.id)
}
function onCommand(cmd) {
  if (cmd === 'logout') {
    store.logout()
    tabs.reset()
    router.push('/login')
  }
}

/* ── 标签栏 ─────────────────────────────────────────────────────── */
/**
 * 路由一变就把当前页登记进标签栏。
 *
 * **path 也要 watch，不能只看 name。** 凭证页的「上一张 / 下一张」走的是
 * **同一个 route name、不同 path**（/vouchers/12 → /vouchers/13）——
 * name 一个字没变，原来那条 `watch(() => route.name, ...)` 根本不触发，
 * 标签记的 path 就永远停在第一张：关掉标签再点回来会跳到**另一张凭证**，
 * 人只会以为凭证丢了。（自动保存那次是 VoucherEdit 自己显式调的，那是个例，
 * 这条 watcher 的口子本身还在。）
 *
 * 拼成**一个字符串**再 watch，而不是 `[route.name, route.path]`：
 * 数组每次求值都是新引用，watcher 按引用比，等于什么依赖一动就触发一次。
 * 字符串按值比，name 和 path 都没真变时不会触发。
 * route.path 不含 query，所以只改筛选条件不会惊动标签（标签存的也是 path）。
 *
 * 触发得再频也不怕：tabs.open 对已存在的标签**只改 path**，不改标题、不动脏状态，
 * 而且地址没变时直接返回、连存储都不写（见 stores/tabs.js 的 open）。
 */
watch(() => `${route.name} ${route.path}`, () => tabs.open(route), { immediate: true })

function closeTab(key) {
  const { next } = tabs.close(key, activeKey.value)
  if (next) router.push(next)
}

function closePrev() {
  const i = tabs.indexOfKey(activeKey.value)
  if (i > 0) closeTab(tabs.opened[i - 1].key)
}
function closeNext() {
  const i = tabs.indexOfKey(activeKey.value)
  if (i >= 0 && i < tabs.opened.length - 1) closeTab(tabs.opened[i + 1].key)
}

function onTabCommand(cmd) {
  if (cmd === 'others') {
    tabs.closeOthers(activeKey.value)
  } else if (cmd === 'all') {
    tabs.closeAll()
    router.push('/books')
  }
}

// 登出把标签清掉：下一个登录的人不该看见上一个用户开着的页面
watch(() => store.token, (t, old) => { if (old && !t) tabs.reset() })

// 离开账套页就清掉「刚建好账套」的提示，免得下次回来又弹一遍已经过期的引导
watch(
  () => route.name,
  (name) => {
    if (name !== 'books') store.setLastCreatedBook(0)
  }
)

onMounted(async () => {
  await store.refreshMe()
  if (!store.books.length || !store.periods.length) await store.loadBooks()
})
</script>
