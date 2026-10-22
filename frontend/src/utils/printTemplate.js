/**
 * 打印模板的**选取口径与可改性**。设计器（PrintTemplate.vue）和打印弹窗
 * （PrintDialog.vue）两处都要回答同一个问题「哪些模板可选、哪一个是默认、
 * 这一份我能不能改」，两处各写一份的结果就是同一个问题在两个页面给出不同
 * 答案 —— 比如设计器标着「默认」的那份，打印时却选了另一份。这份把规则收在一处。
 *
 * 规则本身跟后端 `app/api/printing.py` 对齐，不是前端自定的：
 *   - `_visible_templates(book_id, user_id)`：我的私有副本 + 本账套的 + 全局基线
 *   - `_template(scope, None)`：不指定模板时取默认模板的那条查询
 *   - `_clear_other_defaults(db, keep)`：按 (user_id, book_id, scope) 分组清除默认标记
 *   - `_require_global_template_write` / `_fork_private`：谁能改哪一层
 */

/** 能写的角色。与后端 WRITABLE_ROLES 同源（stores/session.js 也是这一份口径）。 */
const WRITABLE_ROLES = ['OWNER', 'ACCOUNTANT']

/**
 * 模板属于哪一层。后端按 (book_id, user_id) 判，列表接口也把判据原样带回来了
 * （`is_private` / `is_baseline`）—— **以服务端给的为准，不要在前端重算**：
 * 「是不是基线」「归不归我」都是权限问题，前端复算一遍就多一个能错的地方。
 */
function layerOf(t) {
  if (t.is_private) return 'private'
  if (t.is_baseline) return 'baseline'
  return 'book'
}

/**
 * 这个范围里**真正生效**的默认模板，也就是不指定模板直接打印时会用到的那一份。
 *
 * 为什么不能直接找 `is_default` 的那条：后端清除默认标记时是按
 * (user_id, book_id, scope) 分组的，三个归属各算各的。于是同一范围里合法地
 * 存在好几行 `is_default=true`：我的私有副本、账套自己的那份、全局基线。
 * 取用时后端用 `ORDER BY user_id IS NULL, book_id IS NULL, id` 让**我的私有
 * 副本排最前**，真正生效的只有一行。
 *
 * 前端照这条排序复算一遍，是为了让「默认」这个标记指的那一份就是会印出来的那一份。
 * 照着 `is_default` 直接标，会出现好几份都标着默认、实际只生效一份的情况 ——
 * 标记一旦指错，比不标更糟。
 *
 * @param {Array} templates 该范围内的模板（已按范围过滤）
 * @returns {number|null} 生效的模板 id；一个都没有时返回 null
 */
export function effectiveDefaultId(templates = []) {
  const cands = templates.filter((t) => t.is_default)
  if (!cands.length) return null
  // 与后端 _template() 的 ORDER BY 一一对应：我的私有副本 → 本账套 → 全局基线，
  // 同一层按 id 升序（ASC 排序下布尔表达式算出 false 的排前面）。
  const rank = (t) => {
    const layer = layerOf(t)
    return layer === 'private' ? 0 : layer === 'book' ? 1 : 2
  }
  return [...cands].sort((a, b) => rank(a) - rank(b) || a.id - b.id)[0].id
}

/**
 * 选择列表里这份模板要挂的标记：只有生效的那一份标「默认」。
 *
 * **私有副本不标任何东西。** 改基线时后端会写时复制成一份只有改动者看得见的
 * 副本（机制见本文件开头与 canEditTemplate），这是**实现细节**：用户点开
 * 模板改两下、存一下，看到的仍然是那份他熟悉的模板名，忽然多出「（我的）」
 * 反而会让人停下来想「我这是把它改成私有了吗、别人那份还在不在」。
 * 机制不变，只是不必反复提醒。
 *
 * 「默认」必须留着 —— 那是会真的印出来的那一份，标错比不标更糟。
 *
 * @param {object} t 模板
 * @param {number|null} defId effectiveDefaultId 的结果
 * @returns {string[]} 标记（可能为空）
 */
export function templateTags(t, defId) {
  return t.id === defId ? ['默认'] : []
}

/**
 * 这一份模板我能不能改。口径与后端 `update_template` 完全一致：
 *
 * | 模板归属 | 谁能改                             | 改动落到哪            |
 * |----------|------------------------------------|-----------------------|
 * | 私有副本 | 主人本人（管理员也不行）           | 还是它自己            |
 * | 系统基线 | 账套里能写的人（会计即可）         | **复制成我的私有副本** |
 * | 账套模板 | 账套 OWNER 或管理员                | 账套里所有人可见的那份 |
 *
 * 为什么基线那一行不是 OWNER：改动只对改动者一个人可见，爆炸半径是 1。
 * 再套 OWNER 就等于「版式只有一个人能调」，与需求正好相反。
 *
 * 只读账号（VIEWER）对三层都不行 —— 后端 `require_book_write` 先挡一道。
 *
 * @param {object} t 模板
 * @param {{role: string, isAdmin: boolean}} ctx 当前账套里的身份
 */
