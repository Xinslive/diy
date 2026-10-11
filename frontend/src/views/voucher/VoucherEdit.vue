<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-button @click="backToList">← 返回</el-button>
      <el-divider direction="vertical" />
      <span class="muted">凭证号</span>
      <!-- 号已经预填好了（见 /vouchers/next），placeholder 要说清「还能怎么用」：
           清空它就退回后端发号。手填一个已存在的号会被挡下来并把号报给你。
           插入模式下清空不是发下一个号，而是照着插入位发 —— 位置是这次的重点。 -->
      <el-input v-model="form.voucher_no" style="width: 110px"
                :placeholder="insertCtx ? '留空按插入位发号' : '留空自动发号'" />
      <span class="muted">日期</span>
      <el-date-picker v-model="form.voucher_date" type="date" value-format="YYYY-MM-DD" style="width: 150px" />
      <span class="muted">附件</span>
      <el-input-number v-model="form.attachments" :min="0" :max="999" style="width: 100px" />
      <span class="spacer" />
      <!-- 从列表进来的话，前一张/后一张翻的是**列表那个结果集**（筛选条件原样带着，
           可以跨月跨年）；直接打开某张凭证、不知道从哪来的，才退回同期间内翻。
           插入/增加直接开一张新的。原来那个「增加分录」按钮去掉了 ——
           分录靠回车往下加，不用专门去找按钮。 -->
      <el-tooltip :content="neighbourScope || '按同一会计期间的录入顺序前后翻'"
                  placement="bottom" :disabled="!neighbourScope">
        <el-button-group>
          <el-button :disabled="!prevId" @click="goNeighbour('prev')">前一张</el-button>
          <el-button :disabled="!nextId" @click="goNeighbour('next')">后一张</el-button>
        </el-button-group>
      </el-tooltip>
      <el-button :disabled="!canAdd" @click="insertVoucher">插入凭证</el-button>
      <el-button :disabled="!canAdd" @click="addVoucher">增加凭证</el-button>
      <el-button v-if="voucherId" :disabled="form.status === 'POSTED'" @click="printVoucher">打印预览</el-button>
    </div>

    <div class="card">
      <div class="voucher-head">
        <div><span class="muted">会计期间</span> <b>{{ voucherPeriod?.code || store.currentPeriod?.code }}</b></div>
        <!-- 标签上写着「已记账」，前面再顶一个「状态」纯属重复；去掉之后这一行
             少一块零碎的东西，视线不用在两个字上绊一下 -->
        <div>
          <el-tag v-if="form.status === 'POSTED'" type="success" size="small">已记账</el-tag>
          <el-tag v-else type="warning" size="small">未记账</el-tag>
        </div>
        <div v-if="locked" class="tag-bad">当前期间已结账，不可修改</div>
        <div v-else-if="posted" class="tag-bad">
          已记账凭证已锁定，不能修改。请在凭证列表点「反记账」后再改。
        </div>
      </div>

      <!-- 左边空出的 48px 是给每一行那对 ⊕ / ＋ 的：它们挂在各自那一行的左边，
           哪一行变绿才从那儿冒出来，平时不跟数字抢地方 -->
      <div class="entry-wrap">

        <!-- 凭证分录格：整个就是一张纸质凭证的样子。
             序号/摘要/会计科目竖跨表头两行，借贷金额上排写方向、
             下排写「亿千百十万千百十元角分」，竖线一路落到每一行。 -->
        <div class="vgrid">
          <div class="vg-row vg-head">
            <div class="vg-cell th th-idx">序号</div>
            <div class="vg-cell th th-sum">摘要</div>
            <div class="vg-cell th th-subj">会计科目</div>
            <div class="vg-cell th th-amt d">借方金额</div>
            <div class="vg-cell th th-amt c">贷方金额</div>
            <div class="vg-cell th th-units d"><AmountUnits /></div>
            <div class="vg-cell th th-units c"><AmountUnits /></div>
          </div>

          <!-- 一行一个盒子：鼠标扫到哪一行，哪一行整行变绿，
               ⊕ / ＋ 只在这一行左边冒出来 -->
          <div v-for="(row, i) in form.entries" :key="i" class="vg-row"
               :class="{ 'is-on': isOn(row) }"
               @mouseenter="hoverRow = row" @mouseleave="hoverRow = null">
            <div class="vg-act">
              <el-button class="g-ins" circle size="small" title="在本格子上面插一行"
                         :disabled="readonly" @click.stop="insertRowAbove">
                <el-icon><Top /></el-icon>
              </el-button>
              <el-button class="g-add" circle size="small" title="在本格子下面插一行"
                         :disabled="readonly" @click.stop="insertRowBelow">
                <el-icon><Bottom /></el-icon>
              </el-button>
            </div>
            <div class="vg-cell td td-idx">{{ i + 1 }}</div>
            <!-- 锁的是「行」不是「格」：一行之内先填科目还是先填金额都行，
                 只有前面还有没写完的分录时，这几格才一起不开张。
                 禁用的 input 收不到点击（浏览器压根不给它派发），
                 所以还得把指针穿透掉，让点击落到格子上，提示才出得来 -->
            <div class="vg-cell td td-sum" :class="{ 'is-blocked': blocked(i) }"
                 @click="warnBlocked(i)">
              <!-- 点一下整格选中：摘要多半是同一句话抄好几遍，
                   要改就整句重打，不必先把光标挪到末尾一个一个退格 -->
              <input v-model="row.summary" class="cell-input" :disabled="readonly || blocked(i)"
                     :ref="(el) => setSumRef(el, i)"
                     placeholder="摘要" @focus="activeRow = row"
                     @click="selectText($event)" @keydown.enter.prevent="moveNext(i, 'sum')" />
            </div>
            <div class="vg-cell td td-subj"
                 :class="{ 'is-blocked': blocked(i), 'is-bad': badSubject(row) }"
                 @click="warnBlocked(i)">
              <SubjectPicker
                v-model="row.subject_id"
                :ref="(el) => setSubjectRef(el, i)"
                :options="subjects"
                :disabled="readonly || blocked(i)"
                @focusin="activeRow = row"
                @change="onSubject(row)"
                @add="openSubjectCreate(i)"
                @enter="moveNext(i, 'subj')"
                @picked="moveNext(i, 'subj')"
              />
            </div>
            <div class="vg-cell td td-amt d" :class="{ 'is-blocked': blocked(i) }"
                 @click="warnBlocked(i)">
              <AmountGrid
                :model-value="debitOf(row)"
                :ref="(el) => setAmtRef(el, i, 'd')"
                :readonly="readonly || blocked(i)"
                @update:model-value="(v) => setDebit(row, v)"
                @focus="activeRow = row"
                @enter="moveNext(i, 'd')"
                @up="moveRow(i, -1, 'd')" @down="moveRow(i, 1, 'd')"
                @balance="autoBalance(i, 'd')"
              />
            </div>
            <div class="vg-cell td td-amt c" :class="{ 'is-blocked': blocked(i) }"
                 @click="warnBlocked(i)">
              <AmountGrid
                :model-value="creditOf(row)"
                :ref="(el) => setAmtRef(el, i, 'c')"
                :readonly="readonly || blocked(i)"
                @update:model-value="(v) => setCredit(row, v)"
                @focus="activeRow = row"
                @enter="moveNext(i, 'c')"
                @up="moveRow(i, -1, 'c')" @down="moveRow(i, 1, 'c')"
                @balance="autoBalance(i, 'c')"
              />
            </div>
            <!-- 删除 ⊗ 不占格子的位置：它挂在**自己这一行**的右边外面，
                 跟左边的 ⊕ / ＋ 一个待遇 —— 只有变绿的那一行才看得见它 -->
            <div class="vg-del">
              <el-icon v-if="canDeleteRow" class="row-del" :class="{ 'is-off': readonly }"
                       title="删除这一行" @click.stop="removeRow(i)"><CircleCloseFilled /></el-icon>
            </div>
          </div>

          <div class="vg-row vg-total">
            <div class="vg-cell td td-sumcell">合计：{{ amountToChinese(total.debit) }}</div>
            <!-- 合计是 0 就不往格子里塞「0」：空格子比一个孤零零的 0 干净，
                 左边那行「合计：零元整」已经说清楚了 -->
            <div class="vg-cell td td-total d"><AmountCells :value="money2(total.debit)" /></div>
            <div class="vg-cell td td-total c"><AmountCells :value="money2(total.credit)" /></div>
          </div>
        </div>
      </div>

      <div class="vfoot">
        <span class="maker"><el-icon><EditPen /></el-icon> 制单人：{{ maker }}</span>
        <span class="foot-right">
          <span v-if="saving" class="muted">正在自动保存…</span>
          <span v-else-if="savedAt" class="saved-tag">已自动保存 {{ savedAt }}</span>
          <span class="muted">借方合计 <b>{{ fmt(total.debit) }}</b></span>
          <span class="muted">贷方合计 <b>{{ fmt(total.credit) }}</b></span>
          <span v-if="nonLeafRows.length" class="tag-bad">
            第 {{ form.entries.indexOf(nonLeafRows[0]) + 1 }} 行不是末级科目
          </span>
          <span v-if="blank" class="muted">还没录金额</span>
          <span v-else-if="balanced" class="tag-ok">借贷平衡 ✓</span>
          <span v-else class="tag-bad">差额 {{ fmt(Math.abs(total.debit - total.credit)) }}</span>
        </span>
      </div>

      <SubjectFormDialog v-model="subjectCreateOpen" :tree="subjectTree"
                        @saved="onSubjectCreated" />
    </div>

    <!-- 打印预览 -->
    <el-dialog v-model="printVisible" title="记账凭证（打印预览）" width="820px">
      <div class="print-area">
        <div class="print-title">记 账 凭 证</div>
        <table class="print-table">
          <tbody>
            <tr v-for="(e, i) in form.entries" :key="i">
              <td class="w-date">{{ i === 0 ? form.voucher_date : '' }}</td>
              <td class="w-no">{{ i === 0 ? form.voucher_no : '' }}</td>
              <td>{{ e.summary }}</td>
              <td>{{ subjectText(e.subject_id) }}</td>
              <td class="w-amt" :class="{ 'is-red': num(e.amount) < 0 }">{{ e.direction === 'DEBIT' ? fmt(num(e.amount)) : '' }}</td>
              <td class="w-amt" :class="{ 'is-red': num(e.amount) < 0 }">{{ e.direction === 'CREDIT' ? fmt(num(e.amount)) : '' }}</td>
            </tr>
            <tr class="print-total">
              <td colspan="4">合计</td>
              <td class="w-amt">{{ fmt(total.debit) }}</td>
              <td class="w-amt">{{ fmt(total.credit) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <template #footer>
        <el-button @click="printVisible = false">关闭</el-button>
        <el-button type="primary" class="no-print" onclick="window.print()">打印</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as api from '@/api/voucher'
import * as subjectApi from '@/api/subject'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'
import { amountToChinese } from '@/utils/money'
import AmountGrid from '@/components/AmountGrid.vue'
import SubjectPicker from '@/components/SubjectPicker.vue'
import SubjectFormDialog from '@/components/SubjectFormDialog.vue'
import AmountCells from '@/components/AmountCells.vue'
import AmountUnits from '@/components/AmountUnits.vue'
import { Bottom, CircleCloseFilled, EditPen, Top } from '@element-plus/icons-vue'

const store = useSessionStore()
const route = useRoute()
const router = useRouter()

// 这张凭证的 id：0 = 还没落库的空凭证。
// 原来是 computed(route.params.id)，自动保存新建之后 URL 还没来得及换的那一瞬间
// 会以为它是张新凭证 —— 再存一次就多一张重复的
const voucherId = ref(0)
const saving = ref(false)
const subjects = ref([])
const subjectTree = ref([])
const printVisible = ref(false)
// 「插入凭证」这一次的目标：插在谁后面、这张拿哪个号、谁要顺位。
// 只在 voucher-new 且带 ?after= 时有值；存下去就清掉（那时候它已经是张普通凭证了）。
const insertCtx = ref(null)
// 前一张 / 后一张：带着列表筛选条件时翻结果集，没有条件时翻同期间
const prevId = ref(null)
const nextId = ref(null)
// 翻的时候说清楚自己在结果集的第几张，免得以为按钮坏了
const neighbourScope = ref('')
// 当前正在录入的那一行，整行淡绿底：连续录入时眼睛得有个落点
const activeRow = ref(null)
// 鼠标正压着的那一行也算 —— 手扫到哪一行，那一行就变绿
const hoverRow = ref(null)
/** 变绿的那一行。⊕ / ＋ / ⊗ 都只跟着它出现，所以按钮不会指错行。 */
const greenRow = computed(() => hoverRow.value ?? activeRow.value)
const isOn = (row) => row === greenRow.value

/**
 * 这一行能不能填：前面还有没有没写完的分录。
 *
 * 锁的是**行**不是**格**。一行之内先填科目还是先填金额都认 —— 上一条没写完就锁住
 * 这一行的所有格子，包括摘要，那是「不许跳过中间去写第 3 行」的意思，不是
 * 「这一行里科目和金额谁先谁后」。
 * 返回挡着的那一条（用来把话说清楚：第几行还差什么），没有就返回 null。
 */
function blocker(i) {
  for (let j = 0; j < i; j++) {
    const e = form.entries[j]
    if (e && rowProblem(e)) return e
  }
  return null
}
const blocked = (i) => !!blocker(i)
function warnBlocked(i) {
  const e = blocked(i) ? blocker(i) : null
  if (!e || readonly.value) return
  const at = form.entries.indexOf(e) + 1
  ElMessage.warning(rowProblemText(e, at))
}

/**
 * 这一行还差什么：'subj'（没科目）/ 'nonleaf'（挂到了上级科目）/ 'amt'（没金额）/ null。
 * 非末级科目跟「没写完」是一类东西：它是一条**记错地方**的分录，
 * 放着不管既让后面几笔串不起来，存下去也是错的，一样得当场拦住。
 */
function rowProblem(row) {
  if (!row) return null
  if (!row.subject_id) return 'subj'
  if (badSubject(row)) return 'nonleaf'
  if (!num(row.amount)) return 'amt'
  return null
}
function rowProblemText(row, at = form.entries.indexOf(row) + 1) {
  const p = rowProblem(row)
  if (p === 'subj') return `第 ${at} 行还差会计科目，先把它写完再往下写`
  if (p === 'amt') return `第 ${at} 行还差金额，先把它写完再往下写`
  if (p === 'nonleaf') {
    const sub = subjectOf(row.subject_id)
    return `第 ${at} 行挂的是「${sub.code} ${sub.name}」，不是末级科目，请改到末级科目`
  }
  return ''
}
/** 页脚那一行提示用的：没有非末级科目就返回 null */
const nonLeafRows = computed(() => form.entries.filter((e) => badSubject(e) && num(e.amount)))

/** 这张凭证还剩几行：四行是底线，出现第五行才说明「多写了」，那哪一行都删得掉 */
const canDeleteRow = computed(() => form.entries.length > 4)
// 这张凭证自己的会计期间。顶栏那个期间是「现在正在记哪个月」，
// 打开一张旧凭证时两者根本不是一回事，页眉写错期间很容易误导人。
const voucherPeriod = ref(null)

const form = reactive({
  voucher_no: '', voucher_date: '', status: 'UNPOSTED',
  attachments: 0, remark: '', entries: []
})

const locked = computed(() => !!store.currentPeriod?.locked)
const posted = computed(() => form.status === 'POSTED')
// 只读：期间已结账，或凭证本身已记账
const readonly = computed(() => locked.value || posted.value)

// 能不能开新凭证：只读账套不行，期间已结账也不行
const canAdd = computed(() => store.canWrite && !locked.value)

const subjectOf = (id) => subjects.value.find((s) => s.id === id) || null
/** 这一行挂的不是末级科目：科目格标红、保存拦下、回车不许往下走 */
const badSubject = (row) => {
  const sub = subjectOf(row?.subject_id)
  return !!sub && sub.is_leaf === false
}

/** 在凭证里当场加一个科目：加完自动选中，接着就能填金额 */
const subjectCreateOpen = ref(false)
const subjectCreateRow = ref(0)
function openSubjectCreate(i) {
  subjectCreateRow.value = i
  subjectCreateOpen.value = true
}
async function onSubjectCreated(s) {
  await loadSubjects()
  form.entries[subjectCreateRow.value].subject_id = s.id
  activeRow.value = form.entries[subjectCreateRow.value]
}
async function loadSubjects() {
  const tree = await subjectApi.subjectTree(store.bookId)
  subjectTree.value = tree
  // 摊平成一条条，但**父子关系要留着**：凭证面板靠 parent_id 把科目摆成一棵树，
  // 一级一级点开，最后只让末级能选。少了 parent_id 就只剩一张平表，
  // 「为什么 1002 不能选」这个问题在界面上就再也说不清了
  const flat = []
  const walk = (nodes, parent = null) => nodes.forEach((n) => {
    flat.push({
      id: n.id, code: n.code, name: n.name, category: n.category,
      level: n.level, parent_id: parent, is_leaf: n.is_leaf
    })
    if (n.children?.length) walk(n.children, n.id)
  })
  walk(tree, null)
  subjects.value = flat
}

// ---- 借方/贷方是两个独立格子，数据层还是一个 direction + amount ----
// 往借方填数就是借方，往贷方填数就是贷方；清空则只清自己那一侧。
const debitOf = (row) => (row.direction === 'DEBIT' ? row.amount : '')
const creditOf = (row) => (row.direction === 'CREDIT' ? row.amount : '')
function setDebit(row, v) {
  if (v === '' || v === null || v === undefined) {
    if (row.direction === 'DEBIT') row.amount = ''
    return
  }
  row.direction = 'DEBIT'
  row.amount = v
}
function setCredit(row, v) {
  if (v === '' || v === null || v === undefined) {
    if (row.direction === 'CREDIT') row.amount = ''
    return
  }
  row.direction = 'CREDIT'
  row.amount = v
}
const maker = computed(() => store.user?.real_name || store.user?.username || '—')
/** 金额取数。只敲了一个负号的时候 ``Number('-')`` 是 NaN，
 *  NaN 一进合计，整张凭证的差额就变成 NaN，连「不平」都说不出来了。 */
const num = (v) => {
  if (v === '' || v === null || v === undefined) return 0
  const n = Number(v)
  return Number.isFinite(n) ? n : 0
}
/** 合计是算出来的数，落格前补上角分：分录行写的是「2180504.00」，
 *  合计格却只剩「2180504」，同一张纸上两种写法，看着像少了两位。 */
const money2 = (v) => (num(v) ? num(v).toFixed(2) : '')
/** 带符号的合计：哪一侧的数就加在哪一侧，负数照减。
 *  「=」补出来的差额必须用这一对算 —— 显示用的合计把负数搬去了对面，
 *  拿它当差额会把补出来的数翻一倍。 */
const signedTotal = computed(() => {
  let debit = 0, credit = 0
  for (const e of form.entries) {
    if (e.direction === 'DEBIT') debit += num(e.amount)
    else credit += num(e.amount)
  }
  return { debit, credit }
})
/**
 * 凭证上印的合计：**负数算到对面去**。
 *
 *  贷方红字（负数）本质上是借方，借方红字本质上是贷方 —— 这是红字的本意，
 *  不是「贷方减了 300」。所以两栏都只把正数加进来：
 *      借方合计 = 借方正数 + 贷方红字（绝对值）
 *      贷方合计 = 贷方正数 + 借方红字（绝对值）
 *  这么算，两栏都摆得下正数，一眼比得出大小；否则会出现「借 300 / 贷 −300，
 *  差额 600」这种看着像凭空多出 600 的合计。
 *
 *  判平用它也准：借方合计 = 贷方合计 ⟺ 带符号借方 = 带符号贷方，两种口径完全等价。
 */
const total = computed(() => {
  let debit = 0, credit = 0
  for (const e of form.entries) {
    const v = num(e.amount)
    if (v > 0) {
      if (e.direction === 'DEBIT') debit += v
      else credit += v
    } else if (v < 0) {
      // 红字：写到对面去
      if (e.direction === 'DEBIT') credit += -v
      else debit += -v
    }
  }
  return { debit, credit }
})
/** 平不平。**红字凭证（借贷两边都是负的）也是平的** —— 它冲的是老账，
 *  照样该存、该记账。所以判据是「两边相等且不是全零」，不是「大于 0」。
 *  「不是全零」看带符号那一对：正负相抵成 0 的凭证（借100 / 借-100 / 贷50 / 贷-50）
 *  看着是平的，其实什么也没记，后端也拒，不能让它自动存下去。 */
const balanced = computed(() =>
  total.value.debit === total.value.credit && signedTotal.value.debit !== 0)
// 「这张凭证现在长什么样」：自动保存靠它判断有没有改动。没动过就不重存。
const entrySig = computed(() => JSON.stringify([
  form.voucher_date, form.voucher_no, form.attachments, form.remark,
  form.entries.map((e) => [e.subject_id, e.direction, e.amount, e.summary])
]))
const lastSaved = ref('')      // 上一次落库的签名
const savedAt = ref('')        // 上一次落库的时间，页脚上给个准信
// 一分钱都还没录的时候别急着报红字「差额 0.00」，那是噪音不是提示
const blank = computed(() => total.value.debit === 0 && total.value.credit === 0)

function subjectText(id) {
  const s = subjects.value.find((x) => x.id === id)
  return s ? `${s.code} ${s.name}` : ''
}
function onSubject(row) {
  if (!row.summary) {
    const n = form.entries.filter((e) => e.summary).length
    if (n === 0) {
      const first = form.entries.find((e) => e.subject_id)
      if (first) first.summary = ''
    }
  }
}
// 每行的格子都要能拿到实例：回车要挨着往下走，得能直接落笔到某一行某一格
const subjectRefs = new Map()
const sumRefs = new Map()
const amtRefs = new Map()
function setSubjectRef(el, idx) {
  if (el) subjectRefs.set(idx, el)
  else subjectRefs.delete(idx)
}
function setSumRef(el, idx) {
  if (el) sumRefs.set(idx, el)
  else sumRefs.delete(idx)
}
function setAmtRef(el, idx, side) {
  const k = `${idx}:${side}`
  if (el) amtRefs.set(k, el)
  else amtRefs.delete(k)
}

/** 光标落到第 i 行的摘要格：整格选中，接着写就是接着改，不用来回挪光标 */
function focusSum(i) {
  nextTick(() => {
    const el = sumRefs.get(i)
    el?.focus()
    el?.select()
  })
}

/** 点哪个格就选中哪个格的字：改内容整句重打，省得先按住退格键一路退到头 */
function selectText(e) {
  e?.target?.select?.()
}

/**
 * 金额格里按 ↑ / ↓：落到**相邻行的同一侧**。
 *
 *  上下键在单行输入框里本来什么也干不了（框里没有第二行），
 *  而连续录凭证时人正是一路顺着金额列往下敲 —— 让上下键带着手走到
 *  上一行/下一行的借方（或贷方）格，接着就是选中 + 重打，跟鼠标点过去一样。
 * 到头了就停住：凭空加一行不算「上下移动」。
 */
function moveRow(i, delta, side) {
  const at = i + delta
  if (at < 0 || at >= form.entries.length) return
  if (blocked(at)) return warnBlocked(at)
  const row = form.entries[at]
  activeRow.value = row
  nextTick(() => amtRefs.get(`${at}:${side}`)?.focus?.())
}

/**
 * 在金额格里敲「=」：这一格直接补成**让整张凭证平掉**的那个数。
 *
 * 一行只有一个数：往贷方格写数，这一行就从借方变成贷方，原来那笔借方金额
 * **一起没了**（格子只有一个方向）。所以补多少不能拿「当前贷方合计 + 全部借方」
 * 来算 —— 那样会把这一行自己那笔也算两遍。
 *
 *   写进去的方向跟这一行现在一样 → 新的合计 = 原来的合计 − 这一行 + 新数
 *   方向会翻（借方有数的行往贷方写） → 借方少掉这一行，贷方多出这个数
 *
 * 借 100 + 借 200 + 借 200，在第三行贷方敲「=」补 300：这一行从「借 200」
 * 变成「贷 300」，合计正好是借 300 / 贷 300。补 500 的话这一行会被顶成
 * 贷 500，账上就凭空少了那 200 —— 是这张凭证最常见的一处笔误。
 *
 * 已经平了就不用补，直接说一声；补出来是负数就红字（冲销）。
 */
function autoBalance(i, side) {
  const row = form.entries[i]
  if (!row) return
  // opposite = 写进去这一格的**对面**合计；same = 写进去这一格的**本侧**合计
  const opposite = side === 'd' ? signedTotal.value.credit : signedTotal.value.debit
  const same = side === 'd' ? signedTotal.value.debit : signedTotal.value.credit
  const mine = num(row.amount)
  // 这一行现在的方向。空格也记着方向（默认借方），但金额是 0，翻不翻都一样
  const mineSide = row.direction === 'CREDIT' ? 'c' : 'd'
  const gap = opposite - same + (mineSide === side ? mine : -mine)
  const need = Number(gap.toFixed(2))

  // 补出来是 0：这一格该空着。空着本来就没错，说一声已经平了；
  // 不空着（前面有一笔同方向的数）就得清掉 —— 留着它反而不平。
  if (need === 0) {
    if (!mine) return ElMessage.info('这张凭证已经平了，不用补')
    if (side === 'd') setDebit(row, '')
    else setCredit(row, '')
    return ElMessage.info('这一格得空着，填着反而不平')
  }
  // 已经是这个数：什么都不用做，别再报一遍「已按差额补上 300」
  if (mineSide === side && mine === need) return

  if (side === 'd') setDebit(row, String(need))
  else setCredit(row, String(need))
  activeRow.value = row
  ElMessage.success(need < 0
    ? `已按差额补上 ${fmt(need)}（红字冲销）`
    : `已按差额补上 ${fmt(need)}`)
}

/**
 * 回车往哪儿走：一行里「摘要 → 会计科目 → 借方 → 贷方」从左到右，
 * 一行走完落到**下一行的摘要**，接着才是再下一行 —— 像一张纸从上往下写，
 * 而不是第一行写完直接蹦到最后一行去。
 * 这一行没写完（差科目或差金额）就不往下走：中间跳过一条不写完整的分录去写下一行，
 * 存出来是一张谁也看不懂的纸。走到最后一行最后一个格子，才谈得上「加一行」。
 */
function moveNext(i, from) {
  const row = form.entries[i]
  if (!row) return
  activeRow.value = row
  // 落笔都要等这一轮渲染过去：刚选中的科目，那一行的金额格这会儿还挂着 disabled，
  // 这时候 focus() 打在一个禁用的输入框上是打不进去的
  const jump = (fn) => nextTick(fn)
  if (from === 'sum') return jump(() => subjectRefs.get(i)?.focusInput?.())
  if (from === 'subj') return jump(() => amtRefs.get(`${i}:d`)?.focus?.())
  if (from === 'd') return jump(() => amtRefs.get(`${i}:c`)?.focus?.())

  // 这一行到头了。先看它成没成条：没成条就停在原地把话说明白
  const hint = incompleteHint(row)
  if (hint) return ElMessage.warning(hint)
  // 已经是最下面一行 → 试着加一行
  if (i + 1 >= form.entries.length) return addRow(row)

  const nextRow = form.entries[i + 1]
  // 往下一行接着写的多半还是同一件事（凭证还没平），摘要跟着上一行走，
  // 省得每加一行分录都要把同一句话再敲一遍。已经写过的摘要不动。
  if (row.summary && !nextRow.summary && !balanced.value) nextRow.summary = row.summary
  activeRow.value = nextRow
  focusSum(i + 1)
}

/**
 * 一条分录最基本的两个元素：会计科目 + 金额。少一个就不算分录，
 * 也就没资格接着往下写 —— 半条分录一路传下去，最后存出来的是一张谁也看不懂的纸。
 * 返回该说哪句提示；成了返回 null。
 */
function incompleteHint(row) {
  if (!row) return null
  if (!row.subject_id) return '先给这一行选一个会计科目'
  if (badSubject(row)) return rowProblemText(row)
  if (!num(row.amount)) return '先给这一行填上金额'
  return null
}

/**
 * 在绿着的这一行上面 / 下面插一行。
 *
 * 两个按钮一个在上一行、一个在下一行，方向就是按钮所在的位置，不该再靠
 * 「这个 ⊕ 是往下插的」去记 —— 一个记不住的图标比没有图标更糟。
 *
 * 新行沿用这一行的方向和摘要：拆行本来就是把「同一句话有好几笔」摊成几行，
 * 方向得跟着走（连续三行借方），摘要也跟着走（同一句话抄三遍不如抄一次）。
 */
function insertRowAt(offset) {
  const at = Math.max(0, form.entries.indexOf(greenRow.value)) + offset
  const cur = form.entries[at - offset]
  const hint = incompleteHint(cur)
  if (hint) return ElMessage.warning(hint)
  form.entries.splice(at, 0, {
    subject_id: null,
    direction: cur ? cur.direction : 'DEBIT',
    amount: '',
    summary: cur ? cur.summary : ''
  })
  activeRow.value = form.entries[at]
  focusSum(at)
}
const insertRowAbove = () => insertRowAt(0)
const insertRowBelow = () => insertRowAt(1)

function removeRow(i) {
  if (readonly.value || !canDeleteRow.value) return
  form.entries.splice(i, 1)
}

/** 至少留 4 行空着：多数凭证两三条就录完了，但行得摆够，
 *  录第三条第四条时眼睛不用临时去找地方。 */
function ensureRows(n) {
  // 补出来的空行是「等着填」的，不跟着上一行抄摘要 —— 抄了反而像是这张凭证真有四条
  while (form.entries.length < n) {
    form.entries.push({ subject_id: null, direction: 'DEBIT', amount: '', summary: '' })
  }
}

/** 在最后加一行。落笔在**新那行的摘要**上，不去自动弹科目面板 ——
 *  回车是一路往下写的，弹面板会把光标从摘要上抢走。 */
function addRow(row) {
  // 没指定行就是从「＋」来的：看的是最后一行
  const hint = incompleteHint(row ?? form.entries[form.entries.length - 1])
  if (hint) return ElMessage.warning(hint)
  // 自动补一行方向相反的分录，符合连续录入的习惯
  const last = form.entries[form.entries.length - 1]
  form.entries.push({
    subject_id: null,
    direction: last ? (last.direction === 'DEBIT' ? 'CREDIT' : 'DEBIT') : 'DEBIT',
    amount: '',
    summary: last ? last.summary : ''
  })
  activeRow.value = form.entries[form.entries.length - 1]
  focusSum(form.entries.length - 1)
}

/**
 * 从列表带过来的筛选条件（kw / min / max）。
 * 凭证页自己不知道账是怎么筛的，全靠列表页把条件写在 URL 上带过来 ——
 * 进来的路上带着，返回的时候原样捎回去，前一张后一张时也一路带着。
 */
const listFilter = computed(() => {
  const q = route.query || {}
  const out = {}
  if (q.kw) out.keyword = String(q.kw)
  if (q.min !== undefined && q.min !== '') out.min_amount = q.min
  if (q.max !== undefined && q.max !== '') out.max_amount = q.max
  return out
})
/** 有没有跟着列表来。空对象 = 不知道从哪张列表进来的，按同期间翻。 */
const hasListFilter = computed(() => Object.keys(listFilter.value).length > 0)

/** 返回：回**列表页**，并把筛选条件原样带回去。
 *  早先用的是 `$router.back()` —— 那是从哪儿来回哪儿去：从「新增凭证」跳进来
 *  就回新增凭证，从打印预览跳进来就回打印预览，压根不保证回到列表；
 *  就算碰巧回到了列表，条件也已经丢了，用户得重新筛一遍。 */
function backToList() {
  router.push({ name: 'vouchers', query: route.query || {} })
}

/** 前一张 / 后一张：翻的是列表那个结果集，条件一路带着，跳到头/尾时按钮自己禁用。 */
function goNeighbour(which) {
  const id = which === 'prev' ? prevId.value : nextId.value
  if (!id) return
  router.push({ name: 'voucher-edit', params: { id }, query: route.query || {} })
}

/** 插入凭证：接着当前这张开一张新的，日期沿用当前凭证的日期。
 *
 *  `after` 是参照凭证的 id —— 新页拿它去后端问「这张占哪个号、谁要顺位」，
 *  顺延跟建凭证在同一个事务里做。所以这里只跳页、什么也不改：
 *  一个格子都没填就退出，后头那叠号原封不动；空凭证占号、把别人的号顶走才是真事故。 */
function insertVoucher() {
  if (!voucherId.value) return
  router.push({
    name: 'voucher-new',
    query: { date: form.voucher_date, after: voucherId.value }
  })
}

/** 问后端要插入位的预填值。拿不到就退回普通新开一张 —— 别把整页卡住。 */
async function loadInsertPreview(afterId) {
  try {
    return await api.insertPreview(store.bookId, afterId)
  } catch {
    return null
  }
}

/** 增加凭证：不受当前凭证日期影响，按所在期间的默认日期另起一张。 */
function addVoucher() {
  router.push({ name: 'voucher-new' })
}

/** 拉上一张/下一张的导航信息。
 *
 *  seq 传进来是为了丢弃过期响应：load() 里那次必须传，而保存成功后的那次
 *  （保存不走 load()，没有并发）可以省略 —— 所以只在**显式给了**的情况下
 *  才判代次，免得把保存路径也卡住。
 */
async function loadNeighbour(seq) {
  if (seq !== undefined && stale(seq)) return
  if (!voucherId.value) {
    prevId.value = null
    nextId.value = null
    neighbourScope.value = ''
    return
  }
  const myId = voucherId.value
  const r = await api.neighbour(store.bookId, myId, listFilter.value)
  if ((seq !== undefined && stale(seq)) || voucherId.value !== myId) return
  prevId.value = r.prev_id
  nextId.value = r.next_id
  neighbourScope.value = r.scope === 'filtered'
    ? `筛选结果里的第 ${r.index + 1} / ${r.total} 张`
    : ''
}

/** load() 的代次标记。
 *
 *  切换凭证只改 route.params.id，组件不重新挂载，load() 会被反复触发；
 *  而 load() 内部要先 await loadSubjects() 再 await getVoucher()，两次调用
 *  天然会交错。于是「先发的请求后到」时，那份**旧**响应会
 *  Object.assign(form, ...) 覆盖掉新凭证的表单 —— 页面上显示的是 A 凭证的
 *  编号配着 B 凭证的分录，而且没有任何报错。
 *
 *  每次 load() 领一个自增代次；任何 await 之后先确认代次仍是自己的，
 *  不是就直接返回，不碰任何状态。没有 AbortController 是因为这里有 4 个
 *  分散的 await 点，逐个判代次比在每处都塞 signal 更容易看漏 ——
 *  看漏一处就是一个静默覆盖。
 */
let loadSeq = 0
/** 响应回来后确认「我还是最新那一次」，不是就说明该丢弃了。 */
function stale(seq) { return seq !== loadSeq }

async function load() {
  const seq = ++loadSeq
  const myId = Number(route.params.id || 0)
  // 先认下路由里的 id，再去拉科目：换凭证的那一瞬间，工具栏不该还挂着上一张的打印预览
  voucherId.value = myId
  savedAt.value = ''
  insertCtx.value = null
  await loadSubjects()
  if (stale(seq)) return
  subjectRefs.clear()
  if (myId) {
    const v = await api.getVoucher(store.bookId, myId)
    if (stale(seq)) return
    // 双重确认：代次之外再核对 id，防的是「代次对上了但期间切换了」
    if (Number(route.params.id || 0) !== myId) return
    voucherPeriod.value = store.periods.find((p) => p.id === v.period_id) || null
    Object.assign(form, {
      voucher_no: v.voucher_no, voucher_date: v.voucher_date, status: v.status,
      attachments: v.attachments, remark: v.remark,
      entries: v.entries.map((e) => ({
        subject_id: e.subject_id, direction: e.direction, amount: e.amount, summary: e.summary
      }))
    })
    ensureRows(4)
    lastSaved.value = entrySig.value      // 刚打开的这张就是「已保存」的样子，别一进来就重存一遍
    await loadNeighbour(seq)
  } else {
    // 新开一张：凭证号和日期都先替人填好。
    // 号码问后端要（`/vouchers/next`），因为它跟落库时用的是同一个发号函数 ——
    // 自己照着列表猜一个「最大号 +1」会跟真正发出来的号对不上，白填一次。
    // 日期取这个期间**最后一张凭证**的日期：接着上一张往下记才对。
    // 之前填的是「今天，28 号封顶」，月底录 12 月时日期会一天天往回退，看着像倒退。
    // 「插入凭证」带 ?date= 的以它为准 —— 插在谁后面就沿用谁的日期。
    // 「插入凭证」走的是另一个预填源：插在谁后面，这张就拿谁的号，
    // 后面的号整体顺延。顺延真的发生是在保存那一刻（同一个事务里），
    // 这里只是先把号码和「谁要顺位」摆在页面上，让人看清再动手。
    const after = Number(route.query.after || 0)
    if (after) {
      const ip = await loadInsertPreview(after)
      if (stale(seq)) return
      insertCtx.value = ip
      if (ip) {
        form.voucher_no = ip.voucher_no || ''
        form.voucher_date = ip.voucher_date || route.query.date || defaultDate()
      } else {
        const n = await loadNextDefaults()
        if (stale(seq)) return
        form.voucher_no = n.voucher_no || ''
        form.voucher_date = route.query.date || n.voucher_date || defaultDate()
      }
    } else {
      const n = await loadNextDefaults()
      if (stale(seq)) return
      form.voucher_no = n.voucher_no || ''
      form.voucher_date = route.query.date || n.voucher_date || defaultDate()
    }
    voucherPeriod.value = null   // 新凭证还没保存，期间以保存时按日期落库为准
    ensureRows(4)
    prevId.value = null
    nextId.value = null
    neighbourScope.value = ''
  }
}

/** 问后端要「新开一张」的预填值。拿不到就退回空号 + 本地日期，别把整页卡住。 */
async function loadNextDefaults() {
  try {
    return await api.nextVoucher(store.bookId, store.periodId)
  } catch {
    return {}
  }
}

function defaultDate() {
  const p = store.currentPeriod
  if (!p) return new Date().toISOString().slice(0, 10)
  const day = new Date().getDate()
  const d = String(Math.min(day, 28)).padStart(2, '0')
  return `${p.year}-${String(p.month).padStart(2, '0')}-${d}`
}

/**
 * 落库。`quiet` 是自动保存那条路：成功不弹提示、不跳页面 —— 人还在这一张凭证上接着录，
 * 一存完就把人踢回列表，等于逼着人每次都重新找回来。
 * 失败一定要说：自动保存失败而页面上一声不吭，人是会信的。
 */
async function save({ quiet = false } = {}) {
  if (readonly.value) {
    if (!quiet) {
      ElMessage.warning(posted.value ? '已记账凭证不能修改，请先反记账' : '当前期间已结账，不能修改凭证')
    }
    return
  }
  const entries = form.entries
    .filter((e) => e.subject_id && num(e.amount))
    .map((e) => ({
      subject_id: e.subject_id,
      direction: e.direction,
      amount: String(num(e.amount)),
      summary: e.summary || ''
    }))
  if (!entries.length) return quiet || ElMessage.warning('请至少填写一条分录')
  // 非末级科目这一条不分 quiet：自动保存要是闷声不响，人只会以为存好了，
  // 而这笔分录其实根本没落库 —— 等发现时已经按它记了好几天的账。
  if (nonLeafRows.value.length) {
    ElMessage.error(rowProblemText(nonLeafRows.value[0]))
    return
  }
  if (!balanced.value) return quiet || ElMessage.error('借贷不平衡，无法保存')

  const payload = {
    voucher_date: form.voucher_date, entries, remark: form.remark,
    attachments: form.attachments, voucher_no: form.voucher_no || null,
    // 只有新凭证 + 还在插入模式里才带：更新老凭证时带了会把号再顺位一次
    insert_after: !voucherId.value && insertCtx.value ? insertCtx.value.anchor_id : null
  }
  // 记下**发出去的那一版**：请求在路上时人可能还在改，回来后要把水位定在发出去的那版，
  // 否则这几秒里敲的东西会被当成「已保存」丢掉
  const sentSig = entrySig.value
  const wasNew = !voucherId.value
  saving.value = true
  try {
    const saved = wasNew
      ? await api.createVoucher(store.bookId, payload)
      : await api.updateVoucher(store.bookId, voucherId.value, payload)
    voucherId.value = saved.id
    lastSaved.value = sentSig
    savedAt.value = new Date().toTimeString().slice(0, 5)
    if (wasNew) {
      // 地址栏换成 /vouchers/{id}：这时候刷新页面不会变成另一张空凭证。
      // 只动地址栏、**不走 router** —— voucher-new 和 voucher-edit 是两条路由记录，
      // router.replace 会把组件整个重挂一遍：正在填的表单、光标、绿行全没了。
      // replaceState 不触发 hashchange，router 内部状态原样保留。
      history.replaceState(history.state, '', `#/vouchers/${saved.id}`)
      voucherPeriod.value = store.periods.find((p) => p.id === saved.period_id) || null
      // 顺位已经落库，这张从「待插入」变成普通凭证：说明条收起来，
      // 再改内容走的是 update，不会把后面那叠号再顶一次
      insertCtx.value = null
      await loadNeighbour()
    }
    if (!quiet) {
      ElMessage.success('已保存')
      router.push('/vouchers')
    }
  } finally {
    saving.value = false
  }
}

/**
 * 借贷一平就自动落库。半张凭证加一个刷新 = 白填十分钟，会计一天要录几十张，
 * 没人会记得每张都去按一下保存。改动停手 0.7 秒再存，免得每敲一个数字都打一趟接口。
 */
const AUTO_SAVE_DELAY = 700
let autoSaveTimer = null
async function autoSave() {
  // 上一趟还在路上：改完的东西还没发出去，等它回来再走一趟，别丢
  if (saving.value) {
    autoSaveTimer = setTimeout(autoSave, AUTO_SAVE_DELAY)
    return
  }
  await save({ quiet: true })
}
watch([entrySig, balanced], () => {
  clearTimeout(autoSaveTimer)
  if (readonly.value || !balanced.value) return
  if (entrySig.value === lastSaved.value) return    // 跟刚落库的那一版一模一样，不用再存
  autoSaveTimer = setTimeout(autoSave, AUTO_SAVE_DELAY)
})
onBeforeUnmount(() => clearTimeout(autoSaveTimer))

onMounted(async () => {
  if (!store.periodId) return
  await load()
})
// 前一张/后一张只是换 route.params，组件不会重新挂载 —— 必须盯着 id
watch(() => [store.bookId, store.periodId, route.params.id], load)
</script>

<style scoped>
.voucher-head { display: flex; gap: 24px; align-items: center; margin-bottom: 12px; font-size: 13px; }


/* 分录格。左右各空出一条：左边 48px 放每行的 ⊕ / ＋，右边 52px 放每行的 ⊗。
   它们都绝对定位在**自己那一行**的外头，哪一行变绿才冒出来 ——
   所以不用为按钮单开一列，表格右边那条竖线也就没有了。

   这里头有个反直觉的点：两条带子**不能自己** visibility: hidden。
   带子是绝对定位在行外面的（right:100% / left:100%），本身没有背景，看着像空的；
   但它一藏就跟着不接鼠标了 —— 光标停在行左侧那条空带子上时，命中的是
   .entry-wrap 而不是任何一行，hoverRow 置空，绿行就退回「上一次录入的那行」
   纹丝不动。用户看到的就是：鼠标明明压着第二行绿着第一行。

   所以：**带子永远接鼠标，藏的只是里面的按钮**。带子没背景，藏不藏都一个样；
   按钮用 visibility 藏，既不出现在无障碍树里，也收不到点击。 */
.entry-wrap { display: flex; align-items: stretch; padding-left: 48px; padding-right: 52px; }
.vg-act {
  position: absolute;
  right: 100%;
  /* top: -1px：绝对定位是相对 padding box 算的，行顶上那条 1px border 在它上头。
     不探这一像素，光标压在这一行的上边线上，两条带子就都收不到 */
  top: -1px;
  bottom: 0;
  width: 48px;                 /* 正好等于 entry-wrap 的左内边距：整条带子都是这一行的 */
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.vg-act > * { visibility: hidden; }   /* 没变绿的行，这一对按钮不占地方 */
.vg-row.is-on .vg-act > * { visibility: visible; }
.vg-act .el-button + .el-button { margin-left: 0; }
.g-ins { color: #fff; background: #67c23a; border-color: #67c23a; font-weight: 700; }
.g-ins:hover { background: #85ce61; border-color: #85ce61; color: #fff; }
.g-add { color: #fff; background: #409eff; border-color: #409eff; font-weight: 700; }
.g-add:hover { background: #66b1ff; border-color: #66b1ff; color: #fff; }

/* 凭证格：列宽决定一切，所以整张表就是一个 grid。
   1px 的 gap 当格子线 —— 不用给每个单元格描边，行高也就随 grid-auto-rows 走。 */
.vgrid {
  flex: 1;
  min-width: 0;
  --vg-line: #c0c4cc;
  --vg-active: #f2faf2;
  --vg-total: #fffbf7;
  /* 列宽只写这一份：表头照它摆，分录行也照同一份摆，两边永远逐像素对齐 */
  --vg-cols: 56px minmax(170px, 1fr) minmax(230px, 1.4fr) minmax(210px, 1fr) minmax(210px, 1fr);
  display: grid;
  grid-template-columns: var(--vg-cols);
  grid-template-rows: 48px 40px;
  grid-auto-rows: 68px;          /* 行高 68px：抬头一眼看全，不用眯着眼找格子 */
  column-gap: 1px;               /* 竖线走列间距 */
  row-gap: 0;                    /* 横线走行的 border-top —— 见下方 .vg-row 的说明 */
  background: var(--vg-line);
  border: 1px solid var(--vg-line);
  border-radius: 2px;
  /* 这里原来有 overflow: hidden：会把贴在行左边的 ⊕ / ＋ 一起裁掉 */
}

/* 一行一个盒子。原来是 display: contents —— 行没有盒子，鼠标扫过去整行不会变色，
   也没有地方挂「这一行的动作按钮」。现在行自己摆格子（列宽跟外层同一份），
   行高、竖线跟以前一模一样，多出来的只是行有了自己的命中区域。 */
.vg-row {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: var(--vg-cols);
  gap: 1px;
  background: var(--vg-line);
  position: relative;
}
/* 行与行之间那 1px 缝原本属于外层 grid —— 光标正好卡在缝上，两行都收不到鼠标，
   绿行就赖在原地不动，看着就是「鼠标压着第二行，第一行绿着」。
   现在把行间距收成 0，横线改由每一行自己的 border-top 画：行与行严丝合缝，
   每一个像素都有主人，谁也不跟谁抢。

   （走过弯路：先试过 ::before 往外探一像素，透明盒子盖在格子上，
   格子里的输入框就再也收不到点击；又试过负边距让两行各探一像素，
   缝是补上了，但缝上那一像素归谁全看谁在 DOM 里靠后，不该有的运气。） */
.vg-row:not(.vg-head) {
  box-sizing: border-box;
  border-top: 1px solid var(--vg-line);
}
.vg-head { display: contents; }   /* 表头那两行照旧由外层 grid 摆 */
/* 行间距收成 0 了，表头里那两行之间得自己补一条，不然「借方金额」和
   「亿千百十万」会贴在一起 */
.th-amt { border-bottom: 1px solid var(--vg-line); }
.vg-cell { background: #fff; min-width: 0; min-height: 0; display: flex; align-items: center; }

/* 表头：序号/摘要/会计科目竖跨两行，借贷金额上排方向、下排亿…分 */
.th { justify-content: center; font-weight: 600; font-size: 14px; }
.th-idx, .th-sum, .th-subj { grid-row: 1 / 3; }
.th-amt { grid-row: 1; }
.th-units { grid-row: 2; }
.th-idx { grid-column: 1; } .th-sum { grid-column: 2; } .th-subj { grid-column: 3; }
.th-amt.d { grid-column: 4; } .th-amt.c { grid-column: 5; }
.th-units.d { grid-column: 4; } .th-units.c { grid-column: 5; }

/* 分录行 */
.td-idx { grid-column: 1; justify-content: center; color: var(--cwq-muted); }
.td-sum { grid-column: 2; }
.td-subj { grid-column: 3; }
/* 前面有没写完的分录时，这几格一起不开张：淡一点 + 指针变禁止，点一下说清差在哪。
   禁用的 input 收不到点击事件（浏览器压根不给它派发），所以还得把指针穿透掉，
   让点击落到格子上，提示才出得来 —— 否则点半天没反应，人只会以为页面卡了 */
.td-amt { display: block; }
.is-blocked { cursor: not-allowed; }
/* 挂到了上级科目：红框 + 红字。这一格不改掉，整张凭证都存不下去 */
.is-bad { box-shadow: inset 0 0 0 2px #f56c6c; }
.is-bad :deep(.sp-input) { color: #d93025; font-weight: 600; }
.is-blocked .cell-input { opacity: .45; }
.is-blocked :deep(.sp-input) { opacity: .45; }
.is-blocked :deep(.amt) { opacity: .42; }
.is-blocked .cell-input,
.is-blocked :deep(.sp-input),
.is-blocked :deep(.amt-input) { pointer-events: none; }
.td-amt.d { grid-column: 4; } .td-amt.c { grid-column: 5; }

/* 合计行：合计中文写在左边，金额跟分录一样落进同一套格子里 */
.vg-total .td-sumcell { grid-column: 1 / 4; padding: 0 16px; font-weight: 600; font-size: 14px; }
.vg-total .td-total { display: block; }
.vg-total .td-total.d { grid-column: 4; } .vg-total .td-total.c { grid-column: 5; }
.vg-total .vg-cell { background: var(--vg-total); }

/* 当前录入行：淡绿底，眼睛跟着手走。鼠标扫过的那一行同样算当前行 */
.vg-row.is-on .vg-cell { background: var(--vg-active); }
/* 删除 ⊗：挂在这一行的右边外面，跟左边那对 ⊕ / ＋ 一个待遇 */
.vg-del {
  position: absolute;
  left: 100%;
  top: -1px;                   /* 同 .vg-act */
  bottom: 0;
  width: 52px;                 /* 同上，正好铺满右侧留白 */
  display: flex;
  align-items: center;
  justify-content: center;
}
.vg-del > * { visibility: hidden; }    /* 同上：带子接鼠标，⊗ 本体才藏 */
.vg-row.is-on .vg-del > * { visibility: visible; }

/* 格里就是一个普通输入框，不带边框，才像一张填在纸上的凭证 */
.cell-input {
  width: 100%;
  height: 100%;
  border: 0;
  outline: none;
  background: transparent;
  padding: 0 12px;
  font: inherit;
  font-size: 14px;
  color: var(--cwq-text);
}
.cell-input::placeholder { color: #c0c4cc; }

/* 自动保存的报平安：绿，但比「借贷平衡」淡一点 —— 平衡是状态，保存是动作 */
.saved-tag { color: #529b2e; font-size: 12px; }

.row-del { font-size: 17px; color: #d93025; cursor: pointer; }
.row-del.is-off { color: #dcdfe6; cursor: not-allowed; }

.vfoot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-top: 14px;
  font-size: 13px;
}
.maker { display: inline-flex; align-items: center; gap: 6px; color: #606266; }
.foot-right { display: inline-flex; align-items: baseline; gap: 22px; }
.foot-right b { font-variant-numeric: tabular-nums; color: var(--cwq-text); }

.w-date { width: 90px; } .w-no { width: 80px; } .w-amt { width: 110px; text-align: right; }
.print-area { padding: 8px; }
.print-title { text-align: center; font-size: 18px; font-weight: 700; letter-spacing: 6px; margin-bottom: 14px; }
.print-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.print-table td { border: 1px solid #dcdfe6; padding: 7px 8px; }
.print-total td { font-weight: 700; background: #fafafa; }
/* 红字冲销：负数用红字打出来，跟账上写法一致 */
.print-table td.is-red { color: #d93025; }
</style>
