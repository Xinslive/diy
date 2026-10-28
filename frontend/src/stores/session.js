import { defineStore } from 'pinia'
import { effectScope, watch } from 'vue'
import * as authApi from '@/api/auth'
import * as bookApi from '@/api/book'
import { errorText } from '@/utils/errorText'
import queryCache from '@/data/queryCache'

/** 能写的角色。与后端 ROLES（app/api/system.py）保持一致。 */
const WRITABLE_ROLES = ['OWNER', 'ACCOUNTANT']

/** 已经装过「切账套」守卫的 store 实例（一个 store 只装一次）。 */
const bookWatchInstalled = new WeakSet()
const bootstrapPromises = new WeakMap()
const CONTEXT_SNAPSHOT_KEY = 'cwq_context_v1'

function readContextSnapshot(userId) {
  if (!userId) return null
  try {
    const value = JSON.parse(localStorage.getItem(CONTEXT_SNAPSHOT_KEY) || 'null')
    if (!value || String(value.userId) !== String(userId)) return null
    if (!Array.isArray(value.books) || !Array.isArray(value.periods)) return null
    return value
  } catch {
    return null
  }
}

function writeContextSnapshot(store) {
  if (!store.user?.id) return
  localStorage.setItem(CONTEXT_SNAPSHOT_KEY, JSON.stringify({
    version: 1,
    userId: store.user.id,
    savedAt: Date.now(),
    books: store.books,
    bookId: store.bookId,
    periodId: store.periodId,
    periods: store.periods,
  }))
}

function snapshotState() {
  let user = null
  try { user = JSON.parse(localStorage.getItem('cwq_user') || 'null') } catch { /* stale snapshot */ }
  const snapshot = readContextSnapshot(user?.id)
  return { user, snapshot }
}

/**
 * 挑战的形状：**登录**与**重新验证**共用这一个。
 *
 *   { challengeId, clientSecret, expiresIn, emailHint, resendAfter, sentAt }
 *
 * 两处的倒计时算法必须一致，所以**共用一个形状 + 共用两个算法**
 * （§4.5：同一个量只允许一个权威实现）。
 */
function resendWaitOf(m, now = Date.now()) {
  if (!m) return 0
  const after = (m.resendAfter || 60) * 1000
  return Math.max(0, Math.ceil((m.sentAt + after - now) / 1000))
}

function expiresInOf(m, now = Date.now()) {
  if (!m) return 0
  return Math.max(0, Math.ceil((m.sentAt + (m.expiresIn || 0) * 1000 - now) / 1000))
}

/**
 * 「待重新验证」那一次的**接线**。
 *
 *   · ``mfaReauthPromise``：正在等的那一次（两次并发撞闸门时复用同一次）；
 *   · ``mfaReauthSettle``：裁决函数 —— resolve(true) = 拿到了新令牌，
 *     resolve(false) = 取消/失败，**原动作没有被执行**；
 *   · ``mfaReauthTimer``：超时兜底。
 *
 * **放在模块作用域而不是 store.state**：它是**一次性的**接线，
 * 不是可观测的状态。放 state 的话，每次都要判断「这份 resolver 是不是
 * 已经用过了」，而且会被 Pinia 的状态快照/持久化逻辑卷进去。
 */
let mfaReauthPromise = null
let mfaReauthSettle = null
let mfaReauthTimer = null

/**
 * 重新验证的**超时兜底**。没有它，这一次等待可能**永远不结束** ——
 * 而拦截器正**等着**这个结果（原动作就悬在那儿不落地）。
 *
 * 触发它的场景：对话框根本没挂出来（不在那个页面）、管理员开着对话框
 * 走开了、浏览器把标签页挂起导致定时器不跑。
 */
