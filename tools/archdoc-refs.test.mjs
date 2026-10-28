/**
 * 段号契约闸门的反向测试。
 *
 * **为什么必须测这个。** 一个只会通过的闸门等于没有闸门 —— 它可能只是
 * 把「文件读到了」当成「契约成立」。所以这里每一���都是**喂一份有缺陷的
 * 文档进去，断言闸门必须红**。
 *
 * 用的是今天真实踩到的两类缺陷：
 *   · `§11.7.12` 出现两次（真 bug #1）
 *   · 「见 §11.13.4」指不到任何东西（真 bug #2）
 * 外加三类「静默失效」：编号被换给别的标题、章节被删掉、新编号没登记。
 *
 * 跑法：node --test tools/archdoc-refs.test.mjs
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..')
const DOC = path.join(REPO, 'ARCHITECTURE.md')
const TOOL = path.join(HERE, 'archdoc-refs.mjs')

/** 跑闸门，返回 { code, out }。 */
function runGate(docPath) {
  try {
    const out = execFileSync('node', [TOOL, '--doc', docPath], {
      cwd: REPO, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
    })
    return { code: 0, out }
  } catch (e) {
    return { code: e.status ?? 1, out: `${e.stdout || ''}${e.stderr || ''}` }
  }
}

/** 造一份改过的文档副本。 */
function mutatedDoc(transform) {
  const text = fs.readFileSync(DOC, 'utf8')
  const next = transform(text)
  assert.notEqual(next, text, '变异没有生效 —— 测试会假通过')
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'archdoc-')), 'ARCHITECTURE.md')
  fs.writeFileSync(file, next, 'utf8')
  return file
}

test('当前文档通过契约校验', () => {
  const { code, out } = runGate(DOC)
  assert.equal(code, 0, `闸门不该红，却红了：\n${out}`)
  assert.match(out, /段号契约成立/)
})

test('同一个编号指向两个标题 → 拦住（真 bug #1：§11.7.12 出现两次）', () => {
  const file = mutatedDoc(t => t.replace('#### 11.7.14 逃生口清单', '#### 11.7.12 逃生口清单'))
  const { code, out } = runGate(file)
  assert.notEqual(code, 0, '重复编号必须让闸门红')
  assert.match(out, /编号重复/)
  assert.match(out, /11\.7\.12/)
})

test('编号被换给别的标题 → 拦住（静默失效，目录锚点检查抓不到）', () => {
  const file = mutatedDoc(t => t.replace('### §11.7 生产发布的四条硬约束', '### §11.7 生产发布的五条硬约束'))
  const { code, out } = runGate(file)
  assert.notEqual(code, 0, '编号含义变了必须让闸门红')
  assert.match(out, /指向变了/)
  assert.match(out, /11\.7/)
})

test('章节被删掉 → 拦住（引用它的人会指向不存在的地方）', () => {
  const file = mutatedDoc(t => t.replace('### §9.7 登录的 IP 维度软节流（`LOGIN_IP_HARD_LOCK` 默认关）\n', ''))
  const { code, out } = runGate(file)
  assert.notEqual(code, 0, '章节消失必须让闸门红')
  assert.match(out, /消失了/)
})

test('引用指向不存在的编号 → 拦住（真 bug #2：见 §11.13.4 指不到任何东西）', () => {
  // 放进章节正文里，而不是目录段 —— 坏引用藏在目录里也曾是拦不住的
  const file = mutatedDoc(t => t.replace(
    '### §11.13 应用级计划任务\n',
    '### §11.13 应用级计划任务\n\n见 §11.13.99。\n',
  ))
  const { code, out } = runGate(file)
  assert.notEqual(code, 0, '坏引用必须让闸门红')
  assert.match(out, /无法解析/)
  assert.match(out, /11\.13\.99/)
})

test('新增编号没登记进基线 → 拦住（基线是账本，缺条目就等于没记）', () => {
  const file = mutatedDoc(t => t.replace('#### 11.7.14 逃生口清单', '#### 11.7.15 逃生口清单'))
  const { code, out } = runGate(file)
  assert.notEqual(code, 0, '未登记的新编号必须让闸门红')
  assert.match(out, /未登记进基线/)
  assert.match(out, /11\.7\.15/)
})
