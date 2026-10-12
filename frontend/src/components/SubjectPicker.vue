<template>
  <div ref="rootEl" class="sp" :class="{ 'is-nonleaf': isNonLeaf }"
       :title="isNonLeaf ? '这不是末级科目，凭证要记在末级上' : ''"
       @mousedown.self.prevent
       @click="onCellClick">
    <!-- 会计科目这一格**整格就是一个输入框**：没有「选择科目」按钮。
         点哪儿都在这一格打字 —— 输名称、输编码都行，
         下面那块面板跟着过滤，选还是打两条路都通。

         用 textarea 而不是 input：科目名长起来跟摘要一样得有地方折 ——
         「主营业务收入/管理费」这类带层级的名字一长，单行框只会把它顶到边上，
         看不见到底选了哪个科目。用 textarea 写满一行自动折成两行（最多两行）。
         框本身只占内容那几行，格子下半截空着的地方照样算「点这一格」，
         由下面 onCellClick 兜住 —— 那一截还压着科目余额，不是废地。 -->
    <textarea
      ref="inputEl"
      class="sp-input"
      rows="1"
      :value="text"
      :disabled="disabled"
      placeholder="科目编码或名称"
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

      <div class="sp-list" @mouseleave="onHoverEnd()">
        <button v-for="(n, i) in visible" :key="n.id" class="sp-item"
                :class="{ 'is-on': n.id === modelValue, 'is-cursor': i === cursor, 'is-parent': hasKids(n) }"
                :style="{ paddingLeft: `${12 + n.depth * 16}px` }"
                :title="hasKids(n) ? `点开看下面的明细科目` : ''"
                @click="onRow(n)" @mouseenter="onHover($event, i)">
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
const emit = defineEmits(['update:modelValue', 'change', 'add', 'editing', 'enter', 'picked', 'left', 'right'])

const open = ref(false)
/** 这一格是不是正被人编辑（聚焦着）。决定格里写编码还是写全名 */
const focused = ref(false)
// 格子里显示的字：选中之后是科目名（层级分隔写成 /），没选就是空
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
const byId = computed(() => new Map(props.options.map((s) => [s.id, s])))
/**
 * 格里显示的科目：**一级一级拼出来**，中间用「/」隔开。
 *
 *  只盯着名字里的横杠换成斜杠是不够的 —— 科目表里子科目的写法本来就不统一：
 *  600101 存的是「主营业务收入-租金」，同一个上级底下的 600103 却只存「其他」。
 *  换横杠的话，「其他」就还是光秃秃一个「其他」，看不出它是挂在 6001 底下的。
 *
 *  所以要顺着 parent_id 一路拼上去：主营业务收入/租金、主营业务收入/其他。
 *  子名字开头把上级名字抄进去的那一段（「主营业务收入-租金」开头的
 *  「主营业务收入-」）得摘掉，不然拼出来成了「主营业务收入/主营业务收入-租金」。
 *  剩下的名字里若还有横杠（4104「利润分配-未分配利润」这种没有上级的），
 *  照旧按层级写成斜杠 —— 金额那一侧是减号，科目名里再冒一个横杠总会被看成减号。
 *
 *  只改显示，不动数据：搜「1001」「kcxj」照样搜得到，面板里也照旧摆编码，
 *  真正存进凭证的还是 subject_id。
 */
const displayName = (s) => {
  const chain = []
  const seen = new Set()
  for (let cur = s; cur && !seen.has(cur.id); ) {
    seen.add(cur.id)
    chain.unshift(cur)                                    // 从一级排到自己
    cur = cur.parent_id != null ? byId.value.get(cur.parent_id) : null
  }
  return chain.map((node, i) => {
    let name = String(node.name ?? '')
    const up = i > 0 ? String(chain[i - 1].name ?? '') : ''
    for (const sep of ['-', '/', '－']) {               // 摘掉抄了上级名字的那一段
      if (up && name.startsWith(up + sep)) { name = name.slice(up.length + sep.length); break }
    }
    return name.replace(/[-－]/g, '/')
  }).filter(Boolean).join('/')
}
/** 歇着（没在编辑）时格里写什么：一级一级拼出来的科目全名 */
const resting = computed(() => (current.value ? displayName(current.value) : ''))
/**
 * **编辑**状态下格里写什么：科目编码。
 *
 *  这一格是用来「改科目」的 —— 改的时候人手里拿着的是科目表，对的是编码；
 *  「主营业务收入/其他」这一长串是给读凭证的人看的，挡在编码前面只会让人先把
 *  它整个划掉，还得再数一遍自己想去哪个。走进这一格换成编码，走出去再换回全名，
 *  两边各司其职：录的时候对着编码打，读的时候对着名字认。
 */
