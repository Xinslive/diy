/** 金额格：亿 千 百 十 万 千 百 十 元 角 分，一共 11 格。
 *
 *  竖线每三格一根蓝线分组，元/角之间那根换成红的 —— 元以上是整数位，
 *  元以下是角分，红线就是小数点该在的地方。这套线跟纸质凭证一模一样，
 *  抬头是「亿千百十万千百十元角分」，金额就顺着往右落格。 */
export const AMOUNT_UNITS = ['亿', '千', '百', '十', '万', '千', '百', '十', '元', '角', '分']

// 每格左边的竖线：空 = 不要线（格子自己的外框已经在了）
export const AMOUNT_SEPS = ['', 'plain', 'plain', 'blue', 'plain', 'plain', 'blue', 'plain', 'plain', 'red', 'plain']

/** 输入上限：整数 亿…元 共 9 位，小数 角分 共 2 位。 */
export const MAX_INT_DIGITS = 9
export const MAX_DEC_DIGITS = 2

/**
 * 归一化：把**输入法打出来的那些个号码**折成 ASCII，而不是当垃圾剔掉。
 *
 *  这一步以前根本不存在，净化只有一句 `[^\d.]`，于是：
 *  · 「１２３」（全角）→ ''  —— 看着像清了空，人以为是金额格坏了
 *  · 「1。5」（中文句点）→ 15 —— **小数点被吞掉，金额凭空大十倍**
 *  · 「−100」（U+2212 数学减号）→ 100 —— 真负号被当成垃圾字符清掉，
 *    负数变成正数。正是本文件下面点名「记账里最贵的一种错」那一种。
 *
 * 全角数字、句点、各种减号写的是**同一个数**，只是长得不一样（多半是从
 * Excel / PDF 复制来的）。把它们认回来，是净化该干的事；把别人写的数字
 * 悄悄扔掉，才是最贵的那种错。
 */
function normalizeAmountText(raw) {
  return raw
    // 全角数字 ０-９ → 0-9
    .replace(/[\uFF10-\uFF19]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xFEE0))
    // 全角/中文句点 ．。． → 小数点
    .replace(/[\uFF0E\uFF61\u3002]/g, '.')
    // 各种减号 → ASCII 减号。**U+2212（−）是这一组里最要命的一个**：
    // 它跟负号长得一模一样，从 Excel 复制来的金额里全是它。
    .replace(/[\u2212\uFE63\uFF0D\u2010-\u2015\u2796]/g, '-')
    // 正号（＋ ➕）：正号本身不留，但它说明人写的是正数，不能当负号
    .replace(/[\uFF0B\u2795]/g, '+')
}

/**
 * 只留数字和一个小数点：最多 9 位整数、2 位小数。负号保留。
 *
 * 「负号」是**红字冲销**：负数分录就是冲销老账，老规矩用红笔写。
 * 所以负号必须一路留到底 —— 中途被当成垃圾字符剔掉，-300 就成了 300，
 * 冲销变成了再记一遍，方向刚好反了，是记账里最贵的一种错。
 *
 * 只敲了一个负号（还没敲数字）也认：`-` 原样带回去，
 * 否则负号在受控输入里会被当场抹掉，人接着敲的 1 就成了正数。
 *
 * 净化会**截断**超长输入（钱只有 亿…分 那么多格）。截断这件事本身没法
 * 拒绝 —— 但不能悄悄丢：要说明的部分由 {@link sanitizeAmountDetailed}
 * 一并交出去，由界面说出来（审计 P2-19）。
 */
export function sanitizeAmount(v) {
  return sanitizeAmountDetailed(v).value
}

/**
 * 净化，**并且说明这次净化动了手的地方**。
 *
 *  返回 `{ value, notices }`：
 *  · value    —— 落进格子里的值，跟 {@link sanitizeAmount} 完全一致
 *  · notices  —— `[{ code, text }]`：这次**丢过东西**的地方（整数超位 / 小数超位）
 *
 * 为什么单独开一个函数：净化是纯字符串运算、没有出口，装不下「提示」；
 * 而输入框必须能把截断说出来（审计 P2-19：10 位整数被静默截断，
 * 人按回车才发现少了几位，而那笔数已经存进账里了）。
 * 于是净化只负责算，提示由这一层负责带出去 —— 两边共用一套规则，
 * 不会出现「框里截了、存进去没截」这种各说各话的分裂。
 *
 * 全角/中文符号的归一化**不进 notices**：那是把同一个数认回来，
 * 值没变、只是显示成了能落格的半角，没必要惊动人。被丢掉的位数才会。
 *
 * @param {string|number|null|undefined} v
 * @returns {{value: string, notices: Array<{code: string, text: string}>}}
 */
