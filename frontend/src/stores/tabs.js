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
export const useTabsStore = defineStore('tabs', {
  state: () => ({
    opened: [], // [{ key, title, path }]
    sidebarCollapsed: false,
  }),
  getters: {
    /** 菜单项 -> 标签页 key 的反查表，用来把标题从 nav 那边借过来 */
    titles: () => {
      const m = {}
      for (const mod of NAV) for (const it of mod.items) m[it.name] = it.title
      return m
    },
    /** 当前标签在标签栏里的位置，-1 表示还没打开（理论上不会发生） */
    indexOfKey: (s) => (key) => s.opened.findIndex((t) => t.key === key),
    canClosePrev: (s) => s.opened.length > 1,
  },
  actions: {
    /** 切路由时调用：开着就只激活，没开就补一个 */
    open(route) {
      const key = menuKeyOf(route.name)
      if (!key) return
      const hit = this.opened.find((t) => t.key === key)
      if (hit) {
        hit.path = route.path
        return
      }
      this.opened.push({
        key,
        title: this.titles[key] || route.meta?.title || String(route.name),
        path: route.path,
      })
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
      if (!wasActive) return { active: false, next: null }
      if (!this.opened.length) return { active: true, next: '/' }
      // 就近接管：后面还有就从后面顶上，没有就退回前一条
      return { active: true, next: this.opened[i]?.path || this.opened[i - 1]?.path }
    },
    closeOthers(key) {
      this.opened = this.opened.filter((t) => t.key === key)
    },
    closeAll() {
      this.opened = []
    },
    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed
    },
    reset() {
      this.opened = []
    },
  },
})
