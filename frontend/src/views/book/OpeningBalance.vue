<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2 class="page-title">期初余额</h2>
        <div class="muted">
          {{ store.currentBook?.name }} · {{ data.year }} 年度期初余额
          <template v-if="!data.editable">（{{ data.reason }}）</template>
        </div>
      </div>
      <div class="head-actions">
        <el-button @click="downloadTpl">下载空白模板</el-button>
        <el-button @click="exportXlsx">导出当前</el-button>
        <el-button :disabled="initBlocking" @click="importVisible = true">
          从 Excel 导入
        </el-button>
        <el-button type="primary" :loading="saving"
                   :disabled="!data.editable || !dirty || !balanced || initBlocking"
                   @click="save">保存期初余额</el-button>
      </div>
    </div>

    <!-- 第 1 步没做完时，期初根本无处可挂：导入进来的余额找不到科目可落，
         手录也是一样的结果。拦在这里比让用户填完 40 行再吃后端一句 409 好 ——
         以前这页完全不提示，用户是在保存失败之后才知道账套没初始化。 -->
    <el-alert v-if="initBlocking" type="error" :closable="false" show-icon class="mb">
      <template #title>这个账套还没完成初始化第 1 步</template>
      <div>期初余额要挂在科目上，科目还没定下来就无法导入也无法保存。请先完成第 1 步。</div>
      <el-button size="small" type="danger" class="mt" @click="initVisible = true">
        去完成第 1 步
      </el-button>
    </el-alert>

    <!-- 拉数失败（或年份还没定）时**这一页是空的**。
         以前 load() 没有 try/catch，失败后 data 留着上一份 —— 顶栏的年份已经切走，
         屏幕上摆着旧年份的数，保存时还把旧数据存进了新年份（审查 P2-2）。
         这种「数字对不上」最难自查：人只看见自己录进去的那些格子。 -->
    <el-alert v-if="loadError" type="error" :closable="false" show-icon class="mb">
      <template #title>期初余额没有加载出来</template>
      <div>{{ loadError }}。这一页现在是空的 —— 留着上一份的话，标题是新年份、
        内容却是旧年份的数，保存时会把旧数据存进新年份。请重试。</div>
    </el-alert>

    <el-alert v-if="!data.editable && data.reason && !loadError" type="warning" :closable="false" class="mb">
      {{ data.reason }}。如需调整，先处理该年度的凭证：在「期末结账」里反结账，
      或把该年度已记账的凭证改掉。
    </el-alert>

    <div class="card mb" :class="balanced ? 'ok' : 'bad'">
      <div class="check">
        <span class="tag">{{ balanced ? '借贷平衡 ✓' : '借贷不平' }}</span>
        <span>借方合计 <b class="money">{{ money(dr) }}</b></span>
        <span>贷方合计 <b class="money">{{ money(cr) }}</b></span>
        <span v-if="!balanced" class="diff">{{ diffTag }}</span>
        <span class="muted">共 {{ filledCount }} 个科目有余额</span>
      </div>
    </div>

    <el-input v-model="keyword" placeholder="按编码或名称筛选" clearable style="width:240px" class="mb" />

    <div v-for="cat in CATEGORIES" :key="cat.key" class="card mb" v-show="rowsOf(cat.key).length">
      <div class="cat-title">{{ cat.label }}</div>
      <table class="grid">
        <thead>
          <tr>
            <th class="code">科目编码</th>
            <th>科目名称</th>
            <th class="dir">方向</th>
            <th class="amt">期初余额</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in rowsOf(cat.key)" :key="s.id" :class="{ 'is-parent': !s.is_leaf }">
            <td class="code" :style="{ paddingLeft: 8 + (s.level - 1) * 16 + 'px' }">{{ s.code }}</td>
            <td>{{ s.name }}</td>
            <td class="dir">
              <!-- 上级科目也摆成同款标签：这一列看着该是齐的，
                   偏偏上一级是一行灰字，一眼扫下去像少了东西。
                   差别只留在能不能点 —— 上级的方位是下级合计出来的，没有可翻的 -->
              <el-tag
                :type="sideOf(s) === '借' ? 'primary' : 'warning'"
                size="small"
                :class="{ flip: s.is_leaf }"
                :role="s.is_leaf ? 'button' : undefined"
                :tabindex="s.is_leaf ? 0 : undefined"
                @click="flip(s)"
                @keydown.enter.prevent="flip(s)"
                @keydown.space.prevent="flip(s)"
                :title="s.is_leaf ? flipHint(s) : '上级科目：方位和金额都是下面这些明细的合计'"
              >{{ sideOf(s) }}</el-tag>
              <el-tooltip v-if="reverse(s)" content="与科目默认方向相反" placement="top">
                <span class="rev">反</span>
              </el-tooltip>
            </td>
            <td class="amt">
              <el-input
                v-if="s.is_leaf"
                :model-value="shown(s.id)"
                :disabled="!data.editable"
                size="small"
                class="money"
                align="right"
                @update:model-value="(v) => typeAmount(s, v)"
                @blur="dirty = true"
              />
              <!-- 上级不能录（期初只能记在末级上），但也不能空着：
                   空格子看着像没录，而「银行存款这一行到底装了多少」恰恰是这张表最该答的。

                   这里还是**同一个输入框**、只是置灰，不是另外画一个盒子：
                   这一列一列看下来必须是同一种格子 —— 自个儿拿 div 仿一个，
                   高度、内边距、千分位、小数位总会差一点，看着就像另一种东西。
                   写法也跟着下面一样：两位小数、不加千分位。 -->
              <el-input
                v-else
                :model-value="centsToText(subTotal(s))"
                disabled
                size="small"
                class="money sub-total"
                align="right"
              />
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <BookInitDialog v-model="initVisible" @updated="onInitUpdated" />
    <OpeningImportDialog v-model="importVisible" @imported="applyImport" />
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'
import BookInitDialog from '@/components/BookInitDialog.vue'
import OpeningImportDialog from '@/components/OpeningImportDialog.vue'
import { downloadFile } from '@/api/print'
import {
  absMoneyText, amountInputText, centsToText, moneyCents, sanitizeAmountDetailed,
} from '@/utils/amountGrid'
import { moneyText as moneyTextOf } from '@/utils/format'
import { CATEGORY_LABEL } from '@/utils/subjectCode'
import { hasInit, importSummaryText, subjectsReady } from '@/utils/bookInit'
import { importScopeMismatch } from '@/utils/openingImport'

