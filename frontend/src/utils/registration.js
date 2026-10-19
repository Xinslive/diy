/**
 * 登录页的「注册」页签怎么显示。
 *
 * 抽成纯函数是为了能单测：这个判断以前散在模板的 `v-if` 里，出错的表现是
 * 「注册按钮还在但点不动」或者「关掉了还留着半张表单」，两种都不是看一眼
 * 源码就能发现的 —— 而它是这套系统上唯一一处**匿名用户可见的入口**。
 *
 * 前提：调用方拿到的 `enabled` 必须是**失败即关闭**。接口不通时按「关着」
 * 处理（见 LoginView.vue 里那段 try/catch）—— 宁可暂时少一个注册入口，
 * 也不要在一个没人确认过状态的界面上把注册按钮亮出来。
 *
 * **只认严格的 true，不是 `!!enabled`。** `!!'false'` 是 true、
 * `!!'0'` 也是 true —— 后端因此对开关用了 `StrictBool`，前端这里若图省事写成
 * 宽松转换，就会出现「后端明明关着、页面却把注册入口亮出来」，而且只在某个
 * 序列化环节把布尔变成字符串时才发作，现场极难复现。宁可少认，不要多认。
 */

/**
 * @param {boolean} enabled 后端 /api/auth/config 回来自助注册是否开放
 * @param {string} currentTab 当前停在哪个页签（'login' | 'register'）
 * @returns {{showRegister: boolean, tab: string, showTabs: boolean}}
 */
export function registrationView(enabled, currentTab) {
  const showRegister = enabled === true
  return {
    showRegister,
    // 关掉注册时把当前页签拉回登录。少了这一句，一个已经停在「注册」页的人
    // （管理员刚在后台关掉、或他开着页签时刷新失败）会看到一张没有页签栏的
    // 空白注册表单，还以为页面坏了。
    tab: !showRegister && currentTab === 'register' ? 'login' : (currentTab || 'login'),
    // 只剩登录一个页签时，页签栏没有信息量（一行只有一个「登录」），去掉它。
    showTabs: showRegister,
  }
}
