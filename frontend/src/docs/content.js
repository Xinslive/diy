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
import spec from '@docs/plugin-spec.md?raw'
import exampleManifest from '@docs/plugin-example/plugin.json?raw'
import exampleBackend from '@docs/plugin-example/backend/main.py?raw'
import exampleMigration from '@docs/plugin-example/migrations/versions/0001_note.py?raw'
import exampleFrontend from '@docs/plugin-example/frontend/index.js?raw'
import exampleStyle from '@docs/plugin-example/frontend/style.css?raw'

import { buildDocument } from './markdown'

/** 规范正文与其目录。 */
export const specDocument = buildDocument(spec)

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
 * 「从哪开始」：只放锚点链接，**不复述规范里的任何字段名、约束和命令**。
 *
 * 复述就等于开了第二份事实来源 —— 规范改了这里不会改，而读者分不清该信
 * 哪个。链接过去看到的永远是原文。
 *
 * 锚点**不手写**：手写的 id 是规范标题的副本，改了标题就会留下一条指向
 * 死处的链接，而这种错没有任何检查会报。所以这里只按标题去实际目录里查，
 * 查不到就整条不显示 —— 规范改了标题，入口自动跟着消失，而不是骗人。
 */
const START_HERE_PICKS = [
  { title: '信任与授权', hint: '先弄清哪些代码会被执行、权限怎么发' },
  { title: '包和签名', hint: '整包结构、摘要、签名与被拒绝的情形' },
  { title: 'manifest', hint: '逐字段对照，声明错了装不上' },
  { title: 'Python SDK', hint: '账套、凭证与钩子怎么拿到' },
]

export function startHere(toc) {
  return START_HERE_PICKS
    .map((pick) => ({ ...pick, id: toc.find((item) => item.title === pick.title)?.id }))
    .filter((item) => item.id)
}
