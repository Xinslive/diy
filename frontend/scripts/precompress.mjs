/**
 * 构建后预压缩：给可压缩的静态资源生成一份 brotli 版本。
 *
 * **为什么要在构建期做，而不是让 nginx 现压**：生产机的 nginx 是**没有编译
 * brotli 模块**的（`nginx -V` 里只有 `--with-http_v3_module`）。要现压就得
 * 重编 nginx —— 那是一次动整个站点的生产变更，不该为了省 15% 带宽去做。
 * 于是改成「构建期压好，nginx 只负责把压好的那份发出去」。
 *
 * **产物放在哪儿**：平行的一棵树 `dist/br/<原路径>`（例如
 * `dist/assets/index-xxx.js` → `dist/br/assets/index-xxx.js`）。
 * **不是** `xxx.js.br` 那种同目录后缀 —— nginx 认扩展名来决定 Content-Type，
 * 同目录后缀会让 mime.types 认不出 `.br`，模块脚本会被浏览器拒收。
 * 平行树里文件名原样保留，Content-Type 照旧 ✔
 *
 * **谁来找这份文件**：nginx 的 `/assets/` 块。客户端说「我支持 br」时，
 * 它去 `br/` 树里取那份，取不到就退回原文件（详见 deploy/nginx/cwq.conf）。
 *
 * **压不划算就不压**：压完比原来还大（小文件常见）就不产出那份，
 * nginx 会自动走原文件 —— 所以「没有 br 版本」是正常状态，不是故障。
 *
 * 用法：node scripts/precompress.mjs [dist 目录]（默认 ./dist）
 */
import {
  brotliCompressSync, constants, brotliDecompressSync
} from 'node:zlib'
import {
  existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync
} from 'node:fs'
import { dirname, extname, join, relative, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

/** 输出目录名（在 dist 下面）。与 nginx 配置里的 /br/ 对应。 */
export const BR_DIR = 'br'

/**
 * 只压这些后缀。
 *
 * 二进制（图片、字体、wav）压了几乎不省 —— 有些还会更大，而且浏览器对
 * 图片不做 Content-Encoding 解码，白压一趟。
 */
const COMPRESSIBLE = new Set([
  '.js', '.mjs', '.css', '.html', '.json', '.svg', '.map', '.txt', '.xml', '.wasm'
])

/** 递归列出目录下的所有文件（跳过要排除的目录）。 */
function walk(dir, skipDirs, out = []) {
  for (const name of readdirSync(dir)) {
    if (skipDirs.has(name)) continue
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, skipDirs, out)
    else out.push(p)
  }
  return out
}

/**
 * 给一个 dist 目录生成 brotli 副本。
 *
 * @param {string} distDir  dist 目录
 * @returns {{compressed:number, skipped:number, originalBytes:number, compressedBytes:number}}
 */
export function precompress(distDir) {
  const dist = resolve(distDir)
  if (!existsSync(dist)) throw new Error(`dist 目录不存在：${dist}`)

  const outRoot = join(dist, BR_DIR)
  // 先把上一版留下的清掉：文件名带哈希，但删掉的旧哈希文件不该还留着占地方，
  // 更不该被 nginx 当成「某个资源的 br 版本」发出去。
  rmSync(outRoot, { recursive: true, force: true })
  mkdirSync(outRoot, { recursive: true })

  let compressed = 0, skipped = 0, originalBytes = 0, compressedBytes = 0

  for (const file of walk(dist, new Set([BR_DIR]))) {
    const rel = relative(dist, file)
    if (!COMPRESSIBLE.has(extname(file).toLowerCase())) { skipped++; continue }

    const raw = readFileSync(file)
    const br = brotliCompressSync(raw, {
      params: {
        [constants.BROTLI_PARAM_QUALITY]: 11,
        // 尺寸提示：让压缩器提前按「就这么大」来选参数，小文件压得更实。
        [constants.BROTLI_PARAM_SIZE_HINT]: raw.length
      }
    })
    if (br.length >= raw.length) { skipped++; continue }   // 压了反而大 → 不产出

    const target = join(outRoot, rel)
    mkdirSync(dirname(target), { recursive: true })
    writeFileSync(target, br)
    compressed++
    originalBytes += raw.length
    compressedBytes += br.length
  }

  return { compressed, skipped, originalBytes, compressedBytes }
}

/** 直接运行时才干活（被测试 import 时不跑）。 */
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const dist = process.argv[2] || join(process.cwd(), 'dist')
  const r = precompress(dist)
  const pct = r.originalBytes ? Math.round((1 - r.compressedBytes / r.originalBytes) * 100) : 0
  console.log(`brotli 预压缩：${r.compressed} 个文件，` +
    `${(r.originalBytes / 1024).toFixed(0)} KB → ${(r.compressedBytes / 1024).toFixed(0)} KB（省 ${pct}%）`)
  if (r.compressed === 0) {
    console.error('⚠️ 一个文件都没压出来 —— 检查 dist 目录是不是空的')
    process.exit(1)
  }
}
