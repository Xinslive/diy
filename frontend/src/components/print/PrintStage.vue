<template>
  <div class="stage-wrap">
    <!-- 顶部浮层：页码导航 + 缩放。浮在纸上，不占版面空间 -->
    <div v-if="ready" class="stage-tools">
      <div class="tool-group">
        <el-button-group size="small">
          <el-button :disabled="activePage <= 1" @click="go(activePage - 1)">
            <el-icon><ArrowLeft /></el-icon>
          </el-button>
          <el-button disabled class="page-no">{{ activePage }} / {{ pageCount }}</el-button>
          <el-button :disabled="activePage >= pageCount" @click="go(activePage + 1)">
            <el-icon><ArrowRight /></el-icon>
          </el-button>
        </el-button-group>
      </div>
      <slot name="tools" />
      <span v-if="displayH > pageH + 2" class="long-page">长表，可滚动查看</span>
      <div class="tool-group">
        <el-button-group size="small">
          <el-button :disabled="scale <= 0.1" @click="zoomBy(-0.1)" title="缩小">
            <el-icon><ZoomOut /></el-icon>
          </el-button>
          <el-button class="zoom-label" :title="fitMode ? '自动适应窗口' : '按窗口宽度上限自适应'" @click="fitToWindow">
            {{ Math.round(scale * 100) }}%
          </el-button>
          <el-button :disabled="scale >= 2.5" @click="zoomBy(0.1)" title="放大">
            <el-icon><ZoomIn /></el-icon>
          </el-button>
        </el-button-group>
        <el-tooltip content="整页适应窗口" placement="bottom">
          <el-button size="small" :type="fitMode ? 'primary' : ''" @click="fitToWindow">
            <el-icon><FullScreen /></el-icon>&nbsp;适应
          </el-button>
        </el-tooltip>
      </div>
    </div>

    <p v-if="previewNote" class="stage-note">{{ previewNote }}</p>
    <div ref="host" class="stage-host" :class="{ editing: editing }">
      <div class="stage" :style="stageStyle">
        <!--
          纸面本体。iframe 按纸张真实毫米尺寸渲染，再整块 scale 缩放进容器 ——
          所以屏幕上这块白板的像素位置，和纸上每个元素的物理位置严格对应，
          这正是「在预览上直接编辑」能成立的前提。

          sandbox 只给 allow-same-origin：我们要读 contentDocument 量高度；
          但**不能**同时给 allow-scripts，否则脚本可执行 + 同源就能读父页
          localStorage 里的登录 token。
        -->
        <div class="paper" :style="paperStyle">
          <iframe ref="frame" class="frame" :srcdoc="html" title="打印预览"
                  sandbox="allow-same-origin" :style="frameStyle" @load="measure" />
          <!-- 编辑层：与当前纸面同坐标系（page 坐标，非 CSS 坐标）。
               外层 paper 统一缩放，所以 iframe 和编辑层都只保留一次 scale。 -->
          <div v-if="editing" class="layer" :style="{ top: '0px', width: pageW + 'px', height: displayH + 'px' }">
            <slot :page="{ w: pageW, h: displayH }" :content="contentBox" :slots="slotRects"
                  :hotspots="hotspots" :scale="scale" />
          </div>
        </div>
      </div>

      <div v-if="!html" class="stage-empty">正在生成预览…</div>
      <div v-else-if="error" class="stage-empty err">{{ error }}</div>
    </div>
  </div>
</template>

<script setup>
/**
 * 打印预览舞台。
 *
 * 这个组件只做一件事：**把后端渲染的那份打印页 HTML，按纸的真实尺寸摆出来**，
 * 并且把「纸面坐标系」交给上层 —— 上层往纸上叠的编辑层，靠的就是这里算出来的
 * page / content / slots 三组矩形。
 *
 * 为什么不用「塞满容器」那种预览：预览的全部价值是所见即所得。把 A4 缩成一条窄带，
 * 用户看到的分页位置、每页能放几张凭证全都不作数，等纸打出来才发现不对 ——
 * 那时候预览反而是误导。
 *
 * 坐标约定（三层，缺一层就会错位）：
 *   1. **纸坐标** —— pageW/pageH，96dpi 下的真实像素，与后端 .page 的 mm 等价；
 *   2. **编辑层坐标** —— layer 绝对定位在当前纸页的原点，因此上面的任何
 *      left/top 都直接用纸坐标，不用再换算分页偏移；
 *   3. **屏幕坐标** —— 由外层 paper 的 transform: scale 统一缩放，交互时除以
 *      scale 换回纸坐标。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowLeft, ArrowRight, FullScreen, ZoomIn, ZoomOut } from '@element-plus/icons-vue'

const props = defineProps({
  /** 打印页的完整 HTML。 */
  html: { type: String, default: '' },
  /** 纸型 key，取 papers 里的尺寸。 */
  paper: { type: String, default: 'A4' },
  orientation: { type: String, default: 'portrait' },
  /** 后端 meta.papers：[{ key, w, h }]，w/h 单位毫米。 */
  papers: { type: Array, default: () => [] },
  /** 页边距（mm），编辑层据此把内容区框出来。 */
  margins: { type: Object, default: () => ({ top: 10, right: 10, bottom: 10, left: 10 }) },
  /** 一页几张凭证（由纸型决定），编辑层据此切槽位。 */
  slots: { type: Number, default: 1 },
  /** 是否展示编辑层。关掉时不产生任何指针事件，预览就是一张安静的纸。 */
  editing: { type: Boolean, default: false },
  /** 预览出错时的提示文案，由调用方给出（它是懂后端返回的那种）。 */
  error: { type: String, default: '' },
})
const emit = defineEmits(['pages', 'hotspots'])

