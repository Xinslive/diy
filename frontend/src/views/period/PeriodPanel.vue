<template>
  <div class="page">
    <div class="headline">
      <div class="headline-main">
        <span class="eyebrow">当前期间</span>
        <span class="headline-code">{{ store.currentPeriod?.code || '—' }}</span>
        <el-tag v-if="store.currentPeriod?.locked" type="success" effect="light" size="small">已结账</el-tag>
        <el-tag v-else type="warning" effect="light" size="small">未结账</el-tag>
      </div>
    </div>

    <!-- 四个操作：平级的四块，不编号。
         它们不是一条必须按顺序走的流程，编号会让人以为漏了一步就做错了。
         每块只说两件事：我现在是什么状态，以及我能点什么。 -->
    <div class="ops">
      <!-- 结转损益 -->
      <div class="tile">
        <div class="tile-head">
          <span class="tile-icon blue"><el-icon><Switch /></el-icon></span>
          <span class="tile-title">结转损益</span>
        </div>
        <div class="tile-status" :class="prevBlocked ? 'warn' : (carryInfo.has ? 'ok' : 'idle')">
          <template v-if="prevBlocked">
            <el-icon><Warning /></el-icon>
            <span>{{ prevPeriod.code }} 未结账</span>
          </template>
          <template v-else-if="carryInfo.has">
            <el-icon><CircleCheck /></el-icon>
            <span>已结转 · {{ carryInfo.vouchers.join('、') }}</span>
          </template>
          <template v-else>
            <span class="plain">本期尚未结转</span>
          </template>
        </div>
        <p class="tile-desc">损益类科目本期发生额 → 本年利润。</p>
        <!-- 本年利润是结转的**目的科目**。系统按编码自动识别，但每本账的科目表
             是各人自己的 —— 识别不到时账就结不了转，而这一版之前界面上
             没有任何地方能改。

             **它就是一个字段，不是「设置功能」**：值本身就是控件，
             选中即生效。所以没有「设置」按钮，也没有弹窗 ——
             要改的人一眼看到的就是能改的那一格。 -->
        <div class="tile-profit" :class="{ warn: profitMissing }">
          <span class="tile-profit-label">本年利润</span>
          <el-select v-model="profitFormCode" class="tile-profit-select" size="small"
                     filterable :loading="profitLoading" :disabled="!store.canWrite"
                     popper-class="profit-subject-popper" @change="onProfitChange">
            <el-option v-for="o in profitOptions" :key="o.key" :value="o.code" :label="o.name" />
          </el-select>
        </div>
        <el-button class="tile-btn" type="primary" :loading="carrying"
                   :disabled="!store.canWrite || store.currentPeriod?.locked || prevBlocked || profitMissing"
                   @click="doCarry">
          {{ prevBlocked ? '先结上一月'
             : (profitMissing ? '先设本年利润科目'
                : (carryInfo.has ? '重新结转' : '执行结转')) }}
        </el-button>
      </div>

      <!-- 结账 -->
      <div class="tile">
        <div class="tile-head">
          <span class="tile-icon green"><el-icon><Lock /></el-icon></span>
          <span class="tile-title">结账</span>
        </div>
        <div class="tile-status" :class="store.currentPeriod?.locked ? 'ok' : (emptyPeriod ? 'warn' : 'idle')">
          <template v-if="store.currentPeriod?.locked">
            <el-icon><CircleCheck /></el-icon><span>本期已结账</span>
          </template>
          <template v-else-if="emptyPeriod">
            <el-icon><Warning /></el-icon><span>本期没有任何凭证</span>
          </template>
          <template v-else><span class="plain">本期未结账</span></template>
        </div>
        <p class="tile-desc">
          {{ emptyPeriod ? '空月份不能结账：结账后补凭证只能反结账重来，而反结账会连带把后面已结账的月份全部退回。请先录入本期凭证。'
                          : '锁定本期，之后凭证不可再改。' }}
        </p>
        <el-button class="tile-btn" type="success"
                   :disabled="!store.canWrite || store.currentPeriod?.locked || emptyPeriod"
                   @click="doClose">
          {{ store.currentPeriod?.locked ? '已结账'
             : (emptyPeriod ? '无凭证，不能结账' : `结账 ${store.currentPeriod?.code}`) }}
        </el-button>
      </div>

      <!-- 批量反结账 -->
      <div class="tile">
        <div class="tile-head">
          <span class="tile-icon red"><el-icon><Unlock /></el-icon></span>
          <span class="tile-title">批量反结账</span>
        </div>
        <div class="tile-status" :class="uncloseCount ? 'warn' : 'idle'">
          <template v-if="uncloseCount">
            <el-icon><Warning /></el-icon>
            <span>可解锁 {{ uncloseCount }} 个期间</span>
          </template>
          <template v-else><span class="plain">本期及之后没有已结账期间</span></template>
        </div>
        <p class="tile-desc">从本期起连续解锁到最后一个已结账期间。</p>
        <el-button class="tile-btn" type="danger" plain :disabled="!store.canWrite || !uncloseCount"
                   @click="previewUnclose">
          查看影响范围
        </el-button>
      </div>

      <!-- 年度结转 -->
      <div class="tile">
        <div class="tile-head">
          <span class="tile-icon purple"><el-icon><Calendar /></el-icon></span>
          <span class="tile-title">年度结转</span>
        </div>
        <div class="tile-status" :class="yearComplete ? 'ok' : 'warn'">
          <template v-if="yearComplete">
            <el-icon><CircleCheck /></el-icon><span>本年已全部结账，可结转</span>
          </template>
          <template v-else><span class="plain">还剩 {{ yearPeriods.length - closedCount }} 个月未结账</span></template>
        </div>
        <p class="tile-desc">
          把 {{ store.currentYear }} 年期末余额带成 {{ store.currentYear + 1 }} 年 1 月的期初。
        </p>
        <el-button class="tile-btn" :loading="yearEnding" :disabled="!store.canWrite" @click="doYearEnd">
          结转到 {{ store.currentYear + 1 }} 年
        </el-button>
      </div>
    </div>

    <!-- 整年进度：12 个月铺满一屏，账结到哪儿一眼看完 -->
    <div class="card year-card">
      <div class="card-head">
        <span class="title">{{ store.currentYear }} 年结账进度</span>
        <span class="hint">{{ closedCount }} / {{ yearPeriods.length }} 个月已结账</span>
      </div>

      <div class="months">
        <button v-for="p in yearPeriods" :key="p.id" type="button"
                class="month" :class="monthClass(p)"
                @click="store.setPeriod(p.id)">
          <span class="m-code">{{ p.code }}</span>
          <span class="m-stat">{{ p.vouchers ? `${p.vouchers} 张` : '无凭证' }}</span>
          <span class="m-bar" aria-hidden="true">
            <span class="m-bar-fill" :style="{ width: p.locked ? '100%' : (p.vouchers ? '45%' : '0%') }" />
          </span>
        </button>
      </div>

      <div class="legend muted">
        <span><i class="dot ok" />已结账</span>
        <span><i class="dot warn" />未结账</span>
        <span><i class="dot idle" />无凭证</span>
        <span><i class="dot cur" />当前所在</span>
      </div>
    </div>

    <ConfirmDialog v-model="dlgShown" v-bind="dlg" @confirm="resolveAsk(true)" @cancel="resolveAsk(false)" />
  </div>
