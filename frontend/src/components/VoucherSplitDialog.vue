<template>
  <!-- 拆分凭证：在分录之间选一处切开，把这张拆成两张。

       界面只摆**能拆**的切点（不平的切点压根不画出来）——
       会计自己知道切开了要平，不平的地方摆在那儿只是噪音。

       号怎么走是这页唯一必须说清的事：**原凭证继承它自己的号**，
       新凭证往后拿一个（= 原号 +1），后面同期的号整体顺延。 -->
  <el-dialog
    v-model="open"
    title="拆分凭证"
    width="760px"
    align-center
    :close-on-click-modal="false"
    @open="load"
  >
    <div class="vs">
      <div v-if="error" class="vs__err">{{ error }}</div>

      <table class="vs__tbl">
        <thead>
          <tr>
            <th class="vs__no">序号</th>
            <th>摘要</th>
            <th>会计科目</th>
            <th class="vs__amt">借方金额</th>
            <th class="vs__amt">贷方金额</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="(r, i) in entries" :key="r.line_no">
            <tr class="vs__row">
              <td class="vs__no">{{ r.line_no }}</td>
              <td class="vs__sum">{{ r.summary }}</td>
              <td class="vs__subj">{{ r.subject_text }}</td>
              <td class="vs__amt">{{ r.direction === 'DEBIT' ? r.amount : '' }}</td>
              <td class="vs__amt">{{ r.direction === 'CREDIT' ? r.amount : '' }}</td>
            </tr>
            <!-- 能拆的地方才画一条线；点它就是把这里当分界处 -->
            <tr v-if="cutPoints.includes(i + 1)" class="vs__cutrow">
              <td colspan="5">
                <button
                  type="button"
                  class="vs__cut"
                  :class="{ on: atLine === i + 1 }"
                  @click="atLine = i + 1"
                >
                  <span class="vs__cut-rule"></span>
                  <span class="vs__cut-tag">第 {{ i + 1 }} 行后切开</span>
                  <span class="vs__cut-rule"></span>
                </button>
              </td>
            </tr>
          </template>
        </tbody>
      </table>

      <div v-if="!cutPoints.length && !error" class="vs__none">
        没有能拆的地方 —— 每一处切开，两张都不平。
      </div>

      <div v-if="selected" class="vs__cards">
        <div class="vs__card">
          <div class="vs__card-title">原凭证 {{ preview?.original_no || '—' }}</div>
          <div class="vs__card-sub">前半张 · {{ selected.first.lines }} 行</div>
          <div class="vs__card-sum">
            借 {{ selected.first.debit }}<span class="vs__sep">/</span>贷 {{ selected.first.credit }}
          </div>
        </div>
        <div class="vs__card vs__card--new">
          <div class="vs__card-title">新凭证 {{ preview?.created_no || '—' }}</div>
          <div class="vs__card-sub">后半张 · {{ selected.second.lines }} 行</div>
          <div class="vs__card-sum">
            借 {{ selected.second.debit }}<span class="vs__sep">/</span>贷 {{ selected.second.credit }}
          </div>
        </div>
      </div>

      <div v-if="movedCount" class="vs__note">后续 {{ movedCount }} 张凭证号顺延 +1</div>
    </div>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <!-- 在途守卫：双击「确认拆分」会并发两个 POST /vouchers/{id}/split，
           第二次看到凭证已被截短，当场回一句「分界处必须在第 1 行到第 n-1 行之间」——
           用户刚拆成功就看到一条红字报错（审计 §3.29）。 -->
      <el-button type="primary" :disabled="!canConfirm || submitting" :loading="submitting"
                 @click="submit">确认拆分</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import * as api from '@/api/voucher'
import { splitCandidates } from '@/utils/voucherSplit'

const props = defineProps({
  modelValue: Boolean,
  bookId: { type: [Number, String], default: 0 },
  voucherId: { type: [Number, String], default: 0 },
  /** 已经填好的分录：{ line_no, summary, subject_text, direction, amount } */
  entries: { type: Array, default: () => [] },
})

const emit = defineEmits(['update:modelValue', 'confirm'])

const open = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

const preview = ref(null)
const error = ref('')
const atLine = ref(0)
/** 「确认拆分」在途：这一趟还没回来之前，不再发第二趟。 */
const submitting = ref(false)

/**
 * 候选：服务端给的是权威的，它还没回来时先用本地算的顶上。
 *
 * **只留能拆的**（`valid`）—— 不平的切点不画出来：摆在那儿只是噪音，
 * 会计自己知道切开了要平。
 */
const candidates = computed(() => {
  const fromServer = preview.value?.candidates
  const list = fromServer && fromServer.length ? fromServer : splitCandidates(props.entries)
  return list.filter((c) => c.valid)
})

/** 能切的那些位置（第几条分录之后）。 */
const cutPoints = computed(() => candidates.value.map((c) => c.at_line))

/** 选中的那一个。 */
const selected = computed(
  () => candidates.value.find((c) => c.at_line === atLine.value) || null)

