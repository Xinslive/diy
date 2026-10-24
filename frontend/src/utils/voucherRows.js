/**
 * 凭证分录的「第一条没写完的在第几行」。
 *
 *  录入页每一行都要问一次「第 i 行能不能填」（`blocked(i)`）。早先的写法是
 *  **每行从头扫到 i**：一屏 154 行就是一万多次判断，而每次判断又要问
 *  「这个科目是不是末级」——那一下本来是去两千条科目里线性翻的。
 *  三层叠起来是 O(行数² × 科目数)：那张 154 行的凭证打开一次要 41 秒，
 *  主线程整个占住，页面看着就是卡死（2026-10-16 实测）。
 *
 *  答案对所有行其实是**同一个**：第一条没写完的在哪儿，第 i 行往后全按它答。
 *  扫一遍把下标记下来就够 —— 每行 O(1)，整屏 O(行数)。
 */
export function findFirstProblem(rows, problemOf) {
  for (let i = 0; i < rows.length; i++) if (problemOf(rows[i])) return i
  return rows.length
}

/**
 * 这一行是不是空的：科目和金额都没填。
 *
 *  **摘要不算**：新行会带着上一行的摘要，空位上常常是有字的，
 *  可它仍然只是「等着填」的位置，不是分录。
 */
export function isBlankRow(row) {
  return !row || (!row.subject_id && !row.amount)
}

/**
 * 最后一行**有内容**的行号（一张都没填就是 -1）。
 *
 *  底下那几行「等着填」的空位不算内容 —— 它们是留给人写字的地方。
 */
export function lastFilledRowAt(rows) {
  for (let i = rows.length - 1; i >= 0; i--) if (!isBlankRow(rows[i])) return i
  return -1
}

/**
 * 这一行删不删得掉。
 *
 *  以前只看「有没有第五行」（`rows.length > 4`）：只摆得下四行时整张表一个 ⊗
 *  都没有 —— 于是「删掉前四行里的某一行」成了不可能的事（用户 2026-10-24：
 *  「分录不足4行时不显示删除按钮」）。
 *  四行是**行数的下限**，不是**分录的护栏**：它说的是「行得摆够，录第三条时
 *  不用临时找地方」，从来没说过「前四行不许删」。
 *
 *  现在：**底下那几行等着填的空位之外，哪一行都删得掉**，删完后面的行自动往上排。
 *  空位不给删，是因为它删了立刻又被补回来 —— 看着像没删，只会让人以为按钮坏了。
 *
 *  `lastFilled` 可以从外面传进来（页面把它算成一个 computed，每行 O(1)地问）：
 *  一屏 154 行时逐行现算就是 154² 次判断 —— 这一页刚因为同类问题卡死过一次。
 */
export function rowCanBeDeleted(rows, i, lastFilled = lastFilledRowAt(rows)) {
  if (i < 0 || i >= rows.length) return false
  return i <= lastFilled
}
