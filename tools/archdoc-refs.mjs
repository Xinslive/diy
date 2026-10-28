#!/usr/bin/env node
/**
 * 段号引用契约闸门。
 *
 * **为什么要有这个。** 章节号不是装饰，是**引用契约**：代码、脚本、部署文档、
 * 审计报告里按 `§11.7` 这样指认章节。而**目录锚点断了会有人发现，编号被换给
 * 别的章节不会** —— 引用照样「解析得到」，点过去看到的是另一件事，安静地失效。
 *
 * 真实踩过的两个坑，都是这类静默失效：
 *   · `§11.7.12` 出现了两次（第二个排在 `§11.7.13` 之后）—— 引用它的人拿到
 *     的是哪一份？取决于谁先谁后，而顺序会随重组变化。
 *   · 「见 §11.13.4」指不到任何东西（§11.13 的子标题从来没编号过）——
 *     引用看着合法，实际是空的。
 *
 * 本脚本把三件事钉死：
 *   ① 编号唯一：同一个编号不能在 ARCHITECTURE.md 里指向两个标题；
 *   ② 基线不变：`tools/archdoc-sections.json` 里登记的「编号 → 它必须指的东西」
 *      一字都不能改，也不能消失（**新增编号要跑 `--update` 登记**）；
 *   ③ 引用可解：文档正文与仓库里出现的每一个 `§N`，都要能在**某份编号文档**里
 *      找到对应的标题。
 *
 * **③ 为什么不是「只查本文档」。** 本仓库有**好几套并行的编号体系**：本文档、
 * 审计报告、audit-remediation、esa-mtls、tuning.md —— 代码里的 `§3.15` 指的是
 * 审计报告，`§4.5` 在审计报告和本文档里**都存在**（而且指的不是一回事）。
 * 所以「可解」的判据是「在这些文档的标题里能找到」，而不是「在本文档里能找到」——
 * 后者会把几百处合法的外部引用判成断链，判据一旦有噪声就没人看。
 *
 * **⚠ 一条被 CI 抓出来的纪律：编号全集必须只来自版本控制。**
 * 审计报告那一套编号（§3.1–§3.45）定义在 `output/` 下，而 `output/` 是
 * gitignore 的 —— 本地有、CI 的干净 checkout 没有。第一版用目录遍历收集
 * 编号，结果**本地绿、CI 红**（CI run 40723257346），两边各自都"对"，
 * 却没人说得清哪边是判据：本地那份绿只是运气。
 * 现在全集只取三样，全都在版本控制里：本文档自己的编号、**git 跟踪的** .md
 * 的编号（`git ls-files`，不是目录遍历）、以及 `tools/archdoc-external.json`
 * 里登记的外部编号（补 `output/` 那些没进仓库的文档）。
 *
 * 用法：
 *   node tools/archdoc-refs.mjs            # 校验
 *   node tools/archdoc-refs.mjs --update   # 按当前文档重写基线（新增章节后跑）
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..')
const DOC = path.join(REPO, 'ARCHITECTURE.md')
const BASELINE = path.join(HERE, 'archdoc-sections.json')
const EXTERNAL = path.join(HERE, 'archdoc-external.json')

const UPDATE = process.argv.includes('--update')

/** 标题里带编号的写法：`### §11.13 应用级计划任务`、`### 3.17 【高】…`、`## 0. 执行摘要`。 */
const HEAD_RE = /^#{2,6}\s+(?:§\s*)?(\d+(?:\.\d+)*)\.?(?:\s+(.*))?$/
const REF_RE = /§\s*(\d+(?:\.\d+)*)/g

const SKIP_DIRS = new Set(['node_modules', '.git', '.venv', 'dist', 'build', '__pycache__', '.run'])
const SKIP_FILES = new Set(['ARCHITECTURE.md'])

/**
 * 编号全集从**全仓库所有 .md**里自动发现，不手工维护文档清单 ——
 * 以后新增一份带编号的文档，它自动加入，不用回来改这里。
 *
 * 手工列清单有两个坏处：一是会漂移（新文档的编号就判成断链），
 * 二是漏掉一份就得到一堆假红。自动发现把「哪份文档」这个问题消掉了。
 */
const SCAN_EXT = new Set(['.py', '.js', '.mjs', '.cjs', '.ts', '.vue', '.md', '.yml', '.yaml', '.sh', '.json', '.txt'])

function stripNoise(title) {
  return (title || '')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/[（(]\s*20\d\d-\d\d-\d\d[^)）]*[)）]\s*$/, '')
    .trim()
}

