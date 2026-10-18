<template>
  <div class="docs">
    <header class="docs-head">
      <div class="docs-head-inner">
        <div>
          <h1>插件开发文档</h1>
          <p class="docs-lede">
            先按教程走通一遍，再回头查规范里的逐字段定义。全部内容来自仓库 <code>docs/</code>
            下的 Markdown，与源文件逐字一致。
          </p>
        </div>
        <router-link class="docs-back" to="/books">返回系统</router-link>
      </div>
    </header>

    <div class="docs-body">
      <aside class="docs-toc" :class="{ open: tocOpen }">
        <button class="toc-toggle" type="button" @click="tocOpen = !tocOpen">
          目录
        </button>
        <nav aria-label="文档目录">
          <router-link
            v-for="item in toc"
            :key="item.id"
            :to="sectionTo(item.id)"
            :class="['toc-item', `lv${item.level}`, { current: route.query.s === item.id }]"
            @click="scrollToSection(item.id)"
          >{{ item.title }}</router-link>
        </nav>
      </aside>

      <main class="docs-main">
        <section v-if="start.length" class="start-here">
          <h2>从哪开始</h2>
          <ul>
            <li v-for="item in start" :key="item.id">
              <router-link :to="sectionTo(item.id)" @click="scrollToSection(item.id)">
                {{ item.title }}
              </router-link>
              <span>{{ item.hint }}</span>
            </li>
          </ul>
        </section>

        <!-- eslint-disable vue/no-v-html -- 正文由本仓库的 Markdown 构建期渲染，
             markdown-it 以 html:false 关闭了原始 HTML，产物不可能带进标签。 -->
        <section v-for="chapter in chapters" :key="chapter.title" class="chapter">
          <h2 class="chapter-title">{{ chapter.title }}</h2>
          <article class="prose" v-html="chapter.html" />
        </section>

        <section class="examples">
          <h2 id="示例包">示例包</h2>
          <p>
            两个完整示例分别位于 plugin-example 和 plugin-import-example 目录。每个目录独立签名打包；
            下面直接展示仓库源码，文件转凭证示例需要 SDK 1.1.0。
          </p>
          <div v-for="file in exampleFiles" :key="file.label" class="file">
            <div class="file-head">
              <span class="file-path">{{ file.label }}</span>
              <span class="file-lang">{{ file.language }}</span>
              <button class="copy" type="button" @click="copy(file.source, file.label)">{{ copied === file.label ? '已复制' : '复制' }}</button>
            </div>
            <pre><code>{{ file.source }}</code></pre>
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
import { CHAPTERS, EXAMPLE_FILES, startHere, toc } from '@/docs/content'

const route = useRoute()
const chapters = CHAPTERS
const exampleFiles = EXAMPLE_FILES
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

/**
 * 复制示例文件内容。失败（剪贴板被拒、非安全上下文）**不提示**：
 * 页面上的代码本来就能选中，手动复制一直可用，为它弹一个提示只会更烦。
 */
let copiedTimer = null
async function copy(text, label) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    return
  }
  copied.value = label
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
  background: #f6f7f9;
  color: #1f2329;
}

.docs-head {
  background: #fff;
  border-bottom: 1px solid #e5e6eb;
  padding: 28px 24px 22px;
}

