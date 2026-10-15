<template>
  <div class="page book-page">
    <div class="book-page-head">
      <div class="book-page-heading">
        <h2 class="book-page-title">账套管理</h2>
        <span class="book-count">{{ store.books.length }} 个账套</span>
        <!-- 磁贴能拖这件事没有任何图标提示，不说一句就等于没有这个功能 -->
        <span v-if="store.books.length > 1" class="book-order-hint">
          拖动磁贴可排序 · Alt/⌘ + ↑↓ 也可以
        </span>
      </div>
      <!-- 谁都能建账套，建完自己就是 OWNER；管理员授权管的是别人的账套 -->
      <el-button type="primary" class="create-book-button" @click="openCreate">
        <el-icon><Plus /></el-icon>
        新建账套
      </el-button>
    </div>

    <section
      class="book-grid"
      v-loading="loading"
      aria-label="账套列表"
      @dragover.prevent
      @drop.prevent="onDrop"
    >
      <article
        v-for="(row, i) in store.books"
        :key="row.id"
        class="book-tile"
        :class="{ 'is-current': row.id === store.bookId, 'is-dragging': row.id === dragId }"
        :aria-label="`账套：${row.name}`"
        draggable="true"
        tabindex="0"
        @dragstart="onDragStart(row, $event)"
        @dragend="onDragEnd"
        @dragover.prevent="onDragOver(row, $event)"
        @keydown="onTileKeydown(row, $event)"
      >
        <div class="book-tile-head">
          <div class="book-tile-ident">
            <span class="book-tile-icon" aria-hidden="true"><el-icon><Folder /></el-icon></span>
            <div class="book-tile-title">
              <el-link class="book-name-link" type="primary" @click="switchTo(row.id, pickedYear(row))">
                {{ row.name }}
              </el-link>
              <el-tag v-if="row.id === store.bookId" size="small" type="success">当前</el-tag>
              <!-- 「没做完初始化」要抢眼：它决定这本账能不能录凭证，
                   藏在卡片角落就等于没提示（以前连这个标签都没有） -->
              <el-tag v-if="uninitialized(row)" type="warning" size="small">待初始化</el-tag>
            </div>
          </div>
          <!-- 只在已结转时提示；未结转是常态，不必挂标签占版面 -->
          <el-tag v-if="yearOf(row).carried" type="info" size="small">已结转</el-tag>
        </div>

        <div class="book-tile-meta">
          <div class="book-fact">
            <span class="book-fact-label">会计年度</span>
            <!-- 默认停在最新年度；选择其他年度会同步到顶栏 -->
            <el-select
              :model-value="pickedYear(row)"
              class="book-year-select"
              size="small"
              @change="(v) => onYearPick(row, v)"
              @click.stop
            >
              <el-option v-for="y in row.years" :key="y.year" :label="`${y.year} 年`" :value="y.year">
                <span class="year-opt">
                  <span>{{ y.year }} 年</span>
                  <el-tag v-if="y.carried" size="small" type="info">已结完</el-tag>
                </span>
              </el-option>
            </el-select>
          </div>
          <div class="book-fact book-fact-closed">
            <span class="book-fact-label">已结账</span>
            <strong :class="yearOf(row).carried ? 'ok' : ''">
              {{ yearOf(row).closed }} / {{ yearOf(row).total }} 期
            </strong>
          </div>
        </div>

        <!-- 三颗控件必须同高。以前「进入账套」和「✎」各自被 CSS 硬改成 30px，
             只有中间的「继续初始化」漏了（还挂在 size="small" 的 24px 上），
             于是一行里三颗按钮三种高度。高度统一走 .tile-action 一条规则，
             不要再单独给某颗写 height —— 漏一次就又歪了 -->
        <div class="book-tile-actions">
          <el-button type="primary" plain class="tile-action enter-book-button" @click="switchTo(row.id, pickedYear(row))">
            进入账套<el-icon><Right /></el-icon>
          </el-button>
          <el-button v-if="uninitialized(row)" class="tile-action init-book-button" @click="openInit(row)">
            继续初始化
          </el-button>
          <el-tooltip content="编辑账套" placement="top">
            <el-button circle class="tile-action edit-book-button" :aria-label="`编辑账套 ${row.name}`" @click="openEdit(row)">
              <el-icon><Edit /></el-icon>
            </el-button>
          </el-tooltip>
        </div>
      </article>

      <el-empty
        v-if="!loading && !store.books.length"
        description="还没有账套，点右上角「新建账套」自己开一个；也可以请管理员在「用户与授权」里把已有账套授权给你"
      />
    </section>

    <el-dialog v-model="visible" title="新建账套" width="520px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="公司名称" required>
          <el-input v-model="form.company_name" placeholder="如：启明物业管理有限公司" />
        </el-form-item>
        <el-form-item label="账套名称" required>
          <el-input v-model="form.name" placeholder="如：租金账套" />
        </el-form-item>
        <el-form-item label="起始年度" required>
          <el-select v-model="form.start_year" style="width: 100%">
            <el-option v-for="y in yearOptions" :key="y" :label="y + ' 年'" :value="y" />
          </el-select>
        </el-form-item>
        <el-form-item label="启用月份">
          <el-select v-model="form.start_month" style="width: 100%">
            <el-option v-for="m in 12" :key="m" :label="m + ' 月'" :value="m" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="closeCreate">关闭</el-button>
        <el-button type="primary" :loading="saving" :disabled="!canSubmit" @click="submit">
          创建账套
        </el-button>
      </template>
    </el-dialog>

    <!-- 建完账套直接弹初始化向导，不再是创建弹窗里一条可以无视的提示条：
         原来那句「下一步录入期初余额」写在创建弹窗的角落，绝大多数用户建完就关了。 -->
    <BookInitDialog v-model="initVisible" :book-id="initBookId" @updated="onInitUpdated" />

    <el-dialog v-model="editVisible" title="编辑账套" width="560px">
      <el-form :model="editForm" label-width="90px">
        <el-form-item label="单位名称" required>
          <el-input v-model="editForm.company_name" />
        </el-form-item>
        <el-form-item label="账套名称" required>
          <el-input v-model="editForm.name" />
        </el-form-item>
        <!-- 科目编码设置直接排在编辑表单里，不再另开一层弹窗：
             改的是一个字段而已，为它开第二个窗只是多点一次。
             原来是让用户手填「4-2-2-2」，填错了要等到新增科目接错号才发现。 -->
        <el-form-item label="科目编码">
          <div class="rule-block">
            <div class="rule-row">
              <span class="rule-label">科目级次：</span>
              <el-select v-model="ruleLevel" size="small" style="width: 84px">
                <el-option v-for="n in LEVELS" :key="n" :label="String(n)" :value="n"
                           :disabled="n < minLevel" />
              </el-select>
              <span v-if="minLevel > 2" class="rule-note">
                注：已用到 {{ minLevel }} 级，不能再调小
              </span>
            </div>
            <div class="rule-row">
              <span class="rule-label">编码长度：</span>
              <template v-for="(w, i) in ruleWidths" :key="i">
                <span v-if="i" class="rule-dash">-</span>
                <!-- 一级固定 4 位：1xxx 资产、5xxx 损益，动了整张科目表都认不出 -->
                <el-select :model-value="w" size="small" style="width: 66px" :disabled="i === 0"
                           @update:model-value="setWidth(i, $event)">
                  <el-option v-for="n in widthOptions(i)" :key="n" :label="String(n)" :value="n" />
                </el-select>
              </template>
            </div>
            <div class="rule-sample">示例：{{ ruleSample }}</div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!canSaveEdit" @click="submitEdit">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Edit, Folder, Plus, Right } from '@element-plus/icons-vue'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'
