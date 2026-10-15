/**
 * 账套初始化的**纯判定与文案**（两步向导）。
 *
 *  这里只放不碰 Vue、不碰接口的东西：给一份 init，算出「现在该做第几步」
 * 「第 2 步锁不锁」「沿用标准科目表这个按钮该不该灰」，以及几句给人看的字。
 *  理由跟本项目其它 utils 一致 —— 这些判定散在两个组件里各写一遍，
 *  迟早某一边改了一边没改，于是「向导说能做、期初页说不能进」或者反过来。
 *  单测也因此不必挂起组件就能把每条分支跑遍（tests/book_init.test.mjs）。
 */

/** 后端 subjects_source 两个取值的中文名。别的值一律回落到「标准科目表」以外的未知文案。 */
export const SUBJECT_SOURCE_LABEL = {
  IMPORT: '用友科目表',
  CHART: '系统标准科目表'
}

/** initStep 的三个返回值。与 el-steps 的下标差 1（`active = step - 1`），别混用。 */
export const STEP_DONE = 0       // 两步都做完了
export const STEP_SUBJECTS = 1   // 下一步做「导入科目」
export const STEP_OPENINGS = 2   // 下一步做「导入期初余额」

/**
 * 第 2 步锁着时给的那句话。
 *
 *  只说「第 2 步未完成」没用 —— 用户不知道**为什么**打不开，更不知道下一步该点什么。
 *  所以这句话必须自带出路（导入科目 / 沿用标准科目表），它本身就是第 1 步的操作说明。
 */
export const STEP2_LOCK_HINT = '请先完成第 1 步：导入科目表，或确认沿用系统标准科目表'

/**
 * init 常常是空的：老账套行没有 init 字段、接口还没上线、账套列表先于详情到达。
 *
 *  一律 **fail-closed**（缺信息 = 没做完）—— 与 session.js 的 canWrite 同一个取舍：
 *  认不得就当不能做。按钮亮着然后被后端 409 拒掉，比按钮灰着更让人困惑。
 *  但**显示**那一侧要区别对待：见 hasInit —— 「我不知道」不该被渲染成
 *  「我知道了，而且没做」这一条拦截横幅。
 */

export const hasInit = (init) => !!init && typeof init === 'object'

export const subjectsReady = (init) => !!init?.subjects_ready
export const openingsReady = (init) => !!init?.openings_ready

/**
 * 现在该做第几步。
 *
 *  刻意**不从后端给的 init.step 抄**：那个字段是派生量，两份真相迟早对不上，
 *  而这里算出来的两个布尔就是唯一真相（step 只是它的缓存）。对不上时以本地算的为准。
 */
export function initStep(init) {
  if (!subjectsReady(init)) return STEP_SUBJECTS
  if (!openingsReady(init)) return STEP_OPENINGS
  return STEP_DONE
}

/** 第 2 步能不能进 —— 也就是 initStep(init) !== 1。 */
export const canEnterStep2 = (init) => subjectsReady(init)

/**
 * 「沿用系统标准科目表」该不该可点。
 *
 *  认后端的 can_keep_chart，不自己猜「账上是不是已经有科目」—— 那正是
 *  can_keep_chart 的定义，而且猜错一次就是往账里塞了一份没人要的科目。
 *  后端没这个字段时按 false 处理：宁可让人看见按钮灰着（去看一眼为什么），
 *  也不要点了以后拿到一句 409。
 */
export const canKeepChart = (init) => !!init?.can_keep_chart

/** 能不能覆盖导入科目（有凭证/有期初的账套后端会拒）。 */
export const canImportSubjects = (init) => !!init?.can_import_subjects

/**
 * ISO 时间 → 「2026-10-15 16:32」。
 *
 *  按**浏览器本地时区**渲染，不用 toISOString（那是 UTC，会比用户看到的钟慢 8 小时，
 *  一次确认动作的时间戳差了 8 小时，人只会以为是系统坏了）。认不出的串返回空串，
 *  免得界面上冒出 "Invalid Date"。
 */
