/** 金额格：亿 千 百 十 万 千 百 十 元 角 分，一共 11 格。
 *
 *  竖线每三格一根蓝线分组，元/角之间那根换成红的 —— 元以上是整数位，
 *  元以下是角分，红线就是小数点该在的地方。这套线跟纸质凭证一模一样，
 *  抬头是「亿千百十万千百十元角分」，金额就顺着往右落格。 */
export const AMOUNT_UNITS = ['亿', '千', '百', '十', '万', '千', '百', '十', '元', '角', '分']

// 每格左边的竖线：空 = 不要线（格子自己的外框已经在了）
export const AMOUNT_SEPS = ['', 'plain', 'plain', 'blue', 'plain', 'plain', 'blue', 'plain', 'plain', 'red', 'plain']

/**
 * **两个上限，各管各的**（审查 P0-1 / P1-2）。
 *
 *  · {@link MAX_INT_DIGITS} = 9 —— 金额格的**物理容量**：整数 亿…元 一共 9 格。
 *    这是「一格画得下几个数位」，跟**金额能有多大无关**。
 *  · {@link MAX_MONEY_INT_DIGITS} = 18 —— 金额的**数据模型上限**：与后端
 *    `MONEY_LIMIT`（Numeric(20,2)，整数位 18）对齐，18 位整数是后端允许的合法金额。
 *
 * 以前这里只有 `MAX_INT_DIGITS = 9`，而**显示路径与输入路径共用它** ——
 * 于是「读回来的、库里真实存在的 10 位金额」被静默截成 9 位：
 * 打开期初余额页 → 点保存 → 库里那个数被改小，界面全程显示「借贷平衡」。
 * （这条 P0 的完整复现见审查报告 P0-1 / P-34 / P-35。）
 *
 * 截断是**画格子**的事，不是**改数**的理由：格子放不下就显示原值 + 角标，
 * 绝不显示一个被改小的数 —— 那样用户会以为库里存的就是那个被改小的数。
 */
export const MAX_INT_DIGITS = 9
export const MAX_MONEY_INT_DIGITS = 18
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
 * 归一化之后拆成「符号 / 整数位 / 小数位」三块。
 *
 * 净化、体检、落格三处必须用**同一套**拆法：拆法有两套，
 * 「显示的数」与「算的数」迟早各说各话（那正是 P1-1「同一屏两个数」的根）。
 */
function splitAmountText(raw) {
  const neg = raw.includes('-')
  let s = raw.replace(/[^\d.]/g, '')
  const dot = s.indexOf('.')
  if (dot !== -1) s = s.slice(0, dot + 1) + s.slice(dot + 1).replace(/\./g, '')
  const [i = '', d = ''] = s.split('.')
  return { neg, int: i.replace(/^0+(?=\d)/, ''), dec: d }
}

/**
 * 归一化 + 拆分（体检用）。**不截断任何位次**。
 *
 * @param {string|number|null|undefined} v
 * @returns {{neg: boolean, int: string, dec: string}}
 */
function splitAmount(v) {
  return splitAmountText(normalizeAmountText(String(v ?? '')))
}

/**
 * 净化：**只归一化，不改数**。
 *
 * 保留负号、认回全角数字与中文句点、把小数截到分、去掉多余的点，
 * 但**整数位一位都不丢** —— 净化是给**用户敲进来的东西**用的，
 * 读回来的数不归它管（见 {@link sanitizeAmountDetailed} 里两种超限的处理）。
 *
 * 「负号」是**红字冲销**：负数分录就是冲销老账，老规矩用红笔写。
 * 所以负号必须一路留到底 —— 中途被当成垃圾字符剔掉，-300 就成了 300，
 * 冲销变成了再记一遍，方向刚好反了，是记账里最贵的一种错。
 *
 * 只敲了一个负号（还没敲数字）也认：`-` 原样带回去，
 * 否则负号在受控输入里会被当场抹掉，人接着敲的 1 就成了正数。
 */
export function sanitizeAmount(v) {
  return sanitizeAmountDetailed(v).value
}