export function sanitizeAmountDetailed(v) {
  const raw = normalizeAmountText(String(v ?? ''))
  const neg = raw.includes('-')
  let s = raw.replace(/[^\d.]/g, '')
  const dot = s.indexOf('.')
  if (dot !== -1) s = s.slice(0, dot + 1) + s.slice(dot + 1).replace(/\./g, '')
  let [i = '', d = ''] = s.split('.')
  const notices = []
  i = i.replace(/^0+(?=\d)/, '')
  if (i.length > MAX_INT_DIGITS) {
    notices.push({
      code: 'INT_CLIPPED',
      text: `整数最多 ${MAX_INT_DIGITS} 位，第 ${MAX_INT_DIGITS + 1} 位起的「${i.slice(MAX_INT_DIGITS)}」已丢弃`
    })
    i = i.slice(0, MAX_INT_DIGITS)
  }
  if (d.length > MAX_DEC_DIGITS) {
    notices.push({
      code: 'DEC_CLIPPED',
      text: `小数只到分，第 ${MAX_DEC_DIGITS + 1} 位起的「${d.slice(MAX_DEC_DIGITS)}」已丢弃`
    })
    d = d.slice(0, MAX_DEC_DIGITS)
  }
  const sign = neg ? '-' : ''
  if (!i) {
    // 只敲了负号：留着它，别让人接着敲的数字跑到正数那边去
    if (!d) return { value: neg ? '-' : '', notices }
    return { value: `${sign}0.${d}`, notices }
  }
  return { value: d ? `${sign}${i}.${d}` : `${sign}${i}`, notices }
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

/**
 * 净化后的串 → 数值。**认不出来的一律当 0**，包括单摆着的负号（`-`）。
 *
 *  `Number('-')` 是 NaN，而 NaN 是个特别危险的数：
 *  `NaN !== 0` 为**真**，所以它能被「哪些行有余额」的过滤条件选中、
 *  又能把合计整列污染掉 —— 期初余额页以前就是这样把人输入的脏串
 *  静默存成了 0，却还报「已保存 N 条」（审计 P2-20）。
 *  这里把它按**空格子**处理：钱要么有要么没有，
 *  认不出来的串和没填一样，绝不能变成一个别的数。
 */
export function amountToNumber(v) {
  const s = sanitizeAmount(v)
  if (s === '' || s === '-') return 0
  const n = Number(s)
  return Number.isFinite(n) ? n : 0
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
 *  **负号不占位**：它是整格的属性（见 toggleAmountSign），不是第一个数位。
 *  所以算整数位时要把开头的 '-' 摘掉，光标也**永远不落在负号与首位数之间**
 *  —— 那个位置没有含义，它两侧画出来的是同一条格缝。以前没摘，
 *  `caretBoundary('-', 0)` 算成 8（凭空短一格），只有负号的那一格光标画错一格。
 *
 *  · "1234.56" 光标在末尾   → 11（分之后）
 *  · "1234.56" 光标在小数点后 → 9（元/角之间，正是小数点该在的地方）
 *  · "1234"   光标在末尾   → 9（元之后）
 *  · ""       光标在开头   → 9（第一个数字就落在元位）
 *  · "-"      光标在开头   → 9（同上：负号不占位）
 */
export function caretBoundary(v, pos) {
  const s = sanitizeAmount(v)
  const neg = s.startsWith('-')
  // 字符位置 → 数位位置。负号摘掉的那一位不参与任何格缝计算，
  // 所以负号前(0)、负号后(1)映射到同一个位置：两侧本来就是同一条缝。
  const p0 = Math.max(0, Math.min(Number(pos ?? s.length), s.length))
  const p = neg ? Math.max(0, p0 - 1) : p0
  const body = neg ? s.slice(1) : s
  const dot = body.indexOf('.')
  if (dot !== -1 && p > dot) return Math.min(9 + (p - dot - 1), AMOUNT_UNITS.length + 1)
  const head = dot === -1 ? body : body.slice(0, dot)
  const at = dot === -1 ? p : Math.min(p, dot)
  return Math.max(0, Math.min(9 - head.length + at, AMOUNT_UNITS.length + 1))
}