/** 解析一份文档里全部带编号的标题 → { 编号: 标题 }，同时报告重复编号。 */
function parseSections(text) {
  const sections = new Map()
  const duplicates = []
  let inCode = false
  for (const line of text.split('\n')) {
    if (line.startsWith('```')) { inCode = !inCode; continue }
    if (inCode) continue
    const m = line.trim().match(HEAD_RE)
    if (!m) continue
    const num = m[1]
    const title = stripNoise(m[2])
    if (sections.has(num)) duplicates.push({ num, first: sections.get(num), second: title })
    else sections.set(num, title)
  }
  return { sections, duplicates }
}

/**
 * 文档里出现的 §N。
 *
 * **目录段也查。** 早先跳过它（理由是「目录里是链接不是引用」），但那是个
 * 任意特例 —— 而且挡不住坏引用：往目录里塞一句 `见 §11.13.99` 照样没人管。
 * 目录里的编号本来就该指向真实章节，一起查反而多一道保险。
 */
function refsInBody(text) {
  const out = []
  let inCode = false
  text.split('\n').forEach((line, i) => {
    if (line.startsWith('```')) { inCode = !inCode; return }
    if (inCode) return
    for (const m of line.matchAll(REF_RE)) out.push({ num: m[1], line: i + 1, text: line.trim() })
  })
  return out
}

function walk(dir, found = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) walk(full, found)
    else if (SCAN_EXT.has(path.extname(entry.name))) found.push(full)
  }
  return found
}

/**
 * **git 跟踪的**文件（`.md`）。
 *
 * 不用目录遍历是有意的：目录遍历会把 gitignore 的东西也算进来
 * （本仓库的 `output/` 下压着审计报告），于是闸门的判定取决于
 * 「你本机恰好有没有那些文件」—— 本地绿、CI 红，谁也说不清哪边对。
 * `git ls-files` 给的是仓库真正承认的内容，任何 checkout 都一样。
 * 不是 git 仓库时退回目录遍历（本地手工排查用）。
 */
function trackedFiles(filter) {
  try {
    const out = execFileSync('git', ['ls-files'], { cwd: REPO, encoding: 'utf8' })
    return out.split('\n').filter(Boolean)
      .map(f => path.join(REPO, f))
      .filter(f => filter(f))
  } catch {
    return walk(REPO).filter(filter)
  }
}

function trackedMarkdownFiles() {
  return trackedFiles(f => path.extname(f) === '.md')
}

function compareNumbers(a, b) {
  const pa = a.split('.').map(Number)
  const pb = b.split('.').map(Number)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0)
    if (d) return d
  }
  return 0
}