</template>

<script setup>
import { computed, ref, watch, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import * as api from '@/api/book'
import * as subjectApi from '@/api/subject'
import { voucherPeriodCheck } from '@/api/voucher'
import { useSessionStore } from '@/stores/session'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import { Calendar, CircleCheck, Lock, Switch, Unlock, Warning } from '@element-plus/icons-vue'

const store = useSessionStore()
const carryInfo = ref({ has: false, vouchers: [], posted: false })
const carrying = ref(false)
const yearEnding = ref(false)

// 期末结账只管当前年度：一次结一个年度，看别的年度先切顶栏
const yearPeriods = computed(() => store.periodsOfYear(store.currentYear))
const closedCount = computed(() => yearPeriods.value.filter((p) => p.locked).length)
const yearComplete = computed(
  () => yearPeriods.value.length > 0 && closedCount.value === yearPeriods.value.length
)
// 本期及之后还有多少个已结账期间 —— 决定「批量反结账」有没有得解、影响多大
const uncloseCount = computed(() => {
  const cur = store.currentPeriod
  if (!cur) return 0
  return store.periods.filter((p) => p.ord >= cur.ord && p.locked).length
})

// 结转损益的硬前置：上一个月没结账，本月就不能结转。
// 后端已经拦了，这里再拦一道是为了「点之前就看得出来」——
// 结转走本年累计口径，顺序错了会把上月的钱扫进本月，单看本月凭证发现不了。
const prevPeriod = computed(() => {
  const cur = store.currentPeriod
  if (!cur) return null
  return store.periods.find(p => p.ord === cur.ord - 1) || null
})
const prevBlocked = computed(() => !!prevPeriod.value && !prevPeriod.value.locked)

/**
 * 本期一张凭证都没有。
 *
 * 空月份**不能结账**，按钮直接禁用并说明原因，不给「确认一下就放行」的旁路：
 * 点了确定的人多半正是没意识到自己漏录了账的那个人，而后端一旦封了这一期，
 * 补录就只能反结账重来，反结账会连带把后面已结账的月份全部退回。
 */
const emptyPeriod = computed(() => {
  const cur = store.currentPeriod
  return !!cur && !cur.locked && !cur.vouchers
})

/** 月份小块的配色：一眼分清「已收尾」「有凭证没收尾」「本月空着」。 */
function monthClass(p) {
  return {
    locked: p.locked,
    pending: !p.locked && p.vouchers > 0,
    idle: !p.locked && !p.vouchers,
    current: p.id === store.periodId
  }
}

/* ---- 本年利润科目 ---- */
/**
 * 结转损益的**目的科目**：损益类科目的发生额冲到这里。
 *
 * 系统按候选编码（3103 / 4103）自动识别，但每本账的科目表是各人自己的 ——
 * 编码改成 3111 的有、干脆叫「本年损益」的也有。识别不到时账就结不了转，
 * 而**界面上原本没有任何地方能改**（这一版之前确实没有）。
 *
 * 这里摆出来的是三件不同的事，别混成一句：
 *   · ``subject``  —— 实际会冲到的那个科目（自动识别或手工设定）
 *   · ``stale``    —— 手工设定指向的科目已经不在这一年的科目表里
 *     （整表换过一次）。这时实际用的是自动识别到的另一个，而设定值还留着，
 *     由用户决定要不要改回去 —— 系统不替他清。
 *   · ``missing``  —— 两级都没有，确实找不到本年利润科目
 */
const profit = ref(null)
const profitAsked = ref(false)
const profitLoading = ref(false)
/** 这一年的**末级科目**（凭证只记在末级上，所以只给末级）。 */
const profitSubjects = ref([])

/** 确实找不到本年利润科目（**不是**「还没问过」）。 */
const profitMissing = computed(() => profitAsked.value && !!profit.value && !profit.value.subject)

/**
 * 下拉里「自动识别」这一项的值。
 *
 * **不能拿空串当它** —— el-select 的值是空串时走的是「没选」那条路，
 * 界面上摆出来的是占位文字，而不是那一项自己。
 * 所以用一个不会撞上科目编码的哨兵值。
 */
const AUTO = '__auto__'

/** 下拉当前选中的值：``AUTO`` = 自动识别，否则是科目编码。 */
const profitFormCode = ref(AUTO)

/**
 * 下拉里的选项。
 *
 * **第一项是「自动识别」**（value 为空串）：它是正当的一个选项，
 * 不是「没填」—— 选它就回到系统的自动口径。
 * 后面跟这一年的末级科目，选中即手工指定。
 *
 * 「设定值已经失效」的情形（那张科目表被整表换过）**照样把那一项列出来**：
 * 藏起来的话，用户看到的是「我明明设过，怎么没了」，而实际上
 * 系统已经悄悄改用自动识别到的另一个科目了 —— 那才是真的没了。
 */
const profitOptions = computed(() => {
  const p = profit.value
  const head = profitMissing.value
    ? { key: 'auto', code: AUTO, name: '未找到本年利润科目' }
    : {
        key: 'auto',
        code: AUTO,
        name: p?.subject ? `自动识别（${p.subject.code}）` : '自动识别',
      }
  const rows = profitSubjects.value.map((s) => ({
    key: s.code, code: s.code, name: `${s.code} ${s.name}`,
  }))
  const setting = p?.setting_code
  if (setting && !rows.some((r) => r.code === setting)) {
    rows.unshift({ key: setting, code: setting, name: `${setting}（科目表里没有）` })
  }
  return [head, ...rows]
})


async function loadProfit() {
  const cur = store.currentPeriod
  if (!cur) {
    profit.value = null
    profitAsked.value = true
    return
  }
  const bookId = store.bookId
  const year = cur.year
  try {
    const r = await api.getProfitSubject(bookId, year)
    // 切账套/切年份时旧请求可能后到，迟到的响应会盖掉新状态
    if (store.bookId !== bookId || store.currentPeriod?.year !== year) return
    profit.value = r
    // 下拉当前值 = 手工设定值（没有设定就是自动识别）
    profitFormCode.value = r.setting_code || AUTO
    loadProfitSubjects(year)
  } catch {
    if (store.bookId !== bookId || store.currentPeriod?.year !== year) return
    // 问过了但没问到：状态按「未知」处理，**不**当成「没有本年利润科目」——
    // 把网络故障说成「你这本账缺科目」，用户会去查一个不存在的问题。
    profit.value = null
  } finally {
    if (store.bookId === bookId && store.currentPeriod?.year === year) profitAsked.value = true
  }
}

/**
 * 选定即生效。
 *
 * **不用「保存」按钮**：这个值不是表单里的一项待提交的输入，
 * 它就是「结转冲到哪里」这个事实本身 —— 选完就该生效，
 * 多按一次保存只会让人怀疑刚才那一下到底算不算数。
 *
 * 失败时**把下拉拨回原值**：界面上留着一个没存上的选择，
 * 用户会以为已经改了（然后结转时冲错了科目）。
 */
async function onProfitChange(code) {
  const cur = store.currentPeriod
  if (!cur) return
  const before = profit.value?.setting_code || ''
  try {
    const r = await api.setProfitSubject(store.bookId, cur.year, code === AUTO ? null : code)
    profit.value = r
    profitAsked.value = true
    ElMessage.success(r.source === 'setting'
      ? `本年利润科目已设为 ${r.setting_code}`
      : '本年利润科目回到自动识别')
  } catch (e) {
    profitFormCode.value = before || AUTO
    ElMessage.error(e?.response?.data?.detail || '设定失败')
  }
}

/** 拉这一年的末级科目，供下拉选。 */
async function loadProfitSubjects(year) {
  profitLoading.value = true
  try {
    const rows = await subjectApi.subjectFlat(store.bookId, { year })
    if (store.currentPeriod?.year !== year) return
    profitSubjects.value = rows || []
  } catch {
    if (store.currentPeriod?.year !== year) return
    profitSubjects.value = []
  } finally {
    if (store.currentPeriod?.year === year) profitLoading.value = false
  }
}

/* ---- 确认弹窗 ---- */
// 一个 Promise 化的壳：调用方写 `if (!await ask({...})) return`，
// 取消走 resolve(false)，不再有「取消 = 未捕获异常」的老毛病。
const dlgShown = ref(false)
const dlg = ref({ title: '', lead: '', chips: [], confirmText: '确定', tone: 'primary', icon: CircleCheck })
let askResolve = null

function ask(opts) {
  dlg.value = {
    title: '', lead: '', chips: [],
    confirmText: '确定', tone: 'primary', icon: CircleCheck,
    ...opts
  }
  dlgShown.value = true
  return new Promise((resolve) => { askResolve = resolve })
}

function resolveAsk(ok) {
  dlgShown.value = false
  const r = askResolve
  askResolve = null
  r?.(ok)
}

/**
 * 结转损益。重复执行是安全的：后端永远「先删旧凭证再重算」。
 * 确认框只在「要删已有结转凭证」时才提示删除清单 ——
 * 删之前必须让人知道删的是哪几张。
 */
async function doCarry() {
  const p = store.currentPeriod
  // 前端也守一道：按钮虽然已经禁了，但「先结上一月」这句话本身比后端
  // 抛出来的整句报错更早出现在屏幕上，而且指明了该去结哪个月。
  if (prevBlocked.value) {
    return ElMessage.warning(`请先结账 ${prevPeriod.value.code}，再给 ${p.code} 结转损益`)
  }
  const ok = carryInfo.value.has
    ? await ask({
        tone: 'warning', icon: Switch, title: '重新结转损益',
        chips: carryInfo.value.vouchers,
        lead: '先删除旧结转凭证，再按最新余额重新生成。',
        confirmText: '删除并重新结转'
      })
    : await ask({
        tone: 'primary', icon: Switch, title: '结转损益', chips: [p.code],
        lead: '损益类科目本期发生额 → 本年利润',
        confirmText: '结转'
      })
  if (!ok) return

  carrying.value = true
  try {
    const r = await api.carryForward(store.bookId, p.id, true)
    await store.loadPeriods()
    await loadCarryInfo()
    if (r.replaced) {
      ElMessage.success(`已删除 ${r.removed.join('、')}，重新生成 ${r.vouchers.join('、')}`)
    } else {
      ElMessage.success(`已生成 ${r.vouchers.join('、')}`)
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '结转失败')
  } finally {
    carrying.value = false
  }
}

/** 本期已有哪些结转凭证 —— 决定按钮是「执行」还是「重新结转」。 */
const EMPTY_CARRY = { has: false, vouchers: [], posted: false }
async function loadCarryInfo() {
  const p = store.currentPeriod
  if (!p) return (carryInfo.value = { ...EMPTY_CARRY })
  const bookId = store.bookId
  const periodId = p.id
  try {
    const r = await api.carryForwardInfo(bookId, periodId)
    // 切账套时旧请求可能后到，迟到的响应会盖掉新账套的状态
    if (store.bookId !== bookId || store.periodId !== periodId) return
    carryInfo.value = r
  } catch {
    if (store.bookId !== bookId || store.periodId !== periodId) return
    carryInfo.value = { ...EMPTY_CARRY }
  }
}

async function doClose() {
  const p = store.currentPeriod
  // 没有当前期间时按钮是亮着的（`:disabled` 里 `currentPeriod?.locked` 在 null 时
  // 是 undefined，falsy），这里补一道：宁可说一句「先选一个期间」，也别让
  // 后面那句 `p.version` 抛出来变成一次白屏
  if (!p) return ElMessage.warning('先在顶栏选一个会计期间')
  // 封账之前先体检一次：断号、时序不对的月份一旦锁进历史，纸质凭证按号装订
  // 就跟系统对不上了。与其等接口报错，不如在点确认之前就把差在哪儿说清楚。
  try {
    const chk = await voucherPeriodCheck(store.bookId, p.id)
    if (!chk.clean) {
      return ElMessage.warning(
        `${p.code} 的凭证还没整理干净，先别结账：${chk.blocking.join('；')}。`
        + `请到「凭证 → 整理凭证」把号排好再来。`
      )
    }
  } catch {
    // 体检只是提前说一声，体检挂了不该挡住结账 —— 真有问题的后端也会拦
  }
  if (!await ask({
    tone: 'success', icon: Lock, title: '结账', chips: [p.code],
    lead: '锁定后凭证不可再改。', confirmText: '结账'
  })) return
  try {
    const r = await closePeriodWithVersion(p)
    await store.loadPeriods()
    const extra = r.auto_closed_periods?.length ? `，顺带结账 ${r.auto_closed_periods.join('、')}` : ''
    const posted = r.auto_posted ? `，自动记账 ${r.auto_posted} 张` : ''
    ElMessage.success(`${p.code} 已结账${extra}${posted}`)
  } catch (e) {
    await handlePeriodError(e, '结账失败')
  }
}

/**
 * 结账 / 反结账都走 `api/book.js`，并**带上期间版本号**（乐观锁）。
 *
 *  版本号的理由写在 `api/book.js::closePeriod` 的注释里：两个账号各开着
 *  一个页面时，后提交的那个必须被 409 拒掉，而不是按他屏幕上那份陈旧的
 *  「未结账」状态照做。
 *
 *  这里原来绕过 api 层、直接 import 共享 http 手写路径字符串，理由是
 *  「closePeriod 还没有 version 参数」。现在有了，路径回到单一来源 ——
 *  写在两个文件里意味着改后端路由时必然漏改一处。
 */
async function closePeriodWithVersion(p) {
  return api.closePeriod(store.bookId, p.id, p.version)
}

async function uncloseWithVersion(p) {
  return api.unclose(store.bookId, p.id, p.version, true)
}

/**
 * 结账 / 反结账失败的两条路，**必须分开说**（审计 P1-06 的前端一半）。
 *
 *  400 = 「你参数写错了」—— 去看看你自己填的那些，界面上现有报错文案就是干这个的。
 *  409 = 「别人先动了手，你手上的版本过期了」—— 该做的是**刷新**，
 *        不是检查参数。按 400 那样弹一句「结账失败」，人会以为是自己哪里没填对，
 *        反复重试、反复失败，而真相是「页面上这份状态已经是昨天的了」。
 *
 *  409 一律先把期间重新拉一遍：手里的 version 刷新了才能重试，
 *  拿旧版本再点一次只会再撞一次 409。
 */
async function handlePeriodError(e, fallback) {
  const status = e?.response?.status
  const detail = e?.response?.data?.detail
  if (status === 409) {
    ElMessage.warning({ message: detail || `${fallback}：期间状态已被他人更新，请刷新后重试`, duration: 6000 })
    await store.loadPeriods()
    return
  }
  ElMessage.error(detail || fallback)
  await store.loadPeriods()
}

async function previewUnclose() {
  const p = store.currentPeriod
  if (!p) return ElMessage.warning('先在顶栏选一个会计期间')
  const r = await api.unclosePreview(store.bookId, p.id)
  if (!r.periods.length) return ElMessage.info('所选期间及其之后没有已结账的期间，无需反结账')

  const ok = await ask({
    tone: 'danger', icon: Unlock, title: '批量反结账', chips: r.periods,
    lead: `从 ${r.periods[0]} 起连续解锁 ${r.periods.length} 个期间`,
    confirmText: '反结账'
  })
  if (!ok) return

  let done
  try {
    done = await uncloseWithVersion(p)
  } catch (e) {
    await handlePeriodError(e, '反结账失败')
    return
  }
  await store.loadPeriods()
  ElMessage.success(`已解锁 ${done.periods.length} 个期间，${done.unposted_vouchers} 张凭证转未记账`)
}

watch([() => store.periodId, () => store.bookId], loadCarryInfo)
// 本年利润按**年度**取：同一年的 12 个月共用一份设定，
// 而这个设定挂的就是「这一年的科目表里的哪一个科目」。
watch([() => store.bookId, () => store.currentPeriod?.year], loadProfit)
onMounted(() => {
  loadCarryInfo()
  loadProfit()
})

async function doYearEnd() {
  const from = store.currentYear
  if (!await ask({
    tone: 'warning', icon: Calendar, title: '年度结转',
    chips: [`${from} → ${from + 1}`],
    lead: '期末余额转为下一年 1 月期初',
    confirmText: `结转到 ${from + 1} 年`
  })) return
  yearEnding.value = true
  try {
    const r = await api.yearEnd(store.bookId, from, from + 1)
    await store.loadBooks()
    await store.loadPeriods()
    store.setYear(r.new_year)
    ElMessage.success(`已追加 ${r.new_year} 年度（${r.first_period} 起 12 期），结转 ${r.carried_subjects} 个科目期初余额`)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '年度结转失败')
  } finally {
    yearEnding.value = false
  }
}
</script>