import BookInitDialog from '@/components/BookInitDialog.vue'
import { hasInit, initStep, STEP_DONE } from '@/utils/bookInit'
import { dropInto, moveBy, moveDelta, moveTo, orderIds, sameOrder } from '@/utils/bookOrder'

const store = useSessionStore()
const router = useRouter()
const loading = ref(false)
const saving = ref(false)
const visible = ref(false)
/** 正在初始化向导里操作的账套 */
const initVisible = ref(false)
const initBookId = ref(0)

/** 这本账的第 1 步还没做 → 卡片上挂「待初始化」+ 一个「继续初始化」。
 *  走 hasInit：拿不到 init（后端还没这字段）不等于「没做」，别凭空挂标签。 */
const uninitialized = (row) => hasInit(row.init) && initStep(row.init) !== STEP_DONE

// ---------------------------------------------------------------- 磁贴排序
//
// 用**原生 HTML5 拖拽**而不是拖拽库：卡片网格的排序逻辑只有「算出插到第几格」
// 这一件事，为此引入 vuedraggable/sortable 只是为了换一套更顺手的动画，
// 而项目至今没有这类运行时依赖，不该为一个功能破例。
//
// 拖拽计算全在 utils/bookOrder.js（纯函数，可单测），
// 这里只负责把 DOM 事件翻译成两个下标。