export function canEditTemplate(t, ctx = {}) {
  if (!t) return false
  if (!WRITABLE_ROLES.includes(ctx.role)) return false
  const layer = layerOf(t)
  if (layer === 'private') return true      // 列表里能看见它，就说明是主人
  if (layer === 'baseline') return true     // 写路径会先复制成私有副本
  return !!ctx.isAdmin || ctx.role === 'OWNER'
}

/**
 * 这一份模板我能不能删。与能改的口径差两处，都是后端写死的事实：
 *
 * * **系统基线不能删**（403）—— 删它不是改偏好，是把系统变回「没有凭证版式可用」。
 * * 私有副本只有主人能删（别人连它是 404，都到不了这一步）。
 */
export function canDeleteTemplate(t, ctx = {}) {
  if (!canEditTemplate(t, ctx)) return false
  return layerOf(t) !== 'baseline'
}

/**
 * 能不能新建模板。新建出来的是**账套模板**（同账套所有人可见），所以后端要
 * OWNER（`_require_global_template_write`）。前端跟着认同一份，别让会计点了
 * 才知道后端会拒。
 */
export function canCreateTemplate(ctx = {}) {
  if (!WRITABLE_ROLES.includes(ctx.role)) return false
  return !!ctx.isAdmin || ctx.role === 'OWNER'
}

/**
 * 能不能**新建一份只有自己看得见**的版式（POST /templates 带 private:true）。
 *
 * 与 {@link canCreateTemplate} 只差一条 OWNER 门禁，理由和后端
 * `create_template` 里那句「爆炸半径 1」是同一句：新建出来的东西只有改动者
 * 一个人看得见，既不改基线、也不影响同账套的同事。既然影响面是 1，就不该按
 * 「会不会被别人用到」来收门禁 —— 那会让「我自己的凭证纸」这件事对会计也做不到。
 *
 * **和 `canCreateTemplate` 分成两个函数而不是带个参数**，是因为它们在 UI 上
 * 是两个不同的按钮，禁用判据也不同；合成一个 `canCreate(ctx, private)` 的话，
 * 调用点就会变成 `canCreate(whoami, x)`，多出来的那个布尔读不出在判什么。
 *
 * VIEWER 两边都不行：后端 `require_book_write` 依赖先挡一道。
 *
 * **当前没有界面在用它**（设计器里那一项已按需求撤掉，后端 `private: true`
 * 仍然支持、也有用例）。函数留着是因为它描述的是一条真实存在的后端门禁，
 * 删掉的话哪天要把入口加回来就得连门禁一起重建；而留着就得写清它现在是
 * 「备用」而不是「漏接线」。
 */
export function canCreatePrivateTemplate(ctx = {}) {
  return WRITABLE_ROLES.includes(ctx.role)
}

/**
 * 「内置默认」那一项要不要留在选择列表里。
 *
 * 内置版式（后端 `DEFAULT_TEMPLATES`）是**没有存进库**的兜底，只在某个范围
 * 一份保存的模板都没有时才是唯一可选项。
 *
 * 凭证范围现在有一对基线模板：两份都是全局的，所有账套都看得见，默认那一份是
 * 「凭证纸专用」。这时候再摆一个「内置默认」，列表里就成了三种说法 ——
 * 用户得先想明白这三者是什么关系、打印时默认用哪一份，而其中一份还不是配置项。
 * 基线模板已经承担了这个角色，内置那一项去掉。
 *
 * 其它范围（总账/明细账/科目余额表/报表）目前没有基线模板，那一项是它们**唯一**
 * 的选项 —— 一并去掉会让这些范围的设计器直接没有可选项。所以按范围区分，
 * 不是无差别删除。
 *
 * @param {string} scope 打印范围
 * @param {Array} templates 该范围内已保存的模板
 */
export function showBuiltinTemplate(scope, templates = []) {
  if (scope === 'VOUCHER') return templates.length === 0
  // 账簿（总账 / 明细账）：2026-10-22 起各有一份**基线模板**（用户自己在
  // 设计器里调出来的那份，已定为内置默认基线）。基线已经承担了「这一范围
  // 的默认版式」这个角色，再摆一个「内置默认」就是同一件事两种说法 ——
  // 用户得先分清这两者是什么关系、打印时默认用哪一份。
  if (scope === 'LEDGER' || scope === 'DETAIL_LEDGER') return templates.length === 0
  // 科目余额表 / 报表：还没有基线模板，那一项是它们**唯一**的选项 ——
  // 一并去掉会让这两个范围的设计器直接没有可选项。
  return true
}
