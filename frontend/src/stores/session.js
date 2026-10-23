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
    // 账套列表**问过了没有**。
    //
    // 空数组有两种完全不同的含义：「一个账套都没有」和「还没去问」。
    // 路由守卫要据此决定「没有账套就把人送回账套管理」—— 拿「还没问」当成
    // 「没有」，会把有账套的用户在打开深链接的那一瞬间踢回账套管理。
    // 所以这里记一份「问过了」的事实，而不是从 books.length 里反推。
    booksLoaded: false,
    bookId: Number(localStorage.getItem('cwq_book') || 0),
    periods: [],
    periodId: Number(localStorage.getItem('cwq_period') || 0),
    // 刚建好的账套。放在 store 而不是组件里，是因为建完第一个账套时
    // bookId 会从 0 变成新账套，MainLayout 的 viewKey 一变整个页面就重新挂载，
    // 组件内的 newBookId 会被丢掉，「下一步录期初余额」的提示就没了。
    lastCreatedBookId: 0,
    // 每个账套**每个年度**的初始化状态（两步向导要的那份 init）。
    // 形状是 bookId → year → init。
    //
    // **为什么按年度存**：初始化状态是**年度**的属性（后端迁移 0032）——
    // 每个年度有**自己的**科目表，也各有各的「第 1 步做没做」。
    // 账套级的那一份会把 2026 做完的状态套到 2027 上，于是 2027 年可以在
    // **一张科目都没有**的情况下录期初、记账，期初整批挂空。
    //
    // 账套列表行里已经自带 init（装的是当前在记的那一年），
    // 这里存的是**比列表更新的那份**：向导里刚做完一步时，列表还没重新拉过。
    initByBook: {},
    // 管理员二次验证**进行中**的状态。**null = 没有在进行中。**
    //
    // 形状：
    //   { challengeId, clientSecret, expiresIn, emailHint, resendAfter, sentAt }
    //
    // **这里绝不能出现访问令牌。** 二次验证没完成之前，令牌还不存在 ——
    // 它要等邮件里的链接被点开、服务端把挑战标成已验证之后，才由**发起
    // 登录的这台机器**（凭 challenge_id + client_secret）换回来。
    mfa: null
  }),
  getters: {
    isAuthed: (s) => !!s.token,
    // 直接用后端下发的字段，别在前端拿用户名另立一套标准
    isAdmin: (s) => !!s.user?.is_admin,
    currentBook: (s) => s.books.find((b) => b.id === s.bookId) || null,
    /**
     * 某账套的初始化状态。拿不到时返回 **null 而不是一份全 false 的假状态** ——
     * 「不知道」和「知道了，还没做」在界面上是两回事：前者不该弹出
     * 「第 1 步还没做」的拦截条（那是在替后端下结论），后者要弹。
     * 判定动作能不能做时再用 utils/bookInit 的 fail-closed 那套 —— 显示等知情，动作不等。
     *
     *  两份都留着、initByBook 优先：它装的是**单个账套**的精确刷新
     * （loadInit / markInit），而 books 那份是列表接口顺带捎回来的。
     * 代价是 initByBook 一旦陈旧就会盖住更新的列表数据 —— 所以 loadBooks
     * 每次都要用新列表把它冲一遍，那条纪律就写在那里。
     */
    initOf: (s) => (bookId, year) => {
      const byYear = s.initByBook[bookId]
      const book = s.books.find((b) => b.id === bookId)
      // 不指定年度时取**这个账套当前在记的那一年** ——
      // 与后端列表接口随行带回来的那份是同一年（列表给的是「当前在记的那一年」）。
      const wanted = year ?? book?.latest_year
      if (wanted != null && byYear && byYear[wanted]) return byYear[wanted]
      if (year != null) return null
      return book?.init ?? null
    },
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
    /**
     * 某个账套「当前在记的那一年」。
     *
     * 正在看这个账套时跟顶栏的年份一致（用户已经用顶栏表达过
     * 「我在看哪一年」）；看别的账套时取它最新的那个年度。
     *
     * 初始化向导、期初页、科目表页都要用同一个年度 ——
     * 三处各算各的就会「向导说 2026 做完了、期初页说 2027 还没做」。
     */
    yearOfBook() {
      return (bookId) => {
        const book = this.books.find((b) => b.id === bookId)
        if (this.bookId === bookId) return this.currentYear || book?.latest_year
        return book?.latest_year
      }
    },
    yearsOfBook: (s) => (b) => (b?.years || []).map((y) => y.year).sort((x, y) => y - x),
    /**
     * 距离下一次可以「重新发送验证邮件」还有几秒。
     *
     * 传一个 ``now`` 进来（组件里那个每秒跳一次的时钟）是为了让倒计时
     * 真的会走 —— 直接在 getter 里读 ``Date.now()`` 的话，Vue 察觉不到
     * 时间变了，界面上的倒计时会一直停在同一个数。
     */
    mfaResendWait: (s) => (now = Date.now()) => {
      const m = s.mfa
      if (!m) return 0
      const after = (m.resendAfter || 60) * 1000
      return Math.max(0, Math.ceil((m.sentAt + after - now) / 1000))
    },
    /** 验证链接还剩几秒过期。 */
    mfaExpiresIn: (s) => (now = Date.now()) => {
      const m = s.mfa
      if (!m) return 0
      return Math.max(0, Math.ceil((m.sentAt + (m.expiresIn || 0) * 1000 - now) / 1000))
    }
  },
  actions: {
    /**
     * 登录。
     *
     * **返回 `{mfa_required}`**：管理员且服务端开着二次验证时，服务端
     * **不发令牌**，只回一个挑战。这一刻**不能写任何令牌进 localStorage** ——
     * 令牌还不存在，它要等邮件里的链接被点开之后才换得回来。写了就等于
     * 「绕过二次验证也拿到了会话」。
     */
    async login(username, password) {
      const data = await authApi.login(username, password)
      if (data && data.mfa_required) {
        this.mfa = {
          challengeId: data.challenge_id,
          clientSecret: data.client_secret,
          expiresIn: data.expires_in,
          emailHint: data.email_hint || '',
          resendAfter: 60,
          sentAt: Date.now()
        }
        return { mfa_required: true }
      }
      await this.applySession(data)
      return { mfa_required: false }
    },

    /**
     * 把一份「登录成功」的回执写进本地状态。**登录与二次验证换到令牌
     * 之后走的是同一条路** —— 界面上的后续行为（跳首页、拉账套）不该
     * 因为「这次是二次验证过来的」而有一份单独的代码。
     */
    async applySession(data) {
      this.token = data.access_token
      this.user = data.user
      localStorage.setItem('cwq_token', data.access_token)
      localStorage.setItem('cwq_user', JSON.stringify(data.user))
      await this.loadBooks()
    },

    /**
     * 问一次「验证通过了吗」。
     *
     * 返回 `{status}`：``PENDING`` = 还没点链接（**这是正常状态，不是错误**）；
     * ``VERIFIED`` = 通过了，令牌已经写进本地。
     *
     * **轮询失败不能把用户判成「登录失败」** —— 等待邮件期间网络抖一下、
     * 或者链接还没点，都是常事。真正的失败（链接过期、挑战作废）由服务端
     * 用明确的文案说出来，前端照原样显示。
     */
    async pollMfa() {
      const m = this.mfa
      if (!m) return { status: 'IDLE' }
      const data = await authApi.mfaPoll(m.challengeId, m.clientSecret)
      if (data && data.access_token) {
        this.mfa = null
        await this.applySession({ access_token: data.access_token, user: data.user })
        return { status: 'VERIFIED' }
      }
      return { status: (data && data.status) || 'PENDING' }
    },

    /**
     * 重新发送验证邮件。
     *
     * **本地也拦一道最短间隔**：服务端会拦（429 + Retry-After），但让人
     * 一次次点一个注定失败的按钮，只是在制造挫败感。
     *
     * **重发会换一个新的挑战**（新的 challenge_id 与 client_secret），
     * 所以这里用返回值覆盖手里那一对 —— 旧的那对已经作废。
     */
    async resendMfa() {
      const m = this.mfa
      if (!m) return { ok: false, reason: 'no_challenge' }
      const wait = this.mfaResendWait()
      if (wait > 0) return { ok: false, reason: 'too_soon', wait }
      const data = await authApi.mfaResend(m.challengeId, m.clientSecret)
      this.mfa = {
        challengeId: data.challenge_id,
        clientSecret: data.client_secret,
        expiresIn: data.expires_in,
        emailHint: data.email_hint || m.emailHint,
        resendAfter: data.resend_after || 60,
        sentAt: Date.now()
      }
      return { ok: true, data }
    },

    /** 放弃这次二次验证，回到登录表单。**不清令牌** —— 此刻本来就没有令牌。 */
    cancelMfa() {
      this.mfa = null
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
        // 令牌已经失效（或 /me 挂了）—— 这是**被清掉**，不是人点的登出。
        // 所以只清本地、不去补一条 LOGOUT 留痕：那条请求注定 401，
        // 就算通了也会把「被动失效」记成「本人登出」，审计上反而是假的。
        this.logout({ remote: false })
      }
    },

    /**
     * 把「改了姓名」这件事写回本地状态。
     *
     * **为什么不能只改 localStorage**：store 里的 ``user`` 与 storage 里那份
     * 是**两份副本**（登录那一刻各写一份）。只改其中一份，顶栏那个名字
     * 要等下一次 ``refreshMe`` 才会变 —— 那是「明明改了，界面没动」。
     *
     * **为什么是「合并」而不是「整份替换」**：接口只回姓名相关的几个字段，
     * 整份替换会把 ``books``（这个人的账套授权）抹掉 —— 而 ``books`` 是
     * 另一条接口（``/books``）拉来的，不属于「个人资料」这一份。合并的
     * 另一半边界是：**只认接口回的那几个字段** —— 别的键（比如别人塞进来的
     * ``books``）一律不认，免得一份「个人资料」回执把「账套授权」覆盖掉。
     */
    applyProfile(data) {
      if (!data) return
      const next = { ...(this.user || {}) }
      for (const k of ['id', 'username', 'real_name', 'is_admin']) {
        if (data[k] !== undefined) next[k] = data[k]
      }
      this.user = next
      localStorage.setItem('cwq_user', JSON.stringify(next))
    },

    /**
     * 登出：**先本地、后远端**。
     *
     *  语义（顺序是刻意的）：
     *   1. 无条件先清本地。断网、服务没起、令牌早就过期 —— 这三种情况下
     *      远端那条留痕请求必然失败，但人确实已经离开这台机器了，
     *      把令牌留在浏览器里才是真事故（下一个用这台机器的人拿着它就是登进去）。
     *      本地清理**同步**完成，不等任何 await，401 拦截器那条路也照样可靠。
     *   2. 再调后端补一条 LOGOUT 审计。失败**不假装没发生**，也**不拦人**：
     *      只在控制台记一句「这次没留上痕」，界面照常回登录页。
     *      为什么这样取舍：本地登出是**人的意图**，一定会发生；留痕是**事后追查用的线索**，
     *      缺一条只是线索断了一处，反过来把它当成登出的前置条件，
     *      断网时就会把人锁在登录页之外、逼他反复重试。
     *
     *  @param {{remote?: boolean}} [opts] remote=false 只清本地（令牌已失效时用）
     */
    async logout(opts = {}) {
      const token = this.token
      this.clearLocal()
      if (opts.remote === false || !token) return
      try {
        await authApi.logout(token)
      } catch (e) {
        console.warn('[session] 登出留痕未上报（本地已登出，不影响登出本身）', e)
      }
    },

    /**
     * 只清本地状态与 localStorage —— 登出的**全部实际语义**。
     * 令牌是自签名的、服务端不存会话，所以「登出」在服务端做不了撤销动作，
     * 丢掉本地的令牌就是全部（后端 logout 的 docstring 写的就是这件事）。
     * 单独拆出来是为了让每一条「被迫清登录态」的路（401、令牌失效）
     * 都能复用同一段清法，不必各写一遍。
     */
    clearLocal() {
      this.token = ''
      this.user = null
      this.books = []
      // 二次验证进行中的状态也是**这个人的账**：上一个用户停在「等邮件」
      // 的时候，下一个人不该接着用他那个挑战。
      this.mfa = null
      // 下一个人的账套列表还没问过 —— 别拿这一份空列表去判「这个人没有账套」
      this.booksLoaded = false
      // 期间跟账套是一对：书留着的账套 id（下次登录还能落回去），
      // 但**期间必须清** —— 上一个用户停在哪一期跟下一个用户无关，
      // 留着就可能让新用户一进来顶着一个不属于他的期间。periods 一并清掉，
      // 免得登出后那半秒里顶栏还渲染着上一个人的月份（401 那条路是同步清的）。
      this.periods = []
      this.periodId = 0
      // 初始化状态跟账套列表一样是**这个人的账**：上一个用户停在哪一步，
      // 跟下一个用户无关。留着就可能让新用户一进来就看见一张「待初始化」的卡片，
      // 而那本账根本不是他的。books 同理（上面已清）。
      this.initByBook = {}
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
      // 问过了 —— 这一句必须紧跟在列表接口后面，不能挪到 loadPeriods 之后：
      // 期间接口失败不该把「账套列表问过了」这个事实也一起抹掉。
      this.booksLoaded = true
      // 列表里的 init 是**刚取回来的**，比 initByBook 里的任何一份都新，
      // 所以要顺手覆盖掉那份缓存。
      //
      // 不覆盖的话会出现一个很难查的现象：卡片是对的、弹窗是错的。
      // 起因是 initOf 让 initByBook 优先于 books[].init —— 第 2 步在
      // **期初余额页**上做完，回账套管理时列表已经刷新（卡片因此摘掉了
      // 「待初始化」），但 initByBook 还停在这个会话早些时候 loadInit 拉到的
      // 那份，点开向导仍然写着「第 2 步还没做」。而 markInit 之所以能被
      // 信任，是因为它写的本来就是服务端刚返回的值，覆盖不会丢信息。
      // 列表里的 init 是**刚取回来的**，比 initByBook 里的任何一份都新 ——
      // 但它只带**当前在记的那一年**的进度（每个年度各有一份）。
      // 所以按年度合并，**不能整份覆盖**：整份覆盖会把别的年度的进度冲掉
      // （用户切到 2026 做完一步，回来一看 2027 的进度被列表抹了）。
      const merged = {}
      for (const b of this.books) {
        const prev = this.initByBook[b.id] || {}
        if (!b.init) {
          if (Object.keys(prev).length) merged[b.id] = prev
          continue
        }
        const y = b.init.year
        merged[b.id] = y != null ? { ...prev, [y]: b.init } : prev
      }
      this.initByBook = merged
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

    /**
     * 拉某个账套的初始化状态。
     *
     *  失败**不抛**：拿不到就继续用账套列表行里那份（可能旧一点，但也不是没有）。
     *  向导此时的取值动作一律 fail-closed（见 utils/bookInit），所以一次网络抖动
     *  不会变成「按钮亮着、点了被 409」。返回 null 表示这次没拿到新的。
     */
    async loadInit(bookId, year) {
      if (!bookId) return null
      try {
        const init = await bookApi.getInit(bookId, year)
        if (init) this.markInit(bookId, init)
        return init || null
      } catch (e) {
        console.warn('[session] 初始化状态拉取失败，沿用账套列表里的那份', e)
        return null
      }
    },

    /**
     * 就地更新某个账套的初始化状态 —— 账套列表里那一行也一起换掉。
     *
     *  必须连 books 那一行一起更新，否则卡片上的「待初始化」标记要等下一次
     *  loadBooks 才消失，用户明明刚做完第 1 步、眼前那张卡片还在催他。
     *  整份替换而不是就地改：books 是 computed 的输入，原地改字段它不会重算。
     *
     *  loadBooks() 不清 initByBook：向导刚刷新的状态比列表新，
     *  一次改名/新建触发的列表刷新不该把「第 1 步已完成」又打回未完成。
     */
    markInit(bookId, init) {
      if (!bookId || !init) return
      // 按年度存：后端返回的 init 自带它属于哪一年
      const year = init.year
      const byYear = { ...(this.initByBook[bookId] || {}) }
      if (year != null) byYear[year] = init
      this.initByBook = { ...this.initByBook, [bookId]: byYear }
      if (this.books.some((b) => b.id === bookId)) {
        this.books = this.books.map((b) => {
          if (b.id !== bookId) return b
          // 列表行装的是**当前在记的那一年**的那份 ——
          // 别的年度的进度不该覆盖它（那会让卡片上的标记说错年份）。
          if (year != null && b.latest_year != null && b.latest_year !== year) return b
          return { ...b, init }
        })
      }
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
