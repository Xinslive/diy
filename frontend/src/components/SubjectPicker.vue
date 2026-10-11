<template>
  <div ref="rootEl" class="sp" :class="{ 'is-nonleaf': isNonLeaf }"
       :title="isNonLeaf ? '这不是末级科目，凭证要记在末级上' : ''">
    <!-- 会计科目这一格**整格就是一个输入框**：没有「选择科目」按钮。
         点哪儿都在这一格打字 —— 输名称、输编码都行，
         下面那块面板跟着过滤，选还是打两条路都通。 -->
    <input
      ref="inputEl"
      class="sp-input"
      :value="text"
      :disabled="disabled"
      placeholder="科目名称 / 编码"
      autocomplete="off"
      spellcheck="false"
      @focus="onFocus"
      @click="onClick"
      @input="onInput"
      @blur="onBlur"
      @keydown="onKeydown"
    />

    <!-- 面板是一棵树：**先摆一级，点开一个才看见它的下级**。
         带 ▸ 的说明底下还有科目，点它只是展开；不带 ▸ 的是末级，点它才算选中。
         早先把下级全平铺出来，看着像「凭什么 1002 不能选、它的下级却能选」，
         其实差的只是少画了个父子关系。 -->
    <div v-if="open" ref="panelEl" class="sp-panel"
         :style="{ left: pos.left, top: pos.top, width: pos.width }" @mousedown.prevent>
      <div class="sp-tabs">
        <button v-for="t in TABS" :key="t.key" class="sp-tab" :class="{ 'is-on': tab === t.key }"
                @click="tab = t.key">{{ t.label }}</button>
      </div>

      <p v-if="parentHit" class="sp-parent">
        <b>{{ parentHit.code }} {{ parentHit.name }}</b> 是上级科目，点 ▸ 展开，下面的明细才能选
      </p>

      <div class="sp-list">
        <button v-for="(n, i) in visible" :key="n.id" class="sp-item"
                :class="{ 'is-on': n.id === modelValue, 'is-cursor': i === cursor, 'is-parent': hasKids(n) }"
                :style="{ paddingLeft: `${12 + n.depth * 16}px` }"
                :title="hasKids(n) ? `点开看下面的明细科目` : ''"
                @click="onRow(n)" @mouseenter="cursor = i">
          <span class="sp-caret">{{ !hasKids(n) ? '' : isOpen(n) ? '▾' : '▸' }}</span>
          <span class="sp-code">{{ n.code }}</span>
          <span class="sp-name">{{ n.name }}</span>
        </button>
        <p v-if="!visible.length" class="sp-empty">没找到这个科目，可以在下面点「＋ 点击添加」</p>
      </div>

      <button class="sp-add" @click="onAdd">＋ 点击添加</button>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { CATEGORY_LABEL, ensurePinyin, matchSubject } from '@/utils/subjectCode'

const props = defineProps({
  modelValue: { type: [Number, String], default: null },
  /** 全量科目（含上级），扁平的一条条，带 parent_id / level / is_leaf。
   *  末级**可以选**，上级只用来展开 —— 凭证只记在末级上。 */
  options: { type: Array, default: () => [] },
  disabled: { type: Boolean, default: false }
})
const emit = defineEmits(['update:modelValue', 'change', 'add', 'enter', 'picked'])

const open = ref(false)
// 格子里显示的字：选中之后是「编码 名称」，没选就是空
const text = ref('')
// 正在筛的那串字 —— 打字时它跟 text 同步，面板按它过滤
const keyword = ref('')
const tab = ref('all')
const cursor = ref(0)
/** 展开了哪些上级科目。默认一个都不展开：面板打开就是一张一级目录。 */
const expanded = ref(new Set())
const inputEl = ref(null)
const panelEl = ref(null)
const rootEl = ref(null)

const TABS = [
  { key: 'all', label: '全部' },
  ...Object.entries(CATEGORY_LABEL).map(([key, label]) => ({ key: key.toLowerCase(), label }))
]

