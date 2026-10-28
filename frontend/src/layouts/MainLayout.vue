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
            @keydown.enter="router.push({ name: it.name })"
            @mouseenter="prefetch(it.name)"
            @focus="prefetch(it.name)">
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
          @mouseenter="prefetch(landingOf(m))"
          @focus="prefetch(landingOf(m))"
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
      <span v-if="store.contextRefreshing" class="context-sync" aria-live="polite">同步中</span>
      <span v-if="syncError" class="context-sync context-error" aria-live="polite" :title="syncError">
        同步失败，显示上次数据
      </span>
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
            <!-- 「个人资料」排在「退出登录」**上面**：
                 这两件事的先后顺序就是人对「我要走了」的直觉顺序 ——
                 先看看自己的资料，最后才是走。 -->
            <el-dropdown-item command="profile">个人资料</el-dropdown-item>
            <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </header>

    <!-- ── 标签栏：打开过的页面，可关闭、可刷新 ─────────────────────── -->
    <div class="tabbar no-print">
      <!-- 刷新：把当前页重新拉一遍。
           原来的两个「关闭上一页 / 关闭下一页」按钮去掉了 ——
           关标签这件事右侧的「标签操作」下拉已经能做（关闭其他 / 关闭全部），
           而「翻到相邻那一页再把它关掉」这个动作本身很容易误伤：
           标签排到一屏开不下时，这两支按钮点下去关掉的是**人没打算关的那一条**。 -->
      <button class="tb-btn" :disabled="refreshing"
              :title="refreshing ? '正在刷新' : '刷新当前页'" @click="refreshView">
        <el-icon><Refresh /></el-icon>
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
             @click="router.push(t.full || t.path)"
             @keydown.enter="router.push(t.full || t.path)">
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
      <PageSkeleton v-if="showRouteSkeleton" />
      <!-- 数据还在路上时，这一层**不能 display:none**（见 main.css 里 .route-page 的说明）。
           v-show 就是 display:none：那一藏，整棵子树没有布局盒，
           页面里量自己尺寸的地方量到的全是 0 —— 凭证表的列宽塌成 0、
           摘要与科目名塌成 0 高，界面看着像「没数据」，其实是「没盒子」。 -->
      <div class="route-page" :class="{ 'is-pending': showRouteSkeleton }">
        <router-view v-slot="{ Component }">
          <Suspense timeout="0">
            <component :is="Component" :key="viewKey" />
            <template #fallback>
              <PageSkeleton />
            </template>
          </Suspense>
        </router-view>
      </div>
    </main>
    </div>

    <!-- 高危动作撞到「近期二次验证」闸门时的引导（审计 §3.2）。
         挂在壳层而不是各个页面里：闸门管的是**全局**的 13 条受管控写路由，
         哪个页面发起的它不知道，而没有这个对话框，管理员就只看到一句 403
         —— 界面上没有下一步，撞死路。 -->
    <MfaReauthDialog />
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { useTabsStore } from '@/stores/tabs'
import { landingOf, menuKeyOf, visibleNav } from '@/router/nav'
import { prefetchRouteChunk } from '@/utils/routePrefetch'
import { pluginEntries } from '@/plugins/runtime'
import MfaReauthDialog from '@/components/MfaReauthDialog.vue'
import PageSkeleton from '@/components/PageSkeleton.vue'
import queryCache from '@/data/queryCache'
import {
  ArrowDown, Close, DArrowRight, Expand, Fold, HomeFilled, Refresh,
} from '@element-plus/icons-vue'

const store = useSessionStore()
const tabs = useTabsStore()
const route = useRoute()
const router = useRouter()

const routeDataLoading = ref(false)
const showRouteSkeleton = computed(() => routeDataLoading.value || store.contextStatus === 'loading')
const syncError = ref('')
const syncErrorKey = ref('')
const pendingInitialQueries = new Set()
let routeLoadGeneration = 0
let routeLoadTimer = null

function settleRouteLoad(generation, delay = 100) {
  clearTimeout(routeLoadTimer)
  routeLoadTimer = setTimeout(() => {
    if (generation !== routeLoadGeneration || pendingInitialQueries.size) return
    if (store.contextStatus === 'loading') return
    routeDataLoading.value = false
  }, delay)
}

