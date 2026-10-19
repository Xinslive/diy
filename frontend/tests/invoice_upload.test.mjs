/**
 * 发票导入的文件挑选（src/views/voucher/invoice_upload.js，**线上那份源码**）：
 *
 *  ① 非 PDF 被忽略，但忽略项要留下交代（文件名原样交回去）；
 *  ② 重复拖进来的同一份文件只留一份；
 *  ③ 拖进来一个**文件夹**时 `dataTransfer.files` 是空的，必须走
 *     `webkitGetAsEntry` 递归 —— 而且 `readEntries` 一次只给 100 条，
 *     只读一次就会静默丢掉后面的文件。
 *
 * 这几条都是「错了不报错、只是少传了东西」的类型，靠手点一遍看不出来，
 * 所以拿假 FileSystemEntry 直接测递归。
 */
import assert from 'node:assert/strict'

import {
  filesFromDataTransfer, humanBytes, isIgnoredRow, isPdfName, partitionFiles, skipNote,
} from '../src/views/voucher/invoice_upload.js'

const fake = (name, size = 1024, lastModified = 1) => ({ name, size, lastModified })

/** 造一棵目录树：{ name, isDirectory, children }。 */
const dir = (name, children) => ({
  name, isDirectory: true,
  createReader() {
    let done = false
    return {
      readEntries(cb) {
        if (done) { cb([]); return }
        done = true
        cb(children)
      },
    }
  },
})
const fileEntry = (file) => ({
  name: file.name, isFile: true, isDirectory: false,
  file: (cb) => cb(file),
})
/** readEntries 每次只吐 100 条，模拟真实上限。 */
const bigDir = (name, children) => {
  let sent = 0
  return {
    name, isDirectory: true,
    createReader() {
      return {
        readEntries(cb) {
          if (sent >= children.length) { cb([]); return }
          const slice = children.slice(sent, sent + 100)
          sent += slice.length
          cb(slice)
        },
      }
    },
  }
}