<style scoped>
.page { padding: 18px 16px 24px; max-width: 1440px; }

/* ---- 页首 ---- */
.headline {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.headline-main { display: flex; align-items: baseline; gap: 10px; }
.eyebrow { font-size: 12px; color: var(--cwq-muted); }
.headline-code {
  font-size: 26px;
  font-weight: 650;
  letter-spacing: -0.01em;
  font-variant-numeric: tabular-nums;
}

/* ---- 四个操作块 ---- */
.ops {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 14px;
}
@media (max-width: 1280px) { .ops { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 680px)  { .ops { grid-template-columns: 1fr; } }

.tile {
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid var(--cwq-border);
  border-radius: 8px;
  padding: 14px;
}
.tile-head { display: flex; align-items: center; gap: 9px; margin-bottom: 10px; }
.tile-icon {
  width: 26px;
  height: 26px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  font-size: 15px;
  flex: none;
}
.tile-icon.blue   { background: #eff5ff; color: #2563eb; }
.tile-icon.green  { background: #f0fdf4; color: #16a34a; }
.tile-icon.red    { background: #fef2f2; color: #dc2626; }
.tile-icon.purple { background: #f5f3ff; color: #7c3aed; }
.tile-title { font-weight: 600; font-size: 14px; }

/* 状态行是这一块的重点：颜色 + 图标，不用读文字就知道能不能点 */
.tile-status {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  line-height: 1.5;
  margin-bottom: 8px;
  min-height: 18px;
}
.tile-status.ok   { color: #15803d; }
.tile-status.warn { color: #b45309; }
.tile-status.idle { color: var(--cwq-muted); }
.tile-status .el-icon { flex: none; }

.tile-desc {
  margin: 0 0 12px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--cwq-muted);
  flex: 1;
}
.tile-btn { width: 100%; margin: 0; }

/* ---- 本年利润科目这一行 ----
   它**就是一个字段**：值本身就是控件，选中即生效，所以没有「设置」按钮。
   找不到时整行转成琥珀色 —— 这一格动不了的原因是「没有本年利润科目」，
   得让人一眼看见，而不是点下去才知道。 */
.tile-profit {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: -4px 0 12px;
  font-size: 12px;
}
.tile-profit-label {
  flex: 0 0 auto;
  color: var(--cwq-muted);
  font-size: 12px;
  white-space: nowrap;
}
.tile-profit :deep(.tile-profit-select) { flex: 1 1 auto; min-width: 0; }
.tile-profit.warn :deep(.el-select__selected-item),
.tile-profit.warn :deep(.el-select__placeholder) { color: #b45309; font-weight: 600; }

/* ---- 整年月度宫格 ---- */
.year-card { padding: 16px; }

.months {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin: 18px 0 16px;
}
@media (max-width: 1280px) { .months { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 720px)  { .months { grid-template-columns: repeat(2, 1fr); } }

.month {
  appearance: none;
  font: inherit;
  text-align: left;
  cursor: pointer;
  padding: 18px 18px 16px;
  min-height: 104px;
  border-radius: 8px;
  border: 1px solid var(--cwq-border);
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 8px;
  justify-content: space-between;
  transition: border-color .15s, box-shadow .15s, background-color .15s;
}
.month:hover { border-color: #c3cad6; }
.month:focus-visible { outline: 2px solid var(--cwq-primary); outline-offset: 1px; }

.m-code { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }
.m-stat { font-size: 13px; color: var(--cwq-muted); font-variant-numeric: tabular-nums; }

.m-bar {
  display: block;
  height: 4px;
  border-radius: 2px;
  background: #eef1f5;
  overflow: hidden;
}
.m-bar-fill { display: block; height: 100%; border-radius: 2px; }

.month.locked { background: #f6fbf7; border-color: #cfe9d6; }
.month.locked .m-code { color: #15803d; }
.month.locked .m-bar-fill { background: var(--cwq-ok); }

.month.pending { background: #fffdf6; border-color: #f0dcb0; }
.month.pending .m-code { color: #b45309; }
.month.pending .m-bar-fill { background: var(--cwq-warn); }

.month.idle .m-code { color: #a8abb2; }

.month.current {
  border-color: var(--cwq-primary);
  box-shadow: 0 0 0 2px rgba(37, 99, 235, .18);
}

.legend {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  font-size: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--cwq-border);
}
.legend i {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 5px;
  vertical-align: middle;
}
.dot.ok   { background: var(--cwq-ok); }
.dot.warn { background: var(--cwq-warn); }
.dot.idle { background: #d5d8dd; }
.dot.cur  { background: var(--cwq-primary); }
</style>
