<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2 class="page-title">期初余额</h2>
        <div class="muted">
          {{ store.currentBook?.name }} · {{ data.period }} 期初余额
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

    <el-alert v-if="!data.editable" type="warning" :closable="false" class="mb">
      {{ data.reason }}。如需调整，先在「期末结账」反结账，或直接改该期间的凭证。
    </el-alert>

    <div class="card mb" :class="balanced ? 'ok' : 'bad'">
      <div class="check">
        <span class="tag">{{ balanced ? '✓ 借贷平衡' : '✗ 借贷不平' }}</span>
        <span>借方合计 <b class="money">{{ money(dr) }}</b></span>
        <span>贷方合计 <b class="money">{{ money(cr) }}</b></span>
        <span v-if="!balanced" class="diff">差额 <b class="money">{{ money(dr - cr) }}</b></span>
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
                :model-value="Number(subTotal(s)).toFixed(2)"
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'
import BookInitDialog from '@/components/BookInitDialog.vue'
import OpeningImportDialog from '@/components/OpeningImportDialog.vue'
import { downloadFile } from '@/api/print'
import { amountInputText, amountToNumber, sanitizeAmount, sanitizeAmountDetailed } from '@/utils/amountGrid'
import { CATEGORY_LABEL } from '@/utils/subjectCode'
import { hasInit, importSummaryText, subjectsReady } from '@/utils/bookInit'

const store = useSessionStore()
const router = useRouter()
const route = useRoute()
const data = ref({ period: '', editable: false, reason: '', subjects: [] })
const amounts = reactive({})
/** 每行期初的「实际方位」。科目属性只是通常方向，允许逐行翻转 —— 应收账款出现
 *  贷方余额、应交税费为负都是真实业务，强按属性纠正会把余额方向洗掉。 */
const sides = reactive({})
/** 正在敲的那一格（原文）。跟 amounts 分开是因为净化会把刚敲下、还没跟数字的
 *  小数点收掉 —— 那是**正在敲的半句话**，收掉的话「1234.5」会打成「12345」。
 *  跟凭证页 AmountGrid 里 amountInputText 一个道理（同一套净化、同一套规矩）。 */
const drafts = reactive({})
const dirty = ref(false)
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
const initBlocking = computed(() => hasInit(store.initOf(store.bookId)) && !subjectsReady(store.initOf(store.bookId)))

/** 向导里做完第 1 步：刷新状态并把表格重读一遍 —— 科目变了，这张表已经不是刚才那张了 */
async function onInitUpdated() {
  await store.loadInit(store.bookId)
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

const money = (v) => (Number(v || 0)).toLocaleString('zh-CN', { minimumFractionDigits: 2 })

/* ── 金额净化（审计 P2-20）─────────────────────────────────────────
 * 这一页以前直接 `Number(框里的字)`：脏输入（「abc」「1。5」「-」「１00」）
 * 一律变 NaN，而 `NaN !== 0` 是**真** —— 于是这一行既进了合计（NaN 把整列
 * 合计污染成 NaN）又进了保存名单，最后报「已保存 N 条」，
 * 库里却存着 0。人以为存了 100，其实存了 0，期初表从此对不上总账。
 *
 * 现在净化走**凭证金额格那一份**（utils/amountGrid.js）：全角数字、
 * 中文句点、各种减号都折回半角，只有「超出 亿…分 的位次」才截，
 * 而且截了要说出来。别在这一页另写一套净化 —— 两页两套规矩，
 * 迟早在某个角落各说各话。 */

/** 框里该显示的：正在敲的那串原文（可能带着一个刚敲下的小数点），否则是净化后的值 */
const shown = (id) => (drafts[id] !== undefined ? drafts[id] : (amounts[id] ?? ''))

function typeAmount(s, typed) {
  const { value, notices } = sanitizeAmountDetailed(typed)
  amounts[s.id] = value
  drafts[s.id] = amountInputText(typed, value)
  for (const n of notices) {
    ElMessage.warning(`${s.code} ${s.name}：${n.text}`, { grouping: true, duration: 2600 })
  }
}

/**
 * 一行期初的数额（不带符号）。
 *
 *  先净化、再解析 —— 而且「认不出来的串」一律当空格子，绝不产生 NaN：
 *  NaN 有两个坏脾气，`NaN !== 0` 为真（会被「哪些行有余额」的过滤选中），
 *  又能把整列合计污染掉 —— 脏输入那一行既进了保存名单、存进去的却是 0，
 *  人还得到一句「已保存 N 条」（审计 P2-20）。
 * amountToNumber 就是那份净化 + 「认不出当空格子」的解析，与凭证金额格同源。
 */
function amountOf(s) {
  return amountToNumber(amounts[s.id])
}

const filledCount = computed(
  () => data.value.subjects.filter((s) => s.is_leaf && amountOf(s) !== 0).length
)

/** 一行的带符号金额：借正贷负，与后端存储口径一致。 */
function signedOf(s) {
  const v = Math.abs(amountOf(s))
  if (!v) return 0
  return sides[s.id] === '贷' ? -v : v
}

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
      let total = 0
      for (const c of children) total += out.get(c.id) || 0
      out.set(s.id, total)
      stack.pop()
      continue
    }
    // 末级记自己的数；**非末级却没有任何子节点**仍然是 0（与老口径一致）
    out.set(s.id, s.is_leaf ? signedOf(s) : 0)
    stack.pop()
  }
  return out
})

