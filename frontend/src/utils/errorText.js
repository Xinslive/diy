/**
 * 从 axios 错误里取出**给人看的那句话**。**唯一的一份实现**（§4.5）。
 *
 * 后端的 ``detail`` 有三种形态：
 *
 *   · **纯字符串**——``{detail: "只有系统管理员可以访问"}``；
 *   · **结构化**——``{detail: {code: "MFA_REAUTH_REQUIRED", message: "该操作需要…"}}``
 *     （「近期二次验证」闸门那条，见 backend/app/core/deps.py 里的
 *     ``MFA_REAUTH_REQUIRED``）；
 *   · **数组**——FastAPI 的 422 校验错误，形如 ``[{loc: [...], msg: "..."}]``。
 *
 * **为什么不能把 detail 直接丢给 ElMessage**：结构化那一种会被渲染成
 * ``[object Object]`` —— 界面上甩出一串没头没尾的方括号括号，
 * 而这恰恰是**管理员在高危动作上撞闸门时**最需要看懂的一句提示。
 *
 * **为什么收在一处**：同一个错误在 UserAdmin / MfaSettings / NotifySettings
 * 各写一遍取值逻辑，就会各写错一遍 —— 而「有的地方显示中文、有的地方显示
 * ``[object Object]``」这种不一致，是最难被测出来的那种。
 *
 * @param {any} error axios 错误（或任何带 response.data.detail 的东西）
 * @param {string} [fallback] 连兜底文案都没有时的最后一句
 * @returns {string}
 */
export function errorText(error, fallback = '操作失败') {
  const detail = error?.response?.data?.detail

  if (typeof detail === 'string' && detail) return detail

  if (Array.isArray(detail)) {
    const parts = detail.map((d) => d?.msg || d?.message).filter(Boolean)
    if (parts.length) return parts.join('；')
  }

  if (detail && typeof detail === 'object') {
    // **有 message 就用 message**：``code`` 是给程序看的，
    // 甩到界面上对用户没有意义（「MFA_REAUTH_REQUIRED：…」这种）。
    if (typeof detail.message === 'string' && detail.message) return detail.message
    if (typeof detail.msg === 'string' && detail.msg) return detail.msg
    if (typeof detail.code === 'string' && detail.code) return detail.code
  }

  return error?.message || fallback
}