const store = useSessionStore()
const router = useRouter()
const route = useRoute()
const data = ref({ period: '', year: '', editable: false, reason: '', subjects: [] })
const amounts = reactive({})
/** 每行期初的「实际方位」。科目属性只是通常方向，允许逐行翻转 —— 应收账款出现
 *  贷方余额、应交税费为负都是真实业务，强按属性纠正会把余额方向洗掉。 */
const sides = reactive({})
/** 正在敲的那一格（原文）。跟 amounts 分开是因为净化会把刚敲下、还没跟数字的
 *  小数点收掉 —— 那是**正在敲的半句话**，收掉的话「1234.5」会打成「12345」。
 *  跟凭证页 AmountGrid 里 amountInputText 一个道理（同一套净化、同一套规矩）。 */
const drafts = reactive({})
const dirty = ref(false)
/** 这一页没有数据可看的原因（拉数失败 / 年份还没定）。空着比摆着上一份强。 */
const loadError = ref('')
const saving = ref(false)
const keyword = ref('')
const importVisible = ref(false)
const initVisible = ref(false)

/**
 * 第 1 步没做完时这一页不许动。
 *
 *  显示上**必须有 init 才拦**（hasInit）：拿不到状态不等于「没做」，
 *  直接当成没做会对着一个后端还没上线/请求失败的账套弹一条红色拦截条 —— 那是替后端下结论。
 *  真去导入/保存时后端还有一道 409 兜着，两边对不上时以后端为准。
 */
/**
 * 拦截条：这一年的第 1 步（确定科目）还没做，就不让录这一年的期初。
 *
 * **按年度取状态**：初始化状态是**年度**的属性（后端迁移 0032）——
 * 2026 年做完了不代表 2027 年做完了。用错年度的话，2027 年会在
 * 「一张科目都没有」的情况下被放进来，期初整批挂空。
 */
const initOfYear = computed(() => store.initOf(store.bookId, store.currentYear))
const initBlocking = computed(() => hasInit(initOfYear.value) && !subjectsReady(initOfYear.value))

/** 向导里做完第 1 步：刷新状态并把表格重读一遍 —— 科目变了，这张表已经不是刚才那张了 */
async function onInitUpdated() {
  await store.loadInit(store.bookId, store.currentYear)
  await load()
}

/**
 * 分组渲染的类别，顺序即会计恒等式的顺序。
 *
 * 文案一律取自 `@/utils/subjectCode` 的 CATEGORY_LABEL —— 原来这里另存了
 * 一份自己的 EQUITY 标签，于是同一个类别在不同页面显示不同。现在只有一份
 * 来源，取的是简称「权益」（准则里的「所有者权益」只出现在报表行次上，
 * 那里才是它该在的地方，见 subjectCode.js 的说明）。
 *
 * COST 不在这张表里：期初录入按四类分组，成本类目前走不到这里
 * （`v-show` 会把空的分组藏掉，所以留着也无妨，但那样会让人以为
 * 成本类期初是被支持的 —— 与其含糊，不如明确排除）。
 */
