<template>
  <div class="pbox" ref="box">
    <div class="stage" :style="stageStyle">
      <iframe
        ref="frame" class="frame" :srcdoc="html" :style="frameStyle"
        title="打印预览" sandbox="allow-same-origin" @load="measure"
      />
    </div>
    <p v-if="!html" class="empty">正在生成预览…</p>
  </div>
</template>

<script setup>
/**
 * 按**纸张真实尺寸**渲染一份打印页 HTML。
 *
 * 之所以按真实毫米尺寸而不是「塞满容器」：打印预览的全部价值在于所见即所得。
 * 把 A4 缩成一张窄条给用户看，用户看到的分页位置、每页能放几张凭证全都不作数，
 * 等纸打出来才发现不对，那时预览反而是误导。
 *
 * 纸型尺寸来自后端 /print/templates/meta 下发的 papers（w/h 单位毫米），
 * **不在这里写死一份常量**：后端 PAPERS 一改，这份表就会悄悄过期 ——
 * 而且过期时没有任何报错，只是预览框按错误的宽度缩放，看着还挺正常。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { fontsReady } from '@/utils/printFont'

const props = defineProps({
  /** 打印页的完整 HTML（由调用方带鉴权取回）。 */
  html: { type: String, default: '' },
  /** 纸型 key，如 A4 / A5 / CONTINUOUS。 */
  paper: { type: String, default: 'A4' },
  /** portrait | landscape。 */
  orientation: { type: String, default: 'portrait' },
  /** 后端 meta.papers：[{ key, w, h, landscape }, ...]，w/h 单位毫米。 */
  papers: { type: Array, default: () => [] },
})

const box = ref(null)
const frame = ref(null)
const scale = ref(1)
const contentH = ref(0)

const MM2PX = 96 / 25.4

/** 当前纸张在 96dpi 下的像素尺寸；orientation 为横向时宽高互换。 */
const pagePx = computed(() => {
  const spec = props.papers.find((p) => p.key === props.paper)
  const w = spec?.w || 210
  const h = spec?.h || 297
  const landscape = props.orientation === 'landscape'
  return {
    w: Math.round((landscape ? h : w) * MM2PX),
    h: Math.round((landscape ? w : h) * MM2PX),
  }
})

/** 量高留的兜底余量：一像素的取整误差就够顶出一条内部滚动条。 */
const H_PAD = 2

const frameStyle = computed(() => {
  const { w, h } = pagePx.value
  return {
    width: w + 'px',
    // 多页预览时按内容高度撑开，至少一整页
    height: Math.max(h, contentH.value || h) + 'px',
    transform: `scale(${scale.value})`,
  }
})

const stageStyle = computed(() => {
  const { w, h } = pagePx.value
  return {
    width: Math.round(w * scale.value) + 'px',
    height: Math.round(Math.max(h, contentH.value || h) * scale.value) + 'px',
  }
})

/** 容器宽度变了（窗口缩放、面板重排）就重算缩放比。 */
let ro = null
function fit() {
  const host = box.value
  if (!host) return
  // clientWidth 是「内容 + padding」，真正能放版面的只有扣掉左右 padding 的内容宽。
  const cs = getComputedStyle(host)
  const availW = host.clientWidth
    - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0)
  if (availW <= 0) return
  // 宽度不够就缩小；宽度富余则适度放大到 1.4 倍把版面填满，
  // 不再拿高度卡一刀 —— 否则 A5 在宽面板里被压成灰底中间的一小块。
  scale.value = Math.max(0.15, Math.min(1.4, availW / pagePx.value.w))
}

/**
 * 预览文档必须**永远不自己滚动**：外层 .pbox 才是唯一的滚动容器。
 *
 * iframe 上那个 scrolling="no" 是 HTML5 早就废弃的属性，Chrome 根本不认 ——
 * 只要文档比 iframe 高出一丁点，iframe 内部就会自己冒出一条滚动条。滚轮悬在
 * 预览上时先被这条「几乎滚不动」的滚动条吃掉，表现就是预览滑不动；而外层同时
 * 也有滚动条，于是同时出现两条，滑哪条都别扭。
 * 真正管用的是往文档里插一条 overflow 规则。
 */
