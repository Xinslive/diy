/**
 * 「同一个量只允许有一个权威实现」—— 前端那一半。
 *
 * 审计 §7 **第 7 步**要求的结构性防回归规则：金额格式化这类量，
 * 只允许在**一个地方**实现，其余地方一律调用它。
 *
 * 后端那份在 `backend/tests/test_single_source_of_truth.py`（角色归一化、
 * 凭证号解析、凭证字推导）；前端这份守**金额格式化**。
 *
 * **为什么金额格式化要守**：同一个业务对象在两个界面上显示成两个数，
 * 是本仓被反复修过的一类错（期初导入预览显示 "1.005"、期初余额页显示
 * "1.00"；报表屏幕显示 0.33、导出文件里是 0.3333333333333333333333333333）。
 * 每一次都要单独查一遍、单独修一遍，而修完就忘了 ——
 * 结构性规则让它**不会再长出来**。
 *
 * **权威实现（只有这两处）**：
 *   · `src/utils/format.js`
 *       - `moneyText(cents)`  整数分 → 金额文字（带千分位）
 *       - `centsToText` 的对家 `reportNumber(v)` 报表数字（**不截两位**）
 *       - `fmt(v, digits)`    通用浮点格式化（两位小数）
 *   · `src/utils/amountGrid.js`
 *       - `centsToText(c)`    整数分 → 金额文字（**不带**千分位，落金额格用）
 *       - `moneyCents(v)`     金额 → 整数分
 *
 * `moneyText` 与 `centsToText` 看着像同一件事，其实不是：
 * 一个给「显示」（要千分位），一个给「落格」（11 个格子放不下逗号）。
 * 两个都是**定长定点**、全程整数分 —— 这才是它们共同的、不许分叉的口径。
 *
 * **为什么用「形态扫描」而不是 AST**：本仓前端**零依赖**
 * （`package.json` 里没有 acorn/eslint/prettier，加一套会破坏
 * `pnpm install --frozen-lockfile`）。所以这里自己写了一个只认
 * **可执行代码**的扫描器 —— 先剥掉注释与字符串，再找形态。
 *
 * **为什么必须剥注释**：本仓的注释是全仓最有价值的部分之一，
 * 里面**故意**留着反面教材（「以前是 `(Number(v || 0)).toLocaleString(...)`
 * —— 浮点」）。不剥注释的话，那些反面教材会被当成实现，
 * 规则第一条就红 —— 而红的原因跟规则想防的东西毫无关系。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const SRC = path.join(ROOT, 'src')

/** 金额格式化的权威实现所在目录。 */
const AUTHORITY_FILES = new Set([
  path.join(SRC, 'utils', 'format.js'),
  path.join(SRC, 'utils', 'amountGrid.js'),
])

/** 递归列出目录下的所有 .js / .vue 文件。 */
function walk(dir) {
  const out = []
  for (const name of fs.readdirSync(dir)) {
    const p = path.join(dir, name)
    if (fs.statSync(p).isDirectory()) out.push(...walk(p))
    else if (/\.(js|vue)$/.test(name)) out.push(p)
  }
  return out
}

/**
 * 剥掉注释与字符串字面量，只留**可执行代码**的骨架。
 *
 * 为什么要连字符串一起剥：`` `...` `` 里也可能出现 `` toLocaleString ``
 * 这种文本（比如错误提示里写的例子）。而正则里出现形态关键字时，
 * 它在字符串里和在代码里是两回事 —— 后者才是实现。
 *
 * **但模板字符串的 ``${…}`` 是代码，必须留下。**
 * 第一版把整个模板字符串（含插值）一起剥掉，结果
 * ``moneyText`` 的实现体被剥成 `` `${neg ? '' : ''}${yuan}.${''}`` `` ——
 * 规则看不见它自己正要守的那份实现，于是报「centsToText 有 0 份实现」。
 * 插值里的表达式要按代码处理，插值外的文字才剥掉。
 *
 * 正则字面量（``/\B(?=(\d{3})+…)/``）**故意不剥**：千分位的特征就写在
 * 正则字面量里，剥了它那条判据就永远不成立。
 */
function stripCommentsAndStrings(src) {
  let out = ''
  let i = 0
  const n = src.length
  while (i < n) {
    const c = src[i]
    const c2 = src[i + 1]
    if (c === '/' && c2 === '/') {
      while (i < n && src[i] !== '\n') i++
      continue
    }
    if (c === '/' && c2 === '*') {
      i += 2
      while (i < n && !(src[i] === '*' && src[i + 1] === '/')) i++
      i += 2
      continue
    }
    if (c === '"' || c === "'") {
      const quote = c
      i++
      while (i < n) {
        if (src[i] === '\\') { i += 2; continue }
        if (src[i] === quote) { i++; break }
        i++
      }
      out += '""'
      continue
    }
    if (c === '`') {
      out += '`'
      i++
      while (i < n) {
        if (src[i] === '\\') { i += 2; continue }
        if (src[i] === '`') { i++; break }
        if (src[i] === '$' && src[i + 1] === '{') {
          const end = matchBrace(src, i + 2)
          out += '${' + stripCommentsAndStrings(src.slice(i + 2, end)) + '}'
          i = end + 1
          continue
        }
        i++   // 模板里的普通文字：剥掉
      }
      out += '`'
      continue
    }
    out += c
    i++
  }
  return out
}

