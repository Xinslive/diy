import { ref } from 'vue'
import { isCanceled } from '@/api/client'

/** 这一趟已经过期 —— 结果**不该**被采纳，调用方要原样跳过。 */
export const STALE = Symbol('stale-response')

/**
 * 「切条件就重新查」的页面用的守卫：只认最新那一次。
 *
 * 要解决的是这个：在期间下拉上快速连点时，多个请求并发在途，后发先至会让
 * **旧响应覆盖新数据** —— 表���里的数对不上顶栏的期间。`loading` 只是标志位，
 * 能不能拿到结果它一句话都没说，所以它**不构成守卫**。
 *
 * 两道防线，缺一不可：
 *   1. 代次守卫：每次 run() 领一个自增代号，任何 await 之后先确认自己仍是
 *      最新一代，不是就直接返回、什么都不碰。写法照抄 VoucherEdit.load()
 *      里那套 —— 全项目已有一份同源的，别另立标准。
 *   2. AbortController：上一趟还在飞就直接掐掉（见 api/client.js 的
 *      getLatest），省掉白跑的往返。只有第 1 条挡不住「abort 到达之前
 *      就已经 resolve」的那次；只有第 2 条也挡不住它。
 *
 * @param {string} key 抢占槽位：同一把 key 的请求互相取消。
 *   同一把 key 的两趟**必然**是「后一趟取代前一趟」，所以共用一个槽位是对的。
 *
 * 用法：
 *   const { loading, run, isStale } = useLatestLoad('ledger/general')
 *   const d = await run(() => api.general(bookId, params))
 *   if (isStale(d)) return          // 过期的那趟，别写状态
 *   data.value = d
 *
 * 真错（不是取消、也不是过期）会照原样往外抛 —— 拦截器已经弹过提示了，
 * 在这里吞掉只会让人以为查成功了。
 */
export function useLatestLoad(key) {
  const loading = ref(false)
  let seq = 0

  /**
   * @param {() => Promise<any>} task 真正发请求的活
   * @returns {Promise<any>} 认账时的结果；过期/被取消时是 STALE 哨兵
   */
  async function run(task) {
    const mine = ++seq
    loading.value = true
    try {
      const value = await task()
      return mine === seq ? value : STALE
    } catch (e) {
      // 被自己掐掉的那一趟、以及过期的那一趟，都不是错：过期那趟对应的
      // 条件用户早就改掉了，报错只会把红字指在一个没人再看的条件上。
      if (isCanceled(e) || mine !== seq) return STALE
      throw e
    } finally {
      // 只有最新一代配把 loading 收掉 —— 否则过期那趟的 finally 会把
      // 最新那趟的转圈提前关掉。
      if (mine === seq) loading.value = false
    }
  }

  /** 把在途的一趟直接作废（换账套时把上一个人的表抹干净用）。 */
  function invalidate() { ++seq }

  const isStale = (v) => v === STALE

  return { loading, run, invalidate, isStale }
}