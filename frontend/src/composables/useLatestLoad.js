import { ref } from 'vue'
import { isCanceled } from '@/api/client'

/** 这一趟已经过期 —— 结果**不该**被采纳，调用方要原样跳过。 */
export const STALE = Symbol('stale-response')

/**
 * ┌────────────────────────────────────────────────────────────────────┐
 * │  这份守卫**不是全站都有** —— 别以为接上了就到处都安全。          │
 * └────────────────────────────────────────────────────────────────────┘
 *
 * **已接入的（页面自己调 run() + isStale()）**：
 *   · views/ledger/GeneralLedger.vue   'ledger/general'
 *   · views/ledger/DetailLedger.vue    'ledger/detail'
 *   · views/ledger/BalanceSheet.vue    'ledger/balance'
 *   · views/report/ReportView.vue      'report/render'
 *   · views/voucher/VoucherList.vue    'voucher/list'
 *   · views/system/AuditLog.vue        'system/audit'
 *   · views/subject/SubjectTree.vue    'subject/tree'
 *
 * **依赖 MainLayout 的 viewKey 整页重挂兜底（没接这份守卫）**：
 *   账套 / 期间一变，viewKey（`${route.name}-${store.bookId}-${scope}-${refreshKey}`）
 *   就变，**整页重挂** —— 旧组件连同它的 watch 一起被停掉，
 *   旧响应写进的是一个**已经销毁的实例**，用户看不见。
 *   于是「切账套 / 切期间」这一类场景，多半**不需要**额外的守卫。
 *   唯一例外是期初余额页（口径是**年度**，viewKey 用年度，见 MainLayout）。
 *
 * ⚠️ **边界：viewKey 重挂管不着「页内」的动作。**
 *   翻页、改页大小、排序、页内筛选 —— 这些都发生在**同一个组件实例**里，
 *   组件不会被重挂，于是：
 *     · 旧响应**会**写进还活着的页面 → 页脚停在第 4 页、表格里是第 3 页；
 *     · 这类路径**必须**自己接守卫（VoucherList 与 AuditLog 就是样板）。
 *   换句话说：**凡是「不换账套、不换期间、只动页面上的条件」的加载点，
 *   都要自己想清楚有没有守卫。**
 *
 * 另外三种「自己看住自己」的写法，别跟这一份混着记：
 *   · VoucherEdit.vue 的 load() / loadSubjects() / loadBalances()：显式 Seq 守卫
 *     （那三处的响应会写进**同一张表单**，错一次就是张冠李戴）；
 *   · api/client.js 的 getLatest()：**掐掉**上一趟，省掉白跑的往返（另一半防线）；
 *   · 什么守卫都没有的：见上面那份清单 —— 那些是「页内动作」或「一次性加载」。
 */

/**
 * 「切条件就重新查」的页面用的守卫：只认最新那一次。
 *
 * 要解决的是这个：在期间下拉上快速连点时，多个请求并发在途，后发先至会让
 * **旧响应覆盖新数据** —— 表格里的数对不上顶栏的期间。`loading` 只是标志位，
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
  /**
   * 最近一次**真错**的说明（空串 = 没有错）。
   *
   * 为什么要有它：`run()` 把真错照原样往外抛（吞掉只会让人以为查成功了），
   * 而调用方若不接，这个抛错就变成一个**没人管的 promise rejection** ——
   * 页面上 `data` 保持初始值，于是「加载失败」被渲染成「没有数据」。
   * 断网或后端 30 秒超时的时候，用户看到的是一张空表、零提示，
   * 于是以为这个科目一整年没发生额（审计 §3.25）。
   *
   * 所以错误要**同时**有两处落点：一条红字提示（拦截器弹的）和
   * 一个页面上的状态（这里的 `error`）。缺了后者，失败与空数据同形。
   */
  const error = ref('')
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
      if (mine === seq) error.value = ''
      return mine === seq ? value : STALE
    } catch (e) {
      // 被自己掐掉的那一趟、以及过期的那一趟，都不是错：过期那趟对应的
      // 条件用户早就改掉了，报错只会把红字指在一个没人再看的条件上。
      if (isCanceled(e) || mine !== seq) return STALE
      error.value = e?.message || '加载失败'
      throw e
    } finally {
      // 只有最新一代配把 loading 收掉 —— 否则过期那趟的 finally 会把
      // 最新那趟的转圈提前关掉。
      if (mine === seq) loading.value = false
    }
  }

  /** 把在途的一趟直接作废（换账套时把上一个人的表抹干净用）。 */
  function invalidate() { ++seq; error.value = '' }

  const isStale = (v) => v === STALE

  return { loading, error, run, invalidate, isStale }
}