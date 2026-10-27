import { createRouter, createWebHashHistory } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { needsBookRedirect } from '@/utils/bookGuard'

const routes = [
  // 登录页。这里注册的是 **hash 路由里的路径段**（'/login'），
  // 浏览器上的完整地址是 '#/login' —— 那一份字符串住在 api/client.js 的
  // LOGIN_HASH 常量里（令牌失效时用它跳回来）。
  // **改这一处就要改那一处**：以前两边各写一份，改了一处忘了另一处，
  // 令牌一失效就跳到一个不存在的地址，页面上只剩一片白。
  { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true } },
  // 插件开发文档：全站**唯一**免登录的页面，也是全站**唯一没有入口**的页面。
  //
  // 「没有入口」不是靠自觉，而是靠它不在 nav.js 的 NAV 里 —— 不在 NAV
  // 就不会出现在侧栏和顶部模块，tabs.js 的 KNOWN_KEYS（由 NAV 派生）也
  // 自动不含它，于是标签页和 localStorage 同样不会记它。任何页面、README、
  // ARCHITECTURE 都不放它的链接；tests/docs_page.test.mjs 会把这条钉死。
  //
  // 放成顶层路由而不是 '/' 的 children，正是为了不经过 MainLayout：
  // 不创建标签页，也不用它的账套/期间选择器。
  { path: '/docs', name: 'docs', component: () => import('@/views/docs/DocsView.vue'), meta: { public: true, title: '插件开发文档' } },
  {
    path: '/',
    name: 'workspace',
    component: () => import('@/layouts/MainLayout.vue'),
    redirect: '/books',
    children: [
      { path: 'plugin/:pluginId/:rest(.*)*', name: 'plugin-entry', component: () => import('@/plugins/unavailable').then(m => m.PluginUnavailable) },
      { path: 'plugins', name: 'plugins', component: () => import('@/views/system/PluginManager.vue'), meta: { title: '扩展管理', adminOnly: true } },
      { path: 'scheduler-tasks', name: 'scheduler-tasks', component: () => import('@/views/system/SchedulerTasks.vue'), meta: { title: '计划任务', adminOnly: true } },
      { path: 'books', name: 'books', component: () => import('@/views/book/BookManager.vue'), meta: { title: '账套管理' } },
      { path: 'openings', name: 'openings', component: () => import('@/views/book/OpeningBalance.vue'), meta: { title: '期初余额', requiresBook: true } },
      { path: 'vouchers', name: 'vouchers', component: () => import('@/views/voucher/VoucherList.vue'), meta: { title: '凭证', requiresBook: true } },
      { path: 'vouchers/new', name: 'voucher-new', component: () => import('@/views/voucher/VoucherEdit.vue'), meta: { title: '新增凭证', requiresBook: true } },
      { path: 'vouchers/:id', name: 'voucher-edit', component: () => import('@/views/voucher/VoucherEdit.vue'), meta: { title: '修改凭证', requiresBook: true } },
      { path: 'balance', name: 'balance', component: () => import('@/views/ledger/BalanceSheet.vue'), meta: { title: '科目余额表', requiresBook: true } },
      { path: 'general-ledger', name: 'general-ledger', component: () => import('@/views/ledger/GeneralLedger.vue'), meta: { title: '总账', requiresBook: true } },
      { path: 'detail-ledger', name: 'detail-ledger', component: () => import('@/views/ledger/DetailLedger.vue'), meta: { title: '明细账', requiresBook: true } },
      { path: 'reports', name: 'reports', component: () => import('@/views/report/ReportList.vue'), meta: { title: '报表', requiresBook: true } },
      { path: 'reports/:templateId', name: 'report-view', component: () => import('@/views/report/ReportView.vue'), meta: { title: '报表', requiresBook: true } },
      { path: 'periods', name: 'periods', component: () => import('@/views/period/PeriodPanel.vue'), meta: { title: '期末结账', requiresBook: true } },
      { path: 'subjects', name: 'subjects', component: () => import('@/views/subject/SubjectTree.vue'), meta: { title: '科目表', requiresBook: true } },
      { path: 'audit', name: 'audit', component: () => import('@/views/system/AuditLog.vue'), meta: { title: '操作日志', adminOnly: true, requiresBook: true } },
      { path: 'vouchers/organize', name: 'voucher-organize', component: () => import('@/views/voucher/VoucherOrganize.vue'), meta: { title: '整理凭证', requiresBook: true } },
      { path: 'vouchers/import', name: 'voucher-import', component: () => import('@/views/voucher/VoucherImport.vue'), meta: { title: '凭证导入', requiresBook: true } },
      { path: 'vouchers/invoices', name: 'invoice-import', component: () => import('@/views/voucher/InvoiceImport.vue'), meta: { title: '发票导入', requiresBook: true } },
      // 流水对账：与「发票导入」**同级**的独立功能（不是发票导入的附属）。
      { path: 'vouchers/reconciliation', name: 'flow-reconciliation', component: () => import('@/views/bank/FlowReconciliation.vue'), meta: { title: '流水对账', requiresBook: true } },
      { path: 'print-templates', name: 'print-templates', component: () => import('@/views/print/PrintTemplate.vue'), meta: { title: '打印模板', requiresBook: true } },
      { path: 'users', name: 'users', component: () => import('@/views/system/UserAdmin.vue'), meta: { title: '用户与授权', adminOnly: true } },
      // 个人资料：**不占侧栏**，入口在右上角的账号菜单里。
      //
      // 它不属于任何一个模块（不是「凭证处理」，也不是「系统管理」——那一模块
      // 是管理员的），所以侧栏里没有它的位置；放进去反而会让「这一页属于哪个
      // 模块」这件事变得说不清。
      //
      // **不标 requiresBook**：改自己的名字与口令跟账套无关 —— 管理员自己
      // 未必有账套，标了就把这条路堵死。
      { path: 'profile', name: 'profile', component: () => import('@/views/ProfileView.vue'), meta: { title: '个人资料' } },
      { path: 'ocr-settings', name: 'ocr-settings', component: () => import('@/views/system/OcrSettings.vue'), meta: { title: '外部服务', adminOnly: true } },
      { path: 'notify-settings', name: 'notify-settings', component: () => import('@/views/system/NotifySettings.vue'), meta: { title: '邮件通知', adminOnly: true } },
      { path: 'data-security', name: 'data-security', component: () => import('@/views/system/DataSecurity.vue'), meta: { title: '数据安全', adminOnly: true } },
      { path: 'mfa-settings', name: 'mfa-settings', component: () => import('@/views/system/MfaSettings.vue'), meta: { title: '二次验证', adminOnly: true } }
    ]
  },
  { path: '/:pathMatch(.*)*', redirect: '/books' }
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach(async (to) => {
  const store = useSessionStore()
  if (to.meta.public) return true
  if (!store.isAuthed) return { name: 'login' }
  // 菜单已经对非管理员隐藏，这里再兜一道：手敲地址栏也不该进来
  if (to.meta.adminOnly && !store.isAdmin) return { name: 'books' }
  // 账套相关的页面：没有账套就回账套管理 —— 那里是唯一能建账套的地方，
  // 而这些页面在没有账套时打开只会得到一张「暂无数据」的空表，看不出所以然。
  //
  // **先问过再判断**：列表还没拉回来时 books 是空的，那是「还没问」不是「没有」。
  // 直接拿空数组去判，会把有账套的用户在打开深链接的那一瞬间也踢回账套管理。
  if (to.meta.requiresBook && !store.booksLoaded) {
    try {
      await store.loadBooks()
    } catch {
      // 拉不到账套列表就别拦人：宁可放他进去看到空表，也不要把他困在账套管理。
      // 这时候 booksLoaded 仍是 false，判定不会把他当成「没有账套的人」。
    }
  }
  if (needsBookRedirect({
    requiresBook: to.meta.requiresBook,
    booksLoaded: store.booksLoaded,
    bookCount: store.books.length
  })) {
    return { name: 'books' }
  }
  return true
})

export default router
