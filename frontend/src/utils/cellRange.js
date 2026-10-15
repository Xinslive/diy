/**
 * 表格矩形选区的**纯判定**：形状怎么算、导出成什么文本。
 *
 *  为什么不能交给浏览器：原生选区在表格里是**按文档顺序线性**扩的 ——
 *  从 C3 拖到 C9 会把中间几整行连同左右各列一起框进去（用户看到的就是
 *  这一幕）。CSS 改不了选区形状（`user-select` 只能开关不能改形），
 *  所以形状必须自己算：按下时记锚点，移动中记终点，两点围成一个矩形，
 *  再由 `composables/useCellSelection` 把它画到格子上去。
 *
 *  这一层刻意不碰 DOM：矩形怎么归一、TSV 怎么拼都能脱离浏览器验证，
 *  DOM 那部分（定位、绘制、剪贴板）留在 composable 里。
 */

/**
 * 归一成左上 / 右下两个角 —— 拖动方向不影响结果。
 *
 *  「先归一再判断」而不是边拖边判断，是因为用户完全可以从右下往左上拖，
 *  那种方向下 `r1 > r2`，后面每一处比较都要额外处理一遍反向。
 *
 * @param {{r:number,c:number}} anchor 按下的那一格
 * @param {{r:number,c:number}} extent 当前指到的那一格
 * @returns {{r1:number,c1:number,r2:number,c2:number}} 闭区间矩形
 */
export function normRect(anchor, extent) {
  return {
    r1: Math.min(anchor.r, extent.r),
    c1: Math.min(anchor.c, extent.c),
    r2: Math.max(anchor.r, extent.r),
    c2: Math.max(anchor.c, extent.c),
  }
}

/** (row, col) 是否落在矩形内。闭区间，所以单格选区也命中自己。 */
export function inRect(row, col, rect) {
  return row >= rect.r1 && row <= rect.r2 && col >= rect.c1 && col <= rect.c2
}

/** 选区覆盖几行几列 —— 用来跟人说明「复制了多少」，也用来判断有没有拖过。 */
export function rectSize(rect) {
  return { rows: rect.r2 - rect.r1 + 1, cols: rect.c2 - rect.c1 + 1 }
}

/**
 * 单元格里的文字压成单行：报表与账簿里不少格子是从模板带进来的多行文本，
 * 不压的话会连表格本身的换行一起进剪贴板，粘进 Excel 就是错行。
 */
const cleanCell = value => String(value ?? '').replace(/\s+/g, ' ').trim()

/**
 * 取矩形内的文字拼成 TSV（制表符分列、换行分行，粘进 Excel 正好成块）。
 *
 *  `at(r, c)` 由调用方给，而不是收一张二维数组：明细账动辄几千行，
 * 全读进内存再切是白读 —— 只读选中的那一块就够了。
 *
 * @param {(r:number,c:number)=>string} at 取某格文字，越界返回空串
 * @param {{r1:number,c1:number,r2:number,c2:number}} rect
 * @returns {string}
 */
export function rectToTsv(at, rect) {
  const lines = []
  for (let r = rect.r1; r <= rect.r2; r++) {
    const line = []
    for (let c = rect.c1; c <= rect.c2; c++) line.push(cleanCell(at(r, c)))
    lines.push(line.join('\t'))
  }
  return lines.join('\n')
}