const current = computed(() => props.options.find((s) => s.id === props.modelValue) || null)
const display = computed(() => (current.value ? `${current.value.code} ${current.value.name}` : ''))
const isNonLeaf = computed(() => !!current.value && current.value.is_leaf === false)

// 外面改了选中科目（打开一张老凭证、回车加行自动带过去），格子里的字跟着走。
// immediate 是必须的：打开老凭证时首帧 display 就已经是「编码 名称」，
// 后面不会再变 —— 没有 immediate，watcher 一次都不触发，格子就一直空着
watch(display, (v) => { text.value = v }, { immediate: true })

// 拼音字典是按需加载的：它到位之前按「搜不到首字母」过滤，到位后要把
// 同一串输入重新过滤一次，否则先打后加载的用户就永远搜不到了
const dictReady = ref(false)

/** parent_id → 子科目。建一次树，面板每次按它走 */
const tree = computed(() => {
  dictReady.value
  const byParent = new Map()
  for (const s of props.options) {
    const k = s.parent_id ?? null
    if (!byParent.has(k)) byParent.set(k, [])
    byParent.get(k).push(s)
  }
  for (const list of byParent.values()) list.sort((a, b) => String(a.code).localeCompare(String(b.code)))
  return byParent
})
const kidsOf = (id) => tree.value.get(id) || []
const hasKids = (n) => kidsOf(n.id).length > 0

/** 一级科目（按类别页签过滤）。往下一级靠 parent_id 找，不靠 level —— level 是层号，不是父子关系。 */
const roots = computed(() => {
  const all = tree.value.get(null) || []
  return tab.value === 'all' ? all : all.filter((s) => s.category === tab.value.toUpperCase())
})

/**
 * 打上关键字之后摆哪几行：命中的那条，**加上从一级到它的整条路**。
 * 只摆命中项的话搜「工商银行」会凭空冒出一条没有上级的 100202，看着像孤儿科目。
 */
function matchSet(nodes) {
  const kw = keyword.value
  if (!kw.trim()) return null
  const hit = new Set()
  const walk = (list) => {
    for (const n of list) {
      if (matchSubject(n, kw)) hit.add(n.id)
      walk(kidsOf(n.id))
    }
  }
  walk(props.options)
  return hit
}

/**
 * 搜的时候整条路都摊开，不搜的时候只画展开了的那一支。
 * 两者的结果放在一处算，面板上那个 ▸/▾ 才不会跟画出来的东西对不上
 * （搜「1002」时下级明明在，箭头还说「收着」，人只会以为是坏的）。
 */
const hit = computed(() => matchSet(roots.value))
const isOpen = (n) => hasKids(n) && (hit.value !== null || expanded.value.has(n.id))

/** 面板上真正要画的那几行，按缩进拍平。 */
const visible = computed(() => {
  const h = hit.value            // 别跟外层的 hit 同名：const 自己遮自己会踩到暂时性死区
  const out = []
  const walk = (nodes, depth) => {
    for (const n of nodes) {
      const kids = kidsOf(n.id)
      const on = h ? h.has(n.id) : true
      // 搜的时候，祖先靠「底下有命中的」留下；不搜的时候只画展开的那一支
      const keep = h ? on || kids.some((k) => h.has(k.id)) : on
      if (!keep) continue
      out.push({ ...n, depth })
      if (kids.length && isOpen(n)) walk(kids, depth + 1)
    }
  }
  walk(roots.value, 0)
  return out
})

/** 从一级到这个科目的整条路，用来把当前选中的那一条露出来 */
function pathTo(id) {
  const out = []
  const byId = new Map(props.options.map((s) => [s.id, s]))
  let cur = byId.get(id)
  while (cur && cur.parent_id != null) {
    out.push(cur.parent_id)
    cur = byId.get(cur.parent_id)
  }
  return out
}

