# 插件风险记录与生产启用条件

日期：2026-10-18。架构决策依据：本仓库 `plan.md`，受信任、同进程全栈扩展。工程实现及失败路径的验证证据见 [plugin-verification.md](plugin-verification.md)。本记录不代替生产负责人对具体发布者及具体包的人工审阅/风险签署；代码默认 `PLUGIN_ENABLED=false`，本次工作未部署或开启生产插件。

## 决策

仅接受管理员选定、人工审阅、受信任 Ed25519 发布者签名的代码。签名证明来源和完整性，不证明逻辑正确。若组织要求抵抗恶意第三方插件，必须改为子进程/独立服务、独立数据库身份及进程间 API；当前方案不满足这个安全目标。

## 风险与控制

| 风险 | 已交付控制 | 仍然存在的边界 |
|---|---|---|
| Python/迁移读取秘密、破坏数据、exit 或无限占用资源 | SDK 不传 app/DB、明显危险行为扫描、签名、admin、审计 | 同进程代码可绕过 API、资源耗尽会影响核心，无 OS 隔离 |
| 同源 JS 读取登录态/调用其他 API | 审查签名、自包含 ESM、固定资产 URL、现有 CSP、无 query token | 同源代码具有 origin 权限，public asset 不是隔离边界 |
| 管理员能管理但不应自动使用插件 | 独立 plugin_user_access，安装零授权，admin 不豁免 | 管理员可以显式给自己授权，审计记录该操作 |
| 不同 worker 版本/失败状态不一致 | supervisor generation、expected count、DB 租约和逐请求全 worker barrier | 共享 DB 不可用时插件停用；lease 是就绪证据而非强制终止 |
| 插件迁移伤及宿主、漏备份 | 独立 version table、事务、范围反射、真实 FK/guardrail 验证、v2 preflight | 恶意 Python 拥有同一进程权限，范围检查不等于数据库权限隔离 |
| FS rename 与 DB commit 中断 | 同文件系统、fsync、session advisory lock、flock、durable journal、reconcile | 没有跨资源原子事务；结构不明保留现场人工恢复 |
| 运行时撤权/浏览器残留页面 | 每请求 DB 授权、401/404处理、focus/route刷新、清理路由/tabs/styles | 已读取的数据/模块缓存无法撤回；已开始的操作不被回溯取消 |
| 签名密钥轮换/撤销 | 新包拒绝 revoked/historical key，历史 digest 保留校验公钥 | 已运行代码不会自动停止；紧急停用需关闭并重启 |
| 升级失败/卸载误删数据 | 禁用+重启+旧lease清空，备份，升级保持禁用，卸载保留表 | 不支持自动 downgrade；恢复需要匹配代码、库版本、快照和公钥 |

## P0 决策门证据

| 原型 | 成功证据 | 失败证据 |
|---|---|---|
| 整包签名 | 离线工具/示例完整签名、解包核验 | 改 Python/JS/migration/资源、未知/撤销 key、路径/容量攻击拒绝 |
| 真实双 worker | 两个 OS 进程同 generation 可用、提交授权即时一致 | 注入第二 worker 初始化失败，两边统一 503；旧 generation 拒绝 |
| 浏览器资产鉴权 | 生产构建+CSP下 native import 不带 Bearer、API 带 Bearer | 匿名 API 401、未授权 404、模块错误降级，核心继续工作 |
| 独立迁移 | 单插件 version table，追加 revision 保留行，主链不变 | 越界 DDL/迁移异常回滚，启动不执行 DDL |
| backup v2 | 插件实际数据 round-trip、global/book FK 闭包 | 未知/digest/head/metadata/scope/代码不符，缺父行写入前拒绝 |
| 使用授权闭环 | UI 用户搜索/选择/保存，grant/regrant 无重启 | 默认零授权，未授权 admin/其他用户 404，撤销旧 token 下一请求拒绝 |

以上门禁进入 CI；测试通过证明契约中的行为，不证明任意代码安全。阶段不以降低已有测试收集数、跳过全量回归、关闭上传/扫描/签名闸门的方式通过。

## 生产启用前由运营方记录

记录责任人、已审阅插件 id/version/digest、发布者/key id、权限及数据范围、数据备份和代码恢复材料位置、运行目录/配额、两 worker 重启与屏障结果、应急负责人，以及对上述同进程风险的接受结论。拒绝风险则保持默认关闭。本次工程验证不填写虚构的人名或批准结论。

紧急处置：关闭 `PLUGIN_ENABLED` 并重启全部 worker；核心账务仍按原路径运行。保留 registry、业务表、journal、quarantine、备份及签名材料，不自动回退历史主迁移或删除插件表。
