import { defineStore } from 'pinia'
import { NAV, menuKeyOf } from '@/router/nav'

/**
 * 多标签工作区。
 *
 * 一条 tab 记的是「打开过哪些页面」，不是页面内容的副本 —— 所以这里
 * **不做 keep-alive**：切走再切回来，页面照常按当前账套 / 期间重新取数。
 * 缓存实例看着更快，但切账套、记账保存后回列表这些地方会拿到旧数据，
 * 而账务软件拿旧数据的代价比多打一次接口高得多。
 */

/** 标签结构存哪儿。跟令牌同一份 localStorage，但**只存结构**，见下面两条注释。 */
const TABS_KEY = 'cwq_tabs'
/** 结构版本：以后往里加字段时，老结构认不出来就地丢掉重开，别去猜。 */
const TABS_VERSION = 1

/** 菜单里所有的 key（按归属映射折算过），用来校验恢复出来的标签还认不认得 */
const KNOWN_KEYS = new Set(NAV.flatMap((m) => m.items).map((it) => menuKeyOf(it.name)))

/**
 * 一条 tab **只允许有这三个字段**，多一个都不写进 localStorage。
 *
 *  这条注释不是洁癖：「持久化」这个词挨着「顺手多带点东西」太近了。
 *  localStorage 是同源脚本都能读的，令牌已经在里面（见 api/client.js 的请求拦截器，
 *  项目约定就是令牌只放 localStorage），账套 id、期间 id 再顺手带进来一份，
 *  等于把「记住上次看哪儿」这个便利变成一份谁都能翻的账套目录。
 *  path 只留**路径**、连 query 都不留：路径是页面地址，至多一个凭证 id。
 */
const TAB_FIELDS = ['key', 'title', 'path']

/**
 * 恢复出来的标签**属于哪个用户**。
 *
 *  登出正常走一遍，MainLayout 会把标签清掉，所以多数时候用不上这道校验；
 *  用得上的是「浏览器直接被关掉」那次：标签还在 storage 里，
 *  而下一个用这台机器的人可能根本不是上一个。
 *  「下一个登录的人不该看见上一个用户开着的页面」在屏幕上做到了还不够，
 *  存在 storage 里的那份同样得做到。
 *
 *  这里只取 cwq_user 里已经有的 user id —— **不新增任何用户信息**，
 *  那份快照本来就在 localStorage（登录时写的），这里只是拿它当归属标记。
 */
function savedUserId() {
  try {
    return JSON.parse(globalThis.localStorage?.getItem('cwq_user') || 'null')?.id ?? null
  } catch {
    return null
  }
}

/**
 * localStorage 可能整个不可用（隐私模式 / 测试环境 / 被禁），那就只存不读 ——
 * 标签页照常能用，关掉浏览器就没了。存储失败绝不能带崩界面。
 */
function readTabs(plugins = false) {
  try {
    const raw = globalThis.localStorage?.getItem(TABS_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!parsed || parsed.version !== TABS_VERSION || !Array.isArray(parsed.opened)) return []
    // 存的时候人是 A，现在登录的是 B（或压根没登录）：一份都不恢复
    if (parsed.user_id != null && parsed.user_id !== savedUserId()) return []
    return parsed.opened
      .filter((t) => t && typeof t.key === 'string' && typeof t.path === 'string' && t.path.startsWith('/'))
      .map((t) => Object.fromEntries(TAB_FIELDS.map((k) => [k, typeof t[k] === 'string' ? t[k] : ''])))
      // 认不出的 key（菜单改名 / 下线了）直接丢：标签栏里摆一条点进去 404 的标签，
      // 比少摆一条难看得多
      .filter((t) => KNOWN_KEYS.has(t.key) || (plugins && t.key.startsWith("plugin:")))
  } catch {
    return []      // 存了坏数据 / 存不下：当作没有，重新开
  }
}

function saveTabs(opened) {
  try {
    const lean = opened.map((t) => Object.fromEntries(TAB_FIELDS.map((k) => [k, t[k]])))
    globalThis.localStorage?.setItem(
      TABS_KEY, JSON.stringify({ version: TABS_VERSION, user_id: savedUserId(), opened: lean })
    )
  } catch {
    // 写不进去就算了：标签页是「方便」，不是「功能」，存不上不该影响干活
  }
}

