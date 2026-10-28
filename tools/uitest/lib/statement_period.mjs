/**
 * 流水对账自查脚本的**共用一步**：把顶栏的会计期间切到「这份对账单所属的期间」。
 *
 * ## 为什么脚本必须做这一步（2026-10-28）
 *
 * 流水对账页的对账单台账与「选择对账单」下拉**跟着顶栏的会计期间走**
 * （用户批注：「根据这里选中的期间，来显示对账单」）。
 *
 * 而顶栏默认停在「该年度最靠前的一个未结账月份」—— dev 库里是 **2026 年 12 月**，
 * 对账单却在 **9 月**。于是：
 *
 * * 台账列表是空的；
 * * 「选择对账单」下拉里没有东西；
 * * 后面每一步都在等一个不存在的元素 —— **超时**，看起来像「界面坏了」。
 *
 * 这个坑很隐蔽：报错全是「click 超时」「行数 0」，没有一条直说「期间没对上」。
 *
 * ## 切的方式就用页面上那个入口
 *
 * 空态里的「别的期间还有：2026 年 9 月（1 份）」是**可点的跳转链接** ——
 * 那正是产品自己给用户的一条路。脚本走同一条路，顺带把这条路也验了：
 * 链接点得动、顶栏真的切过去了、台账里真的有对账单了。
 *
 * 不走「直接改 localStorage」那种捷径：那样子**绕过了**产品给用户的路径，
 * 界面上的那条路坏没坏就没人知道了。
 */

/**
 * 切到「有对账单的那个期间」。
 *
 * @param {import('playwright').Page} page
 * @returns {Promise<{mode: 'already'|'switched'|'none', rows: number, label?: string, emptyText?: string}>}
 *   - `already`：顶栏已经停在有对账单的期间上；
 *   - `switched`：空态里点了「别的期间还有」跳过去的；
 *   - `none`：**这本账一份对账单都没有**（该走上传流程了）。
 */
export async function gotoStatementPeriod(page) {
  await page.click('.el-tabs__item:has-text("对账单")')
  await page.waitForTimeout(1200)

  // **数据行的行数**：空态那块不是 <tr>，数 tbody tr 也数不到它，
  // 但用 el-table__row 更稳 —— 它只标在真正的数据行上。
  const countRows = () => page.locator('.el-table:visible tr.el-table__row').count()

  const rows = await countRows()
  if (rows > 0) return { mode: 'already', rows }

  // 空态里没有「别的期间还有」⇒ 这本账压根没上传过对账单
  const link = page.locator('.flow-empty-other button')
  if (!(await link.count())) return { mode: 'none', rows: 0 }

  const label = (await link.first().innerText()).trim()
  const emptyText = (await page.locator('.flow-empty-period').innerText()).trim()
  await link.first().click()
  await page.waitForTimeout(1800)

  const after = await countRows()
  if (!after) throw new Error(`点了「${label}」之后，列表里还是没出现对账单`)
  return { mode: 'switched', rows: after, label, emptyText }
}

/**
 * 切到「流水明细」页签并选中第一份对账单。
 *
 * **下拉是空的 = 期间没对上**（不是界面坏了）—— 所以这里要把这句话说清楚，
 * 否则又会变成一条「click 超时」的死胡同。
 *
 * @param {import('playwright').Page} page
 */
export async function selectStatement(page) {
  await page.click('.el-tabs__item:has-text("流水明细")')
  await page.waitForTimeout(900)
  await page.locator('.flow-select').click()
  await page.waitForTimeout(600)
  const option = page.locator('.el-select-dropdown__item:visible').first()
  if (!(await option.count())) {
    throw new Error('「选择对账单」下拉是空的 —— 顶栏的期间没跟对账单对上')
  }
  await option.click()
  await page.waitForTimeout(1500)
}
