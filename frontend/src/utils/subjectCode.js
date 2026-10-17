/** 科目编码规则 + 自动接号。跟柠檬云的「科目编码规则：4-3-2-2」同一套想法。 */

/* 拼音字典有 300KB，只有真的打开科目面板搜「kcxj」时才需要它，
   所以等用户第一次开面板再动态加载，不挂在首屏的关键路径上。 */

/** '4-2-2-2' -> [4, 2, 2, 2]；写坏了就退回 4-2-2-2，别让新增科目整个卡住。 */
export function parseRule(rule) {
  const parts = String(rule || '').split('-').map((n) => parseInt(n, 10))
  return parts.length && parts.every((n) => n > 0 && n <= 8) ? parts : [4, 2, 2, 2]
}

/** 第几级该占几位：超出规则长度就按最后一段算（允许继续往下加细） */
export function segmentOf(rule, level) {
  const seg = parseRule(rule)
  return seg[Math.min(level, seg.length) - 1]
}

/**
 * 接下一个编码：父编码 + 递增序号，序号按本级该占的位数补零。
 *   规则 4-2-2-2、父 1002、已有 100201/100202/100203 → 100204
 * 没有父科目就在一级里接下一个空号（1001、1002…）。
 *
 * 序号不是「子节点个数 + 1」，而是**在现有子编码里找最大号再加一**：
 * 中间删过一个科目时，按个数接号会直接撞上已存在的编码。
 *
 * 注意 children 要用树上的嵌套结构，不能按 parent_id 过滤 ——
 * /subjects 返回的树节点里没有 parent_id，按它过滤会一个子节点都找不到，
 * 每次新增都从 01 重新开始。
 */
export function nextCode(rule, parent, tree) {
  const level = parent ? (parent.level || 1) + 1 : 1
  const width = segmentOf(rule, level)
  const pad = (n) => String(n).padStart(width, '0').slice(-width)

  if (!parent) {
    let max = 0
    for (const t of tree || []) {
      if (/^\d+$/.test(String(t.code))) max = Math.max(max, parseInt(t.code, 10))
    }
    return pad(Math.max(max + 1, 1001))
  }

  let max = 0
  for (const c of parent.children || []) {
    const tail = String(c.code).slice(String(parent.code).length)
    if (/^\d+$/.test(tail)) max = Math.max(max, parseInt(tail, 10))
  }
  return String(parent.code) + pad(Math.max(max + 1, 1))
}

const _py = new Map()
/** 科目名 → 拼音首字母（库存现金 → kcxj）。搜「kcxj」就能搜到。
 *  字典还没加载完就先按「没有首字母」处理，等它到位后同一串输入照样出结果。 */
export function initials(name) {
  const key = String(name || '')
  if (_py.has(key)) return _py.get(key)
  let out = ''
  try {
    out = _loaded?.(key, { pattern: 'first', toneType: 'none', type: 'array' })
      ?.map((c) => (/[a-zA-Z]/.test(c) ? c.toLowerCase() : '')).join('') || ''
  } catch {
    out = ''
  }
  _py.set(key, out)
  return out
}
let _loaded = null
export function ensurePinyin() {
  if (_loaded) return Promise.resolve(_loaded)
  return import('pinyin-pro').then((m) => {
    _loaded = m.pinyin
    _py.clear()          // 字典到位后把之前「查不到首字母」的缓存作废
    return _loaded
  }).catch(() => null)
}

/** 名称 / 编码 / 拼音首字母，三样都能搜到 —— 柠檬云搜索框里写的就是这三样。 */
export function matchSubject(s, kw) {
  if (!kw) return true
  const k = kw.trim().toLowerCase()
  if (!k) return true
  return String(s.code).toLowerCase().includes(k)
    || String(s.name).toLowerCase().includes(k)
    || initials(s.name).includes(k)
}

/**
 * 科目类别的**唯一**中文名。
 *
 * 五个类别用的是**简称**（EQUITY → 「权益」），不是准则里的正式表述
 * 「所有者权益」。它们只出现在科目选择器页签、科目树/导入表的类别列、
 * 新增科目的类别下拉和期初分组上 —— 全是页面上空间紧张的地方，
 * 「所有者权益」四个字会把页签撑得比别的宽一大截。
 *
 * **正式表述另有其人，别在这里统一。** 资产负债表里的
 * 「负债和所有者权益（或股东权益）总计」是《小企业会计准则》的行次名称，
 * 由 `reports/builtin_reports.py` 写死，走的是另一套文案（见 ReportView
 * 的注释）。所以同一个类别在「选科目的页签」上叫权益、在「报表的行次」
 * 上叫所有者权益，是**有意如此**，不是没统一的漏网之鱼。
 *
 * 这里曾经反过来统一成「所有者权益」（就为了消掉上面那个「两个名字」），
 * 结果是页签变宽、ui_subject.mjs 里按「权益」断言的那条验收一直是红的。
 * 代价选小的那个：要窄就窄，报表里的正式名称一个字不动。
 */
export const CATEGORY_LABEL = {
  ASSET: '资产', LIABILITY: '负债', EQUITY: '权益', COST: '成本', PROFIT: '损益'
}
/** 类别在页面上出现的顺序（会计恒等式：资产 = 负债 + 所有者权益 + 损益） */
export const CATEGORY_ORDER = ['ASSET', 'LIABILITY', 'EQUITY', 'COST', 'PROFIT']
/** 类别 → 该类科目的常规余额方向，顶层新增科目时给个默认值 */
export const CATEGORY_DEFAULT_DIRECTION = {
  ASSET: 'DEBIT', COST: 'DEBIT', LIABILITY: 'CREDIT', EQUITY: 'CREDIT', PROFIT: 'CREDIT'
}