const CATEGORIES = ['ASSET', 'LIABILITY', 'EQUITY', 'PROFIT']
  .map((key) => ({ key, label: CATEGORY_LABEL[key] }))

/**
 * 合计栏的金额文字：带千分位。
 *
 * 以前是 `(Number(v || 0)).toLocaleString(...)` —— 浮点。18 位整数在浮点里
 * 已经不是原来那个数了（999999999999999999.99 → 1000000000000000000），
 * 于是「借方合计」显示的会比存的少。现在统一走整数分（BigInt），一分不差。
 */
const money = (cents) => moneyText(cents)

/* ── 金额净化（审计 P2-20）─────────────────────────────────────────
 * 这一页以前直接 `Number(框里的字)`：脏输入（「abc」「1。5」「-」「１00」）
 * 一律变 NaN，而 `NaN !== 0` 是**真** —— 于是这一行既进了合计（NaN 把整列
 * 合计污染成 NaN）又进了保存名单，最后报「已保存 N 条」，
 * 库里却存着 0。人以为存了 100，其实存了 0，期初表从此对不上总账。
 *
 * 现在净化走**凭证金额格那一份**（utils/amountGrid.js）：全角数字、
 * 中文句点、各种减号都折回半角；小数只到分；**整数位一个都不截**
 * （格子只有 9 个整数位是**显示**的事，超出模型上限 18 位才拒绝，
 * 见下面那段「金额：一律整数分」的说明）。
 * 别在这一页另写一套净化 —— 两页两套规矩，迟早在某个角落各说各话。 */

// ---- 金额：一律整数分（判平、合计、保存共用这一份量化结果）----
//
// 与凭证页**同一套口径**：借贷是否平 = 整数分**完全相等**，没有容差。
// 期初页以前是「浮点 + 0.005 容差」，凭证页是「整数分严格相等」——
// 同一件事两套算法（审查 P3-3）。现在两边都是整数分严格相等，
// 提示文案也统一成「借贷平衡 ✓ / 借方大 X / 贷方大 X」。
//
// 为什么这一页用 BigInt（凭证页用 Number）：期初能读到 **18 位整数**
// （后端 Numeric(20,2) 的上限），折成分就是 20 位，早就越过了 Number 的
// 安全整数范围 —— 浮点会把 999999999999999999.99 变成 1000000000000000000，
// 那正是「读回来的金额被改小」的另一种形态。BigInt 一路定点，18 位也不掉精度。
//
// **读回来的数一个字符都不动**（审查 P0-1）：load() 直接用后端给的字符串，
// 存回去时用同一串拼出带方向的字符串 —— 全程不经过浮点、不经过截断。

/** 一行期初的数额（不带符号），单位：分。 */
function centsOfRow(s) {
  return moneyCents(amounts[s.id])
}

/** 一行期初的带符号金额（借正贷负），单位：分。 */
function signedCents(s) {
  const c = centsOfRow(s)
  if (!c) return 0n
  return sides[s.id] === '贷' ? -c : c
}

/**
 * 一行期初的带符号金额，**字符串形态**（存回去的就是它）。
 *
 * 存库走字符串而不是浮点：浮点在 17 位以后会把 12345678901234567
 * 变成 12345678901234568 —— 那一分就是另一笔钱。
 * 往返一次就把余额改小，是这一页以前最贵的一个错（审查 P0-1）。
 */
function signedText(s) {
  const abs = absMoneyText(amounts[s.id])
  if (!abs) return '0'
  return sides[s.id] === '贷' ? `-${abs}` : abs
}

/** 借贷合计（单位：分）：两边各加各的，方向相反的余额自己抵掉。 */
function totalsCents(subjects) {
  let dr = 0n, cr = 0n
  for (const s of subjects) {
    if (!s.is_leaf) continue
    const c = signedCents(s)
    if (c > 0n) dr += c
    else if (c < 0n) cr -= c
  }
  return { dr, cr }
}

/**
 * 合计栏的金额文字：带千分位。定点出来的，一分不差。
 *
 * **格式规则住在 utils/format.js 的 moneyText（整数分 → 文本）** ——
 * 期初导入预览弹窗（components/OpeningImportDialog.vue）用的是**同一份**。
 * 以前这两个界面各写各的，同一个业务对象能显示成两个数
 * （1.005 在导入预览里是 "1.005"，在期初页是 "1.00"）。
 * 这里只留一个名字给页面用，实现交给那一份。
 */
