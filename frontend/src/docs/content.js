/**
 * 文档页的**唯一**内容来源：仓库里的 Markdown 与示例包源码。
 *
 * 为什么要从 docs/ 读而不是在页面里重写一遍：规范会改，页面不会跟着改，
 * 于是两边开始各说各话，而读者（插件作者）只能靠猜哪份是真的。这里是构建
 * 期内联进来的，规范一改重新构建就是最新，不存在「忘了同步」。
 *
 * **代价是改动这里之外的一切都做不到**：想加内容，正确的做法是往 docs/ 里
 * 新增文件再在这里引入，而不是在 Vue 模板里另写一份。
 */
import guide from '@docs/plugin-guide.md?raw'
import data from '@docs/plugin-data.md?raw'
import spec from '@docs/plugin-spec.md?raw'
import exampleManifest from '@docs/plugin-example/plugin.json?raw'
import exampleBackend from '@docs/plugin-example/backend/main.py?raw'
import exampleMigration from '@docs/plugin-example/migrations/versions/0001_note.py?raw'
import exampleFrontend from '@docs/plugin-example/frontend/index.js?raw'
import exampleStyle from '@docs/plugin-example/frontend/style.css?raw'

import { buildDocument } from './markdown'

/**
 * 页面按「先教怎么做 → 再讲数据怎么读写 → 最后给规范」的顺序排：读者第一次来
 * 要的是一条走得通的路，不是先读二十段约束。
 *
 * 数据接口单独成章而不是塞进规范里，是因为它回答的是一个具体问题 ——「我这份
 * 文件怎么变成账上的凭证」—— 而答案横跨科目编码、批量原子性和去重三件事，
 * 放在规范的能力表里只会被压成一行字。
 *
 * 教程带 `guide-` 前缀，数据带 `data-`：三份文档会有同名小节（都有「下一步读
 * 什么」、都有「Python SDK」一类），不隔开的话锚点会互相覆盖。
 */
export const guideDocument = buildDocument(guide, { idPrefix: 'guide-' })
export const dataDocument = buildDocument(data, { idPrefix: 'data-' })
export const specDocument = buildDocument(spec)

/** 页面上的章节顺序，即目录的顺序。 */
export const CHAPTERS = [guideDocument, dataDocument, specDocument]

/** 全页目录：先教程，后数据接口，最后规范。 */
export const toc = CHAPTERS.flatMap((doc) => doc.toc)

/**
 * 示例包的源文件，按「照着抄的顺序」排：先看声明装什么，再看后端、
 * 迁移、前端、样式。label 是给人看的包内相对路径，和真实打包时一致。
 */
export const EXAMPLE_FILES = [
  { label: 'plugin.json', language: 'json', source: exampleManifest },
  { label: 'backend/main.py', language: 'python', source: exampleBackend },
  { label: 'migrations/versions/0001_note.py', language: 'python', source: exampleMigration },
  { label: 'frontend/index.js', language: 'javascript', source: exampleFrontend },
  { label: 'frontend/style.css', language: 'css', source: exampleStyle },
]

/**
 * 「从哪开始」：只放锚点链接，**不复述任何字段名、约束和命令**。
 *
 * 复述就等于开了第二份事实来源 —— 源文档改了这里不会改，而读者分不清该信
 * 哪个。链接过去看到的永远是原文。
 *
 * 锚点**不手写**：手写的 id 是标题的副本，改了标题就会留下一条指向死处的
 * 链接，而这种错没有任何检查会报。所以这里只按标题去实际目录里查，查不到
 * 就整条不显示 —— 源文档改了标题，入口自动跟着消失，而不是骗人。
 */
const START_HERE_PICKS = [
  { title: '第 1 步：生成签名密钥', hint: '先把签名的这条路走通，没有它后面都白做' },
  { title: '第 2 步：写 plugin.json', hint: '声明装什么、能碰什么、菜单在哪' },
  { title: '第 6 步：签名打包', hint: '产出可上传的 .cwp，并自查一遍' },
  { title: '八个能力，没别的', hint: '插件能碰的数据全在这儿，没有别的口子' },
  { title: '第 10 步：确认它真的起来了', hint: '三个判据，少一条都不算成' },
]

export function startHere(allToc) {
  return START_HERE_PICKS
    .map((pick) => ({ ...pick, id: allToc.find((item) => item.title === pick.title)?.id }))
    .filter((item) => item.id)
}