export const useTabsStore = defineStore('tabs', {
  state: () => ({
    pluginItems: [],
    pendingPluginTabs: readTabs(true).filter(t => t.key.startsWith("plugin:")),
    opened: readTabs(), // [{ key, title, path }]
    sidebarCollapsed: false,
  }),
  getters: {
    /** 菜单项 -> 标签页 key 的反查表，用来把标题从 nav 那边借过来 */
    titles: (s) => {
      const m = {}
      for (const mod of NAV) for (const it of mod.items) m[it.name] = it.title
      for (const p of s.pluginItems) m[p.menu.name] = p.menu.title
      return m
    },
    /** 当前标签在标签栏里的位置，-1 表示还没打开（理论上不会发生） */
    indexOfKey: (s) => (key) => s.opened.findIndex((t) => t.key === key),
    canClosePrev: (s) => s.opened.length > 1,
  },
  actions: {
    setPluginEntries(entries) {
      this.pluginItems = entries
      const allowed = new Map(entries.map(p => [p.menu.name, p.menu.route]))
      this.opened = this.opened.filter(t => KNOWN_KEYS.has(t.key) || allowed.get(t.key) === t.path)
      // 只在当前用户 manifest 校验后恢复保存的插件标签。
      for (const t of this.pendingPluginTabs) if (allowed.get(t.key) === t.path && !this.opened.some(x => x.key === t.key)) this.opened.push({ ...t, title: entries.find(p => p.menu.name === t.key).menu.title })
      this.pendingPluginTabs = []
      saveTabs(this.opened)
    },
    /**
     * 切路由时调用：开着就只激活，没开就补一个。
     *
     *  **path 永远跟着最后打开的那一页走**，这条记录的全部意义就在这儿：
     *  关掉标签再点回来要回到刚才那一页，而不是第一次打开时的那个地址。
     *  凭证详情最能看出差别 —— 连着翻 1581、1582 再翻回列表，
     *  标签记的必须是列表；自动保存把地址从 /vouchers/new 换成 /vouchers/{id}
     *  之后，记的必须是新地址（VoucherEdit 保存完会显式调一次本方法：
     *  那次只改地址栏、不走 router，等不到路由名的通知）。
     */
    open(route) {
      const key = menuKeyOf(route?.name)
      if (!key || typeof route?.path !== 'string' || !route.path.startsWith('/')) return
      if (String(key).startsWith('plugin:') && !this.pluginItems.some(p => p.menu.name === key && p.menu.route === route.path)) return
      const hit = this.opened.find((t) => t.key === key)
      if (hit) {
        // 地址没变就**连存储都不写**：调用方是路由 watcher，翻页、筛选、后退
        // 都会叫到这儿，每次都 JSON.stringify 一份再 setItem 纯属白刷。
        // 写存储不是没有副作用的动作，调用频次一高就该先问一句「真的变了吗」。
        if (hit.path === route.path) return
        hit.path = route.path
        saveTabs(this.opened)
        return
      }
      this.opened.push({
        key,
        title: this.titles[key] || route.meta?.title || String(route.name),
        path: route.path,
      })
      saveTabs(this.opened)
    },
    /**
     * 关掉一条标签。
     *
     * 关掉的不是当前标签 -> next 为 null，调用方不用导航。
     * 关掉的正是当前标签 -> 得有人接盘，返回顺位该去的新 path；
     * 标签全关光了则回落到首页，由调用方决定去哪儿。
     * @returns {{active: boolean, next: string|null}}
     */
    close(key, activeKey) {
      const i = this.opened.findIndex((t) => t.key === key)
      if (i < 0) return { active: false, next: null }
      const wasActive = this.opened[i].key === activeKey
      this.opened.splice(i, 1)
      saveTabs(this.opened)
      if (!wasActive) return { active: false, next: null }
      if (!this.opened.length) return { active: true, next: '/' }
      // 就近接管：后面还有就从后面顶上，没有就退回前一条
      return { active: true, next: this.opened[i]?.path || this.opened[i - 1]?.path }
    },
    closeOthers(key) {
      this.opened = this.opened.filter((t) => t.key === key)
      saveTabs(this.opened)
    },
    closeAll() {
      this.opened = []
      saveTabs(this.opened)
    },
    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed
    },
    /**
     * 登出：标签**和持久化那份一起**清掉。
     * 下一个登录的人不该看见上一个用户开着的页面 —— 屏幕上的清掉还不够，
     * 存在 localStorage 里的那份同样得清，否则下次登录标签栏又回来了。
     */
    reset() {
      this.opened = []
      this.pluginItems = []
      this.pendingPluginTabs = []
      saveTabs(this.opened)
    },
  },
})