function moneyText(cents) {
  return moneyTextOf(cents)
}
// ---- 金额段结束（tests/opening_balance.test.mjs 从这里头抠实现跑）----

/** 框里该显示的：正在敲的那串原文（可能带着一个刚敲下的小数点），否则是净化后的值 */
const shown = (id) => (drafts[id] !== undefined ? drafts[id] : (amounts[id] ?? ''))

function typeAmount(s, typed) {
  const { value, notices } = sanitizeAmountDetailed(typed)
  // 超出**数据模型上限**（18 位整数，后端也存不下）：**不收**。
  // 这一类不是「丢了几位」，是「这个数根本不认」——
  // 悄悄截成 18 位会让人以为那就是这笔钱。原值一个字都不动。
  if (notices.some((n) => n.code === 'MONEY_LIMIT')) {
    for (const n of notices) {
      ElMessage.error(`${s.code} ${s.name}：${n.text}`, { grouping: true, duration: 4000 })
    }
    return
  }
  amounts[s.id] = value
  drafts[s.id] = amountInputText(typed, value)
  for (const n of notices) {
    ElMessage.warning(`${s.code} ${s.name}：${n.text}`, { grouping: true, duration: 2600 })
  }
}

/**
 * 一行期初的数额（不带符号），单位：**分**。
 *
 *  定点解析（moneyCents），不是浮点 —— 「认不出来的串」一律当空格子，
 *  绝不产生 NaN：NaN 有两个坏脾气，`NaN !== 0` 为真（会被「哪些行有余额」
 *  的过滤选中），又能把整列合计污染掉 —— 脏输入那一行既进了保存名单、
 *  存进去的却是 0，人还得到一句「已保存 N 条」（审计 P2-20）。
 *
 *  定点还有第二层要紧：18 位整数折成分是 20 位，浮点早就超出安全范围了
 *  （`String(999999999999999999.99)` = "1000000000000000000"）。
 */
function amountOf(s) {
  return centsOfRow(s)
}

const filledCount = computed(
  () => data.value.subjects.filter((s) => s.is_leaf && amountOf(s) !== 0n).length
)

/**
 * 库里**已经存着**的期初条数（这一年度）。
 *
 *  从服务端给的那份数算（``data.subjects[].signed``），**不是**从 ``amounts`` 算 ——
 *  ``amounts`` 是表格上**正在编辑**的值（导入会改它），而这里要回答的是
 *  「保存会抹掉库里多少条快照」，那是**库的现状**，不能被编辑中的值覆盖。
 *
 *  只有末级算数：期初只记在末级上（``set_openings`` 拒收非末级），
 * 上级那一行是下级的合计，存不进去。
 */
const storedCount = computed(
  () => data.value.subjects.filter((s) => s.is_leaf && moneyCents(s.signed) !== 0n).length
)

/**
 * 上级科目的期初 = 它这一支所有末级科目的合计（借正贷负，逐层往下加）。
 *
 *  期初只能记在末级上，上级录不了也存不进去；但一行空白摆在表里，
 *  「银行存款」到底装了多少钱就成了问不出来的事 —— 而这恰恰是翻这张表的目的。
 *  合计按带符号加，方向相反的余额会自己抵掉，不会两行都往上堆。
 */
/**
 * 科目表是**平的**（每行带 parent_id），树要自己搭 —— 一次搭好，按 parent_id 存。
 *
 * 以前 childrenOf() 每次都把全表 filter 一遍，而 subTotal() 在递归里逐个调它：
 * 每算一个后代节点就扫一遍全表，一行渲染 O(N)，整张表 O(N²)。而 amounts/drafts
 * 是 reactive 的，打字时这套东西一直在渲染路径上跑。
 * 写法照抄 SubjectPicker.vue 里那份 byId：computed 里建一次 Map，之后都是 O(1) 取。
 */
const childrenByParent = computed(() => {
  const m = new Map()
  for (const s of data.value.subjects) {
    const list = m.get(s.parent_id)
    if (list) list.push(s)
    else m.set(s.parent_id, [s])
  }
  return m
})

/**
 * 全部合计：**一次自底向上遍历**算完，按 id 缓存。
 *
 * 用显式栈做后序遍历，而不是递归，也不靠「后端一定把父排在子前面」——
 * 顺序一旦变了，父先落定就会把下级漏掉，算出来的上级合计是 0，而页面上看不出来。
 * 依赖 amounts/sides，所以改一个格子只重算一趟 O(N)，不是每行各递归一次。
 */
