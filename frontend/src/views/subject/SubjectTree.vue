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
      <el-table :data="filtered" row-key="id" border default-expand-all
                :tree-props="{ children: 'children' }" v-loading="loading">
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
function pruneTree(nodes, kw) {
  const out = []
  for (const node of nodes) {
    const children = pruneTree(node.children || [], kw)
    const hit = String(node.code || '').toLowerCase().includes(kw)
      || String(node.name || '').toLowerCase().includes(kw)
    if (hit || children.length) out.push({ ...node, children })
  }
  return out
}

const isFiltering = computed(() => !!keyword.value.trim())
const filtered = computed(() =>
  isFiltering.value ? pruneTree(tree.value, keyword.value.trim().toLowerCase()) : tree.value)

const hitCount = computed(() => {
  if (!isFiltering.value) return 0
  let total = 0
  const walk = (nodes) => nodes.forEach((node) => { total += 1; walk(node.children || []) })
  walk(filtered.value)
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
