<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-input v-model="keyword" placeholder="按编码或名称筛选" clearable style="width: 240px" />
      <span class="muted">{{ year }} 年度科目表</span>
      <span class="muted">科目编码规则 {{ rule }}</span>
      <span v-if="isFiltering" class="muted">命中 {{ hitCount }} 个科目</span>
      <span class="spacer" />
      <!-- 「导入并覆盖」是**整年覆盖**：它跟一个正在删除的科目撞上，
           科目表会残缺（有的删了、有的又写回来了）。所以删除在途时
           把这一页所有写动作一起按住（审计 §7 路线图 44）。 -->
      <el-button :disabled="!store.canWrite || busy" @click="importVisible = true">
        <el-icon><Upload /></el-icon>&nbsp;导入并覆盖
      </el-button>
      <el-button type="primary" :disabled="!store.canWrite || busy" @click="openCreate()">新增科目</el-button>
    </div>

    <!-- 加载失败**不是**「没有科目」：这两件事在页面上长得一模一样。
         拦截器弹的那条红字会消失（或者被别的提示盖掉），页面上这一条留着 ——
         口径与账簿三视图一致（审计 §3.25）。 -->
    <el-alert v-if="loadError" type="error" :closable="false" show-icon
              style="margin-bottom: 10px"
              :title="`加载失败：${loadError}。请检查网络后重试。`" />

    <div class="card">
      <el-table :data="filtered" row-key="id" border
                :expand-row-keys="expandKeys"
                :tree-props="{ children: 'children' }"
                @expand-change="onExpandChange" v-loading="loading">
        <!-- 编码列的宽度按本账套的编码规则算，见下面 codeWidth 的注释。
             写死一个数只对某一种规则成立，换个多级账套就折行。 -->
        <el-table-column label="编码" :width="codeWidth" prop="code" />
        <el-table-column label="科目名称" min-width="240" prop="name" />
        <el-table-column label="类别" width="90" align="center">
          <template #default="{ row }">{{ CATEGORY_LABEL[row.category] || row.category }}</template>
        </el-table-column>
        <!-- 表头写死断行：80px 扣掉左右各 12px 内边距只剩 56px，
             刚好卡在「四个 14px 的字」上，于是折成「余额方 / 向」——
             断在词中间。要的是「余额 / 方向」两个词，所以不靠宽度碰运气。 -->
        <el-table-column width="80" align="center">
          <template #header>余额<br>方向</template>
          <template #default="{ row }">{{ row.direction === 'DEBIT' ? '借' : '贷' }}</template>
        </el-table-column>
        <el-table-column v-if="store.canWrite" label="操作" width="190" align="center">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="busy" @click="openCreate(row)">下级</el-button>
            <el-button link type="primary" :disabled="busy" @click="openEdit(row)">编辑</el-button>
            <!-- 删除是这一页唯一的写动作。双击「删除」→ 两个 DELETE 并发 →
                 第一个删成功，第二个拿到的是 404「科目不存在」——
                 用户刚看到「已删除」，转眼又一条红字。
                 后端不是幂等的（`delete_subject` 先 db.get 再查引用，
                 删过就找不到了），所以这道闸只能在前端（审计 §7 路线图 44，
                 修法照 §3.29 / §3.30）。转圈只给**正在删的那一行**，
                 不是满屏都在转。 -->
            <el-button link type="danger" :disabled="busy"
                       :loading="removingId === row.id" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <SubjectFormDialog v-model="visible" :tree="tree" :parent-id="parentId" :year="year" />
    <SubjectEditDialog v-model="editVisible" :subject="editingSubject" @saved="load" />
    <SubjectImportDialog v-model="importVisible" :year="year" @imported="onImported" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload } from '@element-plus/icons-vue'
import * as api from '@/api/subject'
import SubjectFormDialog from '@/components/SubjectFormDialog.vue'
import SubjectEditDialog from '@/components/SubjectEditDialog.vue'
import SubjectImportDialog from '@/components/SubjectImportDialog.vue'
import { useUrlConditions } from '@/composables/useUrlConditions'
import { useLatestLoad } from '@/composables/useLatestLoad'
import { useSessionStore } from '@/stores/session'
import { CATEGORY_LABEL } from '@/utils/subjectCode'
import { SEARCH_AUTO_DEPTH, rowOpen, searchNamed } from '@/utils/subjectSearch'

const store = useSessionStore()
const tree = ref([])
const keyword = ref('')

/**
 * 科目树的加载守卫：只认最新一趟。
 *
 * 账套 / 年度一变，整页会重挂（MainLayout 的 viewKey），那种旧响应写进的是
 * 一个**已经销毁的实例**，用户看不见 —— 所以要管的是**页内**的重载：
 * 编辑弹窗保存后的 load()、导入后的 load()、删除后的 load()，三条路都可能
 * 同时在途。慢的那一趟晚到，就把**已经被删掉的科目**又画回树上，
 * 用户看着像「删不掉」。口径与账簿三视图一致（composables/useLatestLoad.js）。
 */
