<template>
  <el-dialog
    v-model="visible"
    title="账套初始化"
    width="620px"
    :close-on-click-modal="false"
    @closed="reset"
  >
    <div class="muted mb">{{ initSummaryText }}</div>

    <el-steps :active="stepsActive" align-center finish-status="success" class="mb">
      <el-step title="导入科目" />
      <el-step title="导入期初余额" />
    </el-steps>

    <!-- 第 1 步 -->
    <section class="panel">
      <div class="panel-title">第 1 步 · 导入科目</div>
      <div class="panel-desc">
        这一步决定账上有哪些科目，第 2 步的期初余额要挂在这些科目上 —— 科目没定下来，
        余额就无处可挂，所以必须先做这一步。
      </div>

      <div v-if="subjectsDone" class="done-box">
        <el-icon class="done-icon"><CircleCheckFilled /></el-icon>
        <span>{{ subjectLabel }}</span>
      </div>
      <template v-else>
        <div class="entry-row">
          <el-button type="primary" :disabled="!canImport" @click="importVisible = true">
            导入科目表（用友等）
          </el-button>
          <el-button :loading="keeping" :disabled="!canKeep" @click="keepChart">沿用系统标准科目表</el-button>
        </div>
        <div v-if="!canKeep" class="entry-note">
          「沿用标准科目表」要账上已经装好了科目才可用；本账套没有科目时先导入一份。
        </div>
      </template>
    </section>

    <!-- 第 2 步：第 1 步没完成时锁住。锁是**看得见**的（置灰 + 锁图标 + 一句说明），
         不是「少一个按钮」—— 少了按钮用户只会以为这个功能不存在，
         置灰加一句「先做第 1 步」才说得清为什么点不开。 -->
    <section class="panel" :class="{ 'is-locked': !step2Open }">
      <div class="panel-title">
        第 2 步 · 导入期初余额
        <span v-if="!step2Open" class="lock-mark"><el-icon><Lock /></el-icon>未解锁</span>
      </div>

      <div v-if="!step2Open" class="panel-desc lock-desc">{{ STEP2_LOCK_HINT }}</div>
      <template v-else>
        <div class="panel-desc">
          把上一期（月/年末）的科目余额表导进来，作为本账套的起算余额。导入后仍需在期初余额页保存才落库。
        </div>
        <div v-if="openingsDone" class="done-box">
          <el-icon class="done-icon"><CircleCheckFilled /></el-icon>
          <span>{{ openingLabel }}</span>
        </div>
        <div v-else class="entry-row">
          <el-button type="primary" @click="goOpenings">导入期初余额</el-button>
          <!-- 第二条出口：新公司第一年、上一年度本来就是空账，期初该是 0。
               以前只有「导入」一个口子，这类账套的初始化永远停在第 2 步 ——
               卡片上的「待初始化」摘不掉，向导每次都在催一件根本不需要做的事。 -->
          <el-button :loading="skipping" @click="skipOpenings">本期无期初余额，直接开始记账</el-button>
        </div>
      </template>
    </section>

    <!-- 目标账套要显式传进去：这个弹窗默认认的是**当前账套**，
         而向导可以对任意一本账套打开（卡片上的「继续初始化」） -->
    <SubjectImportDialog v-model="importVisible" :book-id="targetId"
                         @imported="onSubjectsImported" />

    <!-- 底部那颗主按钮代表「现在最该做的那件事」，做完就换词。
         全部做完时它变回一个普通的「完成」——留一颗点不动的「下一步」在那儿，
         等于一直在骗人说还有一步没走。 -->
    <template #footer>
      <el-button v-if="!allDone" @click="close">稍后再说</el-button>
      <el-button type="primary" :disabled="!subjectsDone" @click="onFooterClick">
        {{ footerLabel }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheckFilled, Lock } from '@element-plus/icons-vue'
import SubjectImportDialog from '@/components/SubjectImportDialog.vue'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'
import {
  FOOTER_DONE, FOOTER_LABEL, FOOTER_OPENINGS, STEP2_LOCK_HINT,
  canEnterStep2, canImportSubjects, canKeepChart,
  initFooterAction, initOpeningLabel, initStep, initStepsActive,
  initSubjectLabel, initSummary
} from '@/utils/bookInit'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 要初始化的账套；不传就用当前账套 */
  bookId: { type: [Number, String], default: 0 }
})
const emit = defineEmits(['update:modelValue', 'updated'])

const store = useSessionStore()
const router = useRouter()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})
const targetId = computed(() => Number(props.bookId) || store.bookId)
const init = computed(() => store.initOf(targetId.value))

const keeping = ref(false)
const skipping = ref(false)
const importVisible = ref(false)

/** 当前落在第几步。没有 init 时按第 1 步处理（fail-closed），与 initStep 同一口径 */
const currentStep = computed(() => initStep(init.value))
const allDone = computed(() => currentStep.value === 0)
const subjectsDone = computed(() => currentStep.value !== 1)
const openingsDone = computed(() => !!init.value?.openings_ready)
const step2Open = computed(() => canEnterStep2(init.value))
const canKeep = computed(() => canKeepChart(init.value))
const canImport = computed(() => canImportSubjects(init.value))