function beginRouteLoad() {
  const generation = ++routeLoadGeneration
  clearTimeout(routeLoadTimer)
  pendingInitialQueries.clear()
  syncError.value = ''
  syncErrorKey.value = ''
  routeDataLoading.value = true
  // Let cached requests paint first. A cache miss keeps the skeleton until the
  // shared query emits success/error; requests outside the cache layer get a
  // short fallback window instead of a blank route.
  settleRouteLoad(generation, 40)
}

watch(() => store.contextStatus, (status) => {
  if (status === 'ready' || status === 'error' || status === 'snapshot-error') {
    const generation = routeLoadGeneration
    void nextTick(() => settleRouteLoad(generation, 100))
  }
})

const stopQueryTracking = queryCache.subscribeAll((event) => {
  if (event.type === 'fetching' && !event.background && routeDataLoading.value) {
    pendingInitialQueries.add(event.key)
    clearTimeout(routeLoadTimer)
  } else if ((event.type === 'success' || event.type === 'error')
      && pendingInitialQueries.delete(event.key)) {
    settleRouteLoad(routeLoadGeneration, 100)
  }

  const currentBookTag = store.bookId ? `book:${store.bookId}` : ''
  const tags = event.entry?.tags || []
  const belongsToCurrentContext = !currentBookTag
    || tags.includes(currentBookTag)
    || !tags.some((tag) => /^book:\d+$/.test(tag))
  if (!belongsToCurrentContext) return

  if (event.type === 'error' && event.background) {
    syncError.value = '后台刷新失败，当前页面仍显示上次可用数据'
    syncErrorKey.value = event.key
  } else if (event.type === 'success' && syncErrorKey.value === event.key) {
    syncError.value = ''
    syncErrorKey.value = ''
  }
})

const nav = computed(() => visibleNav(store.isAdmin, pluginEntries.value))

// 宿主页面按账套/期间重新取数；插件保留自己选择的目标账套和未保存预览。
//
// **期初余额页例外**：它的口径是**年度**，跟月份无关。所以它的 key 用年度
// 而不是期间 —— 否则顶栏切一下月份就重挂整页，用户刚录了一半的数直接没了，
// 而页面上摆的还是同一份数（换了页还得重新拉一次，白白闪一下）。
//
// **末尾那一段是刷新计数**：刷新按钮加一 → key 变 → 当前页重挂 →
// 页面自己按当前账套/期间重新取数。这是「刷新」在本应用里的实现方式，
// 不是整页 reload（理由见 refreshView 的注释）。
const viewKey = computed(() => {
  if (route.meta.pluginId) return `plugin-${route.meta.pluginId}`
  const scope = route.name === 'openings' ? store.currentYear : store.periodId
  return `${route.name}-${store.bookId}-${scope}-${refreshKey.value}`
})

/**
 * 刷新当前页。
 *
 * **重挂当前页，而不是整页 reload。** 整页 reload 要重新走一遍路由守卫、
 * 重新登录态恢复，还会把「停在哪个账套、哪一期」重算一遍 ——
 * 而那套落位规则会把人带回「最早还在用的那个月」，
 * 于是「刷新一下看看」这个动作顺带把位置也挪了。重挂只换当前这一页，
 * 标签、账套、期间都原样不动。
 *
 * **先拉账套/期间，再重挂。** 顶栏的账套与期间是全局上下文：
 * 别人刚把 12 月结账了，月份下拉就该跟着变，而页面自己只认「当前这一期」。
 *
 * **插件页不重挂。** 插件保留它自己选的目标账套和没保存的预览
 * （见上面 viewKey 的注释），重挂等于替用户把没存的东西扔了。
 * 插件页上这个按钮只刷新全局上下文，页面内容由插件自己管。
 */
const refreshKey = ref(0)
const refreshing = ref(false)
watch(viewKey, beginRouteLoad, { immediate: true, flush: 'sync' })

