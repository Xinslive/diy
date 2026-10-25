/**
 * 凭证页「离开前的最后一次 flush」—— 跨组件重挂传递的那一趟。
 *
 *  切期间时 MainLayout 的 viewKey 变了 → 旧组件 `onBeforeUnmount` 发一个
 *  **不被等待**的 PUT → 新组件 `onMounted` 立刻 GET。
 *  GET 若先于 PUT 被服务端处理完，新页面显示的是**改动前的值**；
 *  用户在新页上再改一次，自动保存 PUT 的是**整张表单**，
 *  前一版改动被无声覆盖（审计 §3.24）。
 *
 *  所以旧组件把那一趟的 promise 挂在这里，新组件 `onMounted` 先 await 它
 *  再 `load()`。这个盒子必须**跨组件**：切期间是整页重挂，
 *  组件内的变量（包括那个 promise）全被换掉了。
 */

/** 正在飞的那一趟 flush（null = 没有）。 */
let pending = null

/** 旧组件挂上来：把「离开前那一趟」的 promise 交出去。 */
export function setPendingFlush(promise) {
  pending = promise || null
}

/**
 * 新组件取走那一趟并等它回来。
 *
 * 没有在途的那一趟时返回 null（调用方直接放行）。
 */
export function takePendingFlush() {
  const p = pending
  pending = null
  return p
}