const { loading, error: loadError, run, isStale } = useLatestLoad('subject/tree')

/**
 * 这一页的**写动作**在途守卫（审计 §7 路线图 44 / §3.29 同款修法）。
 *
 * 这一页只有一个写动作：**删除科目**。双击「删除」→ 两个 DELETE 并发 →
 * 第一个删成功，第二个拿到的是 404「科目不存在」——
 * 用户刚看到「已删除」，转眼又一条红字，像删除失败了。
 *
 * 后端**不是**幂等的（`delete_subject` 先 `db.get` 再查引用，
 * 删过就找不到了），所以这道闸只能在前端。
 *
 * 一次只允许一段在途：删除途中把「下级 / 编辑 / 新增 / 导入并覆盖」一起按住 ——
 * 「导入并覆盖」是**整年覆盖**，跟一个正在删除的科目撞上，科目表会残缺。
 */
const busy = ref(false)

/** 正在删的那一行（转圈只给这一行，不是满屏都在转）。 */
const removingId = ref(null)

/**
 * 筛选关键字 ↔ URL。
 *
 * 标签栏不做 keep-alive：切走再切回来就是组件重建，条件只活在组件里的话，
 * 回来必然被清空 —— 那就是「查询参数被重置」这个毛病。所以条件住进 URL，
 * 标签栏记的完整地址带着它，回来还是那一组条件。
 */
const { restore } = useUrlConditions({
  kw: { type: 'str', get: () => keyword.value, set: (v) => { keyword.value = v } },
})
const visible = ref(false)
const parentId = ref(null)
const editVisible = ref(false)
const editingSubject = ref(null)
const importVisible = ref(false)

/**
 * **当前年度**。
 *
 * 科目表每个年度各有一份（后端迁移 0032）：2026 与 2027 各有自己的
 * 科目表、各有自己的期初。这一页看的是顶栏选中的那一年 ——
 * 「导入并覆盖」也只覆盖那一年，别的年度的科目表一行都不动。
 */
const year = computed(() => store.currentYear || store.currentBook?.latest_year || 0)

const rule = computed(() => store.currentBook?.subject_code_rule || '4-2-2-2')

/**
 * 编码列宽按**本账套的编码规则**算，不写死。
 *
 * 科目树的缩进和展开图标都压在这一列里，最深一级实际要占：
 *   缩进（级数 − 1）× 16px + 展开图标 23px + 编码本身「各级段长之和」位数字
 *   （实测约 8.8px/位，这里按 9px 留余量）+ 单元格左右各 12px 内边距。
 *
 * 写死 180px 只够 4-2-2-2：碰上 4-2-2-2-2 这种五级账套，最深的十二位编码
 * 放不下就会折成两行 —— .cell 的行高跟着翻倍，整行比别的行高一截，
 * 看着就像这行错位了。下限 120px，免得短编码的账套白占地方。
 */
const codeWidth = computed(() => {
  const parts = String(rule.value).split('-').map(Number)
  const digits = parts.reduce((sum, n) => sum + n, 0)
  return Math.max(120, (parts.length - 1) * 16 + 23 + digits * 9 + 24)
})

// ------------------------------------------------------------------ 搜索
//
// 筛选放在前端做：整棵树本来就一次性铺在内存里（表格也没有分页，
// 三千个科目照样全渲染），再为每敲一个键往回要一趟数据是白折腾。
//
// 命中项**连同它下面整棵子树**一起留下 —— 科目是表，搜到 1122 却看不到
// 112201/112202，等于还得再搜一次。不命中的节点只保留有命中的后代那条路径，
// 祖先必须留着：剪枝时把父节点剪掉，这行就成了没有位置的孤儿，
// el-table 按 parent_id 找不着它的上级，整行直接不显示。
//
// **命中的口径是这一页自己的契约**：筛选框上写的是「按编码或名称筛选」，
// 就是编码或名称（子串）。全应用那一份 `matchSubject` 还多一条拼音首字母，
// 但拼音字典要另外加载（选择器是聚焦时才加载的），在这一页时灵时不灵 ——
// 「有时搜得到、有时搜不到」比「搜不到」更糟。
//
// 而「摊开几级」是**同一件事**，跟选择器共用一份判定（`searchNamed` /
// `rowOpen` / `SEARCH_AUTO_DEPTH`）：判定只有一套，「用什么算命中」由各页面
// 作为参数传进去。

/** 这一页的命中口径：编码或名称（子串）。 */
function matchTree(s, kw) {
  const k = String(kw ?? '').trim().toLowerCase()
  if (!k) return true
  return String(s.code ?? '').toLowerCase().includes(k)
    || String(s.name ?? '').toLowerCase().includes(k)
}