let pass = 0, fail = 0
const t = async (name, fn) => {
  try { await fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 挑出 PDF，非 PDF 留下交代 ==')

await t('只看扩展名，且大小写不敏感', () => {
  assert.ok(isPdfName('发票.pdf'))
  assert.ok(isPdfName('SCAN.PDF'))
  assert.ok(!isPdfName('发票.jpg'))
  assert.ok(!isPdfName('台账.xlsx'))
  assert.ok(!isPdfName(''))
})

await t('非 PDF 进忽略清单，PDF 进待传清单', () => {
  const { pdfs, skipped } = partitionFiles([
    fake('a.pdf'), fake('照片.heic'), fake('b.pdf'), fake('台账.xlsx'),
  ])
  assert.deepEqual(pdfs.map((f) => f.name), ['a.pdf', 'b.pdf'])
  assert.deepEqual(skipped, ['照片.heic', '台账.xlsx'])
})

await t('忽略项有交代：带名字、带个数', () => {
  assert.equal(skipNote(['照片.heic', '台账.xlsx']),
    '已忽略 2 个非 PDF 文件：照片.heic、台账.xlsx')
  assert.equal(skipNote([]), '', '没有被忽略的东西时不该有提示')
})

await t('忽略项超过 5 个时只点名 5 个，但个数照实说', () => {
  const note = skipNote(['1.jpg', '2.jpg', '3.jpg', '4.jpg', '5.jpg', '6.jpg'])
  assert.ok(note.includes('已忽略 6 个'), note)
  assert.ok(note.includes('等 6 个'), note)
})

await t('同一份文件重复拖进来只留一份', () => {
  const { pdfs } = partitionFiles([fake('a.pdf', 100, 9), fake('a.pdf', 100, 9), fake('a.pdf', 200, 9)])
  assert.deepEqual(pdfs.map((f) => f.size), [100, 200],
    '同名但大小不同的两份都在；同名同大小的重复只留一份')
})

console.log('== 拖进来的可能是文件夹 ==')

await t('拖进来一个文件夹：files 为空也要能从 entry 里捞出文件', async () => {
  const tree = dir('十月发票', [fileEntry(fake('10月8日.pdf')), fileEntry(fake('10月9日.pdf'))])
  const dt = { files: [], items: [{ webkitGetAsEntry: () => tree }] }
  const picked = await filesFromDataTransfer(dt)
  assert.deepEqual(picked.map((f) => f.name), ['10月8日.pdf', '10月9日.pdf'])
})

await t('子目录里的文件也要捞到', async () => {
  const tree = dir('十月发票', [
    fileEntry(fake('a.pdf')),
    dir('子目录', [fileEntry(fake('b.pdf'))]),
  ])
  const picked = await filesFromDataTransfer({ files: [], items: [{ webkitGetAsEntry: () => tree }] })
  assert.deepEqual(picked.map((f) => f.name).sort(), ['a.pdf', 'b.pdf'])
})

await t('readEntries 一次只给 100 条：超过 100 个文件不能只读一次', async () => {
  const children = Array.from({ length: 250 }, (_, i) => fileEntry(fake(`inv${i}.pdf`)))
  const picked = await filesFromDataTransfer({ files: [], items: [{ webkitGetAsEntry: () => bigDir('多', children) }] })
  assert.equal(picked.length, 250, '目录读到空为止，不能停在第一批')
})

await t('不支持 entry API 的浏览器退回 dataTransfer.files（多文件仍可用）', async () => {
  const dt = { files: [fake('a.pdf'), fake('b.pdf')], items: [{}, {}] }
  const picked = await filesFromDataTransfer(dt)
  assert.deepEqual(picked.map((f) => f.name), ['a.pdf', 'b.pdf'])
})

await t('文件与目录混着拖进来也要分开处理', async () => {
  const dt = {
    files: [],
    items: [
      { webkitGetAsEntry: () => fileEntry(fake('拖进来的.pdf')) },
      { webkitGetAsEntry: () => dir('文件夹', [fileEntry(fake('目录里的.pdf'))]) },
    ],
  }
  const picked = await filesFromDataTransfer(dt)
  assert.deepEqual(picked.map((f) => f.name).sort(), ['拖进来的.pdf', '目录里的.pdf'])
})

await t('病态目录树有上限，不会把页面卡死', async () => {
  const deep = (n) => n === 0 ? fileEntry(fake('x.pdf')) : dir('d', [deep(n - 1)])
  const picked = await filesFromDataTransfer(
    { files: [], items: [{ webkitGetAsEntry: () => deep(5000) }] }, { maxScanned: 50 })
  assert.ok(picked.length <= 1, `扫了 ${picked.length} 个，超上限的目录该被截断`)
})

console.log('== 已忽略的判定 ==')

await t('重开批次时靠服务端状态也能认出被忽略的行', () => {
  // 忽略要等点「重新计算预览」才落库；刷新页面后本地 overrides 是空的。
  // 只认 overrides 的话，这行会重新摊出科目下拉和空明细表，像还没填。
  assert.equal(isIgnoredRow({ id: 7, status: 'IGNORED' }), true)
  assert.equal(isIgnoredRow({ id: 7, status: 'READY' }), false)
})

await t('本地 override 压过服务端状态（点了取消忽略，还没重算）', () => {
  assert.equal(isIgnoredRow({ id: 7, status: 'IGNORED' }, { 7: { ignored: false } }), false)
  assert.equal(isIgnoredRow({ id: 7, status: 'REJECTED' }, { 7: { ignored: true } }), true)
})

await t('本地 override 里没有这一行键，就看服务端状态', () => {
  assert.equal(isIgnoredRow({ id: 7, status: 'IGNORED' }, { 9: { ignored: true } }), true)
})

console.log('== 体积文案 ==')

await t('KB / MB 两位小数', () => {
  assert.equal(humanBytes(512), '512 B')
  assert.equal(humanBytes(2048), '2.0 KB')
  assert.equal(humanBytes(5 * 1024 * 1024), '5.0 MB')
})

console.log(`\n${pass} 通过，${fail} 失败`)
if (fail) process.exit(1)