function armMfaReauthTimeout(ms) {
  if (mfaReauthTimer) clearTimeout(mfaReauthTimer)
  mfaReauthTimer = setTimeout(() => {
    mfaReauthTimer = null
    const s = mfaReauthSettle
    mfaReauthSettle = null
    if (s) s(false)
  }, ms)
  // Node（测试）里不 unref 的话，这个定时器会让进程**不退出**。
  // 浏览器里没有 unref，?.() 天然是 no-op。
  mfaReauthTimer?.unref?.()
}

/**
 * 装「切账套」的守卫：**记住切换前的那一本**。
 *
 * 判据是 **watch 记下来的上一代**（oldValue），跟「谁写的 bookId」**无关**。
 *
 *  为什么不能问「bookId 变了没有」：顶栏那行是 ``v-model="store.bookId"``，
 *  v-model 会**先**把新账套写进 store，``@change`` 里再调 setBook 时
 *  bookId 早就是新的了 —— 「变了没有」永远答 false，于是「换账套前的那一本」
 *  永远记不上，凭证页那句「已切换到账套…」**一次也发不出来**（审计 §3.33；
 *  跟上面 periodsBookId 踩的是同一个坑）。
 *
 *  watch 记的是「写之前那一本」：v-model 先写、直接调 setBook、别的页面改写，
 *  都判得对。
 *
 *  **护栏**：只有 ``old > 0 && new > 0 && old !== new`` 才算「切账套」。
 *  登出、账套列表里没有这一本了 —— 那些路会把 bookId 归 0，不是切账套，
 *  不该让凭证页说一句「你从账套 A 切过来了」。
 *
 *  **flush: 'sync'**：切换的那一刻就把「上一本」记下来，不等调度器 ——
 *  凭证页重挂后**立刻**就要用这个记号，不能让它等一个微任务。
 */
function ensureBookSwitchWatch(store) {
  if (!store || bookWatchInstalled.has(store)) return
  bookWatchInstalled.add(store)
  // **装在一个 detached 的 scope 里**：watch 若是装在某个组件的 setup 里，
  // 那个组件一卸载，守卫就跟着没了 —— 而守卫恰恰是「谁在切账套」都离不开的。
  // detached 的 scope 不属于任何组件，活的时长跟这个 store 一样（整个应用）。
  const scope = effectScope(true)
  scope.run(() => {
    watch(
      () => store.bookId,
      (nv, ov) => {
        if (ov > 0 && nv > 0 && ov !== nv) {
          store.prevBookId = ov
          queryCache.remove({ scope: 'book', bookId: ov })
        }
      },
      { flush: 'sync' }
    )
  })
}

