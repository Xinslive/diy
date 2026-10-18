import * as Vue from 'vue'
import { validatePlugin } from './manifest'
import { fetchManifest, pluginClient, pluginServices } from './api'

export const pluginEntries = Vue.shallowRef([])
import { PluginUnavailable as Unavailable } from './unavailable'
export { PluginUnavailable } from './unavailable'

// 宿主装配依赖，避免 api/client → session → runtime 的循环。
export function createPluginRuntime({ router, session, tabs, fetch = fetchManifest, importer = url => import(/* @vite-ignore */ url), document = globalThis.document, entries = pluginEntries }) {
  const installed = new Map()
  let epoch = 0
  let identity = ''
  let pending = null
  const currentIdentity = () => session.token && session.user?.id ? `${session.user.id}:${session.token}` : ''
  function remove(id) {
    const item = installed.get(id)
    if (!item) return
    item.removeRoute()
    for (const link of item.styles) link.remove()
    installed.delete(id)
  }
  function syncEntries() {
    entries.value = [...installed.values()].map(item => item.plugin)
    tabs.setPluginEntries(entries.value)
  }
  function clear() {
    epoch += 1
    pending = null
    identity = ''
    for (const id of [...installed.keys()]) remove(id)
    syncEntries()
  }
  async function refresh() {
    const key = currentIdentity()
    if (!key) { clear(); return [] }
    if (key !== identity) { if (identity) clear(); identity = key }
    if (pending) return pending
    const ticket = epoch
    const work = (async () => {
      let payload
      try { payload = await fetch() } catch {
        if (ticket === epoch) { for (const id of [...installed.keys()]) remove(id); syncEntries() }
        return []
      }
      if (ticket !== epoch || key !== currentIdentity()) return []
      const valid = new Map(), duplicates = new Set()
      for (const source of Array.isArray(payload?.plugins) ? payload.plugins : []) {
        try {
          const p = validatePlugin(source)
          if (valid.has(p.id) || duplicates.has(p.id)) { valid.delete(p.id); duplicates.add(p.id); throw new Error('重复插件') }
          valid.set(p.id, p)
        } catch (error) { console.warn('插件元数据被拒绝', error.message) }
      }
      for (const [id, item] of installed) if (!valid.has(id) || valid.get(id).digest !== item.plugin.digest) remove(id)
      for (const [id, plugin] of valid) {
        if (installed.has(id)) continue
        if (router.hasRoute(plugin.menu.name)) continue
        const styles = []
        const component = Vue.defineAsyncComponent({
          loader: async () => {
            const module = await importer(plugin.entry)
            if (ticket !== epoch || !installed.has(id) || currentIdentity() !== key) throw new Error('插件授权已变化')
            for (const href of plugin.styles) {
              const link = document?.createElement('link')
              if (link) { link.rel = 'stylesheet'; link.href = href; document.head.appendChild(link); styles.push(link) }
            }
            if (typeof module.default !== 'function') throw new Error('插件必须导出组件工厂')
            const api = pluginClient(id)
            const host = Object.freeze({ h: Vue.h, ref: Vue.ref, computed: Vue.computed, onMounted: Vue.onMounted,
              onUnmounted: Vue.onUnmounted, watch: Vue.watch, api, services: pluginServices(api),
              bookId: () => session.bookId, periodId: () => session.periodId,
              books: () => (session.books || []).map(book => ({
                id: book.id, name: book.name, companyName: book.company_name,
                status: book.status, role: book.role,
                canWrite: ['OWNER', 'ACCOUNTANT'].includes(String(book.role || '').trim().toUpperCase())
              })) })
            return module.default(host)
          },
          errorComponent: Unavailable, timeout: 30000,
        })
        const removeRoute = router.addRoute('workspace', { path: plugin.menu.route, name: plugin.menu.name,
          component, meta: { title: plugin.menu.title, pluginId: id } })
        installed.set(id, { plugin, removeRoute, styles })
      }
      syncEntries()
      return entries.value
    })()
    pending = work
    try { return await work } finally { if (pending === work) pending = null }
  }
  async function routeGuard(to) {
    if (!to.path.startsWith('/plugin/')) return true
    await refresh()
    const item = [...installed.values()].find(it => it.plugin.menu.route === to.path)
    if (!item) return { name: 'books' }
    if (to.name !== item.plugin.menu.name) return { path: to.fullPath, replace: true }
    return true
  }
  async function refreshAndLeave() {
    await refresh()
    const current = router.currentRoute.value
    if (current.path.startsWith('/plugin/') && ![...installed.values()].some(it => it.plugin.menu.route === current.path)) await router.replace({ name: 'books' })
  }
  function rejectPlugin(id) {
    remove(id)
    syncEntries()
    if (router.currentRoute.value.path === `/plugin/${id}` || router.currentRoute.value.path.startsWith(`/plugin/${id}/`)) void router.replace({ name: 'books' })
  }
  return { refresh, refreshAndLeave, routeGuard, clear, rejectPlugin }
}