const host = ref(null)
const frame = ref(null)
const scale = ref(1)
const fitMode = ref(true)
const contentH = ref(0)
const pageCount = ref(1)
/** 每个 .page 在文档里的顶部偏移（纸坐标，未缩放）。 */
const pageTops = ref([0])
const pageHeights = ref([])
const activePage = ref(1)
const ready = ref(false)
const previewNote = ref('')

const MM2PX = 96 / 25.4
const mm = (v) => (Number(v) || 0) * MM2PX
/** 量高留的兜底余量：一像素取整误差就够顶出一条内部滚动条。 */
const H_PAD = 2

/**
 * 当前纸张在 96dpi 下的像素尺寸；orientation 为横向时宽高互换。
 *
 * 尺寸取自后端 meta 下发的 papers（w/h 单位毫米），不在这里写死一份常量：
 * 后端 PAPERS 一改，这份表就会悄悄过期 —— 而且过期时没有任何报错，
 * 只是预览按错误的宽度缩放，看着还挺正常。
 */
const paperMm = computed(() => {
  const spec = props.papers.find((p) => p.key === props.paper)
  const w = spec?.w ?? 210
  const h = spec?.h ?? 297
  const land = props.orientation === 'landscape'
  return land ? { w: h, h: w } : { w, h }
})
const pageW = computed(() => paperMm.value.w * MM2PX)
const pageH = computed(() => paperMm.value.h * MM2PX)
// 账簿/报表可能是后端生成的连续长表，保留完整页面高度供外层滚动查看。
// 自适应仍以一张物理纸的高度为准，避免内容越多、字反而越小。
const displayH = computed(() => Math.max(pageH.value, pageHeights.value[activePage.value - 1] || 0))

/**
 * 内容区（纸去掉页边距）—— 与后端 .page 的 padding 完全一致。
 * 这组矩形决定了编辑层里所有元素能摆到哪，也是拖页边距时的命中区。
 */
const contentBox = computed(() => {
  const m = props.margins || {}
  const x = mm(m.left)
  const y = mm(m.top)
  return {
    x, y,
    w: Math.max(0, pageW.value - x - mm(m.right)),
    h: Math.max(0, displayH.value - y - mm(m.bottom)),
  }
})

/**
 * 凭证槽位矩形，与后端 render_voucher_sheet 的 grid 完全同构：
 *   两张 + 横向 → 左右排列；两张 + 纵向 → 上下排列；一张 → 占满内容区。
 * 槽位之间固定 3mm 缝（后端 .voucher-sheet 的 gap），少算这 3mm，
 * 第二张凭证的元素框就会整体偏 3mm，拖哪儿都对不上。
 */
const slotRects = computed(() => {
  const c = contentBox.value
  if (props.slots <= 1) return [c]
  const gap = mm(3)
  if (props.orientation === 'landscape') {
    const w = (c.w - gap) / 2
    return [0, 1].map((i) => ({ x: c.x + i * (w + gap), y: c.y, w, h: c.h }))
  }
  const h = (c.h - gap) / 2
  return [0, 1].map((i) => ({ x: c.x, y: c.y + i * (h + gap), w: c.w, h }))
})

const docH = computed(() => Math.max(pageH.value, contentH.value || pageH.value))

const frameStyle = computed(() => ({
  width: pageW.value + 'px',
  height: docH.value + 'px',
  top: -(pageTops.value[activePage.value - 1] || 0) + 'px',
}))

/** 纸面只缩放一次。iframe 与编辑层都在这张纸里使用原始纸坐标。 */
const paperStyle = computed(() => ({
  width: pageW.value + 'px',
  height: displayH.value + 'px',
  transform: `scale(${scale.value})`,
}))

