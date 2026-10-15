/**
 * 科目弹窗的提交前校验（新增 / 编辑共用）。
 *
 * 这三行校验以前在 `SubjectFormDialog.vue` 和 `SubjectEditDialog.vue` 里
 * 各写了一遍，**逐字相同**。重复的直接后果是两边开始漂移：
 * 编辑那个后来补了 `catch`，新增那个没有 —— 同一个失败，用户在编辑弹窗里
 * 看得到「保存科目失败」，在新增弹窗里什么都看不到（只有 axios 拦截器兜底）。
 *
 * 所以抽出来。抽的时候**只抽真正共用的部分**：
 *   - 两条空值校验，两边一模一样；
 *   - 「能不能写」的判断逻辑相同，**但文案不同**（新增说「不能新增科目」、
 *     编辑说「不能编辑科目」），所以文案由调用方传进来，不在这里写死。
 *
 * 返回值刻意是 `true | 字符串` 而不是抛异常或返回布尔：
 * 两条调用方现在的写法都是 `if (bad) return ElMessage.warning(bad)`，
 * 这样抽完调用点的控制流一个字都不用改，读起来仍是原来那两行。
 */

/**
 * @param {{canWrite: boolean}} store 会话 store（认的是白名单那份 canWrite）
 * @param {{code?: string, name?: string}} form 表单当前值
 * @param {string} readonlyMessage 只读账套时的提示文案（新增/编辑各说一句）
 * @returns {true | string} 通过返回 true，不通过返回**直接可以弹给用户看**的文案
 */
export function validateSubjectForm(store, form, readonlyMessage) {
  if (!store?.canWrite) return readonlyMessage
  if (!form?.code?.trim()) return '科目编码不能为空'
  if (!form?.name?.trim()) return '科目名称不能为空'
  return true
}