const subTotals = computed(() => {
  const subs = data.value.subjects
  const kids = childrenByParent.value
  const out = new Map()
  // 全部科目都进栈，不预先分根：谁先算到全看顺序，而 out 本身保证
  // 「非末级一定等所有子节点都落定才收自己」，所以后端把父子排成什么样都对。
  const stack = [...subs]
  while (stack.length) {
    const s = stack[stack.length - 1]
    if (out.has(s.id)) { stack.pop(); continue }
    // is_leaf 先判，与老口径同序：末级只认自己的数，哪怕数据里它底下还挂着行
    // （自相矛盾时以末级为准，别把上一版「先看有没有孩子」的顺序偷换成另一套口径）
    const children = s.is_leaf ? [] : (kids.get(s.id) || [])
    if (children.length) {
      let pushed = false
      for (const c of children) if (!out.has(c.id)) { stack.push(c); pushed = true }
      if (pushed) continue                       // 回来时子节点都算完了，再收自己
      let total = 0n
      for (const c of children) total += out.get(c.id) || 0n
      out.set(s.id, total)
      stack.pop()
      continue
    }
    // 末级记自己的数；**非末级却没有任何子节点**仍然是 0（与老口径一致）
    out.set(s.id, s.is_leaf ? signedCents(s) : 0n)
    stack.pop()
  }
  return out
})

const subTotal = (s) => subTotals.value.get(s.id) ?? 0n

/** 上级的方位跟着这一支的合计走：正的在借方、负的在贷方；
 *  加起来正好 0 就没方位可言，退回科目本身的自然方向。 */
function sideOf(s) {
  if (s.is_leaf) return sides[s.id]
  const t = subTotal(s)
  return t ? (t > 0n ? '借' : '贷') : s.natural_side
}

const reverse = (s) => s.is_leaf && sides[s.id] !== s.natural_side
const flipHint = (s) => `点击切换为${sides[s.id] === '借' ? '贷' : '借'}（科目默认 ${s.natural_side}）`
// 这一格**以前根本点不动**：标签上写着「点击切换为…」、样式里也给了 pointer 光标，
// 模板却没挂 @click，翻转函数写在脚本里从没被调用过 ——
// 反方向余额（应收账款挂客户预收）就没法录，而界面一直在暗示它可以。

function flip(s) {
  if (!data.value.editable || !s.is_leaf) return
  sides[s.id] = sides[s.id] === '借' ? '贷' : '借'
  dirty.value = true
}

/**
 * 借贷合计：两边加起来必须是 0。
 *
 *  整数分（BigInt）上算，判平是**完全相等** —— 与凭证页同一套口径
 *  （审查 P3-3：以前这一页是「浮点 + 0.005 容差」，凭证页是「整数分严格相等」，
 *  同一件事两套算法）。差一分就是不平：后端 Decimal 也是这么判的。
 */
const totals = computed(() => totalsCents(data.value.subjects))
const dr = computed(() => totals.value.dr)
const cr = computed(() => totals.value.cr)
const balanced = computed(() => dr.value === cr.value)

/**
 *  差额要说清是**哪一边大** —— 与凭证页的页脚同一套说法
 *  （「借方大 100.10」/「贷方大 100.10」）。
 *  只写「差额 90.00」，人还得回头数一遍才知道自己错在哪。
 */
const diffTag = computed(() => {
  const d = dr.value - cr.value
  return d > 0n ? `借方大 ${moneyText(d)}` : `贷方大 ${moneyText(-d)}`
})

function rowsOf(cat) {
  const kw = keyword.value.trim().toLowerCase()
  return data.value.subjects.filter(
    (s) => s.category === cat && (!kw || s.code.includes(kw) || s.name.toLowerCase().includes(kw))
  )
}

/**
 * 拉数失败时把这一页**清空**。
 *
 * 以前 load() 没有 try/catch：请求一抛错，data.value 留着**上一份**，
 * 而顶栏的年份已经切走了 —— 屏幕上摆着 2026 的数，标题写着 2027，
 * 点「保存期初余额」还把旧数据存进了新年份（审查 P2-2）。
 * 这种「数字对不上」最难自查：人只看见自己录进去的那些格子。
 */
function clearData() {
  data.value = {
    period: '', year: store.currentYear ?? '', editable: false, reason: '', subjects: []
  }
  for (const k of Object.keys(amounts)) delete amounts[k]
  for (const k of Object.keys(sides)) delete sides[k]
  for (const k of Object.keys(drafts)) delete drafts[k]
  dirty.value = false
}