/** 这个节点是不是命中了筛选关键字。 */
function isHit(node, kw) {
  return matchTree(node, kw)
}

function pruneTree(nodes, kw) {
  const out = []
  for (const node of nodes) {
    const children = pruneTree(node.children || [], kw)
    if (isHit(node, kw) || children.length) out.push({ ...node, children })
  }
  return out
}

/**
 * 摊平成「带 parent_id 与嵌套深度」的一维列表。
 *
 * `utils/subjectSearch` 那套判定按 parent_id 算级次（`levelOf`），
 * 而接口给的是**嵌套树**（每个节点带 children）。摊平只是为了喂它。
 * 摊平时顺手记下**嵌套深度**：那是树里最可信的级次 —— 不信库里那个 `level`
 * 字段（改挂父级之后要等后端重算才准，见 subjectSearch::levelOf 的理由）。
 */
function flattenSubjects(nodes, parentId = null, depth = 0, out = []) {
  for (const n of nodes || []) {
    out.push({ ...n, parent_id: parentId, depth })
    flattenSubjects(n.children || [], n.id, depth + 1, out)
  }
  return out
}

/**
 * 默认摊到第几级（0 起：1 = 显示到第二级）。
 *
 * 用户 2026-10-29 批注：「科目表默认显示下一级。筛选条件为空或者为 1 级科目的时候，
 * 最多显示到 2 级科目，筛选条件是 2 级科目的时候，最多显示到 3 级，以此类推。
 * 防止一次性展开太多级。」
 *
 * **这条规矩不是新写的**：科目选择面板（`utils/subjectSearch.js`）早就是这么做的 ——
 * 指名了第几级就显示到第几 + 1 级，指不出名来（敲的是个片段、命中一大片）
 * 只摊到第二级。同一件事、同一套判定，所以**共用那一份**，不另起一套口径：
 * 两边算出不一样的深度，用户在两个页面看到同一棵树的两种摊法。
 *
 * 「指名」= 编码或名称全等，或者全表只有它一条命中（`searchNamed`）。
 */
function autoDepthOf(flat, kw) {
  const named = searchNamed(flat, kw, matchTree)
  return named ? named.level : SEARCH_AUTO_DEPTH
}

/**
 * 默认要展开的行（**字符串** id）。
 *
 * 摊到 `autoDepth` 那一级就收住，再深的**留在数据里**：用户点那支 ▸ 还能自己
 * 往下走 —— 限制的是「默认摊开多少」，不是「能看多少」。
 *
 * 「这一支摊不摊开」问的还是面板那条 `rowOpen`（同一件事，同一个判据），
 * 不在这儿另写一遍 `depth < autoDepth`。`rowOpen` 本来就管三件事：
 * 默认摊到哪一级、**人点开的一律作数**、**人点收的压过一切** ——
 * 后两件正是这一页要的（见下面 manualExpanded / manualCollapsed）。
 *
 * ⚠️ **id 要转成字符串再交出去**：Element Plus 的树形表把行收进
 * `treeData[parentId]` 这个**普通对象**里，于是 `Object.keys(treeData)` 拿到的
 * 是字符串键（"12"），而 `:expand-row-keys` 的值是拿 `includes(key)` 跟它比的。
 * 传数字进去 → 数字跟字符串永远对不上 → 一行都不展开（看起来像「表格不认下级」）。
 * 它的 TS 契约本来就写的是 `Array<string>`，照契约给。
 */
function expandKeysOf(nodes, autoDepth, expanded = null, collapsed = null) {
  const keys = []
  const walk = (list, depth) => list.forEach((n) => {
    const hasKids = (n.children || []).length > 0
    const id = String(n.id)
    if (rowOpen({ hasKids, depth, searching: true, autoDepth, expanded, collapsed, id })) {
      keys.push(id)
    }
    walk(n.children || [], depth + 1)
  })
  walk(nodes || [], 0)
  return keys
}

/**
 * **用户手动开过 / 关过的行**（行 id → true / false）。
 *
 * **为什么单独记一份**：``:expand-row-keys`` 一变，el-table 会把展开状态
 * **整个换成**那批 key（`setExpandRowKeys` 是赋值，不是合并）。而摊开深度一改、
 * 筛选一变，那个数组都会重算 —— 不记这一份，用户刚点开的行就被弹回去了。
 *
 * 记的是**用户的意愿**，不是「当前状态」：开了记 true，关了记 false。
 * 没记过的行按默认来（摊到 `autoDepth` 那一级为止）。
 *
 * 与 `views/bank/FlowReconciliation.vue` 是**同一套做法** —— 本仓库里
 * 「按行控制展开」只有这一种写法。
 */
const rowExpandOverrides = ref(new Map())

