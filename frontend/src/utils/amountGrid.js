/** 金额格：亿 千 百 十 万 千 百 十 元 角 分，一共 11 格。
 *
 *  竖线每三格一根蓝线分组，元/角之间那根换成红的 —— 元以上是整数位，
 *  元以下是角分，红线就是小数点该在的地方。这套线跟纸质凭证一模一样，
 *  抬头是「亿千百十万千百十元角分」，金额就顺着往右落格。 */
export const AMOUNT_UNITS = ['亿', '千', '百', '十', '万', '千', '百', '十', '元', '角', '分']

// 每格左边的竖线：空 = 不要线（格子自己的外框已经在了）
export const AMOUNT_SEPS = ['', 'plain', 'plain', 'blue', 'plain', 'plain', 'blue', 'plain', 'plain', 'red', 'plain']

/**
 * 只留数字和一个小数点：最多 9 位整数、2 位小数。负号保留。
 *
 * 「负号」是**红字冲销**：负数分录就是冲销老账，老规矩用红笔写。
 * 所以负号必须一路留到底 —— 中途被当成垃圾字符剔掉，-300 就成了 300，
 * 冲销变成了再记一遍，方向刚好反了，是记账里最贵的一种错。
 *
 * 只敲了一个负号（还没敲数字）也认：`-` 原样带回去，
 * 否则负号在受控输入里会被当场抹掉，人接着敲的 1 就成了正数。
 */
export function sanitizeAmount(v) {
  const raw = String(v ?? '')
  const neg = raw.includes('-')
  let s = raw.replace(/[^\d.]/g, '')
  const dot = s.indexOf('.')
  if (dot !== -1) s = s.slice(0, dot + 1) + s.slice(dot + 1).replace(/\./g, '')
  let [i, d = ''] = s.split('.')
  i = i.replace(/^0+(?=\d)/, '').slice(0, 9)
  d = d.slice(0, 2)
  const sign = neg ? '-' : ''
  if (!i) {
    // 只敲了负号：留着它，别让人接着敲的数字跑到正数那边去
    if (!d) return neg ? '-' : ''
    return `${sign}0.${d}`
  }
  return d ? `${sign}${i}.${d}` : `${sign}${i}`
}

/**
 * 负号是**整格的属性**，按一下翻一下。
 *
 *  负号不是插在数字中间的一个字符，是这一格朝哪边：光标在万位上按「-」，
 *  人想的是「这一笔是冲销的」，不是「在万位左边插个符号」。所以按下就翻整格，
 *  再按一次翻回来 —— 一个键管一件事，不必先把光标挪到最前头。
 *
 *  · 「1234.56」→ 「-1234.56」→ 「1234.56」
 *  · 空格子 → 「-」（先摆好方向，接着敲的 5 就直接成 -5）
 *  · 「0.00」零没有方向可谈，按空格子算，按一下得「-」
 */
export function toggleAmountSign(v) {
  const s = sanitizeAmount(v)
  if (!s) return '-'
  if (s === '-') return ''
  const neg = s.startsWith('-')
  const body = neg ? s.slice(1) : s
  if (/^0(?:\.0+)?$/.test(body)) return neg ? '' : '-'   // 零算空格子，别摆出「-0.00」这种没人写的数
  return neg ? body : `-${body}`
}

/**
 * 框里该摆的字：值的样子，外加**刚敲下去、还没跟上一个数字的小数点**。
 *
 *  值里不留那个点（2332. 就是 2332），可框里必须留着：少了它，
 *  接着敲的「5」就变成了在末尾接着打 5 —— 「2332」敲「.5」该得 2332.50，不是 23325。
 */
export function amountInputText(typed, next) {
  const n = String(next ?? '')
  return n && /[.]$/.test(String(typed ?? '')) && !n.includes('.') ? `${n}.` : n
}

/** 这个金额是不是红字（负数）。落格显示、合计行、打印都按它决定上不上红。 */
export function isNegativeAmount(v) {
  return Number(v) < 0
}

/** 把金额摊进 11 个格子：整数右对齐落进 亿…元，角、分各占一格。
 *  空金额就是 11 个空格 —— 没填的格子得干干净净，不能满屏 0。
 *  负号也占一格，跟着整数一起右对齐（-1234 的负号落在千位左边那一格）。 */
export function distributeAmount(v) {
  const out = Array(AMOUNT_UNITS.length).fill('')
  const s = sanitizeAmount(v)
  if (!s || s === '-') return out
  const body = s.startsWith('-') ? `\u2212${s.slice(1)}` : s   // 落的是真正的减号 U+2212
  const [i = '', d = ''] = body.split('.')
  const ints = i.padStart(9, ' ').slice(-9).split('')
  for (let k = 0; k < 9; k += 1) out[k] = ints[k]
  out[9] = d[0] ?? ''
  out[10] = d[1] ?? ''
  return out
}

/**
 * 光标（以及选中范围）落在哪条**格缝**上，返回 0-11。
 *
 *  位置必须跟 distributeAmount 用同一套规则反推：整数右对齐占 亿…元 九格，
 *  小数点在元/角之间。反推不出来就会说错话 —— 打完「1234」，光标若停在千位上，
 *  而下一个数字明明要落在元位，人照着光标理解就会算错这一格。
 *
 *  · "1234.56" 光标在末尾   → 11（分之后）
 *  · "1234.56" 光标在小数点后 → 9（元/角之间，正是小数点该在的地方）
 *  · "1234"   光标在末尾   → 9（元之后）
 *  · ""       光标在开头   → 9（第一个数字就落在元位）
 */
export function caretBoundary(v, pos) {
  const s = sanitizeAmount(v)
  const p = Math.max(0, Math.min(Number(pos ?? s.length), s.length))
  const dot = s.indexOf('.')
  if (dot !== -1 && p > dot) return Math.min(9 + (p - dot - 1), AMOUNT_UNITS.length + 1)
  const head = dot === -1 ? s : s.slice(0, dot)
  const at = dot === -1 ? p : Math.min(p, dot)
  return Math.max(0, Math.min(9 - head.length + at, AMOUNT_UNITS.length + 1))
}