/** 服务端对这一刀的说法。 */
const serverCandidate = computed(
  () => (preview.value?.candidates || []).find((c) => c.at_line === atLine.value) || null)

/**
 * **能不能确认，只看服务端。**
 *
 * 本地那份 `valid` 只用来画线 —— 万一两边口径不一致（前端改了判平规则、
 * 后端没跟上），本地放行就会推出一张不平的凭证落进库。
 * 服务端还会再校验一次，这里只是提前把按钮按灰。
 */
const canConfirm = computed(
  () => !!selected.value && !!serverCandidate.value && serverCandidate.value.valid === true)

const movedCount = computed(() => (preview.value?.moved || []).length)

async function load() {
  error.value = ''
  preview.value = null
  atLine.value = 0
  if (!props.voucherId) return
  try {
    const p = await api.splitPreview(props.bookId, props.voucherId)
    preview.value = p
    // 默认选中第一个能拆的地方
    const firstOk = (p.candidates || []).find((c) => c.valid)
    atLine.value = firstOk ? firstOk.at_line : 0
  } catch (e) {
    error.value = e?.response?.data?.detail || '拿不到拆分的预览，稍后再试'
  }
}

function close() {
  open.value = false
}

function submit() {
  if (!canConfirm.value || submitting.value) return
  submitting.value = true
  // 把「这一趟结束了」也交给父组件：成功时弹窗会关掉，失败时父组件要能
  // 让人**再试一次** —— 所以按钮不能一直灰着（审计 §3.29）。
  emit('confirm', { at_line: atLine.value, done: () => { submitting.value = false } })
}

// 父组件换了一张凭证（翻页）时，把上一次的结果清掉
watch(() => props.voucherId, () => {
  preview.value = null
  atLine.value = 0
})
</script>

<style scoped>
.vs__tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.vs__tbl th {
  padding: 0 8px 7px;
  font-size: 12px;
  font-weight: 500;
  color: var(--cwq-muted);
  text-align: left;
  border-bottom: 1px solid var(--cwq-border);
}
.vs__tbl th.vs__amt { text-align: right; }
.vs__row td {
  padding: 9px 8px;
  border-bottom: 1px solid var(--cwq-hairline, #eef1f5);
  color: var(--cwq-text);
}
.vs__row:hover td { background: #fafbfc; }
.vs__no { width: 42px; color: var(--cwq-muted); }
.vs__sum { max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vs__subj { color: var(--cwq-muted); }
.vs__amt { width: 92px; text-align: right; }

/* 那条「在这里切开」的线 —— 页面上唯一能点的地方 */
.vs__cutrow td { padding: 0; border-bottom: none; }
.vs__cut {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 0;
  background: none;
  border: none;
  cursor: pointer;
  outline: none;
}
.vs__cut-rule {
  flex: 1;
  height: 1px;
  background: repeating-linear-gradient(to right, #d6dbe3 0 6px, transparent 6px 12px);
  transition: background .15s;
}
.vs__cut-tag {
  flex: none;
  font-size: 11px;
  line-height: 1;
  padding: 4px 10px;
  border-radius: 999px;
  color: var(--cwq-muted);
  background: #f4f6f9;
  border: 1px solid var(--cwq-border);
  white-space: nowrap;
  transition: color .15s, background .15s, border-color .15s;
}
.vs__cut:hover .vs__cut-tag { color: var(--cwq-primary); border-color: #c7dbff; }
.vs__cut:hover .vs__cut-rule {
  background: repeating-linear-gradient(to right, #b6cdfa 0 6px, transparent 6px 12px);
}
.vs__cut.on .vs__cut-tag {
  color: #fff;
  background: var(--cwq-primary);
  border-color: var(--cwq-primary);
}
.vs__cut.on .vs__cut-rule { background: #c7dbff; }

.vs__none {
  padding: 26px 0;
  text-align: center;
  font-size: 13px;
  color: var(--cwq-muted);
}

.vs__cards {
  display: flex;
  gap: 12px;
  margin-top: 18px;
}
.vs__card {
  flex: 1 1 0;
  min-width: 0;
  padding: 11px 14px;
  border-radius: 8px;
  background: #f7f8fa;
  border: 1px solid var(--cwq-border);
}
.vs__card--new { background: #eff5ff; border-color: #dbe7fd; }
.vs__card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--cwq-text);
  font-variant-numeric: tabular-nums;
}
.vs__card--new .vs__card-title { color: #1d4ed8; }
.vs__card-sub { margin-top: 3px; font-size: 12px; color: var(--cwq-muted); }
.vs__card-sum {
  margin-top: 7px;
  font-size: 13px;
  color: var(--cwq-text);
  font-variant-numeric: tabular-nums;
}
.vs__sep { margin: 0 6px; color: #c3c8d1; }

.vs__note {
  margin-top: 9px;
  font-size: 12px;
  color: var(--cwq-muted);
}

.vs__err {
  margin-bottom: 12px;
  padding: 9px 12px;
  border-radius: 6px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: var(--cwq-danger);
  font-size: 12px;
  line-height: 1.7;
}
</style>
