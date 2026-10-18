<template>
  <div class="docs">
    <header class="docs-head">
      <div class="docs-head-inner">
        <div>
          <h1>插件开发文档</h1>
          <p class="docs-lede">
            写一个插件包需要知道的事，全部来自仓库 <code>docs/plugin-spec.md</code>，
            与规范正文逐字一致。
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
          <a
            v-for="item in doc.toc"
            :key="item.id"
            :href="`#${item.id}`"
            :class="['toc-item', `lv${item.level}`]"
            @click="onTocClick"
          >{{ item.title }}</a>
        </nav>
      </aside>

      <main class="docs-main">
        <section v-if="start.length" class="start-here">
          <h2>从哪开始</h2>
          <ul>
            <li v-for="item in start" :key="item.id">
              <a :href="`#${item.id}`">{{ item.title }}</a>
              <span>{{ item.hint }}</span>
            </li>
          </ul>
        </section>

        <!-- eslint-disable vue/no-v-html -- 正文由本仓库的 Markdown 构建期渲染，
             markdown-it 以 html:false 关闭了原始 HTML，产物不可能带进标签。 -->
        <article class="prose" v-html="doc.html"></article>

        <section class="examples">
          <h2 id="示例包">示例包</h2>
          <p>
            一个能装上、能授权、能升级也能卸干净的完整示例。下面是它的全部源文件，
            顺序就是照着写的顺序。
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
import { ref } from 'vue'
import { EXAMPLE_FILES, specDocument, startHere } from '@/docs/content'

const doc = specDocument
const exampleFiles = EXAMPLE_FILES
const start = startHere(doc.toc)
const tocOpen = ref(false)
const copied = ref('')

/**
 * 目录跳转只改 hash，不触发路由跳转 —— 这一页是独立顶层路由，
 * 走 router.push 会把 hash 路由的路径一起改掉，反而离开文档页。
 * 移动端点完顺手收起目录，否则它盖住刚跳到的那一段。
 */
function onTocClick() {
  tocOpen.value = false
}

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

.docs-main {
  flex: 1;
  min-width: 0;
}

.start-here,
.prose,
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

/* -- 正文排版 --------------------------------------------------------- */
.prose :deep(h1) { font-size: 22px; margin: 0 0 16px; }
.prose :deep(h2) {
  font-size: 18px;
  margin: 32px 0 12px;
  padding-top: 20px;
  border-top: 1px solid #eef0f2;
}
.prose :deep(h2:first-of-type) { border-top: 0; padding-top: 0; margin-top: 0; }
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