function expand(id, on) {
  const next = new Set(expanded.value)
  if (on) next.add(id); else next.delete(id)
  expanded.value = next
}
function toggle(n) {
  if (!hasKids(n)) return
  expand(n.id, !expanded.value.has(n.id))
}

/** 点一行：带下级的只是展开，不带下级的（末级）才算选中 */
function onRow(n) {
  if (hasKids(n)) { toggle(n); return }
  pick(n)
}

// 面板贴着格子展开；下面放不上就翻到上面去。
// 宽度**就取这一格有多宽**：面板是这一格的下拉，宽出一截来，边线对不上格子边 ——
// 看着总像哪里歪了一点。高度跟着画出来的行数走：一级目录就那么几行，
// 面板不该还占着一屏空白
const PANEL_H = 340
const pos = reactive({ left: 0, top: 0, width: '0px' })
function place() {
  const el = rootEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const w = Math.round(r.width)
  pos.width = `${w}px`
  const h = panelEl.value?.offsetHeight || PANEL_H
  const below = window.innerHeight - r.bottom
  pos.left = `${Math.max(8, Math.min(r.left, window.innerWidth - w - 8))}px`
  pos.top = below < h + 12 ? `${Math.max(8, r.top - h - 4)}px` : `${r.bottom + 4}px`
}

/**
 * 打开面板。`reset` 只在面板**收起着**的时候才清筛选 ——
 * 已经开着还去清，会把用户刚打进格子的几个字擦掉。
 */
function openPanel({ reset = true } = {}) {
  if (props.disabled) return
  if (!open.value && reset) {
    keyword.value = ''
    // 选中的那一支先展开：老凭证 / 回车带过来的科目在几层下面，
    // 目录全收着的话打开面板看不见自己选的是什么
    for (const id of pathTo(props.modelValue)) expand(id, true)
    cursor.value = Math.max(0, visible.value.findIndex((s) => s.id === props.modelValue))
  }
  open.value = true
  // 量得到面板多高，才知道该往下开还是往上翻
  nextTick(() => { place(); inputEl.value?.focus() })
}

/** 点这一格：整格选中好重打，面板收起过（比如刚按过 Esc）也得再打开。
 *  —— 焦点一直在这个 input 上，光靠 `focus` 事件，面板是回不来的。 */
function onClick() {
  selectAll()
  openPanel()
}

function onFocus() {
  if (props.disabled) return
  ensurePinyin().then(() => { dictReady.value = true })   // 拉到手，筛选跟着重算
  openPanel()
}

/** 聚焦整格选中：改科目就是重打一遍，不用先把「编码 名称」拖掉。
 *  再点一次已经聚焦的格子不会触发 focus，所以 click 上也得来一遍。 */
function selectAll() {
  inputEl.value?.select()
}

function onInput(e) {
  text.value = e.target.value
  keyword.value = e.target.value
  // 整格删空 = 这一行不要科目了。清空内容是允许的，光靠失焦回填的话，
  // 选错了科目就再也清不掉了 —— 只能换一个，不能没有。
  if (!e.target.value.trim() && props.modelValue != null) {
    emit('update:modelValue', null)
    emit('change', null)
    // 这里**不再**连金额一起清：金额先填、科目后挑是允许的，
    // 先填了再改科目就把数字弄没了，那叫顺序有要求，不叫「不留半个分录」。
    // 少了科目的那一行由外头锁着往下写，漏不掉
  }
  openPanel({ reset: false })
  // 打字时从头开始看结果；上一趟光标停在哪一条不算数（新的 visible 出来时再落位）
  lastCursorId.value = null
  place()
}

/** 输了半天没选中：字还在但库里没这个科目，回填成原来的，
 *  别让格子上写的和实际挂的科目说两套话。
 *  Tab 走到下一格时也得把面板收起来 —— 面板只认鼠标点击关闭的话，
 *  一路按 Tab 下去，面板会一路开着挡住下面的格子。 */
function onBlur() {
  open.value = false
  if (text.value !== display.value) {
    text.value = display.value
    keyword.value = ''
  }
}

