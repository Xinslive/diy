import { defineStore } from 'pinia'
import { NAV, menuKeyOf } from '@/router/nav'

/**
 * 多标签工作区。
 *
 * 一条 tab 记的是「打开过哪些页面」，不是页面内容的副本 —— 所以这里
 * **不做 keep-alive**：切走再切回来，页面照常按当前账套 / 期间重新取数。
 * 缓存实例看着更快，但切账套、记账保存后回列表这些地方会拿到旧数据，
 * 而账务软件拿旧数据的代价比多打一次接口高得多。
 *
 * **不做 keep-alive 不等于「条件也一起丢掉」。** 重新取数是必须的（数据不能旧），
 * 但**取数用哪一组条件**是另一回事：用户设好的科目、期间、关键字不该因为他
 * 看了一眼别的页面就没了。所以标签额外记一个**完整地址**（带 query 的那个），
 * 点回标签时按它走，页面照常重新取数，只是用的是原来那组条件。
 */

/** 标签结构存哪儿。跟令牌同一份 localStorage，但**只存结构**，见下面两条注释。 */
const TABS_KEY = 'cwq_tabs'
/** 结构版本：以后往里加字段时，老结构认不出来就地丢掉重开，别去猜。 */
const TABS_VERSION = 1

/** 菜单里所有的 key（按归属映射折算过），用来校验恢复出来的标签还认不认得 */
const KNOWN_KEYS = new Set(NAV.flatMap((m) => m.items).map((it) => menuKeyOf(it.name)))

/**
 * 「有页面、没菜单」的路由 key。
 *
 * 「个人资料」的入口在右上角的账号菜单里，**不占侧栏** —— 但它照样是一个
 * 正常的标签页：开得起来、关得掉、刷新一次之后还在。
 *
 * KNOWN_KEYS 是**从菜单派生**的（菜单改名/下线时，靠它把旧标签丢掉），所以
 * 这一类页面必须**单独登记**。不登记的后果很具体：标签刚开出来还在，
 * 按一下 F5 刷新页面，标签就没了 —— 页面还开着，但标签栏里那条不见了，
 * 于是「关掉这一页」的把手也没了。
 */
const EXTRA_KEYS = new Set(['profile'])

/**
 * 这个 key 认不认得。
 *
 * 三处都认：菜单里的（KNOWN_KEYS）、有页面没菜单的（EXTRA_KEYS）、
 * 插件页（``plugin:`` 前缀，且要当前用户的 manifest 里确实有它）。
 */
const isKnownKey = (key, plugins = false) =>
  KNOWN_KEYS.has(key) || EXTRA_KEYS.has(key)
  || (plugins && String(key).startsWith('plugin:'))

/**
 * 一条 tab **只允许有这三个字段**，多一个都不写进 localStorage。
 *
 *  这条注释不是洁癖：「持久化」这个词挨着「顺手多带点东西」太近了。
 *  localStorage 是同源脚本都能读的，令牌已经在里面（见 api/client.js 的请求拦截器，
 *  项目约定就是令牌只放 localStorage），账套 id、期间 id 再顺手带进来一份，
 *  等于把「记住上次看哪儿」这个便利变成一份谁都能翻的账套目录。
 *  path 只留**路径**、连 query 都不留：路径是页面地址，至多一个凭证 id。
 *
 *  **带 query 的完整地址（`full`）只在内存里**，见下面 open() 那段 ——
 *  持久化这一条不许破：query 里夹着期间、金额、科目这些上下文。
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
      .map((t) => {
        const o = Object.fromEntries(TAB_FIELDS.map((k) => [k, typeof t[k] === 'string' ? t[k] : '']))
        // 从存储里读回来的标签没有 query（存储里就不许有），
        // 完整地址就是那个裸路径 —— 点它等于回到这一页的默认条件。
        o.full = o.path
        return o
      })
      // 认不出的 key（菜单改名 / 下线了）直接丢：标签栏里摆一条点进去 404 的标签，
      // 比少摆一条难看得多
      .filter((t) => isKnownKey(t.key, plugins))
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

/**
 * 标签记的**完整地址**（path + query + hash）。
 *
 *  vue-router 的 route.fullPath 就是它；万一碰上没有 fullPath 的 route
 *  （测试里手搓的那种），退回 path —— 少带一个 query 不至于让标签不能用。
 */
function fullPathOf(route) {
  const full = typeof route?.fullPath === 'string' ? route.fullPath : ''
  if (full.startsWith('/')) return full
  return typeof route?.path === 'string' ? route.path : ''
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
    /** 标签栏里除当前这一条之外还有没有别的标签（关标签的下拉用） */
    canClosePrev: (s) => s.opened.length > 1,
  },
  actions: {
    setPluginEntries(entries) {
      this.pluginItems = entries
      const allowed = new Map(entries.map(p => [p.menu.name, p.menu.route]))
      this.opened = this.opened.filter(t => isKnownKey(t.key) || allowed.get(t.key) === t.path)
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
     *
     *  **另外还记一个完整地址（full，带 query）**，只在内存里：
     *  页面条件（科目、期间、关键字…）住在 URL 的 query 里，而标签栏不做
     *  keep-alive —— 不记这个完整地址，点回标签时 URL 里的条件就丢了，
     *  用户设好的查询条件被重置成默认值（明细账的科目、凭证的关键字都跑不掉）。
     *  它不进 localStorage：query 里夹着期间、金额这些上下文，
     *  持久化只留裸 path（见 TAB_FIELDS）。
     */
    open(route) {
      const key = menuKeyOf(route?.name)
      if (!key || typeof route?.path !== 'string' || !route.path.startsWith('/')) return
      if (String(key).startsWith('plugin:') && !this.pluginItems.some(p => p.menu.name === key && p.menu.route === route.path)) return
      const full = fullPathOf(route)
      const hit = this.opened.find((t) => t.key === key)
      if (hit) {
        // 完整地址先更新：条件变了（只有 query 变）时，标签才知道该回到哪一步。
        // 这一步**不写存储** —— 存进去的还是那个裸 path。
        hit.full = full
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
        full,
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
      // 就近接管：后面还有就从后面顶上，没有就退回前一条。
      // 走**完整地址**：接班的那条标签带着它自己那组条件（科目、期间、关键字），
      // 只给裸路径的话，接班页面打开就是默认条件 —— 又是「条件被重置」。
      const next = (this.opened[i]?.full || this.opened[i]?.path)
        || (this.opened[i - 1]?.full || this.opened[i - 1]?.path)
      return { active: true, next }
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