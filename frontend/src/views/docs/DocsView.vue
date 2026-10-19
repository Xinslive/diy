<template>
  <div class="docs">
    <header class="docs-head">
      <div class="docs-head-inner">
        <div class="docs-brand">
          <div class="docs-kicker">SDK 1.1.0 <span>·</span> CWP 1</div>
          <h1>插件开发文档</h1>
          <p class="docs-lede">
            面向插件开发者的教程、运行时契约和数据导入指南。先完成一个最小插件，再按需要查 API 和排错手册。
          </p>
          <div class="docs-meta" aria-label="文档特点">
            <span>后端 Python SDK</span>
            <span>前端自包含 ESM</span>
            <span>文件导入与整批制证</span>
          </div>
        </div>
        <router-link class="docs-back" to="/books">返回系统</router-link>
      </div>
    </header>

    <div class="docs-body">
      <aside class="docs-toc" :class="{ open: tocOpen }">
        <button class="toc-toggle" type="button" @click="tocOpen = !tocOpen">
          <span>文档目录</span>
          <span aria-hidden="true">{{ tocOpen ? '收起' : '展开' }}</span>
        </button>
        <nav aria-label="文档目录">
          <section v-for="group in navGroups" :key="group.id" class="toc-group">
            <div class="toc-group-title">{{ group.title }}</div>
            <router-link
              v-for="item in group.items"
              :key="item.id"
              :to="sectionTo(item.id)"
              :class="['toc-item', `lv${item.level}`, { current: route.query.s === item.id, chapter: item.chapter }]"
              @click="scrollToSection(item.id)"
            >{{ item.title }}</router-link>
          </section>
        </nav>
      </aside>

      <main class="docs-main">
        <section v-if="start.length" class="start-here">
          <div class="section-eyebrow">START HERE</div>
          <h2>从哪开始</h2>
          <p class="section-lede">按你的目标选择入口，页面会记住当前章节的位置，复制链接也可以直接回到同一节。</p>
          <ul>
            <li v-for="item in start" :key="item.id">
              <router-link :to="sectionTo(item.id)" @click="scrollToSection(item.id)">
                <span>{{ item.title }}</span>
                <span aria-hidden="true">→</span>
              </router-link>
              <small>{{ item.hint }}</small>
            </li>
          </ul>
        </section>

        <!-- eslint-disable vue/no-v-html -- 正文由本仓库的 Markdown 构建期渲染，
             markdown-it 以 html:false 关闭原始 HTML，产物不可能带进标签。 -->
        <section v-for="group in contentGroups" :key="group.id" class="doc-group">
          <div class="group-heading">
            <div class="section-eyebrow">{{ group.label }}</div>
            <div>
              <h2>{{ group.title }}</h2>
              <p>{{ group.description }}</p>
            </div>
          </div>

          <section v-for="chapter in group.chapters" :id="chapter.id" :key="chapter.id" class="chapter">
            <h3 class="chapter-title">{{ chapter.title }}</h3>
            <article class="prose" v-html="chapter.html" />
          </section>
        </section>

        <section id="examples" class="examples">
          <div class="group-heading">
            <div class="section-eyebrow">EXAMPLES</div>
            <div>
              <h2>示例代码</h2>
              <p>基础插件展示最小闭环，CSV 转凭证示例展示 SDK 1.1.0 的文件导入流程。点击文件名展开源码。</p>
            </div>
          </div>
          <div v-for="group in exampleGroups" :key="group.title" class="example-group">
            <h3>{{ group.title }}</h3>
            <div v-for="file in group.files" :key="fileKey(file)" class="file">
              <div class="file-head">
                <span class="file-path">{{ file.label }}</span>
                <span class="file-lang">{{ file.language }}</span>
                <button class="copy" type="button" @click="copy(file.source, fileKey(file))">
                  {{ copied === fileKey(file) ? '已复制' : '复制' }}
                </button>
              </div>
              <details>
                <summary>查看源码</summary>
                <pre><code>{{ file.source }}</code></pre>
              </details>
            </div>
          </div>
        </section>
      </main>
    </div>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { sectionTo } from '@/docs/anchor'
import { EXAMPLE_GROUPS, NAV_GROUPS, CHAPTER_GROUPS, startHere, toc } from '@/docs/content'

const route = useRoute()
const navGroups = NAV_GROUPS
const contentGroups = CHAPTER_GROUPS.map((group, index) => ({
  ...group,
  label: String(index + 1).padStart(2, '0'),
}))
const exampleGroups = EXAMPLE_GROUPS
const start = startHere(toc)
const tocOpen = ref(false)
const copied = ref('')

function scrollToSection(id) {
  // router-link 已经发起跳转，正文是静态的，等一次渲染再找元素。
  requestAnimationFrame(() => {
    const el = document.getElementById(id)
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    else tocOpen.value = false
  })
}

// 直接粘 `/#/docs?s=…` 进来时也要落到那一节。
watch(() => route.query.s, (id) => {
  tocOpen.value = false
  if (typeof id === 'string' && id) scrollToSection(id)
}, { immediate: true })

