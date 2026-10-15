/**
 * 报表模板列表的可见性：**用户自己的模板一到，同类的内置模板就让位**。
 *
 *  内置的资产负债表 / 利润表只保证「拿本系统导出的账能算」；一旦用户把自家
 *  的用友 .rbk 导进来，口径就该以导入的那张为准 —— 两张同名报表并排摆着，
 *  用户不知道自己看的是哪一张，按下「打开」还很可能算错账。
 *
 *  按 **type** 判定而不是 code/name：判定口径要跟用户心里的「这是不是同一
 *  类报表」一致，而模板名是随时可以改的。
 *
 *  纯展示层过滤：内置模板照旧留在库里，直接按 URL 仍能打开 —— 这不是权限
 *  边界，只是少给一个选择，所以后端接口一个字都不用改。
 */

/**
 * 过滤出该显示的模板。
 *
 *  @param {Array<{id:number,type:string,is_builtin:boolean}>} list 后端模板列表
 *  @param {?number} keepId 正在看的那张，即便被规则藏起来也留在结果里
 *   （报表页靠它查当前模板的名字与 is_builtin，从下拉里消失就查不到了）
 * @returns {Array} 新数组，原顺序不变
 */
export function visibleTemplates(list, keepId = null) {
  const rows = Array.isArray(list) ? list : []
  const own = new Set(rows.filter(t => !t.is_builtin).map(t => t.type))
  return rows.filter(t => !t.is_builtin || !own.has(t.type) || t.id === keepId)
}