.docs-head-inner {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.docs-head h1 {
  margin: 0 0 6px;
  font-size: 24px;
  font-weight: 600;
}

.docs-lede {
  margin: 0;
  color: #60646c;
  font-size: 14px;
  line-height: 1.7;
}

.docs-lede code {
  background: #f0f1f3;
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 13px;
}

.docs-back {
  flex: none;
  font-size: 13px;
  color: #3370ff;
  text-decoration: none;
  padding: 5px 10px;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  background: #fff;
  white-space: nowrap;
}

.docs-back:hover {
  border-color: #3370ff;
}

.docs-body {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
  display: flex;
  gap: 28px;
  align-items: flex-start;
}

.docs-toc {
  flex: none;
  width: 220px;
  position: sticky;
  top: 24px;
  max-height: calc(100vh - 48px);
  overflow-y: auto;
  background: #fff;
  border: 1px solid #e5e6eb;
  border-radius: 6px;
  padding: 12px 0;
}

.toc-toggle {
  display: none;
  width: 100%;
  border: 0;
  background: transparent;
  font-size: 14px;
  padding: 8px 16px;
  text-align: left;
  cursor: pointer;
}

.toc-item {
  display: block;
  padding: 5px 16px;
  font-size: 13px;
  line-height: 1.5;
  color: #4a4e57;
  text-decoration: none;
  border-left: 2px solid transparent;
}

.toc-item.lv3 {
  padding-left: 30px;
  font-size: 12.5px;
  color: #6a6e77;
}

.toc-item:hover {
  color: #3370ff;
  border-left-color: #3370ff;
}

/* 当前位置。
 *
 * 不用 router-link 自带的 router-link-active：它只比路径不比 query，而这一页
 * 所有链接的路径都是 /docs，于是整份目录会同时「全部选中」。这里直接拿
 * route.query.s 自己比，那个才是真正决定落在哪一节的值。 */
.toc-item.current {
  color: #3370ff;
  font-weight: 600;
  background: #f2f6ff;
  border-left-color: #3370ff;
}

.docs-main {
  flex: 1;
  min-width: 0;
}

.start-here,
.chapter,
.examples {
  background: #fff;
  border: 1px solid #e5e6eb;
  border-radius: 6px;
  padding: 20px 24px;
  margin-bottom: 20px;
}

.start-here h2 {
  margin: 0 0 12px;
  font-size: 17px;
}

.start-here ul {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 10px;
}

.start-here li {
  border: 1px solid #eceef1;
  border-radius: 5px;
  padding: 10px 12px;
}

.start-here a {
  display: block;
  color: #3370ff;
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 2px;
}

.start-here span {
  color: #6a6e77;
  font-size: 12.5px;
  line-height: 1.6;
}

/* 章节标题：页面头部已经是 h1，每份文档的标题降一级才不会把层级撑乱 */
.chapter-title {
  margin: 0 0 4px;
  font-size: 21px;
  font-weight: 600;
  padding-bottom: 14px;
  border-bottom: 1px solid #eef0f2;
}

/* -- 正文排版 --------------------------------------------------------- */
.prose :deep(h2) {
  font-size: 17px;
  margin: 30px 0 12px;
}
.prose :deep(h2:first-of-type) { margin-top: 16px; }
.prose :deep(h3) { font-size: 15.5px; margin: 22px 0 8px; }
.prose :deep(p) { font-size: 14px; line-height: 1.85; margin: 0 0 12px; }
.prose :deep(ul), .prose :deep(ol) { font-size: 14px; line-height: 1.85; padding-left: 22px; margin: 0 0 12px; }
.prose :deep(li) { margin-bottom: 4px; }
.prose :deep(a) { color: #3370ff; }

/* manifest 字段表很宽，不横向滚动就会被挤成每行一个字 */
.prose :deep(table) {
  display: block;
  width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
  margin: 0 0 16px;
  font-size: 13px;
}
.prose :deep(th), .prose :deep(td) {
  border: 1px solid #e5e6eb;
  padding: 7px 10px;
  text-align: left;
  vertical-align: top;
}
.prose :deep(th) { background: #f7f8fa; font-weight: 600; white-space: nowrap; }

.prose :deep(code) {
  background: #f0f1f3;
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 12.5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.prose :deep(pre) {
  background: #1f2430;
  color: #e6e6e6;
  padding: 14px 16px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 12.5px;
  line-height: 1.7;
}
.prose :deep(pre code) { background: none; padding: 0; color: inherit; font-size: inherit; }

.prose :deep(blockquote) {
  margin: 0 0 12px;
  padding: 2px 14px;
  border-left: 3px solid #dcdfe6;
  color: #6a6e77;
}

/* -- 示例包 ----------------------------------------------------------- */
.examples h2 { margin: 0 0 8px; font-size: 18px; }
.examples > p { font-size: 14px; color: #60646c; margin: 0 0 16px; }

.file { margin-bottom: 16px; }
.file-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 10px;
  background: #f7f8fa;
  border: 1px solid #e5e6eb;
  border-bottom: 0;
  border-radius: 6px 6px 0 0;
}
.file-path { font-size: 13px; font-weight: 500; }
.file-lang { font-size: 11.5px; color: #8a8e96; }
.copy {
  margin-left: auto;
  font-size: 12px;
  color: #3370ff;
  background: #fff;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  padding: 2px 10px;
  cursor: pointer;
}
.copy:hover { border-color: #3370ff; }
.file pre {
  margin: 0;
  background: #1f2430;
  color: #e6e6e6;
  padding: 14px 16px;
  border-radius: 0 0 6px 6px;
  overflow-x: auto;
  font-size: 12.5px;
  line-height: 1.7;
}
.file code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

/* -- 窄屏：目录收成可展开的横条 ---------------------------------------- */
@media (max-width: 900px) {
  .docs-body { flex-direction: column; gap: 16px; }
  .docs-toc {
    width: 100%;
    position: static;
    max-height: none;
    padding: 0;
  }
  .toc-toggle { display: block; }
  .docs-toc nav { display: none; }
  .docs-toc.open nav { display: block; padding-bottom: 8px; }
  .docs-head-inner { flex-direction: column; }
}
</style>
