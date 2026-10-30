/**
 * 等打印页里的字体加载完。
 *
 * **为什么必须等。** 打印页可以内嵌一份随应用发布的字体（模板里的
 * 「锁定字体」），那份字体到位之前浏览器先拿系统字体排版 ——
 * 预览量到的是系统字体的排版，打印出去的是内嵌字体的排版，
 * 两边对不上；而在字体没到位时按下打印，纸上出来的是**系统字体**那一版，
 * 正是「所见即所得」要消灭的东西。
 *
 * **必须有上限。** 字体加载失败（网络不通、文件没随包发布）时
 * ``fonts.ready`` 可能迟迟不 resolve —— 打印按钮不能因此永远按不动。
 * 超时就放行：没有内嵌字体时排版本来就是对的，等不到也不影响。
 *
 * @param {Document} doc 打印页所在文档（iframe 的 contentDocument 或打印窗口的 document）
 * @param {number} ms 最长等多久（毫秒）
 * @returns {Promise<void>}
 */
export function fontsReady(doc, ms = 5000) {
  const fonts = doc?.fonts
  if (!fonts || !fonts.ready) return Promise.resolve()
  return Promise.race([
    fonts.ready.catch(() => {}),
    new Promise((resolve) => setTimeout(resolve, ms)),
  ])
}