const subTotal = (s) => subTotals.value.get(s.id) ?? 0

/** 上级的方位跟着这一支的合计走：正的在借方、负的在贷方；
 *  加起来正好 0 就没方位可言，退回科目本身的自然方向。 */
function sideOf(s) {
  if (s.is_leaf) return sides[s.id]
  const t = subTotal(s)
  return t ? (t > 0 ? '借' : '贷') : s.natural_side
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

/** 借贷合计：两边加起来必须是 0。 */
const totals = computed(() => {
  let dr = 0, cr = 0
  for (const s of data.value.subjects) {
    if (!s.is_leaf) continue
    const signed = signedOf(s)
    if (signed > 0) dr += signed; else if (signed < 0) cr += -signed
  }
  return { dr, cr }
})
const dr = computed(() => totals.value.dr)
const cr = computed(() => totals.value.cr)
const balanced = computed(() => Math.abs(dr.value - cr.value) < 0.005)

function rowsOf(cat) {
  const kw = keyword.value.trim().toLowerCase()
  return data.value.subjects.filter(
    (s) => s.category === cat && (!kw || s.code.includes(kw) || s.name.toLowerCase().includes(kw))
  )
}

async function load() {
  if (!store.bookId) return router.push('/books')
  // 跟顶栏那一期走：期初是「这一期开始时账上该有多少钱」，问的是某个月，
  // 不是问整个账套。以前这个页面永远拿启用期间，2027 年一个凭证都没有，
  // 屏幕上也还是 2026-01 那份锁死的数，看着像坏了。
  data.value = await api.getOpenings(store.bookId, store.periodId)
  for (const k of Object.keys(amounts)) delete amounts[k]
  for (const k of Object.keys(sides)) delete sides[k]
  for (const k of Object.keys(drafts)) delete drafts[k]
  for (const s of data.value.subjects) {
    // side 来自库里实际存的方位；没录过时退回科目自然方向
    sides[s.id] = Number(s.signed) !== 0 ? s.side : s.natural_side
    // 存回来的也过一遍净化：库里那份是别人（或上一次的手滑）写进去的，
    // 页面不认脏串 —— 认了就等于把脏东西又带回来让人「照着改」。
    amounts[s.id] = s.is_leaf ? sanitizeAmount(s.amount) : ''
  }
  dirty.value = false
}

async function save() {
  const rows = data.value.subjects
    // 过滤条件跟合计用的是**同一个** amountOf：以前这里写的是
    // `Number(框里的字) !== 0`，NaN 也算「有余额」，于是脏输入的行被算进
    // 「已保存 N 条」，实际存进去的是 0。现在 amountOf 认不出的串一律当空格子，
    // 报出来的条数与真正写进库的条数必然一致。
    .filter((s) => s.is_leaf && amountOf(s) !== 0)
    .map((s) => ({ subject_id: s.id, amount: String(signedOf(s)) }))
  if (!balanced.value) {
    return ElMessage.error(`借贷不平：差额 ${money(dr.value - cr.value)}，不能保存`)
  }
  saving.value = true
  try {
    const res = await api.setOpenings(store.bookId, rows, store.periodId)
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
 *  填表口径照旧（sides 取 display_side、金额过一遍 sanitizeAmount），
 *  这两处不是顺手写的：方向在文件里已经定过，照搬才能保住反方向余额；
 *  文件里的数字不净化就可能把这一行变成算不出数的那一行。
 */
function applyImport(res) {
  const ok = (res.rows || []).filter((r) => !r.error)
  let filled = 0
  for (const r of ok) {
    if (!r.subject_id) continue
    const signed = Number(r.signed ?? 0)
    sides[r.subject_id] = r.display_side || (signed < 0 ? '贷' : '借')
    amounts[r.subject_id] = sanitizeAmount(String(Math.abs(signed || Number(r.amount || 0))))
    delete drafts[r.subject_id]
    filled++
  }
  dirty.value = true
  // 计数话术在导入弹窗的完成页也有一份（能一直看着），这里再报一句短的，
  // 因为用户这时候的注意力在这张表上，不在刚刚关掉的弹窗上
  ElMessage.success(`${importSummaryText(res)}，请检查后点「保存期初余额」落库`)
  if (filled === 0) ElMessage.warning('一条都没填进来，表格没有被改动')
}

function exportXlsx() {
  downloadFile(`/books/${store.bookId}/openings.xlsx`,
    store.periodId ? { period_id: store.periodId } : {}, '期初余额.xlsx')
}
function downloadTpl() {
  // 模板就是「导出当前」——科目已经在库里，导出来填金额即可
  exportXlsx()
}

watch(() => store.bookId, load)
// 顶栏换月/换年，这一页也得跟着换 —— 换个空月份进来还锁着，用户只会以为坏了
watch(() => store.periodId, load)
onMounted(async () => {
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
  await store.loadInit(store.bookId)
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
