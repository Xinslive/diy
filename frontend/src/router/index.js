import { createRouter, createWebHashHistory } from 'vue-router'
import { useSessionStore } from '@/stores/session'

const routes = [
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
      { path: 'openings', name: 'openings', component: () => import('@/views/book/OpeningBalance.vue'), meta: { title: '期初余额' } },
      { path: 'vouchers', name: 'vouchers', component: () => import('@/views/voucher/VoucherList.vue'), meta: { title: '凭证' } },
      { path: 'vouchers/new', name: 'voucher-new', component: () => import('@/views/voucher/VoucherEdit.vue'), meta: { title: '新增凭证' } },
      { path: 'vouchers/:id', name: 'voucher-edit', component: () => import('@/views/voucher/VoucherEdit.vue'), meta: { title: '修改凭证' } },
      { path: 'balance', name: 'balance', component: () => import('@/views/ledger/BalanceSheet.vue'), meta: { title: '科目余额表' } },
      { path: 'general-ledger', name: 'general-ledger', component: () => import('@/views/ledger/GeneralLedger.vue'), meta: { title: '总账' } },
      { path: 'detail-ledger', name: 'detail-ledger', component: () => import('@/views/ledger/DetailLedger.vue'), meta: { title: '明细账' } },
      { path: 'reports', name: 'reports', component: () => import('@/views/report/ReportList.vue'), meta: { title: '报表' } },
      { path: 'reports/:templateId', name: 'report-view', component: () => import('@/views/report/ReportView.vue'), meta: { title: '报表' } },
      { path: 'periods', name: 'periods', component: () => import('@/views/period/PeriodPanel.vue'), meta: { title: '期末结账' } },
      { path: 'subjects', name: 'subjects', component: () => import('@/views/subject/SubjectTree.vue'), meta: { title: '科目表' } },
      { path: 'audit', name: 'audit', component: () => import('@/views/system/AuditLog.vue'), meta: { title: '操作日志', adminOnly: true } },
      { path: 'vouchers/organize', name: 'voucher-organize', component: () => import('@/views/voucher/VoucherOrganize.vue'), meta: { title: '整理凭证' } },
      { path: 'vouchers/import', name: 'voucher-import', component: () => import('@/views/voucher/VoucherImport.vue'), meta: { title: '凭证导入' } },
      { path: 'vouchers/invoices', name: 'invoice-import', component: () => import('@/views/voucher/InvoiceImport.vue'), meta: { title: '发票导入' } },
      { path: 'print-templates', name: 'print-templates', component: () => import('@/views/print/PrintTemplate.vue'), meta: { title: '打印模板' } },
      { path: 'users', name: 'users', component: () => import('@/views/system/UserAdmin.vue'), meta: { title: '用户与授权', adminOnly: true } },
      { path: 'ocr-settings', name: 'ocr-settings', component: () => import('@/views/system/OcrSettings.vue'), meta: { title: '外部服务', adminOnly: true } },
      { path: 'notify-settings', name: 'notify-settings', component: () => import('@/views/system/NotifySettings.vue'), meta: { title: '邮件通知', adminOnly: true } },
      { path: 'data-security', name: 'data-security', component: () => import('@/views/system/DataSecurity.vue'), meta: { title: '数据安全', adminOnly: true } }
    ]
  },
  { path: '/:pathMatch(.*)*', redirect: '/books' }
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach((to) => {
  const store = useSessionStore()
  if (to.meta.public) return true
  if (!store.isAuthed) return { name: 'login' }
  // 菜单已经对非管理员隐藏，这里再兜一道：手敲地址栏也不该进来
  if (to.meta.adminOnly && !store.isAdmin) return { name: 'books' }
  return true
})

export default router