function main() {
  // `--doc <path>`：把被检查的文档换成别的文件。反向测试要用 ——
  // 想验证「闸门抓得住某个缺陷」，就得能喂给它一份有缺陷的文档。
  const docIdx = process.argv.indexOf('--doc')
  const docPath = docIdx > 0 && process.argv[docIdx + 1]
    ? path.resolve(process.argv[docIdx + 1])
    : DOC

  const docText = fs.readFileSync(docPath, 'utf8')
  const { sections, duplicates } = parseSections(docText)

  if (UPDATE) {
    const baseline = {
      _说明: '段号引用契约基线：每个章节编号必须永远指向这里登记的标题。',
      _维护: '新增章节后跑 `node tools/archdoc-refs.mjs --update` 重写本文件并提交。',
      sections: Object.fromEntries([...sections.entries()].sort((a, b) => compareNumbers(a[0], b[0]))),
    }
    fs.writeFileSync(BASELINE, `${JSON.stringify(baseline, null, 2)}\n`, 'utf8')
    console.log(`✓ 已更新基线：${sections.size} 个编号 → ${path.relative(REPO, BASELINE)}`)
    return 0
  }

  // 编号全集：几套并行体系加起来
  //
  // **被检查的那份文档要用它自己的编号入集。** 否则拿旧版文档来测时，
  // 集合里混进了仓库当前版的编号 —— 旧版里的坏引用会「解析得到」，
  // 闸门于是对着一份有缺陷的文档说通过。检查什么，就以什么为准。
  //
  // **全集的来源必须与 checkout 无关。** 这里只取三样：
  //   ① 被检查的文档自己的编号；
  //   ② **git 跟踪的** .md 的编号（用 git ls-files，不用目录遍历）；
  //   ③ `tools/archdoc-external.json` 登记的外部编号。
  //
  // ②为什么不用目录遍历：审计报告那一套编号定义在 `output/` 下，而 `output/`
  // 是 gitignore 的 —— 本地有、CI 的干净 checkout 没有。目录遍历会让闸门
  // **本地绿、CI 红**，而且两边都"对"，谁也说不清哪边是判据。
  // （实测踩过：CI run 40723257346 就是这么红的。）
  // ③补的就是这个缺口：那几份文档的编号由显式注册表提供，跟着版本控制走。
  const universe = new Map()
  const owner = new Map()
  const docFiles = trackedMarkdownFiles()
  for (const full of docFiles) {
    const rel = path.relative(REPO, full)
    if (full === DOC && docPath !== DOC) continue
    const { sections: s } = parseSections(fs.readFileSync(full, 'utf8'))
    for (const [num, title] of s) {
      if (!universe.has(num)) { universe.set(num, title); owner.set(num, rel) }
    }
  }
  // 外部编号注册表
  if (fs.existsSync(EXTERNAL)) {
    const ext = JSON.parse(fs.readFileSync(EXTERNAL, 'utf8'))
    for (const [src, nums] of Object.entries(ext.sources || {})) {
      for (const num of nums) {
        if (!universe.has(num)) { universe.set(num, src); owner.set(num, src) }
      }
    }
  }
  // 被检查的文档本身优先入集（它的编号才是「自己的」）
  for (const [num, title] of sections) {
    if (!universe.has(num)) { universe.set(num, title); owner.set(num, path.relative(REPO, docPath) || path.basename(docPath)) }
  }

  const baseline = JSON.parse(fs.readFileSync(BASELINE, 'utf8'))
  const problems = []

  // ① 编号唯一
  for (const d of duplicates) {
    problems.push(`编号重复：§${d.num} 同时指向「${d.first}」和「${d.second}」`)
  }

  // ② 基线不变
  for (const [num, title] of Object.entries(baseline.sections)) {
    if (!sections.has(num)) problems.push(`基线里的 §${num}（${title}）在文档里消失了`)
    else if (sections.get(num) !== title) {
      problems.push(`§${num} 指向变了：基线是「${title}」，现在是「${sections.get(num)}」`)
    }
  }
  for (const [num, title] of sections) {
    // 新增编号也当作问题：**基线是「编号 → 它指什么」的权威账本**，
    // 不登记就等于这份账本缺了条目 —— 以后这个编号被改标题，基线就拦不住。
    // 跑一次 `--update` 登记即可。
    if (!(num in baseline.sections)) {
      problems.push(`新增编号 §${num}（${title}）未登记进基线 —— 跑 node tools/archdoc-refs.mjs --update 登记`)
    }
  }

  // ③ 引用可解
  const unresolved = []
  const bodyRefs = refsInBody(docText)
  const check = (num, where) => {
    if (!universe.has(num)) unresolved.push({ num, where })
  }
  const docLabel = path.relative(REPO, docPath) || path.basename(docPath)
  for (const r of bodyRefs) check(r.num, `${docLabel}:${r.line}`)

  let repoCount = 0
  // 只扫**被跟踪的**文件：CI 的干净 checkout 里只有这些，
  // 扫本地那些 gitignore 的文件会让两边的引用点数量对不上。
  for (const file of trackedFiles(f => SCAN_EXT.has(path.extname(f)))) {
    const rel = path.relative(REPO, file)
    if (SKIP_FILES.has(rel) || rel.startsWith('tools' + path.sep)) continue
    fs.readFileSync(file, 'utf8').split('\n').forEach((line, i) => {
      for (const m of line.matchAll(REF_RE)) { check(m[1], `${rel}:${i + 1}`); repoCount++ }
    })
  }

  // 文档内自引指到别的文档去了 —— 信息性提示，不是失败
  const pointingAway = bodyRefs.filter(r => !sections.has(r.num) && universe.has(r.num))

  console.log(`本文档章节编号：${sections.size} 个（基线登记 ${Object.keys(baseline.sections).length} 个）`)
  console.log(`编号全集（${docFiles.length} 份 .md 自动发现）：${universe.size} 个`)
  console.log(`引用点：文档内 ${bodyRefs.length} 处 + 仓库内 ${repoCount} 处\n`)

  if (pointingAway.length) {
    console.log(`文档内指向别处的引用（不是错误，供人核对）：`)
    for (const p of pointingAway) console.log(`   §${p.num} → ${owner.get(p.num)}   ${path.relative(REPO, docPath)}:${p.line}`)
    console.log()
  }
  if (problems.length) {
    console.log(`✗ 契约被破坏 ${problems.length} 处：`)
    for (const p of problems) console.log(`   • ${p}`)
    console.log()
  }
  if (unresolved.length) {
    console.log(`✗ 无法解析的引用 ${unresolved.length} 处：`)
    for (const u of unresolved.slice(0, 30)) console.log(`   • §${u.num}  ${u.where}`)
    if (unresolved.length > 30) console.log(`   … 另有 ${unresolved.length - 30} 处`)
    console.log()
  }

  if (problems.length || unresolved.length) {
    console.log('段号契约不成立。')
    return 1
  }
  console.log('✓ 段号契约成立：编号唯一、基线未变、所有引用可解')
  return 0
}

process.exit(main())
