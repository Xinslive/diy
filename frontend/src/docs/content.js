/** 文档页只内联仓库源文档和示例源码；不维护另一份接口说明。 */
import guide from '@docs/plugin-guide.md?raw'
import data from '@docs/plugin-data.md?raw'
import spec from '@docs/plugin-spec.md?raw'
import risk from '@docs/plugin-risk.md?raw'
import verification from '@docs/plugin-verification.md?raw'
import roadmap from '@docs/plugin-roadmap.md?raw'
import exampleManifest from '@docs/plugin-example/plugin.json?raw'
import exampleBackend from '@docs/plugin-example/backend/main.py?raw'
import exampleMigration from '@docs/plugin-example/migrations/versions/0001_note.py?raw'
import exampleFrontend from '@docs/plugin-example/frontend/index.js?raw'
import exampleStyle from '@docs/plugin-example/frontend/style.css?raw'
import csvManifest from '@docs/plugin-import-example/plugin.json?raw'
import csvBackend from '@docs/plugin-import-example/backend/main.py?raw'
import csvMigration from '@docs/plugin-import-example/migrations/versions/0001_config.py?raw'
import csvFrontend from '@docs/plugin-import-example/frontend/index.js?raw'
import csvStyle from '@docs/plugin-import-example/frontend/style.css?raw'
import { buildDocument } from './markdown'

export const guideDocument = buildDocument(guide, { idPrefix: 'guide-' })
export const dataDocument = buildDocument(data, { idPrefix: 'data-' })
export const specDocument = buildDocument(spec)
export const riskDocument = buildDocument(risk, { idPrefix: 'risk-' })
export const verificationDocument = buildDocument(verification, { idPrefix: 'verification-' })
export const roadmapDocument = buildDocument(roadmap, { idPrefix: 'roadmap-' })
export const CHAPTERS = [guideDocument, dataDocument, specDocument, riskDocument, verificationDocument, roadmapDocument]
export const toc = CHAPTERS.flatMap(doc => doc.toc)

export const EXAMPLE_FILES = [
  { label: 'plugin-example/plugin.json', language: 'json', source: exampleManifest },
  { label: 'plugin-example/backend/main.py', language: 'python', source: exampleBackend },
  { label: 'plugin-example/migrations/versions/0001_note.py', language: 'python', source: exampleMigration },
  { label: 'plugin-example/frontend/index.js', language: 'javascript', source: exampleFrontend },
  { label: 'plugin-example/frontend/style.css', language: 'css', source: exampleStyle },
  { label: 'plugin-import-example/plugin.json', language: 'json', source: csvManifest },
  { label: 'plugin-import-example/backend/main.py', language: 'python', source: csvBackend },
  { label: 'plugin-import-example/migrations/versions/0001_config.py', language: 'python', source: csvMigration },
  { label: 'plugin-import-example/frontend/index.js', language: 'javascript', source: csvFrontend },
  { label: 'plugin-import-example/frontend/style.css', language: 'css', source: csvStyle },
]

const START_HERE_PICKS = [
  { title: '第 1 步：生成签名密钥', hint: '先准备打包和验签使用的密钥' },
  { title: '第 2 步：写 plugin.json', hint: '声明入口、能力、表和菜单' },
  { title: '第 6 步：签名打包', hint: '产出可上传的 .cwp' },
  { title: 'SDK 能力', hint: '查询、配置和完整导入流程' },
  { title: '文件转凭证示例', hint: '可签名运行的 CSV 插件与使用步骤' },
  { title: '第 10 步：确认它真的起来了', hint: '检查就绪、菜单与授权' },
]
export function startHere(allToc) {
  return START_HERE_PICKS.map(pick => ({ ...pick, id: allToc.find(item => item.title === pick.title)?.id })).filter(item => item.id)
}