export function formatInitTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}`
}

/**
 * 第 1 步完成后的那行字：「科目已确定（来源：用友科目表，时间：2026-10-15 16:32）」。
 *
 *  没完成时返回空串，由组件自己渲染「待完成」那一支 —— 别在这里编一句
 *  「科目未确定」回去，那会让两处各长出一套没做成的样式。
 */
export function initSubjectLabel(init) {
  if (!subjectsReady(init)) return ''
  const parts = [`科目已确定（来源：${SUBJECT_SOURCE_LABEL[init.subjects_source] || '标准科目表'}`]
  const at = formatInitTime(init.subjects_at)
  if (at) parts.push(`，时间：${at}`)
  return parts.join('') + '）'
}

/**
 * 第 2 步的两种来源，中文名。两种「第 2 步已完成」在账面上的含义完全相反：
 * 一种账上有数，一种账上一个数都没有 —— 界面上必须说得清是哪一种，
 * 否则用户看到 ✓ 就以为期初已经录过了。
 */
export const OPENINGS_SOURCE_LABEL = {
  IMPORT: '已导入期初余额',
  NONE: '本期无期初余额'
}

/** 第 2 步完成后的那行字。「没有期初」不是「没做」，两者不能共用一句话。 */
export function initOpeningLabel(init) {
  if (!openingsReady(init)) return ''
  const at = formatInitTime(init.openings_at)
  const tail = at ? `，时间：${at}` : ''
  if (init.openings_source === 'NONE') {
    return `已确认本期无期初余额${tail}`
  }
  return `期初余额已导入（${Number(init.opening_count) || 0} 条${tail}）`
}

/**
 * 标题下那一句：已完成 / 还差第几步。
 *
 *  「已有账套如果第 X 步还没做」要说在标题旁边而不是让人自己数步骤 ——
 *  两个步骤都摆在他面前时，最难判断的恰恰是「我现在处在哪一步」。
 */
export function initSummary(init) {
  const step = initStep(init)
  if (step === STEP_DONE) return '两步初始化都已完成。'
  if (step === STEP_OPENINGS) return '第 1 步已完成，第 2 步「导入期初余额」还没做。'
  return '第 1 步「导入科目」还没做，第 2 步因此锁着。'
}

/** 底部那颗主按钮的三种身份。 */
export const FOOTER_DONE = 'done'
export const FOOTER_OPENINGS = 'openings'
export const FOOTER_NEXT = 'next'

export const FOOTER_LABEL = {
  [FOOTER_DONE]: '完成',
  [FOOTER_OPENINGS]: '去导入期初余额',
  [FOOTER_NEXT]: '下一步'
}

/**
 * 底部主按钮该是什么 —— 决定于**还剩什么没做**，而不是组件里记着「现在是第几步」。
 *
 *  写死成「下一步」的后果是：两步都做完了，这颗按钮还在，点下去什么也不会发生 ——
 *  用户只能理解成「还没初始化完」。而它不消失也不改词，本身就是在说这句假话。
 *  所以规则很简单：做完就换词，没做完就指向真正待办的那件事。
 *  第 1 步没做完时是唯一「点了也没用」的情形，组件把它置灰并让锁住的面板说明原因。
 */
export function initFooterAction(init) {
  const step = initStep(init)
  if (step === STEP_DONE) return FOOTER_DONE
  return step === STEP_OPENINGS ? FOOTER_OPENINGS : FOOTER_NEXT
}

/**
 * 步骤条的高亮下标：已经完成了几个。
 *
 *  同样从两个布尔算，而不是从组件里的本地 step 翻 —— 本地那份迟早跟 init 对不上，
 *  而对不上时用户看到的是「两步都做完了，步骤条却停在第 1 步」。
 */
export function initStepsActive(init) {
  const step = initStep(init)
  return step === STEP_DONE ? 2 : step - 1
}

/**
 * 「科目不存在」多到不正常时，给出**多半是哪个决定造成的**。
 *
 *  为什么要专门说这句：这份余额表来自用友，可账套的科目表是初始化第 1 步
 *  定的。选了「沿用系统标准科目表」的话，两套编码根本对不上 ——
 *  读进来个位数、跳过两百条，表格是平的（每行都填了），
 *  但合计差着几百万，于是「保存期初余额」灰着点不动，
 *  初始化第 2 步自然永远是「还没做」。
 *
 *  用户看到的现象横跨三个页面（导入预览、期初表、账套管理），
 *  自己拼不出「因为第 1 步选错了」这个结论。不点破的话，
 *  这件事的表现就是「导入成功了，但初始化就是不完成」。
 *
 *  @param {object} res 解析结果
 * @returns {string} 要提示的话；没这回事时返回空串
 */
export function mismatchHint(res) {
  const matched = Number(res?.matched) || 0
  const unmatched = Number(res?.unmatched) || 0
  // 阈值：读进来的不到没读进来的两成，且确实有没读进来的
  if (!unmatched || matched * 5 >= unmatched) return ''
  return `这份余额表里有 ${unmatched} 个科目在本账套里不存在，只读进 ${matched} 个。`
    + '多半是初始化第 1 步选了「沿用系统标准科目表」，而这份文件来自另一套'
    + '（如用友）—— 两边编码对不上。请回账套初始化，第 1 步改用「导入科目表」'
    + '把这份系统对应的科目表导进来，再导期初。'
}

/**
 * 导入预览的统计话术：「已读入 42 条，3 条科目在本账套不存在已跳过，499 行不是科目行」。
 *
 *  以前导入完只有一句「已读入 N 条」，用户无从判断自己那份用友表是不是读对了 ——
 *  真正决定「有没有丢东西」的是**没被读进去的那些**，而它们原来一声不吭。
 *  unmatched / skipped 都摆出来，读错文件当场能看出来，而不是等到总账对不上才发现。
 */
export function importSummaryText(res) {
  const matched = Number(res?.matched) || 0
  const unmatched = Number(res?.unmatched) || 0
  const skipped = Number(res?.skipped) || 0
  const parts = [`已读入 ${matched} 条`]
  if (unmatched) parts.push(`${unmatched} 条科目在本账套不存在，已跳过`)
  if (skipped) parts.push(`跳过 ${skipped} 行非科目行`)
  return parts.join('，')
}

/** 借贷差额。balanced 为 true 时没有差额可言，返回 0。 */
export function balanceDiff(res) {
  const dr = Number(res?.total_debit) || 0
  const cr = Number(res?.total_credit) || 0
  return dr - cr
}
