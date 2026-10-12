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
        <el-button class="tile-btn" type="primary" :loading="carrying"
                   :disabled="!store.canWrite || store.currentPeriod?.locked || prevBlocked" @click="doCarry">
          {{ prevBlocked ? '先结上一月' : (carryInfo.has ? '重新结转' : '执行结转') }}
        </el-button>
      </div>

      <!-- 结账 -->
      <div class="tile">
        <div class="tile-head">
          <span class="tile-icon green"><el-icon><Lock /></el-icon></span>
          <span class="tile-title">结账</span>
        </div>
        <div class="tile-status" :class="store.currentPeriod?.locked ? 'ok' : 'idle'">
          <template v-if="store.currentPeriod?.locked">
            <el-icon><CircleCheck /></el-icon><span>本期已结账</span>
          </template>
          <template v-else><span class="plain">本期未结账</span></template>
        </div>
        <p class="tile-desc">锁定本期，之后凭证不可再改。</p>
        <el-button class="tile-btn" type="success" :disabled="!store.canWrite || store.currentPeriod?.locked"
                   @click="doClose">
          {{ store.currentPeriod?.locked ? '已结账' : `结账 ${store.currentPeriod?.code}` }}
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

/** 月份小块的配色：一眼分清「已收尾」「有凭证没收尾」「本月空着」。 */
function monthClass(p) {
  return {
    locked: p.locked,
    pending: !p.locked && p.vouchers > 0,
    idle: !p.locked && !p.vouchers,
    current: p.id === store.periodId
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
    const r = await api.closePeriod(store.bookId, p.id)
    await store.loadPeriods()
    const extra = r.auto_closed_periods?.length ? `，顺带结账 ${r.auto_closed_periods.join('、')}` : ''
    const posted = r.auto_posted ? `，自动记账 ${r.auto_posted} 张` : ''
    ElMessage.success(`${p.code} 已结账${extra}${posted}`)
  } catch (e) {
    // 结账失败的十有八九是「损益还没结平」，原文里有科目和金额，别吞掉
    ElMessage.error(e?.response?.data?.detail || '结账失败')
    await store.loadPeriods()
  }
}

async function previewUnclose() {
  const p = store.currentPeriod
  const r = await api.unclosePreview(store.bookId, p.id)
  if (!r.periods.length) return ElMessage.info('所选期间及其之后没有已结账的期间，无需反结账')

  const ok = await ask({
    tone: 'danger', icon: Unlock, title: '批量反结账', chips: r.periods,
    lead: `从 ${r.periods[0]} 起连续解锁 ${r.periods.length} 个期间`,
    confirmText: '反结账'
  })
  if (!ok) return

  const done = await api.unclose(store.bookId, p.id, true)
  await store.loadPeriods()
  ElMessage.success(`已解锁 ${done.periods.length} 个期间，${done.unposted_vouchers} 张凭证转未记账`)
}

watch([() => store.periodId, () => store.bookId], loadCarryInfo)
onMounted(loadCarryInfo)

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