/**
 * 用户点过的行 → `rowOpen` 要的那两个集合（人点开的 / 人点收的）。
 *
 * `utils/subjectSearch.js` 的 `rowOpen` 本来就管这三件事：默认摊到哪一级、
 * **人点开的一律作数**、**人点收的压过一切**。后两件正是这一页要的。
 */
const manualSets = computed(() => {
  const expanded = new Set()
  const collapsed = new Set()
  for (const [id, open] of rowExpandOverrides.value) {
    if (open) expanded.add(String(id))
    else collapsed.add(String(id))
  }
  return { expanded, collapsed }
})

/**
 * el-table 的展开变化（只有**人点**才会来这一趟）。
 *
 * ⚠️ **两种 `expand-change` 的签名不一样**：
 * - `type="expand"` 的列（流水对账页那样）发的是**展开行的数组**；
 * - **树形表**（这一页）发的是 `(row, expanded)` 的**布尔**。
 * 照抄流水对账页那版（拿数组 `.some()` 判开合）到这里会**永远当成「开了」**。
 */
function onExpandChange(row, expanded) {
  if (!row || row.id === undefined || row.id === null) return
  const next = new Map(rowExpandOverrides.value)
  next.set(String(row.id), !!expanded)
  rowExpandOverrides.value = next
}

const isFiltering = computed(() => !!keyword.value.trim())
const filtered = computed(() =>
  isFiltering.value ? pruneTree(tree.value, keyword.value.trim().toLowerCase()) : tree.value)

const flatSubjects = computed(() => flattenSubjects(tree.value))
const autoDepth = computed(() => autoDepthOf(flatSubjects.value, keyword.value))
const expandKeys = computed(() => expandKeysOf(
  filtered.value,
  autoDepth.value,
  manualSets.value.expanded,
  manualSets.value.collapsed,
))

/**
 * 「命中 N 个科目」数的是**真正命中的那些**。
 *
 * 以前数的是剪枝后树上的全部节点 —— 那是「结果里留下了多少行」，不是「命中多少」。
 * 摊开深度一改，这两者就对不上了：命中 3 个、树上留着 6 行，而摊开的深度又只
 * 露出其中一部分，数字与眼前的行数八竿子打不着。标签写的是「命中」，就数命中。
 */
const hitCount = computed(() => {
  if (!isFiltering.value) return 0
  let total = 0
  for (const s of flatSubjects.value) if (isHit(s, keyword.value)) total += 1
  return total
})

async function load() {
  if (!store.bookId || !year.value) return
  let d
  try {
    d = await run(() => api.subjectTree(store.bookId, { year: year.value }))
  } catch (e) {
    // 加载失败**不是**「没有科目」：这两件事在页面上长得一模一样。
    // 拦截器已经弹过提示了，这里只要别让它变成一个没人接的 rejection ——
    // 树上留着上一次的内容，顶部由 loadError 挂一条「加载失败」。
    return
  }
  // 过期的那一趟：条件早就被改掉了，写进去就是「树上还是旧账套的科目」。
  if (isStale(d)) return
  tree.value = d
}

function openCreate(parent = null) {
  parentId.value = parent?.id || null
  visible.value = true
}

function openEdit(subject) {
  editingSubject.value = subject
  editVisible.value = true
}

async function onImported() {
  await store.loadBooks()
  await load()
  ElMessage.success('科目表已覆盖导入')
}

/**
 * 写动作的统一入口：同一时刻只允许一段在途。
 *
 * 早退放在这里，而不是每个动作里各写一遍 —— 各写一遍的话，
 * 以后再加一个写动作就会漏掉一处（这正是这个 bug 能长期存活的原因）。
 */
async function withBusy(fn) {
  if (busy.value) return
  busy.value = true
  try {
    await fn()
  } finally {
    busy.value = false
    removingId.value = null
  }
}

async function remove(row) {
  await withBusy(async () => {
    try {
      await ElMessageBox.confirm(`确认删除科目 ${row.code} ${row.name}？`, '删除', { type: 'warning' })
    } catch {
      return // 用户点了「取消」—— 不是错误
    }
    removingId.value = row.id
    try {
      await api.deleteSubject(store.bookId, row.id)
    } catch {
      // 拦截器已经弹过原因了（科目已被凭证使用 / 存在下级 / 有期初余额）。
      // 这里吞掉，别让「删除失败」变成一个没人接的 promise rejection ——
      // 否则控制台里躺一条红字，而用户看到的是「已删除」。
      return
    }
    ElMessage.success('已删除')
    await load()
  })
}

onMounted(() => {
  // 筛选关键字从 URL 复原（标签栏切回来时带着它）
  restore()
  load()
})
// 换账套、换年度都要重取：另一个年度是**另一张**科目表
watch(() => [store.bookId, year.value], load)
</script>