/** 正在被拖的那本（置灰用） */
const dragId = ref(0)
/** 拖动开始时的顺序：拖到空白处松手、或落库失败，都拿它还原 */
let rollback = null
/** 这一轮拖动是否以「drop」收尾。dragend 一定触发，drop 之后也会 ——
 *  靠这个区分「松手了，顺序算数」和「拖到空白处放掉了，什么都没发生」。 */
let landed = false

function onDragStart(row, event) {
  dragId.value = row.id
  rollback = store.books.slice()
  landed = false
  // Firefox 不设置 dataTransfer 就不给拖起来
  event.dataTransfer.effectAllowed = 'move'
  event.dataTransfer.setData('text/plain', String(row.id))
}

/**
 * 拖动中就让磁贴**让位**（实时重排），而不是等松手才跳一下。
 *
 * 让位本身就是落点提示 —— 比在两卡之间画一条插入线省事，也不用维护
 * 额外的指示器元素。这里只改 store，**不落库**：dragover 一秒钟能触发
 * 几十次，每一下都发请求的话后端和数据库会被拖拽本身刷爆。
 * 落库只发生在 drop 那一次。
 */
function onDragOver(row, event) {
  const index = store.books.findIndex((b) => b.id === row.id)
  if (index < 0 || !dragId.value) return
  const box = event.currentTarget.getBoundingClientRect()
  event.dataTransfer.dropEffect = 'move'
  // 光标在右半边 = 插到这张**后面**。少了这一支，想把 A 放到 B 后面
  // 就得先把 A 拖到 B 前面、再绕回去调一次。
  // 被指磁贴在结果里会挪到第几，由 dropInto 算（组件不碰那个 ±1）
  const next = dropInto(store.books, dragId.value, index,
    event.clientX > box.left + box.width / 2)
  if (next) store.books = next
}

async function onDrop() {
  landed = true
  const next = store.books
  const prev = rollback
  dragId.value = 0
  rollback = null
  if (!prev || sameOrder(prev, next)) return   // 拖了个寂寞就别打扰后端
  try {
    await api.saveBookOrder(orderIds(next))
  } catch (e) {
    // 前端已经换了序，不退回去的话用户眼前是一套**存不住**的顺序，
    // 刷新一下就弹回原样 —— 那比一开始就不动更让人不信这个功能
    store.books = prev
    ElMessage.error(e?.response?.data?.detail || '排序没存住，已还原')
  }
}

function onDragEnd() {
  // 拖到空白处放掉：这一路的让位全是预览，一格都不能留
  if (!landed && rollback) store.books = rollback
  dragId.value = 0
  rollback = null
  landed = false
}

/** 键盘排：原生拖拽只认鼠标，卡片可聚焦 + Alt/⌘ + ↑↓ 补上这一路 */
async function onTileKeydown(row, event) {
  const delta = moveDelta(event)
  if (!delta) return
  event.preventDefault()
  const prev = store.books.slice()
  const next = moveBy(prev, row.id, delta)
  if (!next) return                            // 到头了
  store.books = next
  try {
    await api.saveBookOrder(orderIds(next))
  } catch (e) {
    store.books = prev
    ElMessage.error(e?.response?.data?.detail || '排序没存住，已还原')
  }
}

const yearOptions = Array.from({ length: 6 }, (_, i) => new Date().getFullYear() - 1 + i)
const form = reactive({
  company_name: '', name: '', start_year: new Date().getFullYear(), start_month: 1
})

