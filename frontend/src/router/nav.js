/**
 * 导航结构：顶部模块 → 左侧二级菜单。
 *
 * 这里只描述「菜单长什么样」，路由仍然是 router/index.js 里那份。
 * 两边用 route name 对齐 —— 加页面时两边都要加，漏了一边菜单就少一项，
 * 路由仍然能直接敲地址栏进得去，所以那种漏网很难在测试里被发现。
 */
import {
  Collection, Connection, DataAnalysis, Document, Files, Folder, Grid, List,
  Notebook, Printer, Setting, Sort, Tickets, TrendCharts, Upload, User, Coin,
  CircleCheck, Lock, Calendar, Tools,
} from '@element-plus/icons-vue'

export const NAV = [
  {
    key: 'voucher',
    title: '凭证处理',
    icon: Tickets,
    // 点顶部模块就落到这一页：凭证处理 → 凭证，账簿报表 → 明细账，
    // 基础资料 → 账套管理。没写 landing 的取第一项。
    landing: 'vouchers',
    items: [
      { name: 'vouchers', title: '凭证', icon: Document },
      // 这两个是偶尔才用一次的批量工具，不占侧栏常驻位置。路由照旧，
      // 菜单里拿 hidden 过滤掉；title 仍留在 NAV 里，标签页标题才认得出。
      { name: 'voucher-organize', title: '整理凭证', icon: Sort, hidden: true },
      { name: 'voucher-import', title: '凭证导入', icon: Upload, hidden: true },
      { name: 'invoice-import', title: '发票导入', icon: Tickets },
      // 结账是录完凭证之后的下一步，跟着「凭证」这一条线走最顺；
      // 原来它跟打印模板混在一个「期末与输出」模块里，两件事互不相干。
      { name: 'periods', title: '期末结账', icon: CircleCheck },
    ],
  },
  {
    key: 'ledger',
    title: '账簿报表',
    icon: DataAnalysis,
    landing: 'detail-ledger',
    items: [
      { name: 'balance', title: '科目余额表', icon: Grid },
      { name: 'general-ledger', title: '总账', icon: Notebook },
      { name: 'detail-ledger', title: '明细账', icon: List },
      { name: 'reports', title: '报表', icon: TrendCharts },
    ],
  },
  {
    key: 'master',
    title: '基础资料',
    icon: Files,
    landing: 'books',
    items: [
      { name: 'subjects', title: '科目表', icon: Collection },
      { name: 'books', title: '账套管理', icon: Folder },
      { name: 'openings', title: '期初余额', icon: Coin },
      { name: 'print-templates', title: '打印模板', icon: Printer },
    ],
  },
  {
    key: 'system',
    title: '系统管理',
    icon: Setting,
    items: [
      { name: 'audit', title: '操作日志', icon: Document, adminOnly: true },
      { name: 'users', title: '用户与授权', icon: User, adminOnly: true },
      // 下面两项都排最后，别插在前面：这个模块没写 landing，点顶部「系统管理」
      // 落到第一项，插到首位会把落地页从「操作日志」悄悄换成这个低频页面。
      { name: 'ocr-settings', title: '外部服务', icon: Connection, adminOnly: true },
      { name: 'data-security', title: '数据安全', icon: Lock, adminOnly: true },
      { name: 'plugins', title: '扩展管理', icon: Grid, adminOnly: true },
      { name: 'scheduler-tasks', title: '计划任务', icon: Calendar, adminOnly: true },
    ],
  },
]

/** 落地页是模块的哪一项：取 ``landing``；没写、或那一项被权限/隐藏过滤掉了，
 *  就退到第一项 —— 第一项本来就是这个模块最常用的入口。 */
function resolveLanding(module) {
  const names = module.items.map((i) => i.name)
  return [module.landing, ...names].find((n) => n && names.includes(n)) || names[0]
}

/**
 * 把落地页顶到侧栏第一位。
 *
 * 点顶部模块就落到这一页，那它就该是侧栏的第一项 —— 否则切一次模块要先在
 * 侧栏里扫一眼才知道自己到了哪（明细账原本排第三、账套管理排第二）。
 *
 * **排序在这里做，而不是把 NAV 的数组重排一次**：以后谁再往落地页前面插一项，
 * 顺序会重新漂回去，而「点顶部落到的那一页」和「侧栏第一项」必须永远一致。
 * 取哪一项仍由 :func:`resolveLanding` 决定 —— 与点顶部模块用的是同一个函数，
 * 两边算不出两个答案。
 */
function liftLanding(module) {
  const landing = resolveLanding(module)
  const first = landing && module.items.find((i) => i.name === landing)
  return first
    ? { ...module, items: [first, ...module.items.filter((i) => i !== first)] }
    : module
}

/** 菜单按当前身份过滤：非管理员看不到系统管理里那两项，hidden 的也不进菜单。 */
export function visibleNav(isAdmin, plugins = []) {
  const source = [...NAV]
  const icons = { Calendar, Grid, Document, DataAnalysis, Tools, Coin, Folder }
  if (plugins.length) source.push({ key: 'extensions', title: '扩展', icon: Tools,
    items: [...plugins].sort((a, b) => (a.menu.order || 0) - (b.menu.order || 0) || a.id.localeCompare(b.id)).map(p => ({ name: p.menu.name, title: p.menu.title, icon: icons[p.menu.icon] || Grid })) })
  return source
    .map((m) => liftLanding({ ...m, items: m.items.filter((it) => (!it.adminOnly || isAdmin) && !it.hidden) }))
    // 整个模块被过滤空了（理论上只有非管理员才会发生）就不占顶部位置
    .filter((m) => m.items.length)
}

/**
 * 点顶部模块落到哪一页。
 *
 * **传入的必须是 visibleNav 里的模块** —— 里面的 items 已经过滤过，
 * 落地页一定在侧栏里找得到。
 */
export function landingOf(module) {
  return resolveLanding(module)
}

/**
 * 有些路由不直接对应菜单项，指回它所属的那一项。
 *
 * 凭证列表 / 新增凭证 / 某张凭证详情共用「凭证」这一个入口 —— 连着翻凭证是
 * 这个系统最高频的动作，每翻一张就多一个标签的话，标签栏很快就撑爆了，
 * 「我刚才看的是哪张」也就找不回来了。报表详情同理，它属于「报表」。
 *
 * 菜单高亮、侧栏归属、标签页 key 三处都用它，免得各算各的算出三个答案。
 */
const ROUTE_MENU = {
  'voucher-new': 'vouchers',
  'voucher-edit': 'vouchers',
  'report-view': 'reports',
}

export function menuKeyOf(routeName) {
  return ROUTE_MENU[routeName] || routeName
}