async function load() {
  if (!store.bookId) return router.push('/books')
  // 跟顶栏的**年度**走：期初是「这一年开始时账上该有多少钱」，问的是一年，
  // 不是问某个月、更不是问整个账套。切到 2026 年的哪个月都是同一份数；
  // 换一年（2027）才是另一份。以前这个页面永远拿启用期间，2027 年一个凭证
  // 都没有，屏幕上也还是 2026-01 那份锁死的数，看着像坏了。
  //
  // **年份还没定下来就别问**：不传年份问回去的是「启用期间那一份」，
  // 拿回来摆在另一个年份的标题底下，比报错还糟。
  if (store.currentYear === undefined || store.currentYear === null) {
    clearData()
    loadError.value = '还没有选定年度'
    return
  }
  try {
    const res = await api.getOpenings(store.bookId, store.currentYear)
    data.value = res
    for (const k of Object.keys(amounts)) delete amounts[k]
    for (const k of Object.keys(sides)) delete sides[k]
    for (const k of Object.keys(drafts)) delete drafts[k]
    for (const s of data.value.subjects) {
      // side 来自库里实际存的方位；没录过时退回科目自然方向。
      // 「有没有数」用定点分判断，不用浮点：18 位整数在浮点里已经不是原来那个数了。
      sides[s.id] = moneyCents(s.signed) !== 0n ? s.side : s.natural_side
      // **直接用后端给的字符串，一个字符都不动**（审查 P0-1）。
      // 以前这里过了一遍 sanitizeAmount，于是 10 位以上的合法金额被静默截成 9 位：
      // 打开这一页 → 点保存 → 库里那个数被改小，界面全程显示「借贷平衡」。
      // 放不下 11 格是**显示**的事（AmountCells 摆原值 + 角标），不是改数的理由。
      amounts[s.id] = s.is_leaf ? String(s.amount ?? '') : ''
    }
    dirty.value = false
  } catch (e) {
    // 拉数失败：**清空**这一页，绝不留着上一份数据。
    // 留着的话，屏幕上摆的是旧年份的数，标题写的是新年份 ——
    // 在那个状态下点「保存期初余额」，存的是旧数据。
    clearData()
    const detail = e?.response?.data?.detail || e?.message || '请稍后重试'
    loadError.value = detail
    ElMessage.error(`期初余额加载失败：${detail}`)
  }
}