/**
 * 净化，**并且说明这次净化动了手的地方**。
 *
 *  返回 `{ value, notices }`：
 *  · value    —— 落下来的值
 *  · notices  —— `[{ code, text }]`：这次**动过手**的地方
 *
 * 两种超限是**两回事**（审查 P0-1 / P1-2）：
 *
 *  ① 整数位超过 {@link MAX_MONEY_INT_DIGITS}（18，**数据模型上限**，与后端
 *     `MONEY_LIMIT` 对齐）→ **拒绝**：不进 `value`，给一条 `MONEY_LIMIT`。
 *     18 位整数后端都存不下，前端更不能假装它存在。
 *  ② 整数位超过 {@link MAX_INT_DIGITS}（9，**金额格的物理容量**）→
 *     **值原样保留**，给一条 `INT_OVERFLOW`：「金额格只有 9 位整数位，
 *     这个数放不进格子里（已按原值显示），请用其它方式录入」。
 *     放不下的是**格子**，不是那个数 —— 显示一个被改小的数，
 *     用户会以为库里存的就是它（这正是 P0-1 的要害）。
 *
 * 为什么单独开一个函数：净化是纯字符串运算、没有出口，装不下「提示」；
 * 而界面必须把「放不下」「不收」说出来（审计 P2-19）。
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
  const { neg, int, dec } = splitAmount(v)
  const notices = []
  const sign = neg ? '-' : ''

  // ① 数据模型上限：拒绝，不进值。
  if (int.length > MAX_MONEY_INT_DIGITS) {
    notices.push({
      code: 'MONEY_LIMIT',
      text: `金额最多 ${MAX_MONEY_INT_DIGITS} 位整数（后端能存的上限），这个数超出可表示范围，已拒绝录入`
    })
    return { value: '', notices }
  }

  // ② 金额格的物理容量：**值原样保留**，只说「格子里放不下」。
  if (int.length > MAX_INT_DIGITS) {
    notices.push({
      code: 'INT_OVERFLOW',
      text: `金额格只有 ${MAX_INT_DIGITS} 位整数位（亿…元），这个数放不进格子里`
    })
  }

  // ③ 小数只到分。
  let d = dec
  if (d.length > MAX_DEC_DIGITS) {
    notices.push({
      code: 'DEC_CLIPPED',
      text: `小数只到分，第 ${MAX_DEC_DIGITS + 1} 位起的「${d.slice(MAX_DEC_DIGITS)}」已丢弃`
    })
    d = d.slice(0, MAX_DEC_DIGITS)
  }

  if (!int) {
    // 只敲了负号：留着它，别让人接着敲的数字跑到正数那边去
    if (!d) return { value: neg ? '-' : '', notices }
    return { value: `${sign}0.${d}`, notices }
  }
  return { value: d ? `${sign}${int}.${d}` : `${sign}${int}`, notices }
}

/**
 * 体检：这个数放不放得进 11 格、够不够得着数据模型的上限。
 *
 *  · overGrid  —— 整数位超过 {@link MAX_INT_DIGITS}：**格子放不下**（值是真的，不许改小）
 *  · overModel —— 整数位超过 {@link MAX_MONEY_INT_DIGITS}：**数据模型装不下**（后端也会拒）
 *
 * 显示层（AmountGrid / AmountCells）拿它决定「走 11 格落格」还是
 * 「显示原值 + 超容量角标」—— 两条路都不许显示一个被改小的数。
 */
export function inspectAmount(v) {
  const { int } = splitAmount(v)
  const digits = int.length
  return {
    intDigits: digits,
    overGrid: digits > MAX_INT_DIGITS,
    overModel: digits > MAX_MONEY_INT_DIGITS
  }
}

/**
 * 显示用的原值：负号换成落格用的真正减号 U+2212（红字那一笔看着跟纸上一样）。
 *
 * 放不进格子时，界面显示的就是它 —— **原值**，不是某个被改小的数。
 */
