/**
 * 窗口 resize / pointermove 这类「一秒钟能来几十次」的事件的按帧节流。
 *
 * 为什么必须节流：这些事件的每一次都跟着一串强制同步布局（写 style → 读
 * scrollHeight → 再写 style）。resize 一秒 60 次，一张凭证 8~20 个框，
 * 就是一秒最多一千多次强制重排 —— 拖窗口时整页跟着卡，跟页面本身多重无关。
 *
 * 用 rAF 而不是 setTimeout：我们要的不是「隔多久跑一次」，而是
 * 「一帧之内只干一次活」。rAF 天然跟浏览器绘制对齐，一帧最多回调一次，
 * 而且不写、不读布局时浏览器本可以不排版。
 */

/**
 * 把高频回调压到「一帧最多一趟」。
 *
 * 同一帧后来的调用会被合并掉 —— 窗口在两次绘制之间缩了 30 次，
 * 量一次就够，因为中间那些中间态根本没人看得见。
 *
 * @param {Function} fn
 * @returns {Function} 带 flush() / cancel() 的节流版
 */
export function rafThrottle(fn) {
  let frame = 0
  let lastArgs = null
  const wrapped = (...args) => {
    lastArgs = args
    if (frame) return
    frame = requestAnimationFrame(() => {
      frame = 0
      fn(...lastArgs)
    })
  }
  /**
   * 立刻把欠着的那一趟跑掉（用最后一次的参数）。
   *
   * 拖动场景**必须**在松手前 flush：那一趟被丢掉的话，元素会停在倒数第二个
   * 位置上，而人看到的是「我明明拖到了那儿」。所以 flush 要在把拖动状态作废
   * **之前**调，否则那一趟跑起来已经没有拖动可算了。
   */
  wrapped.flush = () => {
    if (frame) cancelAnimationFrame(frame)
    frame = 0
    const args = lastArgs
    lastArgs = null          // 用完即弃：下一轮拖动不该拿上一次的位置去算
    if (args) fn(...args)
  }
  /** 组件被拆掉时把还挂着的这一趟撤掉，别让它对着已经卸载的 DOM 跑。 */
  wrapped.cancel = () => {
    if (frame) cancelAnimationFrame(frame)
    frame = 0
    lastArgs = null
  }
  return wrapped
}

const frameCache = new Map()
let scheduled = false

function clearNextFrame() {
  if (scheduled) return
  scheduled = true
  requestAnimationFrame(() => {
    scheduled = false
    frameCache.clear()
  })
}

/**
 * 「同一帧内共用同一份量出来的值」。
 *
 * 一张凭证上挂着 8~20 个 SubjectPicker，它们的 resize 监听会**各自**被浏览器
 * 叫一遍，但量的是同一个 CSS 类定出来的 line-height —— 20 个框量 20 遍，
 * 量出来是同一个数。这里按 key 缓存到帧末：
 *   · 值在**同一帧内**才共用，跨帧一定重算（CSS 真的变了就能看到）；
 *   · key 由调用方给，**不同口径不要共用一个 key** —— 那是缓存串味。
 *
 * @param {string} key 口径标识
 * @param {() => any} measure 真去量的活，只在那一帧第一次调用时才跑
 */
export function perFrame(key, measure) {
  if (!frameCache.has(key)) {
    clearNextFrame()
    frameCache.set(key, measure())
  }
  return frameCache.get(key)
}