function pick(s, viaKey = false) {
  emit('update:modelValue', s.id)
  emit('change', s)
  text.value = `${s.code} ${s.name}`
  keyword.value = ''
  open.value = false
  // 回车选中的：直接落到这一行的借方金额格，一路敲下去不用再补一次回车
  if (viaKey) emit('picked', s)
}
function onAdd() {
  open.value = false
  emit('add')
}

/** ↑↓ 走，→ 展开，← 收起，Enter 选中，Esc 收起面板 —— 全程不用碰鼠标 */
function onKeydown(e) {
  // 面板收着时（刚按过 Esc、或者刚输完就走了）：回车跟别的格子一个规矩，往右走
  if (!open.value) {
    if (e.key === 'Enter') { e.preventDefault(); emit('enter') }
    return
  }
  const list = visible.value
  const at = list[cursor.value]
  if (e.key === 'Escape') {
    e.preventDefault()
    keyword.value = ''
    text.value = display.value
    open.value = false
  } else if (e.key === 'ArrowDown') {
    e.preventDefault()
    cursor.value = Math.min(cursor.value + 1, list.length - 1)
    scrollTo()
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    cursor.value = Math.max(cursor.value - 1, 0)
    scrollTo()
  } else if (e.key === 'ArrowRight') {
    // 展开这一支；已经展开了就往下走一级，站在它第一个下级上
    if (at && hasKids(at) && !expanded.value.has(at.id)) {
      e.preventDefault()
      expand(at.id, true)
      cursor.value = Math.min(cursor.value + 1, list.length - 1)
      scrollTo()
    }
  } else if (e.key === 'ArrowLeft') {
    // 收起来；已经收着就跳回它爹那一行 —— 和文件树一个手感
    if (at && hasKids(at) && expanded.value.has(at.id)) {
      e.preventDefault()
      expand(at.id, false)
    } else if (at && at.parent_id != null) {
      e.preventDefault()
      const i = list.findIndex((s) => s.id === at.parent_id)
      if (i >= 0) { cursor.value = i; scrollTo() }
    }
  } else if (e.key === 'Enter') {
    e.preventDefault()
    // 停在上级科目上：展开它，别急着当成选中
    if (at && hasKids(at)) { toggle(at); return }
    if (at) { pick(at, true); return }
    // 一条都没有：没打关键字就是把面板收了让路；打了关键字还没匹配上，
    // 字留在格子里继续改 —— 什么都不做才是对的
    if (!keyword.value) { open.value = false; emit('enter') }
  }
}
function scrollTo() {
  nextTick(() => {
    rootEl.value?.querySelectorAll('.sp-item')[cursor.value]?.scrollIntoView({ block: 'nearest' })
  })
}

function onDocClick(e) {
  if (open.value && rootEl.value && !rootEl.value.contains(e.target)) open.value = false
}
/** 关键字命中了一个上级科目：它只会展开，不会被选中 */
const parentHit = computed(() => {
  if (!keyword.value.trim()) return null
  return props.options.find((s) => s.is_leaf === false && matchSubject(s, keyword.value)) || null
})

/** 第一条**真正命中**关键字的行。祖先行只是给人看的路，不算结果。 */
const firstHit = computed(() => {
  if (!keyword.value.trim()) return null
  return visible.value.find((n) => matchSubject(n, keyword.value))?.id ?? null
})

// 展开一级之后下面的行整体往下挪，光标得留在原来那一行上 ——
// 一按 → 就跳回第一条的话，人得从头再找一遍自己在哪儿
const lastCursorId = ref(null)
watch(cursor, (i) => { lastCursorId.value = visible.value[i]?.id ?? null })
watch(visible, () => {
  // 搜的时候落在第一条真命中的上：搜「600101」时 6001 主营业务收入只是垫在前面的路，
  // 停在它上面按回车只会展开上级，那张要找的明细还得再按两下
  const want = firstHit.value ?? lastCursorId.value
  const i = want == null ? -1 : visible.value.findIndex((s) => s.id === want)
  cursor.value = i >= 0 ? i : 0
})