async function save() {
  const rows = data.value.subjects
    // 过滤条件跟合计用的是**同一个** amountOf：以前这里写的是
    // `Number(框里的字) !== 0`，NaN 也算「有余额」，于是脏输入的行被算进
    // 「已保存 N 条」，实际存进去的是 0。现在 amountOf 认不出的串一律当空格子，
    // 报出来的条数与真正写进库的条数必然一致。
    .filter((s) => s.is_leaf && amountOf(s) !== 0n)
    // 存回去的就是**原值**（字符串）：往返一次不改一个字符（审查 P0-1）。
    // 浮点会在 17 位以后把 12345678901234567 变成 12345678901234568。
    .map((s) => ({ subject_id: s.id, amount: signedText(s) }))
  if (!balanced.value) {
    return ElMessage.error(`借贷不平：${diffTag.value}，不能保存`)
  }
  // ---- 保存是**整年替换**，空表会把手里的期初全抹掉 --------------------
  //
  // 后端 ``set_openings`` 的语义是「这一年的期初 = 我发过去的这些行」：
  // 发过去的行里没有的科目，**库里的那条快照会被删掉**。
  // 平时这没问题（表格上看得见每一行）；但**表格上一行数都没有**的时候，
  // 人看见的是「空表」，看不见的是「库里有 N 条」。
  //
  // 生产实测（2026-10-31）：导入因年度对不上没填进来 → 表格全是 0 →
  // 点保存 → 后端把这一年的期初**清空**，报一句「已保存 0 条」。
  // 于是「刷新 → 清空 → 保存 → 抹掉」成了一个闭环，钱再也进不去。
  //
  // 所以这里拦一道：空表要保存，先把「会抹掉多少」说清楚，让人确认。
  if (!rows.length && storedCount.value > 0) {
    try {
      await ElMessageBox.confirm(
        `这一页上一行数都没有，但库里 ${data.value.year} 年度已经有 `
        + `${storedCount.value} 条期初余额。保存会把它们**全部清空**。`
        + '确定要这样保存吗？',
        '保存会清空这一年的期初',
        { type: 'warning', confirmButtonText: '确定清空', cancelButtonText: '返回检查' }
      )
    } catch {
      return
    }
  }
  saving.value = true
  try {
    const res = await api.setOpenings(store.bookId, rows, store.currentYear)
    // 后端在同一次事务里把初始化第 2 步标成了完成，顺手把最新的 init 带回来。
    // **必须写回 store**：第 2 步是在**另一个页面**（账套管理）上显示的，
    // 不写回去的话，用户从这儿回到账套管理，向导和卡片都还在说「还没做」——
    // 而那一步他明明刚刚做完了。
    store.markInit(store.bookId, res?.init)
    ElMessage.success(`已保存 ${rows.length} 条期初余额`)
    await load()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

/**
 * 导入弹窗确认后把结果填进表格。
 *
 *  **这里只填表，不落库** —— 落库是下面那个「保存期初余额」按钮的事。
 *  用户还可以在表格里逐行改，改完再存；弹窗直接写库就把这最后一道人工检查拿掉了。
 *
 *  填表口径（sides 取 display_side、金额过一遍净化）不是顺手写的：
 *  方向在文件里已经定过，照搬才能保住反方向余额；
 *  文件里的数字不净化就可能把这一行变成算不出数的那一行。
 *
 *  但净化**不许截断整数位**（审查 P0-1）：文件里写的是多少就是多少，
 *  放不下 11 格是显示的事。超容量只**提示**（哪几行超出了），
 *  值照原样填进去 —— 悄悄截小的话，用户看不出是哪一处被改了。
 */
function applyImport(res) {
  // ---- 先核对「这份解析结果是不是我这一页的」再填 ----------------------
  //
  // 解析结果里的每一行都指着**解析时那张科目表**的科目 id。年度对不上时，
  // 这些 id 在本页面的科目里**一个都找不到** —— 填进去等于把数写给了不存在的行，
  // 页面仍然是 0，而人看到的只是「导入没生效」。
  //
  // 生产实测（2026-10-31，账套「泉州劲松珠宝有限公司」）：请求没带年度，
  // 后端按 2026 年认科目，页面是 2023 年那一张 → 表格全是 0 →
  // 刷新清空 → 保存把那 0 存进去 → 死循环，钱一分没进去。
  //
  // 所以这里**当场拒绝**，并且把「为什么」说清楚：年度对不上就报年度，
  // id 对不上就报 id。**宁可报错，也不能让页面装出「已经导进来了」的样子。**
  const parsed = (res.rows || []).filter((r) => !r.error && r.subject_id)
  const mismatch = importScopeMismatch({
    parsedYear: res?.year,
    pageYear: data.value.year,
    parsedIds: parsed.map((r) => r.subject_id),
    onScreenIds: data.value.subjects.map((s) => s.id),
  })
  if (mismatch) {
    ElMessage.error(mismatch.text)
    return
  }
  const ok = parsed
  let filled = 0
  const over = []
  for (const r of ok) {
    if (!r.subject_id) continue
    // 后端给的是**字符串**（`str(Decimal)`），别再过一遍浮点 ——
    // 浮点会把 18 位金额改掉（那正是 P0-1 那一类错）。
    const raw = String(r.signed ?? r.amount ?? '0')
    const neg = raw.trim().startsWith('-')
    sides[r.subject_id] = r.display_side || (neg ? '贷' : '借')
    // 导入的值也得走净化（文件里的脏串不能直接进表），但**不许截断整数位**：
    // 截了就等于把文件里的数改小了，而用户看不出是哪一处。
    const { value, notices } = sanitizeAmountDetailed(absMoneyText(raw))
    if (notices.some((n) => n.code === 'MONEY_LIMIT')) {
      over.push(`${r.code || r.name || r.subject_id}（超出可表示范围，未填入）`)
      continue
    }
    for (const n of notices) {
      if (n.code === 'INT_OVERFLOW') over.push(`${r.code || r.name || r.subject_id}（${n.text}）`)
    }
    amounts[r.subject_id] = value
    delete drafts[r.subject_id]
    filled++
  }
  if (over.length) {
    ElMessage.warning(`有 ${over.length} 行超出可表示范围：${over.slice(0, 3).join('；')}` +
      `${over.length > 3 ? ' 等' : ''}。放得下的按原值填入，请核对后再保存`, { duration: 6000 })
  }
  dirty.value = true
  // 计数话术在导入弹窗的完成页也有一份（能一直看着），这里再报一句短的，
  // 因为用户这时候的注意力在这张表上，不在刚刚关掉的弹窗上
  ElMessage.success(`${importSummaryText(res)}，请检查后点「保存期初余额」落库`)
  if (filled === 0) ElMessage.warning('一条都没填进来，表格没有被改动')
}

function exportXlsx() {
  downloadFile(`/books/${store.bookId}/openings.xlsx`,
    store.currentYear ? { year: store.currentYear } : {}, '期初余额.xlsx')
}
function downloadTpl() {
  // 模板就是「导出当前」——科目已经在库里，导出来填金额即可
  exportXlsx()
}

/**
 * 关页签 / 刷新前问一句。
 *
 * **导入的数只活在表格里**（落库是「保存期初余额」那一步）——
 * 这一条设计是刻意的：弹窗直接写库就把最后那道人工检查拿掉了。
 * 但代价是「刷新一下，刚导入的东西就没了」，而这正是用户最容易做的一个动作
 * （数字看着不对，想刷一下看看是不是没生效）。
 *
 * 浏览器自己的提示语救不了场（它是英文、也不说清丢的是什么），
 * 所以这里先把「有东西要存」这件事算清楚，丢的时候至少问一句。
 *
 * **什么时候算「有东西要存」**：表格改过（``dirty``），**而且**
 * ① 表格上有数，或者 ② 库里本来有数。
 * 只翻了个方向、金额还是 0 的那种改动**不算** —— 提示了也是白提示。
 */
function hasUnsavedChanges() {
  if (!dirty.value) return false
  return filledCount.value > 0 || storedCount.value > 0
}

function onBeforeUnload(e) {
  if (!hasUnsavedChanges()) return
  e.preventDefault()
  e.returnValue = ''
}

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
})