const stageStyle = computed(() => ({
  width: Math.round(pageW.value * scale.value) + 'px',
  height: Math.ceil(displayH.value * scale.value) + 'px',
}))

/** 屏幕可用区域（扣掉 host 自己的 padding）。 */
function availBox() {
  const h = host.value
  if (!h) return null
  const cs = getComputedStyle(h)
  return {
    w: h.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0),
    h: h.clientHeight - (parseFloat(cs.paddingTop) || 0) - (parseFloat(cs.paddingBottom) || 0),
  }
}

/**
 * 自适应：**把当前页塞进可视区**。
 *
 * 多页文档仍由顶部页码切换，但每次只展示一张真实纸页。不能拿整份文档的
 * 总高度参与 fit，否则 4 页预览会被一起缩成缩略图，单页内容就失去可读性。
 */
function fit() {
  const box = availBox()
  if (!box || box.w <= 0 || box.h <= 0) return
  if (!fitMode.value) return
  const byW = box.w / pageW.value
  const byH = box.h / pageH.value
  scale.value = Math.max(0.15, Math.min(1.6, Math.min(byW, byH)))
}

function fitToWindow() {
  fitMode.value = true
  fit()
}

function zoomBy(d) {
  fitMode.value = false
  scale.value = Math.max(0.1, Math.min(2.5, Math.round((scale.value + d) * 100) / 100))
}

/**
 * 预览文档必须**永远不自己滚动**：外层 .stage-host 才是唯一的滚动容器。
 * HTML5 早就废弃的 scrolling="no" 在 Chrome 上根本不认，真正管用的是插一条
 * overflow 规则；否则文档只比 iframe 高一丁点，iframe 内部就会自己冒出一条
 * 滚动条，滚轮悬在预览上先被它吃掉，表现就是「预览滑不动」。
 */
function lockInnerScroll(d) {
  const target = d.head || d.documentElement
  if (!target || d.getElementById('__stage_lock')) return
  const st = d.createElement('style')
  st.id = '__stage_lock'
  st.textContent = 'html,body{overflow:hidden!important;}'
  target.appendChild(st)
}

/** 量「排版后内容真正占多高」的下沿，不受 iframe 视口高影响。 */
function readContentBottom(d, w) {
  const body = d.body
  if (!body) return 0
  let bottom = body.getBoundingClientRect().bottom
  for (const node of body.children) {
    const r = node.getBoundingClientRect()
    const mb = parseFloat(w.getComputedStyle(node).marginBottom) || 0
    bottom = Math.max(bottom, r.bottom + mb)
  }
  return bottom
}

/**
 * 页面上可点选的区块。
 *
 * 这些框不是画上去的，是**从渲染结果里量出来的**：在 iframe 文档里按类名找到
 * 标题、副标题、页眉信息、表、页脚，把它们的真实位置换算成纸坐标交给上层。
 * 所以框永远贴着内容本身，而不是贴着一份写死的假设版式 ——
 * 用户点标题，点的就是真标题。
 *
 * 凭证范围不用这套：那边有元素层，逐个元素都已经能点能拖了。
 */
const HOTSPOT_SELECTORS = {
  title: '.doc-title',
  subtitle: '.doc-subtitle',
  meta: '.doc-meta',
  table: 'table.grid',
  footer: '.doc-footer',
}
const hotspots = ref([])

function readHotspots(d) {
  if (props.slots > 1) { hotspots.value = []; return }   // 凭证走元素层
  const pages = [...d.querySelectorAll('.page')]
  const pageEl = pages[activePage.value - 1]
  if (!pageEl) { hotspots.value = []; return }
  const pr = pageEl.getBoundingClientRect()
  const out = []
  for (const [key, sel] of Object.entries(HOTSPOT_SELECTORS)) {
    const el = pageEl.querySelector(sel)
    if (!el) continue
    const r = el.getBoundingClientRect()
    if (r.width < 4 || r.height < 4) continue
    out.push({
      key,
      x: Math.round(r.left - pr.left),
      y: Math.round(r.top - pr.top),
      w: Math.round(r.width),
      h: Math.round(r.height),
    })
  }
  hotspots.value = out
}

/**
 * 量出文档总高、总页数、每页顶部偏移 —— 缩放不改变这些的相对关系，
 * 所以编辑层可以直接用纸坐标摆位，不用反算 CSS 像素。
 */
function readPages(d) {
  const pages = [...d.querySelectorAll('.page')]
  if (!pages.length) return { count: 1, tops: [0], heights: [] }
  const tops = pages.map((p) => Math.max(0, p.getBoundingClientRect().top
    + (d.defaultView.scrollY || 0)))
  return { count: pages.length, tops, heights: pages.map((p) => p.getBoundingClientRect().height) }
}

let innerRO = null
function disconnectInnerRO() {
  innerRO?.disconnect()
  innerRO = null
}

