/**
 * 公开文档的唯一内容入口。
 *
 * 公开页只装载面向插件开发者的教程、契约、数据导入和排错文档。
 * 生产风险记录、验收报告和内部路线图仍然可以留在仓库里，但不属于开发者
 * 文档，不应随着公开页面一起渲染。
 */
import overview from '@docs/plugin-overview.md?raw'
import guide from '@docs/plugin-guide.md?raw'
import spec from '@docs/plugin-spec.md?raw'
import data from '@docs/plugin-data.md?raw'
import troubleshooting from '@docs/plugin-troubleshooting.md?raw'
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
import { buildDocument, slugify } from './markdown'

function makeChapter(document, prefix) {
  return { ...document, id: `chapter-${prefix}${slugify(document.title)}` }
}

export const overviewDocument = makeChapter(buildDocument(overview, { idPrefix: 'overview-' }), 'overview-')
export const guideDocument = makeChapter(buildDocument(guide, { idPrefix: 'guide-' }), 'guide-')
export const specDocument = makeChapter(buildDocument(spec, { idPrefix: 'spec-' }), 'spec-')
export const dataDocument = makeChapter(buildDocument(data, { idPrefix: 'data-' }), 'data-')
export const troubleshootingDocument = makeChapter(
  buildDocument(troubleshooting, { idPrefix: 'troubleshooting-' }),
  'troubleshooting-',
)

export const CHAPTER_GROUPS = [
  {
    id: 'start',
    title: '开始开发',
    description: '先了解插件边界，再完成一个可以安装的最小插件。',
    chapters: [overviewDocument, guideDocument],
  },
  {
    id: 'reference',
    title: '开发参考',
    description: '开发过程中反复查阅的 manifest、权限和运行时契约。',
    chapters: [specDocument],
  },
  {
    id: 'workflow',
    title: '业务流程',
    description: '文件导入、账套配置和整批制证的标准实现方式。',
    chapters: [dataDocument],
  },
  {
    id: 'release',
    title: '发布与排错',
    description: '打包、安装、升级后遇到问题时，从正确的阶段开始定位。',
    chapters: [troubleshootingDocument],
  },
]

export const CHAPTERS = CHAPTER_GROUPS.flatMap(group => group.chapters)

function chapterItems(chapter) {
  return [
    { id: chapter.id, title: chapter.title, level: 1, chapter: true },
    ...chapter.toc,
  ]
}

export const EXAMPLES_SECTION = { id: 'examples', title: '示例代码', level: 1, chapter: true }

export const NAV_GROUPS = [
  ...CHAPTER_GROUPS.map(group => ({
    ...group,
    items: group.chapters.flatMap(chapterItems),
  })),
  {
    id: 'examples-group',
    title: '示例代码',
    description: '两个可以直接阅读和复制的完整示例。',
    items: [EXAMPLES_SECTION],
  },
]

export const toc = [
  ...NAV_GROUPS.flatMap(group => group.items),
]

export const EXAMPLE_FILES = [
  { group: '基础插件', label: 'plugin.json', language: 'json', source: exampleManifest },
  { group: '基础插件', label: 'backend/main.py', language: 'python', source: exampleBackend },
  { group: '基础插件', label: 'migrations/versions/0001_note.py', language: 'python', source: exampleMigration },
  { group: '基础插件', label: 'frontend/index.js', language: 'javascript', source: exampleFrontend },
  { group: '基础插件', label: 'frontend/style.css', language: 'css', source: exampleStyle },
  { group: 'CSV 转凭证', label: 'plugin.json', language: 'json', source: csvManifest },
  { group: 'CSV 转凭证', label: 'backend/main.py', language: 'python', source: csvBackend },
  { group: 'CSV 转凭证', label: 'migrations/versions/0001_config.py', language: 'python', source: csvMigration },
  { group: 'CSV 转凭证', label: 'frontend/index.js', language: 'javascript', source: csvFrontend },
  { group: 'CSV 转凭证', label: 'frontend/style.css', language: 'css', source: csvStyle },
]

export const EXAMPLE_GROUPS = ['基础插件', 'CSV 转凭证'].map(title => ({
  title,
  files: EXAMPLE_FILES.filter(file => file.group === title),
}))

const START_HERE_PICKS = [
  { title: '插件开发总览', hint: '先理解插件能做什么，以及宿主负责什么' },
  { title: '快速开始：从零创建一个插件', hint: '按步骤完成一个最小可安装插件' },
  { title: 'Manifest、权限与运行时参考', hint: '查 capability、入口和宿主 API' },
  { title: '数据导入 API：从文件到整批凭证', hint: '实现上传、预览、确认和提交' },
  { title: '排错与错误码', hint: '按安装、加载、授权和业务阶段定位问题' },
  { title: '示例代码', hint: '查看可以直接复制的完整示例源码' },
]

export function startHere(allToc) {
  return START_HERE_PICKS
    .map(pick => ({ ...pick, id: allToc.find(item => item.title === pick.title)?.id }))
    .filter(item => item.id)
}