const initSummaryText = computed(() => initSummary(init.value))
const subjectLabel = computed(() => initSubjectLabel(init.value))
const openingLabel = computed(() => initOpeningLabel(init.value))
const stepsActive = computed(() => initStepsActive(init.value))
const footerAction = computed(() => initFooterAction(init.value))
const footerLabel = computed(() => FOOTER_LABEL[footerAction.value])

/** 打开时拉一份最新的，任一步做完再刷新 —— 父组件拿账套列表那份可能已经过期 */
async function refresh() {
  await store.loadInit(targetId.value)
}

watch(visible, (open) => {
  if (!open) return
  refresh()
})

/**
 * 底部主按钮的三种去向。
 *
 *  不用组件自己记「第几步」：那份本地状态跟 init 迟早对不上，对不上时
 *  表现就是一颗按了没反应、词也不变的按钮。改成每次点都问一遍 utils，
 *  判定只有一份真相。
 */
function onFooterClick() {
  if (footerAction.value === FOOTER_DONE) return close()
  if (footerAction.value === FOOTER_OPENINGS) return goOpenings()
  // 第 1 步没做完时这按钮是灰的，走不到这里；留着分支是为了不让它静默失败
}

/** 关掉前把这一份写回 store，让账套卡片上的标记立刻跟着变 */
function close() {
  store.markInit(targetId.value, init.value)
  visible.value = false
  emit('updated', init.value)
}

function reset() {
  importVisible.value = false
}

async function keepChart() {
  keeping.value = true
  try {
    const res = await api.keepChartSubjects(targetId.value)
    // 后端把最新的 init 一并带回来了，直接就地更新，不必再拉一次
    store.markInit(targetId.value, res.init)
    ElMessage.success('已沿用系统标准科目表')
    emit('updated', res.init)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  } finally {
    keeping.value = false
  }
}

/** 导入完科目后刷新一次：后端已把 subjects_ready 置真 */
async function onSubjectsImported() {
  await refresh()
  emit('updated', init.value)
}

/**
 * 第 2 步要去期初余额页做 —— 但**不在向导里做**。
 *
 *  导入结果要塞进页面上那张可以逐行改的表格、并由人点「保存期初余额」才落库，
 *  向导里没有那张表。所以关掉向导、带着一个 query 跳过去，让那一页把弹窗弹出来。
 *  query 用完由那一页 replace 掉：留在地址栏里的 ?import=1 会在刷新时再弹一次。
 */
/**
 * 声明「本期没有期初余额」。
 *
 * 二次确认是必须的：这句话在账上的含义是「从零开始」，按错一下，
 * 一个刚建好、期初还没导的账套就被标成「确认过没有期初」——
 * 而账上确实一个数都没有时，谁都看不出它被标过。
 */
async function skipOpenings() {
  try {
    await ElMessageBox.confirm(
      '确认这个账套本期没有期初余额？\n\n'
      + '适用于新公司第一年、或上一年度本来就是空账的情况。\n'
      + '确认后初始化即算完成，可以直接开始记凭证；之后仍然可以再导入期初余额。',
      '本期无期初余额？',
      { type: 'warning', confirmButtonText: '确认无期初', cancelButtonText: '我再想想' }
    )
  } catch {
    return
  }
  skipping.value = true
  try {
    const res = await api.skipOpenings(targetId.value)
    store.markInit(targetId.value, res.init)
    ElMessage.success('已记下：本期无期初余额')
    emit('updated', res.init)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  } finally {
    skipping.value = false
  }
}

async function goOpenings() {
  store.markInit(targetId.value, init.value)
  if (store.bookId !== targetId.value) await store.setBook(targetId.value)
  visible.value = false
  emit('updated', init.value)
  await router.push({ path: '/openings', query: { import: '1' } })
}
</script>

<style scoped>
.mb { margin-bottom: 12px; }
.muted { color: var(--cwq-muted); font-size: 13px; }
/* 两个步骤面板在小屏上会高过一屏。不封顶的话底部「稍后再说 / 下一步」被顶出屏幕，
   而第 1 步没做完时那个按钮本来就是灰的 —— 用户会以为是没渲染出来 */
:deep(.el-dialog__body) { max-height: calc(100vh - 200px); overflow-y: auto; }
.panel {
  padding: 14px 16px;
  border: 1px solid var(--cwq-border, #e2e7ed);
  border-radius: 8px;
}
.panel + .panel { margin-top: 12px; }
.panel-title { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-weight: 700; }
.panel-desc { color: var(--cwq-muted); font-size: 13px; line-height: 1.7; }
.entry-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.entry-note { margin-top: 8px; color: var(--cwq-muted); font-size: 12px; line-height: 1.6; }
.done-box {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 10px 12px;
  color: var(--cwq-ok);
  background: #f2f9f3;
  border: 1px solid #cfe6d2;
  border-radius: 6px;
  font-size: 13px;
}
.done-icon { font-size: 16px; }
/* 锁态靠**三样东西一起**说：整块置灰、挂锁图标、说一句该做什么。
   只把按钮置灰的话，用户看到的是一个灰按钮，说不出它为什么灰 */
.panel.is-locked {
  background: #f7f8fa;
  border-color: #e3e7ec;
  border-style: dashed;
}
.panel.is-locked .panel-title { color: #8993a1; }
.panel.is-locked :deep(.panel-desc) { color: #8993a1; }
.lock-desc { color: #8993a1; }
.lock-mark {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  color: #8993a1;
  font-size: 12px;
  font-weight: 400;
}
</style>