async function refreshView() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    queryCache.invalidate({ scope: 'book', bookId: store.bookId })
    await store.bootstrap({ force: true })
  } catch {
    // 拉不到也让页面重挂：它自己取数时会再报一次错。
    // 在这里吞掉、页面却还是旧数据，才是最坏的那种「点了没反应」。
  } finally {
    refreshKey.value += 1
    refreshing.value = false
  }
}

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
 *  顶部亮着一个模块、侧边列着另一个模块的菜单，两处对不上。
 *
 *  **末尾那级兜底**：个人资料页不属于任何模块，直接敲地址栏进来时
 *  「当前模块」是空的 —— 这时候侧栏退到第一个模块，而不是留一条空白的
 *  侧栏（一个空图标 + 空的菜单区，看起来像坏了）。从界面上点进来时
 *  上一级兜底就够用：activeModule 还留着上一个模块，侧栏不变，
 *  人不会觉得「页面跳走了」。 */
const shownModule = computed(
  () => nav.value.find((m) => m.key === activeModule.value)
    || activeModuleObj.value
    || nav.value[0]
)

/**
 * 悬停/聚焦菜单项时，把那一页的代码块先下载下来。
 *
 * 单页应用换页不换 HTML，换的是路由对应的 JS 代码块 —— 所以预取的对象
 * 是「代码块」而不是「页面」。点进去时那份代码已经在浏览器里了，
 * 不用等网络往返（几百 KB 的页面代码块，这一等就是好几秒）。
 *
 * 失败无所谓：预取只是快一点，不预取照样能进。
 */
function prefetch(name) {
  prefetchRouteChunk(router, name)
}

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

/**
 * 换账套。
 *
 * 注意上面那行是 ``v-model="store.bookId"`` —— v-model 会**先**把新账套
 * 写进 store，这个 @change 再进来时 bookId 早就是新的了。所以「旧期间要不要清」
 * 这件事**不能**由这里判断，store 自己记了一份「手上这批期间是谁的」
 * （periodsBookId），setBook 按那份判据走。详见 stores/session.js 与
 * ARCHITECTURE.md 1.7 节。
 */
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
  } else if (cmd === 'profile') {
    // 走路由名而不是写死的路径：路径写两遍，改一处忘一处就是 404。
    router.push({ name: 'profile' })
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
 *
 * **watch 的是 fullPath，不是 path** —— 页面条件（科目、期间、关键字）住在
 * query 里，只改条件时 path 一个字没动，但标签必须跟着更新它记的**完整地址**，
 * 否则点回标签时条件没了（见 stores/tabs.js 里 full 那段）。
 * 只改条件**不会动标签栏的结构**，也**不写 localStorage**：存进去的仍是裸 path。
 *
 * 触发得再频也不怕：tabs.open 对已存在的标签**只改地址**，不改标题、不动脏状态，
 * 而且裸地址没变时直接返回、连存储都不写（见 stores/tabs.js 的 open）。
 */
watch(() => `${route.name} ${route.fullPath || route.path}`, () => {
  tabs.open(route)
  // **换了页面就别再提「上一本账」**：store 里那份 prevBookId 是给凭证页的
  // 「撞号」提示用的（见 stores/session.js）。用户换到别的页面再回来，
  // 那条提示就不该再冒出来 —— 否则会对着一个他压根没打开过的凭证说
  // 「你原来看的是哪本账的哪张」（审计 §3.33）。
  //
  // **切账套不会走到这里**：切账套时路由不变（变的只是顶栏那个下拉），
  // 所以这个 watcher 不触发，那个记号活到凭证页重挂之后被取走。
  store.prevBookId = 0
  // 「刚建出来的那张新凭证」也是**上一段路**的事（审计 N5）：
  // 用户点「新增凭证」重新开一张时，不该被带回刚才那张 ——
  // 那个记号只对「同一段路里的重挂」有用（重挂不换路由，所以这里也不会清它）。
  store.createdVoucher = null
}, { immediate: true })

function closeTab(key) {
  const { next } = tabs.close(key, activeKey.value)
  if (next) router.push(next)
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

onMounted(() => {
  // 上下文快照已经能让壳层立即渲染；权威 bootstrap 在后台完成。
  void store.bootstrap().catch(() => {})
})

onUnmounted(() => {
  clearTimeout(routeLoadTimer)
  stopQueryTracking()
})
</script>