const editing = computed(() => (current.value ? String(current.value.code ?? '') : ''))
/**
 * 人正在这一格里**标记科目**吗（正敲着 / 敲了一半）。
 *
 *  选中之后框里写的是那个科目的编码，跟 `editing` 一致 —— 这就叫「编辑好了」，
 * 格子左下角的余额是这一格该看的数，说一声就在。
 *  可正敲着的时候（'100'、'10020'）框里的字还没对上任何科目，角上那个余额
 *  是**上一个**科目的：看着像刚敲的这串号有多少钱，人就照着它填金额了。
 *  数字没错，错的是它挂在谁名下 —— 所以标记期间先把余额收起来。
 */
const marking = computed(() => focused.value
  && text.value.trim() !== editing.value.trim())
watch(marking, (v) => emit('editing', v), { immediate: true })
const isNonLeaf = computed(() => !!current.value && current.value.is_leaf === false)

// 外面改了选中科目（打开一张老凭证、回车加行自动带过去），格子里的字跟着走。
// immediate 是必须的：打开老凭证时首帧 display 就已经是科目名，
// 后面不会再变 —— 没有 immediate，watcher 一次都不触发，格子就一直空着
watch(resting, (v) => { if (!focused.value) { text.value = v; nextTick(autosize) } }, { immediate: true })

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

const kidsOf2 = (id) => tree.value.get(id) || []

/**
 * 这一行已经填了科目时，面板只摆**那一支**：
 * 它上面那一整条路（上级）+ 同一层底下的其它科目（同级），默认摊开。
 *
 *  什么都不填才摆整棵一级目录 —— 人是在「给这一行挑科目」，不是在「浏览科目表」。
 *  一行录到第三条分录，面板里还摊着几十个资产科目，刚刚选的那一支反倒要自己翻出来。
 *
 *  翻到别的页签（资产/负债…）就不收窄了：那是在浏览，不是改这一行。
 */
const focusBranch = computed(() => {
  const s = current.value
  if (!s) return null
  // 翻到别的页签（资产/负债…）就不收窄了：那是在浏览科目表，不是改这一行
  if (tab.value !== 'all' && s.category !== tab.value.toUpperCase()) return null
  // 一级科目没有上级可收，它的同级就是整个一级目录 —— 照旧摆，不假装收窄
  if (s.parent_id == null) return null
  const keep = new Set()
  let cur = byId.value.get(s.parent_id)     // 上级：顺着 parent_id 一路摆到一级
  while (cur) { keep.add(cur.id); cur = cur.parent_id != null ? byId.value.get(cur.parent_id) : null }
  for (const n of kidsOf2(s.parent_id)) keep.add(n.id)   // 同级：它底下那一层全给
  return { keep, parentId: s.parent_id }
})