/** 从 ``src[from]`` 开始，找到与 ``{`` 配对的 ``}`` 的位置（跳过字符串）。 */
function matchBrace(src, from) {
  let depth = 1
  let i = from
  while (i < src.length && depth > 0) {
    const c = src[i]
    if (c === '"' || c === "'" || c === '`') {
      const quote = c
      i++
      while (i < src.length) {
        if (src[i] === '\\') { i += 2; continue }
        if (src[i] === quote) { i++; break }
        i++
      }
      continue
    }
    if (c === '{') depth++
    else if (c === '}') depth--
    if (depth > 0) i++
  }
  return i
}

/** .vue 只取 <script> 段 —— 模板与样式不是 JavaScript。 */
function scriptOnly(src, file) {
  if (!file.endsWith('.vue')) return src
  const parts = []
  const re = /<script\b[^>]*>([\s\S]*?)<\/script>/g
  let m
  while ((m = re.exec(src)) !== null) parts.push(m[1])
  return parts.join('\n')
}

/** 每个源码文件 → 剥干净的代码。 */
function codeFiles() {
  return walk(SRC).map((file) => ({
    file,
    rel: path.relative(ROOT, file),
    code: stripCommentsAndStrings(scriptOnly(fs.readFileSync(file, 'utf8'), file)),
  }))
}

const ALL = codeFiles()

/**
 * 形态一：浮点金额格式化。
 *
 * `toLocaleString` 配上 `minimumFractionDigits` / `maximumFractionDigits` ——
 * 这就是「拿 Number 走一遍再定小数位」的写法。
 * 它在 18 位整数上会把金额改小（999999999999999999.99 → 1000000000000000000），
 * 而 `fmt` 的注释里记着这件事。
 */
const FLOAT_MONEY = /toLocaleString\s*\([^)]*?(?:minimumFractionDigits|maximumFractionDigits)/s

/**
 * 形态二：整数分 → 金额文字（自己拼）。
 *
 * 指纹是**两样东西同时在场**：
 *   · 角分的两位补零 —— ``padStart(2, …)`` / ``padStart(MAX_DEC_DIGITS, …)``；
 *   · 分位换算       —— ``/ 100`` 或 ``% 100``。
 *
 * **为什么必须是「同时」**：单看 ``padStart(2, '0')`` 会把日期时间的补零
 * 全拖进来 —— ``utils/datetime.js``、``SchedulerTasks.vue``、
 * ``DocsView.vue`` 的序号、``VoucherEdit.vue`` 的日期，全是它。
 * 那一版判据一上来就报了 5 个文件，全是误报。
 * 金额文字的特征不是「补零」，是「补零 **且** 在做分位换算」。
 */
const CENT_PAD = /padStart\s*\(\s*(?:2|MAX_DEC_DIGITS)\s*[,)]/
const CENT_DIV = /(?:\/\s*100n?\b|%+\s*100n?\b)/

/**
 * 形态三：千分位插入。
 *
 * 正则 ``\B(?=(\d{3})+`` 是千分位的标准写法 —— 有了它，
 * 「整数分 → 金额文字」就自带千分位，这一段代码就是那个量的实现。
 */