function lockInnerScroll(d) {
  const host = d.head || d.documentElement
  if (!host || d.getElementById('__print_lock')) return
  const st = d.createElement('style')
  st.id = '__print_lock'
  st.textContent = 'html,body{overflow:hidden!important;}'
  host.appendChild(st)
}

/** 量「排版后内容真正占多高」的下沿，不受 iframe 视口高影响。 */
function readContentBottom(d, w) {
  const body = d.body
  if (!body) return 0
  // body 自身的下沿兜住「溢出 body 的内容」（绝对定位、超长子表格）。
  let bottom = body.getBoundingClientRect().bottom
  for (const node of body.children) {
    const r = node.getBoundingClientRect()
    const mb = parseFloat(w.getComputedStyle(node).marginBottom) || 0
    bottom = Math.max(bottom, r.bottom + mb)
  }
  return bottom
}

/** 文档内部的尺寸观察者：内容变高时自动把 iframe 补高。 */
let innerRO = null
function disconnectInnerRO() {
  innerRO?.disconnect()
  innerRO = null
}

/**
 * iframe 是 about:srcdoc，同源可读：量出真实内容高度，好让多页预览不被裁掉。
 *
 * 量的是**最后一个纸页的下边缘**，不是 documentElement.scrollHeight ——
 * scrollHeight 永远不小于视口高，而视口高又是我们设进去的，拿它定高会变成
 * 「越量越高」的死循环（实测一张 A5 横向被撑到 2795px，实际只有 1 页 559px）。
 *
 * sandbox 只给 allow-same-origin 是刻意的：这里要读 contentDocument 量高度，
 * 去掉它量不了；但**不能**同时给 allow-scripts —— 两者同时给等于没 sandbox，
 * 脚本能跑 + 同源就可以读父页的 localStorage（登录 token 就在里面）。
 */
function measure() {
  try {
    const d = frame.value?.contentDocument
    if (!d) return
    lockInnerScroll(d)
    disconnectInnerRO()
    const w = d.defaultView

    const apply = () => {
      const h = Math.max(pagePx.value.h, Math.ceil(readContentBottom(d, w)) + H_PAD)
      if (h !== contentH.value) contentH.value = h
    }
    apply()

    // srcdoc 一换就重新加载文档，而 contentH 还停在上一版的高度：新内容更高时，
    // 在量到之前 iframe 是偏矮的，内部就会闪出一条滚动条（改纸型时最容易撞上）。
    if (w.ResizeObserver) {
      innerRO = new w.ResizeObserver(apply)
      innerRO.observe(d.body)
    }

    // **字体到位后重量一次。**
    //
    // 打印页可能内嵌一份随应用发布的字体（模板里的「锁定字体」）。iframe 的
    // load 事件只说明文档加载完了，字体可能还在路上 —— 那时量到的是**系统
    // 字体**的排版（摘要折行的位置、行的高度都可能不一样）。等字体到位
    // 再量一次，预览才是真正要印出来的那一份。
    fontsReady(d).then(() => {
      apply()
      nextTick(fit)
    })
  } catch { /* 跨源或尚未就绪，保持一整页高度即可 */ }
  nextTick(fit)
}

watch(() => props.html, () => nextTick(fit))
watch(() => [props.paper, props.orientation], () => nextTick(fit))

onMounted(() => {
  if (typeof ResizeObserver === 'undefined') return
  ro = new ResizeObserver(fit)
  if (box.value) ro.observe(box.value)
  fit()
})

onBeforeUnmount(() => {
  ro?.disconnect()
  ro = null
  disconnectInnerRO()
})
</script>

<style scoped>
/* 唯一的滚动容器：预览区再长也只有这一条滚动条。 */
.pbox {
  flex: 1; min-height: 0; overflow: auto; padding: 12px;
  background: #f0f2f5; text-align: center;
}
/* transform 缩放不改变布局尺寸，所以外层要一个同尺寸的盒子把缩放后的版面接住，
   否则 iframe 缩到 0.5 倍会在右边和底下留出等量的空白。 */
.stage { margin: 0 auto; }
.frame {
  border: 0; display: block; transform-origin: top left;
  background: #fff; box-shadow: 0 1px 6px rgba(0, 0, 0, .16);
}
.empty { color: #909399; font-size: 13px; margin: 24px 0; }
</style>