onMounted(() => document.addEventListener('mousedown', onDocClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))

defineExpose({
  openPanel: () => openPanel(),
  /** 回车一路往下走时要能直接落到这一格（面板照常跟着开，跟手点进去一样） */
  focusInput: () => { inputEl.value?.focus(); selectAll() }
})
</script>

<style scoped>
/* flex: 1 —— 格子有多宽，输入框就铺多宽。少了它，框会被撑成刚好放得下占位符的宽度 */
.sp { position: relative; flex: 1; height: 100%; min-width: 0; }

/* 跟摘要格一模一样的输入框：整格宽、整格高、不带边框，才像填在纸上的凭证 */
.sp-input {
  width: 100%;
  height: 100%;
  border: 0;
  outline: none;
  background: transparent;
  padding: 0 12px;
  font: inherit;
  font-size: 14px;
  color: var(--cwq-text);
  cursor: text;
}
.sp-input::placeholder { color: #c0c4cc; }
/* 只读就是只读：跟摘要列、金额列一样是白底。铺一块灰只会让这一列看着像坏了 */
.sp-input:disabled { cursor: not-allowed; }
/* 挂的不是末级科目：字标红，一眼能看出这一行不能就这么存 */
.is-nonleaf .sp-input { color: #d93025; font-weight: 600; }

.sp-panel {
  position: fixed;
  z-index: 2200;
  background: #fff;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, .14);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 页签等分：面板宽度跟着「会计科目」那一列走，六个页签挤一挤正好铺满，
   不换行也不溢出（换行的话面板高度会跟着跳） */
.sp-tabs { display: flex; gap: 2px; padding: 0 6px; border-bottom: 1px solid #ebeef5; }
.sp-tab {
  flex: 1 1 0;
  min-width: 0;
  border: 0;
  background: none;
  padding: 9px 2px 10px;
  font: inherit;
  font-size: 13px;
  white-space: nowrap;
  color: #606266;
  cursor: pointer;
  border-bottom: 2px solid transparent;
}
.sp-tab:hover { color: #67c23a; }
.sp-tab.is-on { color: #67c23a; border-bottom-color: #67c23a; font-weight: 600; }

.sp-parent {
  margin: 0;
  padding: 8px 12px;
  background: #fdf6ec;
  border-bottom: 1px solid #f5dab1;
  font-size: 12px;
  line-height: 1.5;
  color: #b88230;
}
.sp-parent b { font-weight: 600; }

.sp-list { max-height: 300px; overflow: auto; padding: 4px 0; }
.sp-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  border: 0;
  background: none;
  padding: 7px 12px;
  font: inherit;
  font-size: 13px;
  color: var(--cwq-text);
  cursor: pointer;
  text-align: left;
}
/* 上级科目：点它是展开，不是选中，箭头得留出固定的位置，
   不然下级跟着往左挪，整棵树每深一级就错开一格 */
.sp-caret {
  flex: none;
  width: 12px;
  color: #a8abb2;
  font-size: 11px;
  line-height: 1;
}
.sp-item.is-parent { cursor: pointer; }
.sp-item.is-parent .sp-name { color: #303133; font-weight: 600; }
.sp-item.is-cursor { background: #f5f7fa; }
.sp-item.is-on { background: #f0f9eb; color: #529b2e; font-weight: 600; }
.sp-code { color: #909399; font-variant-numeric: tabular-nums; flex: none; }
.sp-item.is-on .sp-code { color: #67c23a; }
.sp-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sp-empty { padding: 24px 12px; text-align: center; color: #c0c4cc; font-size: 13px; }

.sp-add {
  border: 0;
  border-top: 1px solid #ebeef5;
  background: #fff;
  color: #409eff;
  font: inherit;
  font-size: 13px;
  padding: 10px 0;
  cursor: pointer;
}
.sp-add:hover { background: #f5f7fa; }
</style>