const canSubmit = computed(() => form.company_name.trim() && form.name.trim())

// 每个账套当前选中的年度。默认最新年度，用户可以往前翻。
const picked = reactive({})
const yearsOf = (row) => (row.years || []).slice().sort((a, b) => b.year - a.year)
const pickedYear = (row) => picked[row.id] ?? (yearsOf(row)[0]?.year ?? row.start_year)
const yearOf = (row) =>
  (row.years || []).find((y) => y.year === pickedYear(row)) ||
  { closed: 0, total: row.start_month === 1 ? 12 : 0, carried: false }

function onYearPick(row, y) {
  picked[row.id] = y
  // 当场切过去：用户选年度就是想看那一年，不必再点「进入」
  if (row.id === store.bookId) store.setYear(y)
}

function closeCreate() {
  visible.value = false
  // 不清掉的话，下次重挂载又会把「刚建好账套」的向导弹出来
  store.setLastCreatedBook(0)
}

function openCreate() {
  store.setLastCreatedBook(0)
  form.company_name = ''
  form.name = `${form.start_year} 账套`
  form.start_month = 1
  visible.value = true
}

/** 从卡片上打开向导 */
async function openInit(row) {
  initBookId.value = row.id
  // 打开前先核一遍这份账套的状态，不要拿卡片上那份就开。
  // 卡片上的 init 是**列表接口**带下来的，可能停在用户上一次进这个页面的那一刻
  // —— 最典型的就是第 2 步：向导把人送去期初页导入，回来后卡片还是旧的，
  // 于是「继续初始化」点开，里面也还是「第 2 步没做」。
  // loadInit 内部会 markInit，所以卡片上的「待初始化」标签也一起变。
  await store.loadInit(row.id)
  initVisible.value = true
}

/** 向导里做完一步：把账套列表整体拉一遍（向导只更新了那一个账套的 init） */
async function onInitUpdated() {
  await store.loadBooks()
}

async function submit() {
  saving.value = true
  try {
    const book = await api.createBook({ ...form })
    // 建第一个账套时 bookId 从 0 变过来，页面会整个重挂载，
    // 这里先把 id 存到 store，挂载后好把向导接回来
    store.setLastCreatedBook(book.id)
    await store.loadBooks()
    // **切到刚建的这本账**。不切的话顶栏停在旧账套：用户看着向导以为在初始化
    // 新账套，实际上当前账套是旧的 —— 任何走「当前账套」的入口
    // （比如科目表导入弹窗）都会作用到旧账上，初始化表现为「怎么都做不完」。
    await store.setBook(book.id)
    ElMessage.success(`账套《${book.company_name} · ${book.name}》已创建，标准科目表已装载`)
    // 关掉创建弹窗、直接弹向导：初始化得当场看见，
    // 放在创建弹窗里一条提示条的位置上等于没有（原来就是这么写的，用户直接关了）
    visible.value = false
    initBookId.value = book.id
    initVisible.value = true
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '创建失败')
  } finally {
    saving.value = false
  }
}

// ---- 编辑账套名 / 单位名 ----
const editVisible = ref(false)
const editTarget = ref(null)
const editForm = reactive({ company_name: '', name: '', subject_code_rule: '4-2-2-2' })
const canSaveEdit = computed(() => editForm.company_name.trim() && editForm.name.trim())

// ---- 科目编码设置 ----
const LEVELS = [2, 3, 4, 5, 6]
// 二级往后只给 2/3/4 位。一级固定 4 位；下级再长的话，12 位以后的编码
// 在凭证、科目表、打印抬头里都读不顺眼，实际用起来也记不住。
const WIDTHS = [2, 3, 4]
const usedLevel = ref(2)

// 老账套可能存着别的位数（1 位或 8 位），照实列出来，
// 免得下拉里找不到当前值、打开就显示成空的
const widthOptions = (i) => {
  const w = ruleWidths.value[i]
  return WIDTHS.includes(w) ? WIDTHS : [w, ...WIDTHS].sort((a, b) => a - b)
}