const useSessionStoreBase = defineStore('session', {
  state: () => {
    const { user, snapshot } = snapshotState()
    const rememberedBookId = Number(localStorage.getItem('cwq_book') || snapshot?.bookId || 0)
    const rememberedPeriodId = Number(localStorage.getItem('cwq_period') || snapshot?.periodId || 0)
    return {
    token: localStorage.getItem('cwq_token') || '',
    user,
    // 账套与期间只保存元数据快照；凭证、余额、报表和日志明细不会进入这里。
    books: snapshot?.books || [],
    // 账套列表**问过了没有**。
    //
    // 空数组有两种完全不同的含义：「一个账套都没有」和「还没去问」。
    // 路由守卫要据此决定「没有账套就把人送回账套管理」—— 拿「还没问」当成
    // 「没有」，会把有账套的用户在打开深链接的那一瞬间踢回账套管理。
    // 所以这里记一份「问过了」的事实，而不是从 books.length 里反推。
    booksLoaded: Boolean(snapshot),
    contextStatus: snapshot ? 'snapshot' : 'idle',
    bookId: rememberedBookId,
    /**
     * **换账套前的那一本**（0 = 没有）。
     *
     *  为什么记在 store 而不是凭证页的组件里：切账套时 MainLayout 的 viewKey
     * 变了 → 凭证页**整个重挂** → 组件内的变量（包括「上一张是哪个账套的
     * 哪张」那个记号）全没了。而「你原来看的是哪本账的哪张」这句话，
     * 恰恰要**跨过这次重挂**才能说清 —— 记在组件里就永远发不出来
     * （审计 §3.33）。
     *
     *  一次性：提示过就清（见 takePrevBookId）。
     *
     *  **谁来写都不算数**：判据是「watch 记下来的上一代」，不是「谁写的
     *  bookId」（见 ensureBookSwitchWatch 的说明）。
     */
    prevBookId: 0,
    /**
     * **刚建出来的那张新凭证**（null = 没有这回事）。
     *
     *  形状：``{ bookId, id, createKey }`` ——
     *   · bookId：它属于哪本账（换账套后不能拿它去新账套里查）；
     *   · id：落库后服务端给的凭证 id（**还没落库时是 0**）；
     *   · createKey：这一张的幂等键（响应在路上丢了的那一趟靠它去重）。
     *
     *  为什么记在 store 而不是组件里：新建凭证落库后，地址栏换成了
     *  ``/vouchers/{id}``，但 **router 内部还停在 ``/vouchers/new``**
     *  （走 router 会把组件整个重挂，正在填的表单、光标、绿行全没了 ——
     *  见 VoucherEdit.vue 里 save() 那段说明）。于是「这张凭证已经建出来了」
     *  这个事实只活在组件里 → 一重挂就丢 → 重挂后又被当成一张**全新的空凭证**，
     *  下一趟自动保存**又 POST 一张**，库里多出一张几乎一样的（审计 N5）。
     *
     *  一次性：**换页面就清**（MainLayout 的路由 watcher 里跟 prevBookId 一起清）——
     *  用户点「新增凭证」重新开一张时，不该被带回刚才那张。
     */
    createdVoucher: null,
    periods: snapshot?.periods || [],
    // **手上这批期间属于哪个账套。** 0 = 手上没有期间。
    //
    //  为什么非要单独记一份：判断「要不要清掉旧期间」不能看 bookId 变没变。
    //  顶栏那行是 ``v-model="store.bookId"``，而 v-model 会**先**把新账套
    //  写进 store，@change 里再调 setBook 时 bookId 早就是新的了 ——
    //  「变了没有」永远答 false，旧账套的期间就一直留着。拿它当判据，
    //  等于在问一个已经被回答过的问题。
    //
    //  真正该问的是「手上这批期间是谁的」。这个判据跟「谁来写 bookId」无关，
    //  所以 v-model 先写、还是直接调 setBook，都判得对。
    periodsBookId: snapshot?.bookId || 0,
    periodId: rememberedPeriodId,
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
    mfa: null,
    /**
     * 高危动作撞到「近期二次验证」闸门时的**待办**（null = 没有这回事）。
     *
     * 形状：``{ phase, challenge, emailHint, error }``
     *   · phase：``'idle'``（还没发信）→ ``'sent'``（已发信，等点链接）
     *     → ``'verifying'``（正在轮询）→ ``'failed'``（出了岔子，可重试）；
     *   · challenge：上面那个挑战形状；
     *   · emailHint：收件地址的掩码（``o***s@example.com``）。
     *
     * **为什么单独记一份而不是复用 ``mfa``**：``mfa`` 是**登录**阶段的挑战
     * （那时还没有令牌），这里是有令牌之后为**高危动作**重新发起的挑战。
     * 两件事不会同时发生，但**收尾方式不同**（登录成功要拉账套列表，
     * 重新验证不要），混在一份里迟早会串。
     */
    mfaReauth: null
    }
  },
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
    /**
     * 当前账套的期间**到位了没有**。
     *
     *  切账套的**那一瞬间**手上可能什么都没有：旧的刚被清掉，新的还在路上。
     *  这时候拿着 periodId 去查，就是拿着上一个账套的期间 id 查新账套 ——
     *  后端会回「会计期间不存在」（总账）或「起始期间不存在」（凭证列表）。
     *
     *  判据是「手上的期间是不是当前账套的」，不是「期间数组空不空」：
     *  账套一个期间都没有是可能的（刚建好还没启用月份），
     *  那种情况下期间**到位了**，只是空的 —— 拿「空不空」当判据会把
     *  这种账套的页面永远卡在「不查」。
     */
    periodsReady: (s) => s.bookId > 0 && s.periodsBookId === s.bookId,
    contextReady: (s) => s.contextStatus === 'ready',
    contextRefreshing: (s) => s.contextStatus === 'refreshing' || s.contextStatus === 'snapshot',
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
    mfaResendWait: (s) => (now = Date.now()) => resendWaitOf(s.mfa, now),
    /** 验证链接还剩几秒过期。 */
    mfaExpiresIn: (s) => (now = Date.now()) => expiresInOf(s.mfa, now),
    /** 同上两条，**重新验证**那一份挑战的（形状一样，算法共用）。 */
    mfaReauthResendWait: (s) => (now = Date.now()) =>
      resendWaitOf(s.mfaReauth?.challenge, now),
    mfaReauthExpiresIn: (s) => (now = Date.now()) =>
      expiresInOf(s.mfaReauth?.challenge, now)
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
      this.applyToken(data)
      await this.bootstrap()
    },

    /**
     * **只**更新令牌与用户，不碰别的。
     *
     * 「登录」「二次验证换令牌」「高危动作重新验证换令牌」三处都要写令牌，
     * 但**只有登录那两处**需要顺带拉一次账套列表 —— 重新验证发生在一次
     * 请求的**中途**（拦截器里），那时候去拉账套列表既没必要，
     * 又可能因为网络抖动把整条链路拖失败。
     *
     * @param {{access_token: string, user?: object}} data
     */
    applyToken(data) {
      if (!data?.access_token) return
      if (data.user?.id != null && this.user?.id != null
          && String(data.user.id) !== String(this.user.id)) {
        queryCache.clear()
        localStorage.removeItem(CONTEXT_SNAPSHOT_KEY)
      }
      this.token = data.access_token
      localStorage.setItem('cwq_token', data.access_token)
      // **user 缺省就别动它**：换了令牌不等于换了人，而把 undefined
      // 写进 localStorage 会让下次启动读到 "undefined"（JSON.parse 之后
      // 是 null，登录态看着像没了）。
      if (data.user) {
        this.user = data.user
        localStorage.setItem('cwq_user', JSON.stringify(data.user))
      }
    },

    /**
     * 轮询一次挑战。**登录与重新验证共用这一份**（§4.5：同一个量只允许
     * 一个权威实现 —— 两处的「怎么算验证通过」必须一模一样）。
     *
     * 验证通过时**只更新令牌**（``applyToken``），不碰账套列表。
     *
     * @param {{challengeId: number, clientSecret: string}|null} challenge
     * @returns {Promise<{status: 'VERIFIED'|'PENDING'|'IDLE'}>}
     */
    async pollChallenge(challenge) {
      if (!challenge) return { status: 'IDLE' }
      const data = await authApi.mfaPoll(challenge.challengeId, challenge.clientSecret)
      if (data && data.access_token) {
        this.applyToken({ access_token: data.access_token, user: data.user })
        return { status: 'VERIFIED' }
      }
      return { status: (data && data.status) || 'PENDING' }
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
      const r = await this.pollChallenge(m)
      if (r.status === 'VERIFIED') {
        this.mfa = null
        // 登录这条路上令牌是**刚拿到的**，账套列表得跟着拉一次 ——
        // 顶栏的账套下拉就是靠它填上的。
        await this.bootstrap()
      }
      return r
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

    // ================================================================
    // 高危动作的「近期二次验证」闸门（审计 §3.2）
    //
    // 闸门扩到 13 条受管控写路由之后，管理员在令牌超过宽限期时碰这些动作，
    // 会收到一条 403。前端**必须有下文**：不然撞死路，而且没有任何提示
    // 告诉他该做什么（这是本轮修复前真实存在的缺口）。
    //
    // 这里的动作被 api/client.js 的响应拦截器调用：
    //   requestMfaReauth() → （等管理员走完）→ true = 可以重试原动作。
    // ================================================================

    /**
     * 高危动作撞到闸门时调用：**打开引导，等管理员走完（或取消）**。
     *
     * 返回 ``true`` = 拿到了带新鲜验证时刻的新令牌，调用方可以重试原动作；
     * 返回 ``false`` = 取消/失败，**原动作没有被执行**。
     *
     * **两次并发撞闸门共用同一次**：两个请求同时被 403 拦下时，
     * 只该有一封信、一个对话框 —— 否则管理员要验两次，而且第二封邮件
     * 会把第一封作废（重发会换新挑战）。所以这里复用同一个 promise。
     */
    async requestMfaReauth() {
      if (mfaReauthPromise) return mfaReauthPromise
      this.mfaReauth = { phase: 'idle', challenge: null, emailHint: '', error: '' }
      // 还没发信之前也得上保险：管理员可能根本不开那个对话框。
      armMfaReauthTimeout(10 * 60 * 1000)
      mfaReauthPromise = new Promise((resolve) => { mfaReauthSettle = resolve })
      try {
        return await mfaReauthPromise
      } finally {
        mfaReauthPromise = null
        mfaReauthSettle = null
        if (mfaReauthTimer) { clearTimeout(mfaReauthTimer); mfaReauthTimer = null }
        this.mfaReauth = null
      }
    },

    /**
     * 发起一次「重新验证」：发信，并把挑战记下来。
     *
     * 服务端换的是**新挑战**（新的 challenge_id 与 client_secret），
     * 所以旧的那对立刻作废 —— 这里记的是返回值，不是发信前的那份。
     */
    async startMfaReauth() {
      const data = await authApi.mfaReauth()
      const challenge = {
        challengeId: data.challenge_id,
        clientSecret: data.client_secret,
        expiresIn: data.expires_in,
        emailHint: data.email_hint || '',
        resendAfter: data.resend_after || 60,
        sentAt: Date.now()
      }
      this.mfaReauth = {
        ...(this.mfaReauth || {}),
        phase: 'sent',
        challenge,
        emailHint: challenge.emailHint,
        error: ''
      }
      // 超时按挑战的寿命算：链接过期之后再怎么等也不会通过。
      armMfaReauthTimeout((challenge.expiresIn + 30) * 1000)
      return challenge
    },

    /**
     * 重新发送验证邮件（链接丢了 / 没收到）。
     *
     * **最短间隔本地也拦一道**：服务端会拦（429 + Retry-After），
     * 但让人一次次点一个注定失败的按钮，只是在制造挫败感 ——
     * 收件箱就是通知通道本身，刷爆它等于把告警通道一起刷爆。
     */
    async resendMfaReauth() {
      const c = this.mfaReauth?.challenge
      if (!c) return { ok: false, reason: 'no_challenge' }
      const wait = this.mfaReauthResendWait()
      if (wait > 0) return { ok: false, reason: 'too_soon', wait }
      const data = await authApi.mfaResend(c.challengeId, c.clientSecret)
      const challenge = {
        challengeId: data.challenge_id,
        clientSecret: data.client_secret,
        expiresIn: data.expires_in,
        emailHint: data.email_hint || c.emailHint,
        resendAfter: data.resend_after || 60,
        sentAt: Date.now()
      }
      this.mfaReauth = { ...this.mfaReauth, phase: 'sent', challenge, emailHint: challenge.emailHint }
      return { ok: true, data }
    },

    /**
     * 轮询一次「重新验证」的挑战。
     *
     * 验证通过时**只换令牌**（``pollChallenge`` 里的 applyToken）——
     * 账套列表不动：这个流程发生在一次请求的中途。
     *
     * **失败分两类**（决定「还能不能继续等」）：
     *   · **4xx**（链接过期、挑战作废）—— 这条路走不通了，得重新发一封信；
     *   · **网络类 / 429**（轮询太频繁）—— 下一轮还会再试，别把界面卡死。
     */
    async pollMfaReauth() {
      const c = this.mfaReauth?.challenge
      if (!c) return { status: 'IDLE' }
      this.mfaReauth = { ...this.mfaReauth, phase: 'verifying' }
      try {
        const r = await this.pollChallenge(c)
        if (r.status !== 'VERIFIED') {
          // 还没点链接是**正常状态**，不是错误 —— 别把 phase 改成 failed。
          this.mfaReauth = { ...this.mfaReauth, phase: 'sent' }
        }
        return r
      } catch (e) {
        const status = e?.response?.status
        const fatal = status >= 400 && status < 500 && status !== 429
        this.mfaReauth = {
          ...this.mfaReauth,
          phase: fatal ? 'failed' : 'sent',
          error: fatal ? errorText(e, '验证失败，请重新发送验证邮件') : ''
        }
        return { status: fatal ? 'FAILED' : 'PENDING', error: this.mfaReauth.error }
      }
    },

    /**
     * 验证通过：把裁决交给调用方（拦截器会去重试原动作）。
     */
    finishMfaReauth() {
      const s = mfaReauthSettle
      mfaReauthSettle = null
      if (s) s(true)
    },

    /**
     * 管理员取消（或流程没走通）：**原动作没有被执行**。
     *
     * 这里的 ``false`` 很重要 —— 调用方据此**不重试**原动作。
     * 把「取消」当成「通过」去重试，等于把一个管理员明确拒绝的动作
     * 又做了一遍。
     */
    cancelMfaReauth() {
      this.mfaReauth = null
      const s = mfaReauthSettle
      mfaReauthSettle = null
      if (s) s(false)
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
     * 一次请求取得用户、账套和当前期间。快照已经存在时页面可以先用快照挂载，
     * 这里只负责在后台把权威结果写回；同一个 store 的并发调用共享 promise。
     */
    async bootstrap({ force = false } = {}) {
      if (!this.token) return null
      const previous = bootstrapPromises.get(this)
      if (previous && !force) return previous
      if (previous && force) return previous

      this.contextStatus = this.books.length ? 'refreshing' : 'loading'
      const promise = (async () => {
        try {
          const data = await authApi.bootstrap(this.bookId, this.periodId)
          if (!data || !Array.isArray(data.books) || !Array.isArray(data.periods)) {
            await this.loadBooks()
            this.contextStatus = 'ready'
            return { books: this.books, periods: this.periods, user: this.user }
          }
          this.applyBootstrap(data)
          return data
        } catch (error) {
          // 前后端短暂不同步时沿用旧的两个接口，避免新前端阻断登录。
          const status = error?.response?.status
          if (status === 404 || status === 405) {
            await this.loadBooks()
            this.contextStatus = 'ready'
            return { books: this.books, periods: this.periods, user: this.user }
          }
          this.contextStatus = this.books.length ? 'snapshot-error' : 'error'
          throw error
        } finally {
          bootstrapPromises.delete(this)
        }
      })()
      bootstrapPromises.set(this, promise)
      return promise
    },

    /** 将 bootstrap 的权威结果写入 Pinia，并更新仅包含上下文元数据的快照。 */
    applyBootstrap(data = {}) {
      ensureBookSwitchWatch(this)
      if (data.user) {
        this.user = data.user
        localStorage.setItem('cwq_user', JSON.stringify(data.user))
      }
      this.books = Array.isArray(data.books) ? data.books : []
      this.booksLoaded = true
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
      const wantedBook = Number(data.selected_book_id || this.bookId || 0)
      const nextBook = this.books.find((book) => book.id === wantedBook) || this.books[0]
      this.bookId = nextBook?.id || 0
      this.periods = nextBook && Array.isArray(data.periods) ? data.periods : []
      this.periodsBookId = nextBook?.id || 0
      const requestedPeriod = Number(data.selected_period_id || this.periodId || 0)
      const selected = this.periods.find((period) => period.id === requestedPeriod)
        || this.periods.find((period) => !period.locked)
        || this.periods[this.periods.length - 1]
      this.periodId = selected?.id || 0
      if (this.bookId) localStorage.setItem('cwq_book', String(this.bookId))
      else localStorage.removeItem('cwq_book')
      if (this.periodId) localStorage.setItem('cwq_period', String(this.periodId))
      else localStorage.removeItem('cwq_period')
      this.contextStatus = 'ready'
      writeContextSnapshot(this)
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
      this.contextStatus = 'idle'
      // **正在等的高危动作也要就地结束**（裁决为「没通过」）。
      // 不结束的话：拦截器正等着这次重新验证的结果，那个被拦下的请求
      // 会一直悬着（直到超时兜底才作罢）—— 而人已经登出了，悬着它毫无意义。
      // 裁决成 false（不是 true）：原动作**没有**执行，不该被重试。
      if (mfaReauthSettle) {
        const s = mfaReauthSettle
        mfaReauthSettle = null
        s(false)
      }
      this.mfaReauth = null
      // 期间跟账套是一对：书留着的账套 id（下次登录还能落回去），
      // 但**期间必须清** —— 上一个用户停在哪一期跟下一个用户无关，
      // 留着就可能让新用户一进来顶着一个不属于他的期间。periods 一并清掉，
      // 免得登出后那半秒里顶栏还渲染着上一个人的月份（401 那条路是同步清的）。
      this.periods = []
      this.periodId = 0
      this.periodsBookId = 0
      // 初始化状态跟账套列表一样是**这个人的账**：上一个用户停在哪一步，
      // 跟下一个用户无关。留着就可能让新用户一进来就看见一张「待初始化」的卡片，
      // 而那本账根本不是他的。books 同理（上面已清）。
      this.initByBook = {}
      // 「刚建出来的那张新凭证」也是**这个人的账**：上一个用户刚存的那张，
      // 跟下一个用户无关。留着的话，下一个用户新开一张凭证就可能被
      // 认成「刚才那张」（审计 N5）。
      this.createdVoucher = null
      localStorage.removeItem('cwq_token')
      localStorage.removeItem('cwq_user')
      localStorage.removeItem('cwq_book')
      localStorage.removeItem('cwq_period')
      this.lastCreatedBookId = 0
      localStorage.removeItem(CONTEXT_SNAPSHOT_KEY)
      queryCache.clear()
    },
    /** 拉账套列表，并保证当前账套/期间一定能落到一个有效值上。
     *
     *  这里必须**无条件**跟着 loadPeriods：以前只在「记住的账套 id 失效」时才
     *  走 setBook，于是「localStorage 里的账套还在、期间数组却是空的」这种状态
     *  没人收拾 —— 表现就是顶栏期间下拉点开只有「无数据」。 */
    async loadBooks() {
      // 守卫装上：顶栏的账套下拉里的选项就是这一份列表装的，
      // 所以「用户第一次能切账套」一定晚于这一刻（见 ensureBookSwitchWatch）。
      ensureBookSwitchWatch(this)
      this.books = await bookApi.listBooks({ cache: false })
      // 问过了 —— 这一句必须紧跟在列表接口后面，不能挪到 loadPeriods 之后：
      // 期间接口失败不该把「账套列表问过了」这个事实也一起抹掉。
      this.booksLoaded = true
      this.contextStatus = 'ready'
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
        // 记住的账套没了（被删了，或者换人了）：它那批期间也是**别的账**的，
        // 留着就是「拿着上一个账套的期间查下一个账套」。一并清掉。
        this.periods = []
        this.periodsBookId = 0
        localStorage.removeItem('cwq_period')
      }
      if (!this.bookId) this.bookId = this.books[0]?.id || 0
      if (this.bookId) localStorage.setItem('cwq_book', String(this.bookId))
      await this.loadPeriods()
      writeContextSnapshot(this)
    },

    /**
     * 取走「换账套前的那一本」，并清掉。
     *
     * 一次性：提示只该出现一次；不清的话，下一次进凭证页还会再提一遍
     * 上一本账（「你原来看的是哪本账的哪张」变成一句莫名其妙的话）。
     */
    takePrevBookId() {
      const prev = this.prevBookId
      this.prevBookId = 0
      return prev
    },

    /** 换账套：默认进最新年度。 */
    async setBook(id) {
      // 「换账套前的那一本」**不再在这里判断** ——
      // 顶栏是 ``v-model="store.bookId"``：v-model 先把新账套写进 store，
      // @change 里再进来时 bookId 早就是新的了，「变了没有」永远答 false，
      // 于是那个记号一次也记不上（审计 §3.33）。
      // 现在由 watch 记「写之前那一本」（见 ensureBookSwitchWatch）：
      // 判据跟「谁写的 bookId」无关。
      // 这里只保证守卫装上了（万一 setBook 是第一次碰这个 store）。
      ensureBookSwitchWatch(this)
      this.bookId = id
      localStorage.setItem('cwq_book', String(id))
      // 「要不要清掉旧期间」问的是**手上的期间属于哪个账套**，不是 bookId 变没变。
      // 顶栏那行是 v-model="store.bookId"：v-model 会先把新账套写进 store，
      // @change 里再进来时 bookId 早就是新的了 —— 拿「变了没有」当判据，
      // 这里永远进不来，旧期间就一直留着。页面拿着上一个账套的期间 id
      // 去查新账套，后端回「会计期间不存在」。
      //
      // 换句话说：以前问的「bookId 变了吗」是个已经被回答过的问题，
      // 该问的是「手上这批期间是谁的」。
      if (this.periodsBookId !== id) {
        // 换账套后旧的 periodId 一定不属于新账套，先清掉避免期间下拉闪旧值。
        // 进账套一律按「最早还在用的年度 + 最早未结账月」落位，不接着上次的账套看。
        this.periods = []
        this.periodId = 0
        this.periodsBookId = 0
        localStorage.removeItem('cwq_period')
      }
      await this.loadPeriods()
    },
    async loadPeriods() {
      const bookId = this.bookId
      if (!bookId) {
        this.periods = []
        this.periodId = 0
        this.periodsBookId = 0
        return
      }
      const periods = await bookApi.listPeriods(bookId, { cache: false })
      // 期间是**账套的属性**：请求在路上时用户可能又切了账套。
      // 旧账套的那份这时候回来，直接盖上去就是「拿着 A 的期间查 B」。
      // 所以只在账套还是那个账套时才落。
      if (this.bookId !== bookId) return
      this.periods = periods
      this.periodsBookId = bookId
      if (!this.periods.find((p) => p.id === this.periodId)) {
        // 记住的期间已经不存在（换年度 / 反结账）—— 落在「还在用的」那一期
        this.setPeriod(this.defaultPeriodId(this.defaultYear()))
      }
      writeContextSnapshot(this)
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
      writeContextSnapshot(this)
    },
    periodCodeOf(id) {
      return this.periods.find((p) => p.id === id)?.code || ''
    }
  }
})

/**
 * 取 store —— **顺带保证「切账套」的守卫装上了**。
 *
 * 为什么包一层：那个守卫要赶在**第一次切账套之前**装好（顶栏的 v-model 会
 * 先把新账套写进 store，守卫装晚了就看不见那一次切换，见 ensureBookSwitchWatch）。
 * 而「第一次切账套之前」这个时刻，代码里没有哪个 action 一定跑过 ——
 * 装在 action 里就等于赌「登录时一定调过 loadBooks」。
 * 所以改成：**谁取 store，谁就装上**。取 store 是这个应用里最频繁的一件事，
 * 守卫装上去的时机一定早于用户点顶栏。
 */
export function useSessionStore(...args) {
  const store = useSessionStoreBase(...args)
  ensureBookSwitchWatch(store)
  return store
}
useSessionStore.$id = useSessionStoreBase.$id
