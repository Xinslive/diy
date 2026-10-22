import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

/**
 * 页面查询条件 ↔ URL。
 *
 *  ## 为什么条件要住进 URL
 *
 *  标签栏**不做 keep-alive**（见 stores/tabs.js 开头）：切走再切回来 = 组件重建。
 *  重建的页面只认 URL —— 条件要是只活在组件的 ref 里，回来必然是默认值，
 *  用户设好的科目、期间、关键字全被重置。
 *
 *  所以条件一律写进 URL。写进去之后顺带白送三件事：
 *   - 标签栏切走再切回来，条件还在（标签记的完整地址带着 query）；
 *   - 刷新页面，条件还在；
 *   - 浏览器前进/后退，能回到上一组条件。
 *
 *  ## 用法
 *
 *      const { restore } = useUrlConditions({
 *        subject: { ref: subjectId, type: 'id' },
 *        from:    { ref: fromId,    type: 'id' },
 *        unposted: { ref: includeUnposted, type: 'bool', def: true },
 *      })
 *
 *      onMounted(async () => {
 *        await loadSubjects()   // 科目树要先到位，科目 id 才有意义
 *        restore()              // 从 URL 把条件捡回来
 *        load()
 *      })
 *
 *  `restore()` **必须在条件确定之后调用**，而且是**同步**的：它顺带把条件
 *  写回 URL（URL 上写的是真话），之后条件一变就自动同步。
 *
 *  ## 两处不能省的规矩
 *
 *  1. **复原完成之前不许往 URL 写。** 页面初始化时往往先给 ref 塞默认值
 *     （「默认落在 1001 库存现金」），那一刻要是照着默认值写 URL，
 *     用户地址栏里那组条件就被默认值顶掉了 —— 正好把要修的毛病又造一遍。
 *     所以复原之前自动同步是**关着的**，`restore()` 才把它打开。
 *  2. **写 URL 用 replace，不用 push。** 每改一次条件就塞一条历史记录的话，
 *     用户按一次「后退」要按很多下才能离开这个页面。条件不是「去过的这个地方」。
 */

/** 每种类型的编码 / 解码。解不出来（脏参数、手敲错的）一律给 null = 不认。 */
const TYPES = {
  str: {
    encode: (v) => (v === null || v === undefined ? null : String(v)),
    decode: (raw) => String(raw),
  },
  num: {
    encode: (v) => (v === null || v === undefined || Number.isNaN(Number(v)) ? null : String(v)),
    decode: (raw) => {
      const n = Number(raw)
      return Number.isFinite(n) ? n : null
    },
  },
  bool: {
    // 只认 1/0 与 true/false：地址栏是人能手改的，'yes' 不该被当成真
    encode: (v) => (v === null || v === undefined ? null : v ? '1' : '0'),
    decode: (raw) => (raw === '1' || raw === 'true' ? true : raw === '0' || raw === 'false' ? false : null),
  },
}
/** 数字型的 id：正整数。小数、负数、0 都不是 id。 */
const ID = {
  encode: TYPES.num.encode,
  decode: (raw) => {
    const n = Number(raw)
    return Number.isInteger(n) && n > 0 ? n : null
  },
}
TYPES.id = ID

/** 两份 query 是不是一回事（只看键和值，不看顺序）。 */
function sameQuery(a = {}, b = {}) {
  const ka = Object.keys(a)
  const kb = Object.keys(b)
  if (ka.length !== kb.length) return false
  return ka.every((k) => {
    const av = Array.isArray(a[k]) ? a[k].join(',') : String(a[k])
    const bv = Array.isArray(b[k]) ? b[k].join(',') : String(b[k])
    return av === bv
  })
}

/**
 * 条件表里每一项的两种写法：
 *   - `{ ref, type, def }` —— 页面上的值跟 URL 上的是同一个东西。
 *   - `{ get, set, type, def }` —— **两边不是一回事**时用这个。
 *
 *     比如明细账的科目：页面上认的是 id，URL 里该放的是**编码** `1001`。
 *     编码跨年度不变，id 每年都换（后端迁移 0032：科目表每个年度各有一份），
 *     把 id 写进 URL，换个年度再点回来就指到一个不存在的科目上去了。
 *     编码对得上哪一年是哪一年的事，交给页面在 set 里换。
 *
 * @param {Object} spec 条件表：{ [queryKey]: { ref | get/set, type, def } }
 *   - type: 'str' | 'num' | 'bool' | 'id'
 *   - def: 默认值。等于默认值的一律**不写进 URL** —— 地址栏干净点，
 *         条件也就只写「用户真改过的那些」。
 */
export function useUrlConditions(spec) {
  const route = useRoute()
  const router = useRouter()

  /** 取值：两种写法都支持。 */
  const readOpt = (opt) => (typeof opt.get === 'function' ? opt.get() : opt.ref.value)
  /** 赋值：两种写法都支持。 */
  const writeOpt = (opt, v) => {
    if (typeof opt.set === 'function') opt.set(v)
    else opt.ref.value = v
  }

  /**
   * 自动同步的门闩：复原之前是关着的。
   *
   * 见文件头「两处不能省的规矩」第一条 —— 页面初始化时给 ref 塞默认值的那一刻，
   * 要是照着默认值写 URL，用户地址栏里那组条件就被顶掉了。
   */
  let armed = false

  /** 条件 → query。默认值与空值都不带。 */
  function toQuery() {
    const q = {}
    for (const [key, opt] of Object.entries(spec)) {
      const t = TYPES[opt.type || 'str']
      const raw = readOpt(opt)
      if (opt.def !== undefined && raw === opt.def) continue
      const enc = t.encode(raw)
      if (enc === null || enc === '') continue
      q[key] = enc
    }
    return q
  }

  /** 把当前条件写回 URL。地址没变就什么都不做。 */
  function sync() {
    const q = toQuery()
    if (sameQuery(route.query || {}, q)) return
    router.replace({ path: route.path, query: q, hash: route.hash })
  }

  /**
   * 从 URL 把条件捡回来。
   *
   * @param {string[]} [keys] 只复原这几项（不传 = 全部）。
   *   分两步复原时用得上：科目要先定下来，子科目范围才知道该取哪一段。
   * @returns {Object} 复原出来的值，键是 query 的键
   */
  function restore(keys) {
    const q = route.query || {}
    const list = keys || Object.keys(spec)
    const got = {}
    for (const key of list) {
      const opt = spec[key]
      if (!opt) continue
      const raw = q[key]
      if (raw === undefined || raw === null) continue
      const t = TYPES[opt.type || 'str']
      const v = t.decode(Array.isArray(raw) ? raw[0] : raw)
      if (v === null || v === undefined) continue
      writeOpt(opt, v)
      got[key] = v
    }
    // 复原完了，自动同步才打开（见文件头「两处不能省的规矩」第一条）
    armed = true
    sync()
    return got
  }

  /**
   * 条件变了就写回 URL。
   *
   * 盯的是**编码后的 query 字符串**，不是那堆 ref 本身：ref 每次求值都是
   * 新数组，按引用比等于什么依赖一动就触发一次；编码后的字符串按值比，
   * 条件真的变了才响。
   */
  const signature = computed(() => JSON.stringify(toQuery()))
  watch(signature, () => { if (armed) sync() })

  return { restore, sync, toQuery }
}