// 存的是「4-2-2-2」这样一个串，界面上是「级次 + 每级几位」。
// 改动先落在数组上，确定保存时再拼回串 —— 界面上没保存的改动不跟去接口。
const ruleWidths = computed({
  get: () => String(editForm.subject_code_rule || '4-2-2-2').split('-').map(Number),
  set: (arr) => { editForm.subject_code_rule = arr.join('-') }
})

// 级次就是「几段」。调大时补一段默认长度，调小时砍掉后面的。
const ruleLevel = computed({
  get: () => ruleWidths.value.length,
  set: (n) => {
    const cur = ruleWidths.value
    ruleWidths.value = Array.from({ length: n }, (_, i) => cur[i] ?? 2)
  }
})
// 示例按「每个下级都是本级的第 1 号」来排：3 位就是 001、2 位就是 01，
// 正好对得上用户在别处看过的「1001 001 01 01」
const ruleSample = computed(() => [
  '1001',
  ...ruleWidths.value.slice(1).map((w) => '0'.repeat(Math.max(0, w - 1)) + '1')
].join(' '))
// 账上已经有第 N 级的科目，级次就调不到 N 以下 —— 后端也拦一道，这里先置灰
const minLevel = computed(() => Math.max(2, usedLevel.value))
function setWidth(i, v) {
  const next = ruleWidths.value.slice()
  next[i] = v
  ruleWidths.value = next
}

async function openEdit(row) {
  editTarget.value = row
  editForm.company_name = row.company_name
  editForm.name = row.name
  editForm.subject_code_rule = row.subject_code_rule || '4-2-2-2'
  editVisible.value = true
  // 账上用到第几级，只有单账套的详情里才有（列表页不给，免得每个账套都多扫一次科目表）
  try {
    const detail = await api.getBook(row.id)
    editTarget.value = { ...row, ...detail }
    editForm.subject_code_rule = detail.subject_code_rule || editForm.subject_code_rule
    usedLevel.value = detail.subject_max_level || 2
  } catch { /* 拿不到就按 2 级起算，后端还会再拦一道 */ }
}

