/**
 * 路由代码块预取：鼠标落到菜单上时，先把那一页的代码块下载下来。
 *
 * **为什么不用 `<link rel="prefetch">`**：那是给「整页跳转」用的预取。
 * 本站是单页应用 —— 换页不换 HTML，换的是**路由对应的 JS 代码块**。
 * 预取 HTML 页面在这里等于预取一份永远用不上的外壳（入口 HTML 是
 * `Cache-Control: no-cache`，取回来用的时候还得回源校验，一点没省）。
 *
 * **怎么做到**：路由里的页面组件都是 `() => import('...')` 这种惰性加载，
 * 而 vue-router 把这个加载函数原样挂在路由记录的 `components` 上。
 * 直接把它调一遍，动态 import 就开始了 —— 浏览器把模块缓存起来；
 * 等人真点进去时，vue-router 再调同一个函数，拿到的是**已经加载好的模块**，
 * 于是「点开即显示」，不用等网络。
 *
 * **为什么不用重复调**：vue-router 加载完之后会把 `components[name]`
 * 从「加载函数」换成「组件对象」。组件对象不是函数，一眼就能认出来 ——
 * 所以这里天然只会在第一次预取时真的下载，之后每次悬停都只是空转。
 *
 * **前提**：本站的路由组件全是 SFC（`vite build` 出来是对象），
 * 没有函数式组件 —— 否则「函数」这个判据会把函数式组件当成加载函数调。
 * 这条前提由 tests/route_prefetch.test.mjs 钉住。
 */

/** 预取过的路由名 → 那一次预取的 promise（失败会被删掉，下次悬停重试）。 */
const prefetched = new Map()

/**
 * 从路由记录里找出「还没加载」的组件加载函数。
 * 找不到就返回 null（已经加载过、或者根本不是惰性路由）。
 */
function pendingLoader(record) {
  const components = record?.components
  if (!components) return null
  for (const key of Object.keys(components)) {
    const c = components[key]
    // vue-router 4.6 把惰性路由的 components[name] 存成加载函数本身；
    // 少数字段版本存成 [加载函数, props]，两种都认。
    const fn = Array.isArray(c) ? c[0] : c
    if (typeof fn === 'function') return fn
  }
  return null
}

/**
 * 预取某个路由（按 route name）的代码块。
 *
 * @param {object} router  vue-router 的实例
 * @param {string} name    路由名（NAV 里用的那个 name）
 * @returns {Promise|null} 预取完成的 promise；没有可预取的（已加载 / 路由不存在）时是 null
 */
export function prefetchRouteChunk(router, name) {
  if (!router || !name) return null

  const cached = prefetched.get(name)
  if (cached) return cached

  let record = null
  try {
    record = router.getRoutes().find((r) => r.name === name)
  } catch {
    // 路由表还没建好（极少见）：预取是锦上添花，不该因此报错。
    return null
  }
  if (!record) return null

  const loader = pendingLoader(record)
  if (!loader) return null

  // 用 Promise.resolve().then() 包一层：加载函数在微任务里才被调用，
  // 悬停事件的处理函数立刻就能返回，不会卡住事件循环。
  const p = Promise.resolve()
    .then(() => loader())
    .catch((err) => {
      // 预取失败不影响正事 —— 点进去时 vue-router 会自己再加载一次。
      // 从缓存里删掉，下次悬停还能重试。
      prefetched.delete(name)
      console.warn('[prefetch] 预取路由代码块失败：', name, err)
    })

  prefetched.set(name, p)
  return p
}