/** 面板上真正要画的那几行，按缩进拍平。 */
const visible = computed(() => {
  const h = hit.value            // 别跟外层的 hit 同名：const 自己遮自己会踩到暂时性死区
  const out = []
  // 没在搜、又已经填了科目 → 只摆那一支（上级 + 同级），其余的一级目录先收起来
  const br = h === null ? focusBranch.value : null
  const walk = (nodes, depth) => {
    for (const n of nodes) {
      const kids = kidsOf(n.id)
      // 搜的时候，祖先靠「底下有命中的」留下；收窄的时候只留那一支；
      // 都不适用时，摆在哪个一级目录下就画哪一支
      const keep = h ? h.has(n.id) || kids.some((k) => h.has(k.id))
        : br ? br.keep.has(n.id)
          : true
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

/**
 * 鼠标扫过哪一行，光标就跟到哪一行 —— 键盘光标和鼠标光标本来就是同一条。
 *
 * 但**鼠标没动过**的不认：面板重排（筛选、展开、收窄到那一支）会把新的行
 * 顶到同一个像素上，浏览器照旧甩一个 mouseenter 过来 —— 那是布局变了，
 * 不是人挪过去了。手还搭在面板上、转头去敲「1002」，光标就被这个没动过的
 * 鼠标从 1002 拽到顶上来的 100201；接着按回车，选中的就成了另一条科目。
 * 坐标跟上一次分毫不差，就是没挪，不算数。
 */
let lastHover = null
function onHover(e, i) {
  if (lastHover && lastHover.x === e.clientX && lastHover.y === e.clientY) return
  lastHover = { x: e.clientX, y: e.clientY }
  cursor.value = i
}
/** 指针真的走出去了又回来：哪怕落在同一个像素上，那也是人挪过去的，该跟 */
function onHoverEnd() { lastHover = null }

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
  // 刚打开的面板另起一段：上一次那点鼠标不算数，这一次真的悬停才算
  if (!open.value) lastHover = null
  if (!open.value && reset) {
    keyword.value = ''
    // 选中的那一支先展开：老凭证 / 回车带过来的科目在几层下面，
    // 目录全收着的话打开面板看不见自己选的是什么
    for (const id of pathTo(props.modelValue)) expand(id, true)
    // 光标落在**这一格已经填着的那个科目**上：人 reopen 面板是来改它的，
    // 不是从头浏览一级目录。按 ↑↓ 从第一条挪到它，得按四五下。
    setCursor(Math.max(0, visible.value.findIndex((s) => s.id === props.modelValue)))
  }
  open.value = true
  // 量得到面板多高，才知道该往下开还是往上翻
  nextTick(() => { place(); inputEl.value?.focus() })
}

/** 点这一格：刚进来时整格选中好重打，面板收起过（比如刚按过 Esc）也得再打开。
 *  —— 焦点一直在这个 input 上，光靠 `focus` 事件，面板是回不来的。
 *
 *  但**再点一下**（人已经在这格里待过了）就不能再替他全选了：
 *  全选是「这一格接下来要重打一遍」的省事，不是锁 —— 编码 6001 想改成 6002、
 *  想把中间两位 10 挑出来改的时候，全选把人堵死了。
 *  这时候别碰选区，让浏览器把光标落到点上的位置。
 *  点在框下面那截空白上（框只有内容那么高）没有位置可落，就落到末尾 ——
 *  跟单行输入框里点到右边的空白是一个意思。 */
function onClick(e) {
  const el = inputEl.value
  if (el && !selAllArmed) {
    if (document.activeElement === el) {
      if (e?.target !== el) el.setSelectionRange(el.value.length, el.value.length)
      openPanel()
      return
    }
    el.focus()
  }
  if (el && document.activeElement === el) selectAll()
  openPanel()
}

function onFocus() {
  if (props.disabled) return
  focused.value = true
  keyword.value = ''
  text.value = editing.value          // 进来就写编码：改科目是照着科目表打编码的事
  // 编码要**当场**写进框里、光标当场全选，不能只改 text 等下一次渲染：
  // 那之前框里躺的还是那一长串全名，外部往里塞的字（输入法首字母、粘贴、
  // 脚本）会插到旧字后面去，出来就是「1001kcxj」。
  // 整格选中也是同一件事：这一格接下来就是重打一遍，先把编码让出来。
  const el = inputEl.value
  if (el && el.value !== text.value) el.value = text.value
  selectAll()
  ensurePinyin().then(() => { dictReady.value = true })   // 拉到手，筛选跟着重算
  openPanel()
}

/**
 * 科目名按内容折行：写满一行折成两行，最多两行，再多就在框里滚。
 * 跟摘要格同一套口径（VoucherEdit.autosizeSum）。
 *
 *  框本身只有内容那么高，格子下半截（压着科目余额那截）不是框 ——
 *  点在上面也得能选中这一格，见 onCellClick。
 */
function autosize() {
  const el = inputEl.value
  if (!el) return
  const lh = parseFloat(getComputedStyle(el).lineHeight) || 22
  const max = lh * 2
  el.style.height = 'auto'
  const need = el.scrollHeight
  el.style.height = Math.min(need, max) + 'px'
  el.style.overflowY = need > max ? 'auto' : 'hidden'
  // 一行就跟着摘要格一起居中，两行贴上沿 —— 居中的两行会压到左下角的科目余额
  rootEl.value?.classList.toggle('is-wrap', need > lh)
}

/** 点这一格。e.target 必须是格子自己 —— 点在框上、面板上，都不算这一手 */
function onCellClick(e) {
  if (e.target === rootEl.value) onClick(e)
}

/**
 * 整格选中只在**刚进这一格**的那一下有效。
 *
 *  编码比名字短得多，人常常不是要重打一遍，是要改其中几位
 *  （6001 改成 6002、把中间两位挑出来）。全选又是方向键按不掉、
 *  又是鼠标点不掉的时候，这一格就只能整段覆盖着改 —— 那是添堵，不是省事。
 */
let selAllArmed = false

/** 聚焦整格选中：改科目就是重打一遍，不用先把「编码 名称」拖掉。
 *  点进这一格（mousedown 先 focus、click 后到）这一下也得跟一次，
 *  否则只有 focus 那一次全选，等于没使。
 *
 *  但光这一遍**还不够**：Chrome 把「光标落到点上的那个位置」（mousedown 的默认动作）
 *  排在 click 处理器**之后**才做，于是刚选好的整格又被抹成一个光标位 ——
 *  「再点一下好重打」打出来的字接在原来那串后面（库存现金 + 100202）。
 *  下一帧再选一次：这时候浏览器自己那一手已经落完了，抹掉的正是它。 */
function selectAll() {
  const el = inputEl.value
  if (!el) return
  el.select()
  selAllArmed = true
  requestAnimationFrame(() => {
    // 焦点已经走了、人自己动手了（打字、输入法、粘贴），就别抢回来
    if (selAllArmed && document.activeElement === el && el.selectionStart === el.selectionEnd) el.select()
    // 这一下只管「刚进来的那一次」。过了这一帧，人再点、再按方向键，
    // 都是他自己要挪光标 —— 下一手就别再替他全选了。
    selAllArmed = false
  })
}

function onInput(e) {
  // 已经动手改了：这一下「整格选中」到此为止，别等下一帧又把人打的字划掉
  selAllArmed = false
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
  autosize()
}

/** 输了半天没选中：字还在但库里没这个科目，回填成原来的，
 *  别让格子上写的和实际挂的科目说两套话。
 *  Tab 走到下一格时也得把面板收起来 —— 面板只认鼠标点击关闭的话，
 *  一路按 Tab 下去，面板会一路开着挡住下面的格子。 */
function onBlur() {
  open.value = false
  focused.value = false
  // 输了半天没选中：字还在但库里没这个科目，回填成原来的，
  //  别让格子上写的和实际挂的科目说两套话
  keyword.value = ''
  text.value = resting.value
}

function pick(s, viaKey = false) {
  emit('update:modelValue', s.id)
  emit('change', s)
  // 人还停在这一格里（多半是刚点完面板那一行）就继续写编码；
  // 已经走了（下一次回车落到金额格）就写回全名
  text.value = focused.value ? String(s.code ?? '') : displayName(s)
  keyword.value = ''
  open.value = false
  nextTick(autosize)
  // 回车选中的：直接落到这一行的借方金额格，一路敲下去不用再补一次回车
  if (viaKey) emit('picked', s)
}
function onAdd() {
  open.value = false
  // 把人**敲进去的那串**带出去（keyword，不是 text）：
  // 填了一个查不到的编码再点添加，说的是「照这个号建一个」，不是「随便建一个」——
  // 编码、上级科目、类别、余额方向都该照着它推，不用人再敲一遍。
  //
  // 传 text 是错的：格子里聚焦时写的是**已选那个科目**的编码，
  // 人一个字没敲就点添加，也会把「1001」当成他要新建的号带过去 ——
  // 接着选个上级，编码就死死卡在这个已存在的号上，后端直接 400。
  // keyword 只装人自己敲的那些字，没敲就是空的，正好回到原来那套「照上级接号」。
  emit('add', String(keyword.value ?? '').trim())
}

/** ↑↓ 走列表，←→ 换格子，Enter 选中，Esc 收起面板 —— 全程不用碰鼠标 */
function onKeydown(e) {
  // 左右键是**格子之间**的移动，不是面板里的树 —— 面板摆的是哪一格，
  // 左右键一动，被换走的是人手里正录的那一格，不是下面那块列表。
  // 展开/收起交给点 ▸ 或 →、↑↓ 走到那一行再回车。
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    // 整格选中还挂着：这一下先收选区、按浏览器的老规矩挪一格光标。
    // 不这么做，按一下方向键人就被甩到隔壁格去了，全选永远消不掉。
    const el = e.target
    const s = el.selectionStart ?? 0
    if ((el.selectionEnd ?? s) !== s) return
    // 光标是塌着的：还在格内就照默认行为挪，到边上了才换格子
    const edge = e.key === 'ArrowRight' ? el.value.length : 0
    if (s !== edge) return
    e.preventDefault()
    emit(e.key === 'ArrowLeft' ? 'left' : 'right')
    return
  }
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
    text.value = editing.value        // 还停在这一格上，接着写的还是编码
    open.value = false
  } else if (e.key === 'ArrowDown') {
    e.preventDefault()
    cursor.value = Math.min(cursor.value + 1, list.length - 1)
    scrollTo()
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    cursor.value = Math.max(cursor.value - 1, 0)
    scrollTo()
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
/** 第一条**真正命中**关键字的行。祖先行只是给人看的路，不算结果。 */
const firstHit = computed(() => {
  if (!keyword.value.trim()) return null
  return visible.value.find((n) => matchSubject(n, keyword.value))?.id ?? null
})

// 展开一级之后下面的行整体往下挪，光标得留在原来那一行上 ——
// 一按 → 就跳回第一条的话，人得从头再找一遍自己在哪儿
const lastCursorId = ref(null)

/**
 * 光标落到第几行，**顺手把「它指着哪个科目」记下来**。
 *
 *  光标记身份这件事不能只交给 watch(cursor)：那两个 watch 是排队跑异步的，
 *  面板刚打开时展开的那一下先把 watch(visible) 排进了队，它读 lastCursorId 时
 *  watch(cursor) 还没轮到 —— 手里还是上一次（甚至压根没有）的身份，
 *  于是刚摆到已填科目上的光标又被 watch(visible) 拽回第一条去。
 *  身份得跟光标**同一时刻**记下来，中间那一帧读到的才是这个人，不是上一个。
 */
function setCursor(i) {
  cursor.value = i
  lastCursorId.value = visible.value[i]?.id ?? null
}

watch(cursor, (i) => { lastCursorId.value = visible.value[i]?.id ?? null })
watch(visible, () => {
  // 搜的时候落在第一条真命中的上：搜「600101」时 6001 主营业务收入只是垫在前面的路，
  // 停在它上面按回车只会展开上级，那张要找的明细还得再按两下
  const want = firstHit.value ?? lastCursorId.value
  const i = want == null ? -1 : visible.value.findIndex((s) => s.id === want)
  setCursor(i >= 0 ? i : 0)
})

// 列宽跟着窗口走，窗口一改折行位置就变了，高度得跟着重量一遍
function onResize() { autosize() }
onMounted(() => {
  document.addEventListener('mousedown', onDocClick)
  window.addEventListener('resize', onResize)
  autosize()
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocClick)
  window.removeEventListener('resize', onResize)
})

defineExpose({
  openPanel: () => openPanel(),
  /** 回车一路往下走时要能直接落到这一格（面板照常跟着开，跟手点进去一样） */
  focusInput: () => { inputEl.value?.focus(); selectAll() }
})
</script>

<style scoped>
/* flex: 1 —— 格子有多宽，输入框就铺多宽。少了它，框会被撑成刚好放得下占位符的宽度 */
/* 框在格子里**居中**，跟摘要格（td-sum 也是 align-items:center）落在同一条基线上 ——
   改成一个写满一行贴上沿，科目名会比同一行的摘要高一截，整张表看着就歪了。
   折成两行时改回上沿：居中的两行正好压住左下角那个科目余额。 */
.sp { position: relative; display: flex; align-items: center; flex: 1; height: 100%; min-width: 0; }
.sp.is-wrap { align-items: flex-start; }

/* 跟摘要格一模一样的输入框：整格宽、不带边框，才像填在纸上的凭证。
   高度由 autosize() 按内容写进 style，这里只给初值（一行）；
   line-height 必须写成固定值 —— autosize 要靠它算「两行有多高」，写成 normal 量不出来。
   resize:none 是不要右下角那个拖拽把手，这一格高度由内容说了算。 */
.sp-input {
  display: block;
  width: 100%;
  height: 22px;
  border: 0;
  outline: none;
  background: transparent;
  padding: 0 12px;
  font: inherit;
  font-size: 14px;
  line-height: 22px;
  color: var(--cwq-text);
  cursor: text;
  resize: none;
  overflow-y: hidden;
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