const THOUSANDS = /\\B\(\?=\(\\d\{3\}\)/

/**
 * 三个形态。每一项都是「判据 + 说明」，
 * 报错信息里要能让人一眼看出**为什么**这一行被拎出来。
 */
const MONEY_SHAPES = [
  {
    label: '浮点金额格式化（toLocaleString + 小数位）',
    test: (body) => FLOAT_MONEY.test(body),
  },
  {
    label: '整数分金额文字（角分补零 + 分位换算）',
    test: (body) => CENT_PAD.test(body) && CENT_DIV.test(body),
  },
  {
    label: '千分位插入',
    test: (body) => THOUSANDS.test(body),
  },
]

test('金额格式化只能在权威文件里实现', () => {
  const offenders = []
  for (const { file, rel, code } of ALL) {
    if (AUTHORITY_FILES.has(file)) continue
    for (const shape of MONEY_SHAPES) {
      const m = shape.test(code)
      if (!m) continue
      offenders.push(`${rel} —— ${shape.label}`)
    }
  }
  assert.equal(offenders.length, 0,
    '金额格式化出现了第二个实现。\n' +
    '同一个业务对象在两个界面上显示成两个数（期初预览 1.005 vs 期初页 1.00、\n' +
    '报表屏幕 0.33 vs 导出 0.3333333333333333333333333333）就是这么来的。\n' +
    '请调用 utils/format.js 的 moneyText / reportNumber / fmt，\n' +
    '或 utils/amountGrid.js 的 moneyCents / centsToText。\n  ' +
    offenders.join('\n  '))
})

test('整数分金额文字的实现只有一份，且在权威文件里', () => {
  /**
   * 「实现」的定义：**函数体里真的在做金额格式化**。
   *
   * 为什么不是「函数名出现一次」：页面上写一个
   * ``function moneyText(cents) { return moneyTextOf(cents) }``
   * 这种**纯委托**是允许的 —— §4.5 的约定是「其余地方一律调用它」，
   * 委托正是调用。把它算成第二实现，规则就成了噪声。
   *
   * 真正要拦的是：函数体里**自己**在拼金额文字
   * （``padStart(2`` / ``toLocaleString`` / 千分位正则）。
   */
  const MONEY_SHAPES_LOCAL = MONEY_SHAPES
  const defs = []
  for (const { file, rel, code } of ALL) {
    const re = /\b(?:export\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/g
    let m
    while ((m = re.exec(code)) !== null) {
      const body = extractBody(code, m.index + m[0].length)
      if (!body) continue
      if (!MONEY_SHAPES_LOCAL.some((shape) => shape.test(body))) continue
      defs.push({ name: m[1], rel, inAuthority: AUTHORITY_FILES.has(file) })
    }
  }
  for (const name of ['moneyText', 'centsToText']) {
    const hits = defs.filter((d) => d.name === name)
    assert.equal(hits.length, 1,
      `${name} 的实现应该只有一份，实际有 ${hits.length} 份：` +
      hits.map((h) => h.rel).join(', '))
    assert.ok(hits[0].inAuthority,
      `${name} 的实现必须放在 utils/format.js 或 utils/amountGrid.js 里，` +
      `现在在 ${hits[0].rel}`)
  }
})

/** 从 ``(`` 之后开始按大括号配平，取出函数体。 */
function extractBody(code, from) {
  const open = code.indexOf('{', from)
  if (open < 0) return null
  let depth = 0
  for (let i = open; i < code.length; i++) {
    if (code[i] === '{') depth++
    else if (code[i] === '}') {
      depth--
      if (depth === 0) return code.slice(open + 1, i)
    }
  }
  return null
}

test('moneyText 与 centsToText 都必须走整数分，不许碰浮点', () => {
  const format = fs.readFileSync(path.join(SRC, 'utils', 'format.js'), 'utf8')
  const grid = fs.readFileSync(path.join(SRC, 'utils', 'amountGrid.js'), 'utf8')
  const body = (src, name) => {
    const start = src.indexOf(`export function ${name}(`)
    assert.ok(start >= 0, `${name} 不见了`)
    return extractBody(src, start + `export function ${name}`.length) || ''
  }
  for (const [src, name] of [[format, 'moneyText'], [grid, 'centsToText']]) {
    const text = body(src, name)
    assert.ok(text.includes('100n'),
      `${name} 必须用整数分算元角分（100n），不该退回浮点`)
    // 浮点那两条路都别要：toFixed 保留两位、toLocaleString 定小数位。
    assert.ok(!text.includes('toFixed'), `${name} 里出现了 toFixed —— 那是浮点`)
    assert.ok(!text.includes('toLocaleString'), `${name} 里出现了 toLocaleString —— 那是浮点`)
  }
})

test('报表页不许用会截位的 fmt', () => {
  /**
   * 报表的数字**不截两位**：后端给的是 Decimal 的全精度字符串
   * （`app/reports/evaluate.py` 里是 `str(value)`），
   * 而 `fmt` 走 Number 且 `maximumFractionDigits: 2` ——
   * 屏幕写着 0.33、导出文件里是 0.3333333333333333333333333333，
   * 两处对不上且没有任何提示（审计 §3.26）。
   *
   * 报表页要用就用 `reportNumber`。
   */
  const reportFiles = ALL.filter(
    ({ file }) => file.includes(`${path.sep}views${path.sep}report${path.sep}`) ||
      file.includes(`${path.sep}components${path.sep}Report`))
  assert.ok(reportFiles.length > 0, '没找到报表页源码 —— 判据可能失效了')
  const offenders = []
  for (const { rel, code } of reportFiles) {
    const m = /import\s*\{([^}]*)\}\s*from\s*['"][^'"]*utils\/format\.js['"]/.exec(code)
    if (!m) continue
    const names = m[1].split(',').map((s) => s.trim().split(/\s+as\s+/)[0])
    if (names.includes('fmt')) offenders.push(`${rel} —— 从 utils/format.js 引入了 fmt`)
  }
  assert.equal(offenders.length, 0,
    '报表页引入了 fmt（会截两位）。\n' +
    '报表的数字不截两位：屏幕与导出必须同一个数（审计 §3.26）。\n' +
    '请改用 utils/format.js 的 reportNumber。\n  ' +
    offenders.join('\n  '))
})