export function amountDisplayText(v) {
  const s = String(v ?? '')
  return s.startsWith('-') ? `\u2212${s.slice(1)}` : s
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
 *
 *  ⚠️ 这个函数走的是浮点：**17 位以上的数会被浮点改写**
 *  （`String(12345678901234567)` = "12345678901234568"）。
 *  算账、判平、存库一律改用 {@link moneyCents}（定点，分）。
 */
export function amountToNumber(v) {
  const s = sanitizeAmount(v)
  if (s === '' || s === '-') return 0
  const n = Number(s)
  return Number.isFinite(n) ? n : 0
}

/**
 * 一个金额折成**整数分**（BigInt）。全程定点，不经过浮点。
 *
 *  · 认不出来的串一律当 0（空格子）—— 与 {@link amountToNumber} 同一口径，
 *    绝不让 NaN 混进合计（`NaN !== 0` 为真，会被「哪些行有余额」的过滤选中）。
 *  · 小数只取到分（多的位次丢掉，与凭证页的 `centsOf` 同一规矩）。
 *  · 负号认半角 `-` 与落格用的真正减号 `−`（U+2212）：两种都得是红字，
 *    把其中一个当成正数，冲销就成了再记一遍，方向刚好反。
 *  · **用 BigInt 而不是 Number**：金额上限 18 位整数（后端 Numeric(20,2)），
 *    折成分就是 20 位，早就越过了 Number 的安全整数范围 ——
 *    浮点会把 999999999999999999.99 变成 1000000000000000000，
 *    那正是「读回来的金额被改小」的另一种形态。
 */
export function moneyCents(v) {
  const raw = String(v ?? '').trim()
  if (!raw) return 0n
  const neg = raw[0] === '-' || raw[0] === '\u2212'
  const body = (neg ? raw.slice(1) : raw).replace(/[^0-9.]/g, '')
  const dot = body.indexOf('.')
  const int = (dot === -1 ? body : body.slice(0, dot)) || '0'
  const dec = dot === -1 ? '' : body.slice(dot + 1)
  if (!/^\d+$/.test(int)) return 0n
  const fen = BigInt((dec.slice(0, MAX_DEC_DIGITS) || '0').padEnd(MAX_DEC_DIGITS, '0'))
  const cents = BigInt(int) * 100n + fen
  return neg ? -cents : cents
}

/**
 * 整数分（BigInt）→ 金额文字（"1241610200.12" / "-0.05"）。
 * 不带千分位 —— 跟金额格、凭证页的写法一致。
 */
export function centsToText(c) {
  const n = typeof c === 'bigint' ? c : BigInt(Math.trunc(Number(c) || 0))
  const neg = n < 0n
  const a = neg ? -n : n
  return `${neg ? '-' : ''}${a / 100n}.${String(a % 100n).padStart(MAX_DEC_DIGITS, '0')}`
}

/**
 * 一个金额的**绝对值**（字符串形态）："-1241610200.12" → "1241610200.12"。
 *
 * 存回去的时候用它拼出带方向的串 —— **全程字符串**：
 * 浮点会在 17 位以后把 12345678901234567 变成 12345678901234568，
 * 那一分就是另一笔钱（这正是 P0-1 要根治的那一类错）。
 */
export function absMoneyText(v) {
  const s = String(v ?? '').trim()
  if (!s) return ''
  return s[0] === '-' || s[0] === '\u2212' ? s.slice(1) : s
}

/** 把金额摊进 11 个格子：整数右对齐落进 亿…元，角、分各占一格。
 *  空金额就是 11 个空格 —— 没填的格子得干干净净，不能满屏 0。
 *  负号也占一格，跟着整数一起右对齐（-1234 的负号落在千位左边那一格）。
 *
 *  **放不进 11 格时返回 11 个空格**：宁可不落格，也不落一个**被改小的数**。
 *  显示层（AmountCells）改用「原值 + 超容量角标」把原值摆出来 ——
 *  落一个改小了的数，用户会以为库里存的就是它。 */
export function distributeAmount(v) {
  const out = Array(AMOUNT_UNITS.length).fill('')
  const s = sanitizeAmount(v)
  if (!s || s === '-') return out
  if (inspectAmount(s).overGrid) return out      // 格子放不下 → 不落格（见上面的说明）
  const body = s.startsWith('-') ? `\u2212${s.slice(1)}` : s   // 落的是真正的减号 U+2212
  const [i, d = ''] = body.split('.')
  const ints = i.padStart(MAX_INT_DIGITS, ' ').slice(-MAX_INT_DIGITS).split('')
  for (let k = 0; k < MAX_INT_DIGITS; k += 1) out[k] = ints[k]
  out[9] = d[0] ?? ''
  out[10] = d[1] ?? ''
  return out
}

/**
 * 光标（以及选中范围）落在哪条**格缝**上，返回 0-11。
 *
 * 位置必须跟 distributeAmount 用同一套规则反推：整数右对齐占 亿…元 九格，
 * 小数点在元/角之间。反推不出来就会说错话 —— 打完「1234」，光标若停在千位上，
 * 而下一个数字明明要落在元位，人照着光标理解就会算错这一格。
 *
 * **负号不占位**：它是整格的属性（见 toggleAmountSign），不是第一个数位。
 * 所以算整数位时要把开头的 '-' 摘掉，光标也**永远不落在负号与首位数之间**
 * —— 那个位置没有含义，它两侧画出来的是同一条格缝。以前没摘，
 * `caretBoundary('-', 0)` 算成 8（凭空短一格），只有负号的那一格光标画错一格。
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
