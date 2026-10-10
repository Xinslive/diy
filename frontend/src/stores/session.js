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
    currentBook: (s) => s.books.find((b) => b.id === s.bookId) || null,
    currentPeriod: (s) => s.periods.find((p) => p.id === s.periodId) || null,
    canWrite: (s) => s.currentBook?.role !== 'VIEWER'
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
    logout() {
      this.token = ''
      this.user = null
      this.books = []
      localStorage.removeItem('cwq_token')
      localStorage.removeItem('cwq_user')
      localStorage.removeItem('cwq_book')
      localStorage.removeItem('cwq_period')
    },
    async loadBooks() {
      this.books = await bookApi.listBooks()
      if (!this.books.find((b) => b.id === this.bookId)) {
        this.setBook(this.books[0]?.id)
      }
    },
    async setBook(id) {
      this.bookId = id
      localStorage.setItem('cwq_book', String(id))
      await this.loadPeriods()
    },
    async loadPeriods() {
      if (!this.bookId) return
      this.periods = await bookApi.listPeriods(this.bookId)
      if (!this.periods.find((p) => p.id === this.periodId)) {
        this.setPeriod(this.defaultPeriodId())
      }
    },
    /** 默认停在「正在用的」那个期间：
     *  1) 最后一个未结账的期间（正在记账的）
     *  2) 凭证最多的期间（全部结完账时，至少能看到东西）
     *  3) 第一个期间 */
    defaultPeriodId() {
      const open = [...this.periods].reverse().find((p) => !p.locked)
      if (open) return open.id
      const busiest = [...this.periods].sort((a, b) => b.vouchers - a.vouchers)[0]
      return (busiest?.vouchers ? busiest : this.periods[0])?.id
    },

    setPeriod(id) {
      this.periodId = id
      localStorage.setItem('cwq_period', String(id))
    },
    periodCodeOf(id) {
      return this.periods.find((p) => p.id === id)?.code || ''
    }
  }
})
