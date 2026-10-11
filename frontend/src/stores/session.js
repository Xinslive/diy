import { defineStore } from 'pinia'
import * as authApi from '@/api/auth'
import * as bookApi from '@/api/book'

/** 能写的角色。与后端 ROLES（app/api/system.py）保持一致。 */
const WRITABLE_ROLES = ['OWNER', 'ACCOUNTANT']

export const useSessionStore = defineStore('session', {
  state: () => ({
    token: localStorage.getItem('cwq_token') || '',
    user: JSON.parse(localStorage.getItem('cwq_user') || 'null'),
    books: [],
    bookId: Number(localStorage.getItem('cwq_book') || 0),
    periods: [],
    periodId: Number(localStorage.getItem('cwq_period') || 0),
    // 刚建好的账套。放在 store 而不是组件里，是因为建完第一个账套时
    // bookId 会从 0 变成新账套，MainLayout 的 viewKey 一变整个页面就重新挂载，
    // 组件内的 newBookId 会被丢掉，「下一步录期初余额」的提示就没了。
    lastCreatedBookId: 0
  }),
  getters: {
    isAuthed: (s) => !!s.token,
    // 直接用后端下发的字段，别在前端拿用户名另立一套标准
    isAdmin: (s) => !!s.user?.is_admin,
    currentBook: (s) => s.books.find((b) => b.id === s.bookId) || null,
    currentPeriod: (s) => s.periods.find((p) => p.id === s.periodId) || null,
    /** 当前账套能不能写。**失败时关闭（fail-closed）**。
     *
     *  原来是 ``s.currentBook?.role !== 'VIEWER'`` —— 黑名单写法。
     *  currentBook 为 null 时（账套还没加载完、加载失败、刚切账套的
     *  中间态）得到 undefined !== 'VIEWER' → **true**，于是加载间隙里
     *  可写按钮是亮的，点下去被后端 403 拒掉。
     *
     *  改成白名单：认不得的角色一律当不能写。后端认的是 OWNER /
     *  ACCOUNTANT（见 ROLES），前端跟着认同一份，不要各立一套标准。
     *  多认一个角色只会在后端拒的时候让用户困惑，少认一个顶多按钮
     *  灰着 —— 两个方向的代价不对称。 */
    canWrite: (s) => !!s.currentBook && WRITABLE_ROLES.includes(s.currentBook.role),
    /** 正在看的年度 —— 年度是账套的属性，不是另一个账套。
     *  由期间反推，不再单独存一份，免得两边对不上。 */
    currentYear: (s) => s.periods.find((p) => p.id === s.periodId)?.year
      ?? s.books.find((b) => b.id === s.bookId)?.latest_year ?? 0,
    /** 当前年度的期间，页面上按年度分组用 */
    periodsOfYear: (s) => (y) => s.periods.filter((p) => p.year === y),
    yearsOfBook: (s) => (b) => (b?.years || []).map((y) => y.year).sort((x, y) => y - x)
  },
  actions: {
    async login(username, password) {
      const data = await authApi.login(username, password)
      this.token = data.access_token
      this.user = data.user
      localStorage.setItem('cwq_token', data.access_token)
      localStorage.setItem('cwq_user', JSON.stringify(data.user))
      await this.loadBooks()
    },
    /** 重新拉一次登录态。
     *  localStorage 里的 user 是登录那一刻的快照：老版本缓存的 user 没有
     *  is_admin 字段，不刷新的话管理员会凭空丢掉「用户与授权」菜单。
     *  顺带也能让被降权的人立刻生效。 */
    async refreshMe() {
      try {
        this.user = await authApi.me()
        localStorage.setItem('cwq_user', JSON.stringify(this.user))
      } catch {
        this.logout()
      }
    },
    logout() {
      this.token = ''
      this.user = null
      this.books = []
      localStorage.removeItem('cwq_token')
      localStorage.removeItem('cwq_user')
      localStorage.removeItem('cwq_book')
      localStorage.removeItem('cwq_period')
      this.lastCreatedBookId = 0
    },
    /** 拉账套列表，并保证当前账套/期间一定能落到一个有效值上。
     *
     *  这里必须**无条件**跟着 loadPeriods：以前只在「记住的账套 id 失效」时才
     *  走 setBook，于是「localStorage 里的账套还在、期间数组却是空的」这种状态
     *  没人收拾 —— 表现就是顶栏期间下拉点开只有「无数据」。 */
    async loadBooks() {
      this.books = await bookApi.listBooks()
      if (!this.books.find((b) => b.id === this.bookId)) {
        this.bookId = 0
        this.periodId = 0
      }
      if (!this.bookId) this.bookId = this.books[0]?.id || 0
      if (this.bookId) localStorage.setItem('cwq_book', String(this.bookId))
      await this.loadPeriods()
    },

    /** 换账套：默认进最新年度。 */
    async setBook(id) {
      const changed = this.bookId !== id
      this.bookId = id
      localStorage.setItem('cwq_book', String(id))
      if (changed) {
        // 换账套后旧的 periodId 一定不属于新账套，先清掉避免期间下拉闪旧值。
        // 进账套一律按「最早还在用的年度 + 最早未结账月」落位，不接着上次的账套看。
        this.periods = []
        this.periodId = 0
        localStorage.removeItem('cwq_period')
      }
      await this.loadPeriods()
    },
    async loadPeriods() {
      if (!this.bookId) {
        this.periods = []
        this.periodId = 0
        return
      }
      this.periods = await bookApi.listPeriods(this.bookId)
      if (!this.periods.find((p) => p.id === this.periodId)) {
        // 记住的期间已经不存在（换年度 / 反结账）—— 落在「还在用的」那一期
        this.setPeriod(this.defaultPeriodId(this.defaultYear()))
      }
    },
    /** 默认停在的期间（用户 2026-10-11 定的口径）：
     *
     *  年度 = **最早一个还在用**的年度：至少有一个月没结账的年度里最靠前的那个。
     *    2026 全结完并已年度结转、2027 刚开好 → 2027（2027 每个月都没结账）
     *    2026 全结完但没年度结转 → 2026（2026 才是还在用的那一年）
     *
     *  月份 = 该年度里**最靠前的一个未结账月份**；全年都结完了就停在 12 月。
     *    结到 2026-12 且已结转 → 2027-01
     *    结到 2026-12 未结转 → 2026-12
     *    结到 2027-02      → 2027-03
     *
     *  以前这里是「最后一个未结账的期间」，一年没结完就会直接跳到 12 月，
     *  打开账套看到的不是眼下该干活的那个月。
     */
    defaultPeriodId(year) {
      const pool = this.periods
      if (!pool.length) return undefined
      const pool0 = year ? pool.filter((p) => p.year === year) : pool
      if (!pool0.length) return undefined

      const open = pool0.find((p) => !p.locked)     // 数组按 ord 升序，最靠前即最小
      if (open) return open.id
      return pool0[pool0.length - 1].id              // 全结完 → 该年度最后一个月（12 月）
    },

    /** 默认年度：最早一个还有月份没结账的年度；全都结完就取最后一年。 */
    defaultYear() {
      if (!this.periods.length) return undefined
      const years = [...new Set(this.periods.map((p) => p.year))].sort((a, b) => a - b)
      return years.find((y) => this.periods.some((p) => p.year === y && !p.locked))
        ?? years[years.length - 1]
    },

    /** 切到某个年度：停在该年度「正在用的」那个期间。
     *  进入账套默认就是最新年度，往年可以手动选回来。 */
    setYear(year) {
      if (!year) return
      const id = this.defaultPeriodId(year)
      if (id) this.setPeriod(id)
    },

    /** 记下刚建好的账套：页面会因 viewKey 变化重新挂载，提示得跨过这一次挂载。 */
    setLastCreatedBook(id) {
      this.lastCreatedBookId = id || 0
    },

    setPeriod(id) {
      this.periodId = id || 0
      if (id) localStorage.setItem('cwq_period', String(id))
      else localStorage.removeItem('cwq_period')
    },
    periodCodeOf(id) {
      return this.periods.find((p) => p.id === id)?.code || ''
    }
  }
})
