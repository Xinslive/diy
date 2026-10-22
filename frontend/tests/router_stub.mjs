/**
 * vue-router 的替身 —— 给 composables/useUrlConditions.js 用。
 *
 * 那个组合式函数要在**组件的 setup 里**跑（它拿的是路由注入），
 * 这里没有组件可挂。所以把 vue-router 换成这个替身，
 * 让 useRoute() / useRouter() 落到测试自己手里的 route / router 上。
 *
 * 状态挂在 globalThis 上：测试文件是 node 直接 import 这个模块，
 * 而组合式函数那份是 vite 的 ssrLoadModule 加载的 —— **两个实例**。
 * 挂在 globalThis 上，两边才碰得到同一份。
 */
export const env = (globalThis.__cwqRouterStub ??= { route: null, router: null })

export function useRoute() { return env.route }
export function useRouter() { return env.router }