function fileKey(file) {
  return `${file.group}/${file.label}`
}

/**
 * 复制示例文件内容。失败（剪贴板被拒、非安全上下文）**不提示**：
 * 页面上的代码本来就能选中，手动复制一直可用，为它弹一个提示只会更烦。
 */
let copiedTimer = null
async function copy(text, key) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    return
  }
  copied.value = key
  clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => { copied.value = '' }, 1600)
}

/**
 * 零入口的页面更不该被收录：没有链接指向它，但路径是猜得到的。
 * 装/卸时挂上 head、离开时摘掉 —— 单页应用只有一个 head，不摘就会
 * 跟着留到别的页面上，把整站都标成 noindex。
 */
let robotsMeta = null
onMounted(() => {
  if (document.querySelector('meta[name="robots"]')) return
  robotsMeta = document.createElement('meta')
  robotsMeta.setAttribute('name', 'robots')
  robotsMeta.setAttribute('content', 'noindex, nofollow')
  document.head.appendChild(robotsMeta)
})
onUnmounted(() => {
  robotsMeta?.remove()
  robotsMeta = null
})
</script>

<style scoped>
.docs {
  min-height: 100vh;
  background: #f5f7fb;
  color: #182230;
}

.docs-head {
  background:
    radial-gradient(circle at 82% 0%, rgba(73, 125, 255, .15), transparent 32%),
    linear-gradient(135deg, #ffffff 0%, #f7faff 100%);
  border-bottom: 1px solid #e3e8f2;
  padding: 42px 28px 36px;
}

.docs-head-inner {
  max-width: 1220px;
  margin: 0 auto;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 32px;
}

.docs-brand { max-width: 780px; }
.docs-kicker,
.section-eyebrow {
  color: #4d70bc;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .14em;
  line-height: 1.4;
  text-transform: uppercase;
}
.docs-kicker span { color: #aab8d2; padding: 0 4px; }
.docs h1 { margin: 10px 0 12px; font-size: clamp(30px, 4vw, 42px); letter-spacing: -.035em; line-height: 1.12; }
.docs-lede { max-width: 700px; margin: 0; color: #526174; font-size: 16px; line-height: 1.75; }
.docs-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 22px; }
.docs-meta span { padding: 6px 10px; border: 1px solid #dfe7f5; border-radius: 999px; background: rgba(255, 255, 255, .72); color: #526174; font-size: 12px; }
.docs-back { flex: none; padding: 9px 13px; border: 1px solid #d8e1f0; border-radius: 8px; color: #4567aa; background: rgba(255, 255, 255, .78); font-size: 13px; text-decoration: none; }
.docs-back:hover { border-color: #9db4e5; background: #fff; }

.docs-body { max-width: 1220px; margin: 0 auto; padding: 28px; display: flex; align-items: flex-start; gap: 28px; }
.docs-toc { position: sticky; top: 24px; flex: 0 0 250px; max-height: calc(100vh - 48px); overflow-y: auto; padding: 14px 0; border: 1px solid #e1e7f0; border-radius: 12px; background: rgba(255, 255, 255, .92); box-shadow: 0 8px 24px rgba(31, 50, 82, .05); }
.toc-toggle { display: none; width: 100%; padding: 8px 16px; border: 0; background: transparent; color: #27364c; font-size: 14px; text-align: left; cursor: pointer; }
.toc-group + .toc-group { margin-top: 16px; }
.toc-group-title { padding: 0 16px 6px; color: #8995a8; font-size: 10px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.toc-item { display: block; padding: 6px 16px 6px 18px; border-left: 2px solid transparent; color: #5a687c; font-size: 12.5px; line-height: 1.45; text-decoration: none; }
.toc-item.lv3 { padding-left: 32px; color: #7b8798; font-size: 12px; }
.toc-item.chapter { color: #283a55; font-size: 13px; font-weight: 650; }
.toc-item:hover { border-left-color: #9bb4e9; color: #2e60bd; background: #f7f9ff; }
.toc-item.current { border-left-color: #3973db; color: #2458b2; background: #edf3ff; font-weight: 650; }

.docs-main { flex: 1; min-width: 0; }
.start-here, .doc-group, .examples { margin-bottom: 22px; padding: 26px 30px; border: 1px solid #e1e7f0; border-radius: 14px; background: #fff; box-shadow: 0 8px 24px rgba(31, 50, 82, .04); }
.start-here h2, .group-heading h2 { margin: 4px 0 6px; color: #17253a; font-size: 22px; letter-spacing: -.02em; }
.section-lede, .group-heading p { margin: 0; color: #6b788a; font-size: 13px; line-height: 1.7; }
.start-here ul { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 20px 0 0; padding: 0; list-style: none; }
.start-here li { min-width: 0; padding: 12px 14px; border: 1px solid #e6ebf3; border-radius: 10px; background: #fbfcfe; }
.start-here li:hover { border-color: #bdd0f5; background: #f7faff; }
.start-here li a { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: #2e60bd; font-size: 13px; font-weight: 650; text-decoration: none; }
.start-here li a span:last-child { color: #82a0d8; font-size: 16px; }
.start-here small { display: block; margin-top: 5px; color: #7a8798; font-size: 12px; line-height: 1.5; }

.group-heading { display: flex; align-items: flex-start; gap: 16px; padding-bottom: 18px; border-bottom: 1px solid #edf0f5; }
.group-heading .section-eyebrow { flex: none; min-width: 28px; padding-top: 7px; }
.chapter { scroll-margin-top: 24px; }
.chapter + .chapter { margin-top: 36px; padding-top: 30px; border-top: 1px solid #edf0f5; }
.chapter-title { margin: 22px 0 4px; color: #1e2e45; font-size: 22px; letter-spacing: -.02em; }
.prose :deep(h2) { margin: 30px 0 12px; color: #263852; font-size: 18px; letter-spacing: -.015em; scroll-margin-top: 24px; }
.prose :deep(h2:first-of-type) { margin-top: 22px; }
.prose :deep(h3) { margin: 24px 0 9px; color: #354965; font-size: 15.5px; scroll-margin-top: 24px; }
.prose :deep(p) { margin: 0 0 13px; color: #4e5d71; font-size: 14px; line-height: 1.85; }
.prose :deep(ul), .prose :deep(ol) { margin: 0 0 14px; padding-left: 22px; color: #4e5d71; font-size: 14px; line-height: 1.85; }
.prose :deep(li) { margin-bottom: 5px; }
.prose :deep(a) { color: #2e60bd; }
.prose :deep(table) { display: block; width: 100%; overflow-x: auto; margin: 0 0 18px; border-collapse: collapse; font-size: 13px; }
.prose :deep(th), .prose :deep(td) { padding: 8px 10px; border: 1px solid #e2e7ef; color: #4c5d73; text-align: left; vertical-align: top; }
.prose :deep(th) { background: #f6f8fc; color: #334966; font-weight: 650; white-space: nowrap; }
.prose :deep(code) { padding: 2px 5px; border-radius: 4px; background: #f0f3f8; color: #344e78; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }
.prose :deep(pre) { margin: 0 0 18px; padding: 16px 18px; overflow-x: auto; border: 1px solid #29344a; border-radius: 9px; background: #1e2738; color: #e8edf6; font-size: 12.5px; line-height: 1.7; }
.prose :deep(pre code) { padding: 0; background: none; color: inherit; font-size: inherit; }
.prose :deep(blockquote) { margin: 0 0 14px; padding: 4px 15px; border-left: 3px solid #9db7e9; color: #6b788a; }

.examples { scroll-margin-top: 24px; }
.example-group + .example-group { margin-top: 28px; padding-top: 24px; border-top: 1px solid #edf0f5; }
.example-group h3 { margin: 22px 0 12px; color: #263852; font-size: 16px; }
.file { margin-bottom: 12px; border: 1px solid #e2e7ef; border-radius: 9px; overflow: hidden; }
.file-head { display: flex; align-items: center; gap: 9px; padding: 9px 12px; background: #f8faff; }
.file-path { color: #354965; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; font-weight: 600; }
.file-lang { color: #8793a4; font-size: 11px; }
.copy { margin-left: auto; padding: 4px 9px; border: 1px solid #d4def0; border-radius: 5px; background: #fff; color: #3868b8; font-size: 11.5px; cursor: pointer; }
.copy:hover { border-color: #88a7e0; background: #f7faff; }
.file details summary { padding: 8px 12px; border-top: 1px solid #e7ebf2; color: #58709a; font-size: 12px; cursor: pointer; }
.file details[open] summary { border-bottom: 1px solid #2b3548; background: #222d40; color: #cbd8ef; }
.file pre { margin: 0; padding: 15px 16px; overflow-x: auto; background: #1e2738; color: #e8edf6; font-size: 12.5px; line-height: 1.7; }
.file code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

@media (max-width: 900px) {
  .docs-head { padding: 30px 20px 26px; }
  .docs-head-inner { flex-direction: column; gap: 18px; }
  .docs-body { flex-direction: column; gap: 16px; padding: 18px 14px; }
  .docs-toc { position: static; width: 100%; max-height: none; padding: 0; }
  .toc-toggle { display: flex; align-items: center; justify-content: space-between; }
  .docs-toc nav { display: none; padding-bottom: 10px; }
  .docs-toc.open nav { display: block; }
  .start-here, .doc-group, .examples { padding: 22px 18px; border-radius: 11px; }
  .start-here ul { grid-template-columns: 1fr; }
}

@media (max-width: 560px) {
  .docs h1 { font-size: 32px; }
  .docs-lede { font-size: 14px; }
  .docs-meta { display: grid; grid-template-columns: 1fr; }
  .group-heading { gap: 10px; }
  .group-heading .section-eyebrow { min-width: 24px; font-size: 10px; }
  .chapter-title { font-size: 20px; }
}
</style>