async function submitEdit() {
  saving.value = true
  try {
    await api.updateBook(editTarget.value.id, { ...editForm })
    await store.loadBooks()
    editVisible.value = false
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function switchTo(id, year) {
  await store.setBook(id)
  if (year) store.setYear(year)
  router.push('/vouchers')
}

// 账套列表刷新后，把「当前账套」的对齐到顶栏正在看的那一年
watch(() => store.currentPeriod?.year, (y) => {
  const b = store.currentBook
  if (b && y && b.years?.some((x) => x.year === y)) picked[b.id] = y
})

onMounted(async () => {
  loading.value = true
  try {
    await store.loadBooks()
    const b = store.currentBook
    if (b && store.currentPeriod?.year) picked[b.id] = store.currentPeriod.year
    // 重挂载后把「刚建好账套」的向导接回来，并直接弹出来，
    // 免得用户建完账不知道下一步该录期初
    const last = store.lastCreatedBookId
    if (last && store.books.some((x) => x.id === last)) {
      initBookId.value = last
      initVisible.value = true
    }
  } finally {
    loading.value = false
  }
})
</script>


.rule-block { width: 100%; }
.rule-row { display: flex; align-items: center; gap: 5px; margin-bottom: 8px; flex-wrap: wrap; }
.rule-label { width: 70px; text-align: right; color: var(--cwq-text); }
.rule-dash { color: var(--cwq-muted); }
.rule-note { margin-left: 10px; color: var(--cwq-danger); font-size: 12px; white-space: nowrap; }
.rule-sample { color: var(--cwq-ok); font-size: 14px; letter-spacing: 1px; margin-left: 75px; }
<style scoped>
.book-page { padding: 24px; }
.book-page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.book-page-heading { display: flex; align-items: center; gap: 12px; }
.book-page-title {
  margin: 0;
  color: var(--cwq-chrome-strong);
  font-size: 22px;
  font-weight: 600;
  line-height: 1.35;
}
.book-count {
  padding-left: 12px;
  border-left: 1px solid #d9dfe7;
  color: #778292;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.create-book-button { min-height: 38px; padding: 0 16px; }
.create-book-button :deep(.el-icon) { margin-right: 5px; }
.book-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 330px), 1fr));
  align-items: stretch;
  gap: 12px;
}
.book-tile {
  display: flex;
  min-width: 0;
  min-height: 170px;
  flex-direction: column;
  padding: 14px 16px 12px;
  background: #fff;
  border: 1px solid #e2e7ed;
  border-radius: 8px;
  transition: border-color 140ms ease, box-shadow 140ms ease;
}
.book-tile:hover {
  border-color: #cbd6e2;
  box-shadow: 0 5px 16px rgb(31 39 51 / 6%);
}
.book-tile.is-current {
  background: #f9fbff;
  border-color: #c5d8f0;
  box-shadow: inset 3px 0 0 var(--cwq-primary);
}
.book-tile-head { display: flex; min-height: 42px; align-items: flex-start; justify-content: space-between; gap: 10px; }
.book-tile-ident { display: flex; min-width: 0; align-items: flex-start; gap: 9px; }
.book-tile-icon {
  display: grid;
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  place-items: center;
  color: #68788b;
  background: #f1f4f7;
  border-radius: 7px;
  font-size: 15px;
}
.book-tile.is-current .book-tile-icon { color: #2864a8; background: #eaf2fc; }
.book-tile-title {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px 7px;
  padding-top: 3px;
}
.book-name-link {
  min-width: 0;
  color: #2c3848;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.4;
  text-align: left;
  white-space: normal;
}
.book-name-link :deep(.el-link__inner) { text-align: left; white-space: normal; overflow-wrap: anywhere; }
.book-tile-meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  margin-top: 8px;
  padding: 9px 0;
  border-top: 1px solid #edf0f3;
  border-bottom: 1px solid #edf0f3;
}
.book-fact { display: flex; min-width: 0; align-items: center; gap: 7px; }
.book-fact + .book-fact { padding-left: 18px; border-left: 1px solid #edf0f3; }
.book-fact-label { flex: 0 0 auto; color: #8993a1; font-size: 12px; line-height: 1.4; }
.book-fact strong { color: #3a4655; font-size: 14px; font-weight: 600; }
.book-year-select { width: 100px; }
.book-tile-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; padding-top: 8px; }
/* 拖拽的两个态：被拖的那张压暗、整块网格给一个「可以放」的鼠标形状。
   落点靠磁贴在拖动中**让位**来表达 —— 比在两卡之间画一条线省事，
   也不用额外维护一个插入指示器元素。 */
.book-tile { cursor: grab; }
.book-tile.is-dragging { opacity: .45; cursor: grabbing; }
.book-tile:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.book-order-hint { color: var(--cwq-muted, #8993a1); font-size: 12px; }
/* 卡片底部这一排的唯一尺寸来源：三颗控件的高度、字号、圆角都在这里定，
   以后加第四颗只要挂 .tile-action，不用再量一遍前面那几颗 */
.tile-action { height: 30px; padding: 0 12px; font-size: 12px; }
.enter-book-button { min-width: 92px; }
.enter-book-button :deep(.el-icon) { margin-left: 6px; }
.edit-book-button { width: 30px; padding: 0; }
/* 年度下拉选项：文字在左、「已结完」贴右且垂直居中 */
.year-opt { display: flex; align-items: center; }
.year-opt .el-tag { margin-left: auto; }
.mt { margin-top: 8px; }
.mb { margin-bottom: 12px; }
.ok { color: var(--cwq-ok); font-weight: 600; }

@media (max-width: 700px) {
  .book-page { padding: 16px; }
  .book-page-head { margin-bottom: 18px; }
  .book-page-title { font-size: 20px; }
  .book-grid { grid-template-columns: 1fr; gap: 10px; }
  .book-tile { min-height: 166px; padding: 14px; }
}
@media (max-width: 420px) {
  .book-page-heading { align-items: flex-start; flex-direction: column; gap: 4px; }
  .book-count { padding-left: 0; border-left: 0; }
  .book-page-head { align-items: flex-end; }
  .create-book-button { padding: 0 11px; }
  .book-tile-meta { gap: 8px; }
  .book-fact { align-items: flex-start; flex-direction: column; gap: 4px; }
  .book-fact + .book-fact { padding-left: 12px; }
  .book-year-select { width: 110px; }
}
</style>