// 页面内换路由（去别的账、去别的模块）也是同一件事：表格上的数会没。
// 取消确认框会 reject（「cancel」），不接住就是一个未处理的 rejection。
onBeforeRouteLeave(() => {
  if (!hasUnsavedChanges()) return true
  return ElMessageBox.confirm(
    '这一页的期初余额还没保存，离开就会丢掉（导入进来的数也只是填在表里）。仍然离开？',
    '期初余额未保存',
    { type: 'warning', confirmButtonText: '仍然离开', cancelButtonText: '留下继续录' }
  ).then(() => true).catch(() => false)
})

/**
 * 换账套 / 换年度时，表格上没保存的数会被**换掉**。
 *
 *  这一页是「编辑」页面：导入进来的数只活在表格里（落库是「保存期初余额」那一步）。
 * 换账套或换年度都要重新拉数 —— 拉回来的是新数据，旧数据就没了。
 * 拦不住（store 里的账套是全局状态，不是这一页的），但**至少要说一声**：
 * 悄无声息地丢掉几十行刚录的期初，比报错更让人火大。
 */
function warnDiscarded() {
  if (!hasUnsavedChanges()) return
  ElMessage.warning('这一页还有没保存的期初余额，换账套 / 换年度后它们就没了')
}

watch(() => store.bookId, () => { warnDiscarded(); load() })
// 顶栏换**年**，这一页才跟着换。换成另一年就是另一份期初（2027 年的起点），
// 换月份这一页纹丝不动 —— 期初是年度口径的，月份对它没有意义。
watch(() => store.currentYear, () => { warnDiscarded(); load() })
onMounted(async () => {
  window.addEventListener('beforeunload', onBeforeUnload)
  // 初始化向导第 2 步是**跳到这里**再导的（向导里没有这张表）：
  // 它带着 ?import=1 过来，把导入弹窗直接弹出来。
  // 消费掉就把 query 换掉 —— 留着的话每次刷新都会再弹一次，
  // 而用户刷新多半是因为他以为没点上。replace 而不是 push：不留历史记录，
  // 浏览器「后退」回得来就不会又弹一次。
  if (route.query.import === '1') {
    importVisible.value = true
    router.replace({ path: '/openings' })
  }
  // 账套列表行里自带 init，但可能旧了；拉一份准的再决定要不要拦这一页
  await store.loadInit(store.bookId, store.currentYear)
  load()
})
</script>

<style scoped>
.page-title { margin: 0 0 2px; font-size: 18px; }
.head-actions { display: flex; gap: 8px; align-items: center; }
.mb { margin-bottom: 12px; }
.mt { margin-top: 8px; }
.check { display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }
.check .tag { font-weight: 700; }
.check b { font-size: 15px; }
.diff, .bad b { color: var(--cwq-danger); }
.ok .tag { color: var(--cwq-ok); }
.cat-title { font-weight: 700; margin-bottom: 8px; }
.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
.grid th, .grid td { border-bottom: 1px solid var(--cwq-border); padding: 5px 8px; }
.grid th { background: #f7f8fa; font-weight: 600; text-align: left; }
.grid .code { width: 130px; color: var(--cwq-muted); }
.grid .dir { width: 88px; text-align: center; }
.grid .dir .flip { cursor: pointer; user-select: none; }
.grid .dir .rev { margin-left: 4px; font-size: 11px; color: var(--cwq-danger); }
.grid .amt { width: 220px; }
/* 上级科目的小计跟别的格子长得一模一样，只把字加粗一点 ——
   「这是下级加出来的」靠粗体说，不靠换个盒子说 */
.grid .sub-total :deep(input) { font-weight: 600; }
</style>
