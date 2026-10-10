import { defineStore } from 'pinia'
import * as authApi from '@/api/auth'
import * as bookApi from '@/api/book'

export const useSessionStore = defineStore('session', {
  state: () => ({
    token: localStorage.getItem('cwq_token') || '',
    user: JSON.parse(localStorage.getItem('cwq_user') || 'null'),
    books: [],
    bookId: Number(localStorage.getItem('cwq_book') || 0),
    periods: [],
    periodId: Number(localStorage.getItem('cwq_period') || 0)
  }),
  getters: {
    isAuthed: (s) => !!s.token,
    // 直接用后端下发的字段，别在前端拿用户名另立一套标准
    isAdmin: (s) => !!s.user?.is_admin,
    currentBook: (s) => s.books.find((b) => b.id === s.bookId) || null,
    currentPeriod: (s) => s.periods.find((p) => p.id === s.periodId) || null,
    canWrite: (s) => s.currentBook?.role !== 'VIEWER',
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
        // 换账套后旧的 periodId 一定不属于新账套，先清掉避免期间下拉闪旧值
        this.periods = []
        this.periodId = 0
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
        // 记住的期间已经不存在（换年度 / 反结账）—— 落在最新年度最「在用」的那一期
        const latest = this.books.find((b) => b.id === this.bookId)?.latest_year
        this.setPeriod(this.defaultPeriodId(latest))
      }
    },
    /** 默认停在「正在用的」那个期间：
     *  1) 最后一个未结账的期间（正在记账的）
     *  2) 凭证最多的期间（全部结完账时，至少能看到东西）
     *  3) 第一个期间 */
    defaultPeriodId(year) {
      const pool = year ? this.periods.filter((p) => p.year === year) : this.periods
      if (!pool.length) return undefined
      const open = [...pool].reverse().find((p) => !p.locked)
      if (open) return open.id
      const busiest = [...pool].sort((a, b) => b.vouchers - a.vouchers)[0]
      return (busiest?.vouchers ? busiest : pool[0])?.id
    },

    /** 切到某个年度：停在该年度「正在用的」那个期间。
     *  进入账套默认就是最新年度，往年可以手动选回来。 */
    setYear(year) {
      if (!year) return
      const id = this.defaultPeriodId(year)
      if (id) this.setPeriod(id)
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