function measure() {
  try {
    const d = frame.value?.contentDocument
    if (!d) return
    lockInnerScroll(d)
    previewNote.value = d.querySelector('.page-note')?.textContent?.trim() || ''
    disconnectInnerRO()
    const w = d.defaultView

    const apply = () => {
      const h = Math.max(pageH.value, Math.ceil(readContentBottom(d, w)) + H_PAD)
      if (h !== contentH.value) contentH.value = h
      const { count, tops, heights } = readPages(d)
      if (count !== pageCount.value) {
        pageCount.value = count
        activePage.value = Math.min(activePage.value, count)
        emit('pages', count)
      }
      if (tops.length) pageTops.value = tops
      pageHeights.value = heights
      readHotspots(d)
    }
    apply()

    // srcdoc 一换文档就重新加载，而 contentH 还停在上一版：内容更高时，
    // 在量到之前 iframe 偏矮，内部会闪出一条滚动条（改版式时最容易撞上）。
    if (w.ResizeObserver) {
      innerRO = new w.ResizeObserver(apply)
      innerRO.observe(d.body)
    }
    ready.value = true
  } catch { /* 跨源或尚未就绪，保持一整页高度即可 */ }
  nextTick(fit)
}

function go(n) {
  const target = Math.max(1, Math.min(pageCount.value, n))
  if (target === activePage.value) return
  activePage.value = target
}

/** 纸型改变后重新从纸张顶部开始，避免沿用上一张长纸的滚动位置。 */
function resetScroll() {
  if (!host.value) return
  host.value.scrollTop = 0
  host.value.scrollLeft = 0
}

let ro = null
onMounted(() => {
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => fit())
    if (host.value) ro.observe(host.value)
  }
  fit()
})

onBeforeUnmount(() => {
  ro?.disconnect()
  ro = null
  disconnectInnerRO()
})

watch(() => props.html, () => { nextTick(fit) })
watch(() => [props.paper, props.orientation], () => {
  contentH.value = 0
  activePage.value = 1
  pageTops.value = [0]
  pageHeights.value = []
  nextTick(() => {
    resetScroll()
    fit()
  })
})
watch(activePage, () => {
  const d = frame.value?.contentDocument
  if (d) readHotspots(d)
})

defineExpose({ go, fit: fitToWindow, resetScroll, getScale: () => scale.value })
</script>

<style scoped>
.stage-wrap {
  display: flex; flex-direction: column;
  height: 100%; min-height: 0; position: relative; background: #f2f4f7;
}

.stage-tools {
  position: relative; align-self: center; flex: none; margin: 10px 10px 4px;
  max-width: calc(100% - 20px);
  z-index: 12; display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; align-items: center;
  padding: 4px; border-radius: 10px;
  background: rgba(255, 255, 255, .94);
  border: 1px solid var(--cwq-hairline, #e8ebf0);
  box-shadow: 0 2px 10px rgba(15, 23, 42, .10);
}
.tool-group { display: flex; gap: 6px; align-items: center; }
.long-page { font-size: 11px; color: var(--cwq-muted, #909399); white-space: nowrap; }
.stage-tools :deep(.el-button-group .el-button) { padding: 6px 9px; }
.page-no { font-variant-numeric: tabular-nums; pointer-events: none; }
.zoom-label { min-width: 62px; font-variant-numeric: tabular-nums; }

.stage-note {
  flex: none; margin: 2px 20px 0; text-align: center;
  font-size: 11px; line-height: 18px; color: var(--cwq-muted, #909399);
}

/* 唯一的滚动容器：预览再长也只有这一条滚动条 */
.stage-host {
  flex: 1; min-height: 0; overflow: auto;
  display: flex; flex-direction: column; padding: 16px 24px 24px;
  background:
    radial-gradient(circle at 1px 1px, rgba(148, 163, 184, .28) 1px, transparent 0) 0 0 / 22px 22px,
    #f2f4f7;
}
.stage { flex: none; margin: auto; position: relative; }

.paper {
  position: relative; overflow: hidden; transform-origin: top left;
  background: #fff;
  box-shadow: 0 2px 6px rgba(15, 23, 42, .10), 0 12px 32px rgba(15, 23, 42, .10);
}
.frame {
  position: absolute; left: 0; top: 0;
  border: 0; display: block;
  background: #fff;
}
/* 编辑层铺满整张纸，坐标就是纸坐标；透明、不拦截事件的地方留给上层按需打开 */
.layer { position: absolute; left: 0; }

.stage-empty {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: #94a3b8; font-size: 14px; pointer-events: none;
}
.stage-empty.err { color: var(--cwq-danger, #dc2626); padding: 0 40px; text-align: center; }
</style>
