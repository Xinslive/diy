# 审计报告修复验收记录

> 对应 `output/AUDIT-REPORT-2026-10-25.md`（45 条发现 + §7 修复路线图 50 项）。
> 记录日期：2026-10-25
>
> **判据**：每一条结论都必须落到 `file:line` 或一条可复现命令的输出上。
> 文档与提交记录**不作为**结论来源 —— 审计报告里写的行号有一批是旧快照
> （§3.1 的四条复核主张就是按旧行号复核的，见 §6），所以下面每一条都重新取过证。

---

## 0. 结论

| 口径 | 结果 |
|---|---|
| §3 的 **45 条发现** | 45/45 已修。每条都有落点与守着它的用例 |
| §7 的 **50 项路线图** | 50/50 有代码证据（第 11 项的**「怎么做」已落成可跑的命令**；「谁来做」必须在真实库上人工执行，见 §4） |
| 第 7 步（CI 规则） | 已落：`backend/tests/test_single_source_of_truth.py` + `frontend/tests/single_source.test.mjs` |
| 机制偏离（报告建议 ≠ 实际实现） | 3 处，见 §3 |
| 明确没有做的部分 | 2 项，见 §4 |
| 回归 | 后端 **3554** 绿（带 PG 客户端）/ 前端 **238** 个测试文件全绿 / 部署 **242** 绿 / dev.sh 29 绿；`app/` 行覆盖率 **89.30%**（CI 下限 88%） |
| **第二轮独立复核** | 判 **FAIL**，三条确认问题（A-1 / A-2 / A-3）—— **已全部修完**，见 §5.8 |
| **第三轮独立复核** | 判 **PASS_WITH_CAVEATS**：三条主张全部成立、**无回归**；留三条 caveat（F-1 终审第 6/37 项判据无牙 / F-2 新用例不 hermetic / F-3 缺「配了密钥⇒密文」断言）—— **已全部修完**，见 §5.9 |
| **第四轮独立复核**（§3.1–§3.8） | 判 **FAIL**：§3.2 的闸门**管得太窄**（只挂 7 条路由）→ 整道闸门从侧翼被击穿。四条（N-1 改收件人 / N-2 铸 MCP 令牌 / N-3 假牙守卫 / N-4 其余路由）—— **已全部修完**，见 §5.10 |
| **第五轮独立复核**（§3.9–§3.15） | 判 **PARTIAL**：三条（N-1 `ReportRow.style` 零审计 / **N-2 恢复旧备份会把护栏一起带没且恢复仍返回 0** / N-3 报表行审计）—— **已全部修完**，见 §5.11 |
| **第六轮独立复核**（§3.16–§3.22） | 判 **PARTIAL**：三条（N-1 §3.17 的 M 判据 ≠「整理真能补得上」/ N-2 §3.16 文案陈述不成立事实 / N-3 §3.19 文案未区分年度不匹配）—— **已全部修完**，见 §5.12 |
| **第七轮独立复核**（§3.23–§3.33） | 判 **FAIL**：**§3.26 根本没修**（同一个数四个通道四个值）/ **§3.33 根本没修**（`prevBookId` 被 v-model 抢先写废）+ 四条新发现 + 8 条缺行为级用例 —— **已全部修完**，见 §5.13 |
| **第八轮独立复核**（§3.34–§3.45） | 判 **PASS_WITH_CAVEATS**：12 条代码修复**全部为真**；四条 caveat（**N-1 依赖回退会把现网变成「新代码+旧依赖」** / N-2~N-4 三处钉子漏网）—— **已全部修完**，见 §5.14 |
| **第一次生产发布** | 发布到 `43.138.178.174` 时，§3.44 那类**启动闸门**第一次在生产上真的生效：`.env` 照抄示例（`MCP_ALLOWED_HOSTS=` 空）⇒ 服务拒绝启动、站点中断约 2 分钟。已修，并暴露两个真缺陷：**报错文案会引人配出「启动过了但 MCP 全被 421 挡下」的配置**（已改文案 + 2 条用例）、**§3.41 的闸门让 `target-host.md` 记的调用方式少了必需参数**（已补）。发布后 8 项验证全过 —— 见 §5.17 |
| **CI 抓到的最后一条** | 新写的用例依赖了 **不在版本库里** 的示例插件源码（``/plugins/`` 被 gitignore）—— 本机全绿、**CI 必红**。已修（改成跳过，并把另一条「空转即绿」的循环一起修掉）；并补一条验收标准：**新用例必须在干净 checkout 上也成立**，「本地全绿」不能作为证据 —— 见 §5.16 |
| **前端撞闸门之后没有下文** | 闸门扩到 13 条受管控写路由后，管理员只看到一句 403、**界面上没有下一步**（`mfaReauth()` 全仓无调用方）。已补齐「403 → 引导 → 重新验证 → **自动重试**」整条链 —— 见 §5.15 |
| **终审判据的牙齿** | 12 处判据由「任一命中」收紧为「**都要在**」；第 43 项的反向逻辑修正；证据改为**优先报代码行**；第 26/27 项的判据收紧到**实现本身**（原来能被注释满足）。**两轮反向验证各 19/19 变红**（其中一轮**只抹代码行、留着注释**） |
| **可复现的取证** | `tools/audit_roadmap_check.py`（§7 50 项，51/51）<br>`tools/audit_plugin_sandbox_recheck.py`（§3.1 四条主张，退出码 0 = 都不成立且都被用例钉住）<br>`backend/scripts/data_health_check.py`（§7 第 11 项存量数据体检；退出码 0/2/3 = 通过/阻断/提示）<br>`backend/tests/test_db_backup_restore.py`（备份→恢复**真实往返**；需 PG 客户端，见 §3.5） |

**没有全部做完的地方，说清楚是什么**：§7 第 11 项的**体检命令已经落成**
（`backend/scripts/data_health_check.py`，只读），但**体检本身必须在生产库上
人工跑一次**，而且「哪一条重复该留、金额该怎么修」只有懂业务的人能决定；
插件隔离的 systemd/容器形态要在部署侧才能验（本机 macOS）—— 见 §4。

---

## 1. 怎么核的（可复现）

§7 的 50 项用一条机械脚本逐项找代码证据，找不到就是 FAIL，不做「看起来应该改了」的推测：

```bash
python3 tools/audit_roadmap_check.py      # → 通过 51 / 失败 0
```

脚本判据一律指向**代码**而不是注释 —— 第一版有几项命中的是注释（例如
`TRUSTED_PROXIES`、`this_ip`、OSS 加密三处），已逐条收紧到闸门代码本身。
**第二轮复核又发现 63 条规格里有 17 条的首条命中仍是注释或 import**，
于是又收紧了一轮（见 §5.8）。下表是**当前**的判据与**实测**落点
（行号随代码漂移，所以这里写的是「取证的命令」，不是一份会过期的快照）：

| 项 | 收紧后的判据 | 证据（实测） |
|---|---|---|
| 3 | 两个文件都要有 `normalize_role(` | `backend/app/api/reports.py:142`、`backend/app/api/printing.py:633` |
| 12 | 两个文件都要有 `.with_for_update()` | `backend/app/api/vouchers.py:729`、`backend/app/accounting/vouchers.py:751` |
| 13 | `CONSTRAINT_NAME = "uq_entry_voucher_line_no"` | `backend/migrations/versions/0038_entry_voucher_line_unique.py:94` |
| 17 | `const payableEntries = computed` | `frontend/src/views/voucher/VoucherEdit.vue:889` |
| 21 | `def verify_key(` + `def verify_guard(` | `backend/app/notify/mfa.py:680` / `:699` |
| 22 | `verify_password(payload.password, _dummy_hash())` | `backend/app/api/auth.py:1042` |
| 23 | 补算那行 `pbkdf2_hmac(..., _ITERATIONS - iters)` | `backend/app/core/security.py:173` |
| 26 | 两个脚本都要有 `NGINX_T_LOG="$(mktemp)"`（**那行赋值**，不是提到 mktemp 的注释） | `deploy/esa-mtls.sh:162`、`deploy/esa-realip-update.sh:246` |
| 27 | 两个 unit 都要有 `^TimeoutStartSec=\d+`（**那一条指令**，不是注释） | `deploy/systemd/cwq-backup.service:30`（=1800）、`cwq-oss-backup.service:25`（=3600） |
| 32 | 「前端首页取不到」+ 那句 `die` | `deploy/deploy.sh:1477` |
| 34 | `if [[ -z "$this_ip" ]]` | `deploy/deploy.sh:282` |
| 35 | `if not trusted_proxy_networks(settings.trusted_proxies)` | `backend/app/core/config.py:1338` |
| 36 | 闸门那一行 + 那条报错文案 | `backend/app/core/config.py:1260` / `:1268` |
| 6 | 判据 `key_error = _backup_encryption_key_error` + **阻断点**（那句报错文案） | `backend/app/core/config.py:1303` / `:1313` |
| 37 | `if _oss_backup_configured(settings)` + **阻断点** | `backend/app/core/config.py:1302` / `:1313` |
| 38 | `grep -E …access_log`（真去配置里取日志路径） | `deploy/esa-mtls.sh:110` |
| 43 | 「`fmt(` **不出现**」**且**改用 `reportNumber` | `frontend/src/views/report/ReportView.vue:59` |
| 44 | 三个文件都要有在途守卫 | `VoucherSplitDialog.vue:87`、`ReportView.vue:123`、`SubjectTree.vue:12` |
| 46 | `isConnected` + `current = null` | `frontend/src/composables/useCellSelection.js:90` / `:25` |
| 47 | 记号存在 store，重挂后取（三处都要在） | `frontend/src/stores/session.js:410` |
| 48 | `class WorkerHandle` + 隔离分流 | `backend/app/plugins/ipc/host.py:109` |
| 49 | 裁剪 builtins + 拒非 `.py` + 注入 `__builtins__` | `backend/app/plugins/sandbox.py:144/567/583` |
| 51 | 后端 + 前端各一条用例 | `backend/tests/test_single_source_of_truth.py:74`、`frontend/tests/single_source.test.mjs` |

> **「都要在」的那些项（3/6/12/26/27/36/37/39/41/44/46/47/48/49/51）用 `ev_all()`**。
> `ev(a) + ev(b)` 的语义是「**任一**命中就算过」—— 用它拼「都要在」的判据，
> 少一处照样 PASS，那条判据就是假的（把体检脚本整个删掉，终审脚本仍然说
> 第 11 项通过）。**19/19 条判据经反向验证能变红** —— 而且是**两轮**：
> 一轮把判据的全部命中都抹掉，一轮**只抹代码行、把注释留着**
> （后者抓出了第 26/27 两处仍能被注释满足的假牙）。见 §5.8。

### 1.1 §7 第 11 项：存量数据体检怎么跑

这一项是**上线前在真实库上跑的一次性动作**（代码替不了「该留哪一条」那个决定），
但「怎么查」已经落成一条命令，不需要临场自己写 SQL：

```bash
cd backend
.venv/bin/python scripts/data_health_check.py     # 读 .env 里那个库
```

| 退出码 | 含义 | 动作 |
|---|---|---|
| 0 | 体检通过 | 可以上线 |
| 2 | **有阻断项**（同一 `voucher_id` 下 `line_no` 重复） | **不能上线**：先清理重复分录。迁移 `0038` 会因为有重复行而拒绝加唯一约束 |
| 3 | 只有提示项（整理就绪体检不干净：断号/重号/混用凭证字/时序） | 上线前人工确认；这些期间不能记账、不能结账 |
| 1 | 体检自己没跑成（连不上库） | 修连接串/脚本，**不是**「库有问题」 |

两个设计点值得说明：

- **只读。** 体检脚本一条写操作都没有，跑完之后库里逐字节不变 —— 它不污染
  被体检的对象。
- **判据只有一份。** 「同一张凭证里行号重复」这个判据在
  `app/accounting/data_health.py`；迁移 `0038` 的前置检查与体检脚本**都调它**
  （审计报告 §4.5：同一个量只允许一个权威实现）。两边点名的口径由
  `describe_duplicate()` 一处产出，所以不会出现「体检说干净、迁移说有重复」。
  `test_audit_fix_data_health_check.py` 钉的就是这一条。

体检用的是**「整理凭证」页面上那个判据**（`app.accounting/organize.py` 的
`check_period`），不是另写一份 —— 所以上线前体检报的断号，与页面上点
「整理凭证」看到的断号，必然是同一批。

---

## 2. §3 的 45 条发现

「用例」列是**守着这条修复的回归用例**；没有用例的，本轮补了（见 §5）。

| 发现 | 严重度 | 修复落点 | 用例 |
|---|---|---|---|
| §3.1 插件沙箱可绕过 | 高 | 静态扫描（`plugins/scan_policy.py`）+ 运行期能力裁剪（`plugins/sandbox.py`）+ **独立进程执行**（`plugins/ipc/`） | `test_plugin_sandbox.py`（111 条，含 20 组命名空间走私 + 6 组动态拼名 + 失败即闭不变量）、`test_plugin_worker_isolation.py`、`test_plugin_isolation_modes.py` |
| §3.2 装插件/建号缺二次验证 | 高 | `plugins/api.py` 写路径 + `api/system.py` 的**管理员写路由**挂 `require_recent_admin_mfa`（本轮从 7 条扩到 **13 条**：铸造/改写凭据、放松限制、插件任务开关、调度密钥轮换；**吊销类刻意不挂** —— 给安全阀加锁是把安全阀焊死）+ 改收件人时**作废所有未完成的挑战** + 前端补上「撞闸门之后有下文」的整条链（见 §5.15） | `test_audit_fix_recent_mfa.py`、`test_audit_fix_admin_write_mfa_gates.py`（25 条）、`test_audit_fix_mfa_reauth_contract.py`（5 条，含**跨语言核对**）、`mfa_reauth.test.mjs`（10 条） |
| §3.3 MFA 验证端点共享 IP 桶 | 高（条件性） | `notify/mfa.py` 的 `verify_guard` 键改为以 `email_token` 摘要为主键 | `test_audit_fix_mfa_throttle.py` |
| §3.4 MFA 轮询限流键可伪造 | 高 | `notify/mfa.py` 的 `poll_guard` 键改为 `(challenge_id, sha256(client_secret))` | `test_audit_fix_mfa_throttle.py` |
| §3.5 登录时序旁路 | 高 | `core/security.py:177` `password_needs_rehash` + 登录成功后补算 | `test_audit_fix_login_timing.py` |
| §3.6 注册接口耗时成为枚举预言机 | 高 | `api/auth.py` 重名分支执行等量散列 | `test_audit_fix_login_timing.py` |
| §3.7 失败计数无锁读改写 | 中 | `api/auth.py` 行锁串行化（机制偏离，见 §3） | `test_audit_fix_login_failure_counter.py` |
| §3.8 生产未强制备份加密密钥 | 中 | **启动闸门** `core/config.py:1302` `if _oss_backup_configured(settings)` → 不配密钥即 `problems.append`（拒绝启动）；**运行闸门** `backup/config.py::require_encrypted_ready`（按**生效配置**判，**界面上配 OSS 的那条路也覆盖** —— 见 §5.8 A-1） | `test_audit_fix_prod_config_gates.py`、`test_audit_fix_backup_encryption_runtime.py` |
| §3.9 报表模板改动无审计 | 中 | `api/reports.py` 模板写路径补 `log()`；**快照带上 `style`**（拷贝）并参与 `before != after` —— 否则「把双栏表右栏的项目名换掉」不留痕 | `test_audit_fix_report_template_audit.py`（含 style 一条） |
| §3.10 报表入参未类型化 | 中 | `api/reports.py` 改用带类型的入参模型 | `test_audit_fix_report_input_types.py` |
| §3.11 审计 `limit` 未夹下限 | 中 | `api/books.py` `Query(100, ge=1, le=1000)` | `test_audit_fix_books_audit_limit.py` |
| §3.12 「必须是 OWNER」两套实现 | 中 | `api/reports.py` / `api/printing.py` 统一走 `normalize_role` | `test_audit_fix_owner_role_normalize.py` |
| §3.13 授权门在函数体内 | 中 | `api/reports.py` 写接口把门禁提到签名（`Depends(require_template_write)` / `require_owned_template`） | `test_audit_fix_report_input_types.py` |
| §3.14 / §3.22 8 条 CHECK 未在 ORM 声明 | 中 | 库里 19 条 CHECK 约束，模型侧现在**全部**同名同义声明（分布在 8 个 model 文件）；回归守卫是**拿活库比**，不是比一个写死的数字 | `test_audit_fix_check_constraints.py` |
| §3.15 凭证修改无并发控制 | **严重** | `api/vouchers.py` + `accounting/vouchers.py` 加 `with_for_update()`；版本不一致返 **409**；`entry` 加唯一约束（迁移 `0038`）+ `db_init.sql` 触发器；**恢复路径也要对齐 schema**（恢复旧备份会把护栏一起带没，见 §5.11 N-2） | `test_audit_fix_voucher_concurrency.py`、`test_audit_fix_voucher_version.py`、`test_audit_fix_entry_migration_preflight.py`、`test_db_backup_restore.py`（新增 2 条） |
| §3.16 年度结转静默跳过 | 高 | `accounting/periods.py` `retained is None` → fail-closed；**文案按 `profit is None` 分流** —— 原来那句「科目不存在或已停用」在两种情况下都是**假话**（见 §5.12 N-2） | `test_audit_fix_carry_forward_retained.py`、`test_audit_fix_cross_year_subject.py` |
| §3.17 断号判据两份实现 | 高 | `accounting/organize.py` 抽出 `_assign_sequences`（**发号规则唯一实现**）+ `_organize_resulting_rows`（**照整理规则模拟一次整理**，看这个号之后还是不是一个断号）—— 原来的 M 启发式会把「真能补得上」的号判成断号（310 组配置里 44 组误报） | `test_audit_fix_organize_gap.py`（243 组配置穷举，不一致 26 → **0**） |
| §3.18 凭证字写死「记」 | 高 | `accounting/vouchers.py` 改用 `period_prefix` | `test_audit_fix_voucher_prefix.py` |
| §3.19 跨年度科目校验用旧年份 | 高 | `accounting/vouchers.py` 改用目标期间的年份；文案**区分「年度不匹配」**（见 §5.12 N-3） | `test_audit_fix_cross_year_subject.py` |
| §3.20 完整性违例误判 | 高 | `api/vouchers.py` 按**约束名**区分 `IntegrityError` | `test_audit_fix_integrity_error.py` |
| §3.21 `task_key` 塞进 `String(64)` | 高 | `api/system.py` `entity_id` 改用 sha256 截断到 32 位 | `test_audit_fix_scheduler_audit_entity_id.py` |
| §3.23 显示口径 ≠ 提交口径 | 高 | `VoucherEdit.vue` 抽出 `payableEntries()`，显示与提交共用同一份 | `voucher_deadcode.test.mjs` |
| §3.24 切期间时 flush 与新页 GET 竞态 | 高 | `utils/voucherFlush.js` 跨组件盒子：`onBeforeUnmount` 挂出、`onMounted` 先 await 再 `load()` | **`audit_fix_frontend.test.mjs`（本轮补）** |
| §3.25 网络失败渲染成「没有数据」 | 高 | `api/client.js` 加网络/超时兜底 + **5xx 兜底**；账簿三视图 `load()` 加 try/catch + `loadError` 状态 | `audit_fix_frontend.test.mjs`、**`test_recheck_load_failure.test.mjs`（本轮补）** |
| §3.26 屏幕与导出不是同一个数 | 中 | **口径定在唯一的生产者 `evaluate()` 上**（`api/reports.py` / `api/printing.py` / `mcp/tools/analysis.py` 全部经它）→ **新建 `backend/app/core/money.py`**（金额两位小数的**唯一权威实现**，`ROUND_HALF_EVEN` 与 MCP 原行为逐字一致），四条通道**全部读同一个两位小数值**；Excel 超 15 位有效数字改写成**文本**（精确数字）；口径说明走独立的 `display_scale` 字段（**不进 `notices`**）并在界面注明。**注**：原方案「前端保留 28 位原值」会让屏幕离另外三条通道**更远**，已废弃 | **`test_audit_fix_report_caliber.py`（7 条）**、**`report_display_scale.test.mjs`（6 条）**、`test_mcp_caliber.py`、`single_source.test.mjs` |
| §3.27 体检失败时放开 RENUM | 中 | `VoucherOrganize.vue` 抽成 `checkState` 状态机，**只有 `ok` 允许 RENUM** | `audit_fix_frontend.test.mjs`、**`test_recheck_organize_check.test.mjs`（本轮补）** |
| §3.28 新建凭证无幂等键 | 中 | 后端 `core/idempotency.py` + 迁移 `0039`；前端 `utils/idempotency.js` + `failedSig` 短路 | `test_audit_fix_voucher_idempotency.py`、`voucher_idempotency.test.mjs` |
| §3.29 拆分弹窗无在途守卫 | 低 | `VoucherSplitDialog.vue` `submitting` + `:loading` + 早退 | `voucher_split.test.mjs` |
| §3.30 报表设计无在途守卫 | 低 | `ReportView.vue` 共用 `structureBusy` + `withStructureBusy` | `audit_fix_frontend.test.mjs`、**`test_recheck_row_refs.test.mjs`（本轮补）** |
| §3.31 `rowEls` 误删行引用 | 低 | `VoucherEdit.vue` `rowEls` 改用**下标键** | `voucher_race.test.mjs`、**`test_recheck_row_refs.test.mjs`（本轮补）** |
| §3.32 选区复制到旧表格 | 低 | `useCellSelection.js` `liveRows()` 校验 `isConnected` + `hashchange` 清选区 | **`audit_fix_frontend.test.mjs`（本轮补）** |
| §3.33 切账套提示永不执行 | 低 | 记号搬进 store（`prevBookId` / `takePrevBookId()`），MainLayout 负责清理；判据改成 **watch 记下来的上一代**（`ensureBookSwitchWatch`，`flush: 'sync'`）—— 原方案问「bookId 变了没有」，而 `v-model` **会抢先写**，永远答 false | `test_reviewfix_cross_book.test.mjs`、**`session_book_switch.test.mjs`**、**`test_recheck_book_switch_prompt.test.mjs`** |
| §3.34 回退闸在覆盖之后 | 高 | `deploy/deploy.sh` `ROLLBACK_SCOPE=code` 前移到 tar 覆盖**之前**；解包改到 `$INCOMING` 再 `rsync --delete` | `deploy/tests/test_deploy.sh` |
| §3.35 固定 `/tmp` 路径 | 高 | `esa-mtls.sh` / `esa-realip-update.sh` 改用 `mktemp` | 同上 |
| §3.36 备份 unit 无 `TimeoutStartSec` | 高 | `deploy/systemd/cwq-backup.service`、`cwq-oss-backup.service` | 同上 |
| §3.37 半截 dump 被当成功 | 高 | `deploy/deploy.sh` 改用 `db_backup.py --out` | 同上 |
| §3.38 回退库命令不可执行 | 高 | `deploy/deploy.sh` 打印**两步**恢复流程并警告会丢写入 | 同上 |
| §3.39 依赖就地改写现网 venv | 高 | `deploy/deploy.sh` 新依赖装进独立 venv（快照 `.venv` 进回退清单） | 同上 |
| §3.40 mtime 判据写反 | 高 | `deploy/deploy.sh` 改用 `-mtime "+${keep_days}"` | 同上 |
| §3.41 发布不覆盖 nginx | 高 | `deploy/deploy.sh` nginx 配置纳入渲染范围；`APP_DIR != /opt/cwq` 即 die | 同上 |
| §3.42 mtls 读的配置路径对不上 | 高 | `deploy/esa-mtls.sh` 自动探测 vhost 与 `access_log` | 同上 |
| §3.43 不拒 `0.0.0.0/0` | 高 | `deploy/esa-realip-update.sh` 前缀长度闸（`:44` / `:184`） | 同上 |
| §3.44 `TRUSTED_PROXIES` 只告警 | 高 | 应用侧 `config.py` + 部署侧 `deploy.sh` 双侧阻断 | `test_audit_fix_prod_config_gates.py` + `test_deploy.sh` |
| §3.45 目标机守卫 fail-open | 高 | `deploy/deploy.sh:282` `this_ip` 为空即 die | `test_deploy.sh` |

---

## 2.5 §7 的 50 项逐条核对

| 项 | 报告 §7 的要求 | 代码证据 | 判定 |
|---:|---|---|---|
| 1 | `app/api/books.py:1088` → `Query(100, ge=1, le=1000)`（§3.11） | `backend/app/api/books.py:1088` def audit_logs(book_id: int, limit: int = Query(100, ge=1, l | ✅ |
| 2 | `app/api/reports.py:785,838` → 改用带类型的入参模型（§3.10） | `backend/app/api/reports.py:587` class ReportTemplateIn(BaseModel): | ✅ |
| 3 | `app/api/reports.py:131`、`app/api/printing.py:622` → 改用 `normalize_role`（§3.12） | `backend/app/api/reports.py:142` if normalize_role(link.role) != "OWNER":<br>`backend/app/api/printing.py:633` if normalize_role(link.role) != "OWNER": | ✅ |
| 4 | `app/models/accounting.py` 等 5 张表 → 补齐 8 条同名同义的 `CheckConstraint`（§3.22 / §3.14） | `backend/app/models/accounting.py:100` CheckConstraint("status IN ('UNPOSTED','POSTED')", name="ck_<br>`backend/app/models/accounting.py:112` CheckConstraint(<br>`backend/app/models/accounting.py:178` CheckConstraint("direction IN ('DEBIT','CREDIT')", name="ck_<br>`backend/app/models/accounting.py:179` CheckConstraint("amount <> 0", name="ck_entry_amount_nonzero | ✅ |
| 5 | `app/api/reports.py:662-894` → 补审计 `log()`（§3.9） | `backend/app/api/reports.py:270` log(db, action="FORK_REPORT_TEMPLATE", entity="report_templa | ✅ |
| 6 | `app/core/config.py` → `validate_production_settings` 增加 `BACKUP_ENCRYPTION_KEY` 检查（§3.8） | `backend/app/core/config.py:1303` key_error = _backup_encryption_key_error(settings.backup_enc | ✅ |
| 7 | `app/api/system.py:1209` → `entity_id` 改用 sha256 截断到 32 位（§3.21） | `backend/app/api/system.py:1265` entity_id = hashlib.sha256(row.task_key.encode()).hexdigest( | ✅ |
| 8 | `app/api/vouchers.py:271-281` → 按约束名区分 `IntegrityError`（§3.20） | `backend/app/api/vouchers.py:383` except IntegrityError as exc: | ✅ |
| 9 | `app/accounting/vouchers.py:731-732` → 改传目标期间的年份（§3.19） | `backend/app/accounting/vouchers.py:438` _validate_entries(db, book_id, payload, period.year, allow_i | ✅ |
| 10 | `app/accounting/vouchers.py:529` → 新建凭证改用 `period_prefix`（§3.18） | `backend/app/accounting/vouchers.py:555` prefix = svc_organize.period_prefix(db, period, fallback="记" | ✅ |
| 11 | **先做一次存量数据体检**：查 `entry` 是否已存在同一 `voucher_id` 下 `line_no` 重复的行（§3.15 场景 B 已经能落库）。有重复则先人工清理 —— **这一步是第 12 项的前置**。 | `backend/scripts/data_health_check.py:81` return duplicate_line_no(db, book_id=book_id)<br>`backend/app/accounting/data_health.py:59` def duplicate_line_no(<br>`backend/migrations/versions/0038_entry_voucher_line_unique.py:143` from app.accounting.data_health import duplicate_line_no | **命令已落成**（只读）；**执行须在真实库上人工跑一次**，见 §1.1 与 §4 |
| 12 | `app/api/vouchers.py:254` / `app/accounting/vouchers.py:608` → 对 voucher 行加 `with_for_update()` + `db.refresh()`，再判 `status`（照抄 `split_voucher` 的写法）；… | `backend/app/api/vouchers.py:729` .with_for_update()<br>`backend/app/accounting/vouchers.py:751` .with_for_update() | ✅ |
| 13 | 新增迁移：`entry` 加 `UniqueConstraint("voucher_id", "line_no")`（§3.15） | `backend/migrations/versions/0038_entry_voucher_line_unique.py:94` CONSTRAINT_NAME = "uq_entry_voucher_line_no" | ✅ |
| 14 | `app/core/db_init.sql` → `fn_posted_voucher_guard` 增加「entry 侧 DELETE/INSERT 也要看凭证状态」的触发器（§3.15） | `backend/app/core/db_init.sql:118` CREATE OR REPLACE FUNCTION fn_entry_lock_guard() RETURNS tri<br>`backend/app/core/db_init.sql:224` CREATE TRIGGER trg_entry_lock_guard BEFORE INSERT OR UPDATE <br>`backend/app/core/db_init.sql:215` RAISE EXCEPTION '凭证 % 已记账，不能修改或删除它的分录；如需改动请先反记账', v_no | ✅（**机制偏离**：并进既有触发器，见 §3.4） |
| 15 | `app/accounting/periods.py:1359` → `retained is None` 时 fail-closed 并给出可操作文案（§3.16） | `backend/app/accounting/periods.py:1358` retained = _subject_by_any_code(db, book.id, current_year, R | ✅ |
| 16 | `app/accounting/organize.py` → 把 M 判据抽成唯一共享函数，`assert_tidy()` 与 `irreparable_gaps()` 都调它（§3.17） | `backend/app/accounting/organize.py:599` def fixable_gaps(rows: list[_VoucherRow]) -> tuple[list[str] | ✅ |
| 17 | 前端：`VoucherEdit.vue` 抽出 `payableEntries()`，让显示口径与提交口径**用同一个集合**；并对「有金额无科目」的行明确报错（§3.23） | `frontend/src/views/voucher/VoucherEdit.vue:889` const payableEntries = computed(() => | ✅ |
| 18 | `app/plugins/api.py` 拆分读/写管理依赖，写路径挂 `require_recent_admin_mfa`（§3.2） | `backend/app/plugins/api.py:65` _mfa: SysUser = Depends(require_recent_admin_mfa)): | ✅ |
| 19 | `app/api/system.py:270` `create_user` 挂 `require_recent_admin_mfa`（§3.2） | `backend/app/api/system.py:274` _mfa: SysUser = Depends(require_recent_admin_mfa)): | ✅ |
| 20 | `app/notify/mfa.py` 的 `poll_guard` 键改为 `(challenge_id, sha256(client_secret))`（§3.4） | `backend/app/notify/mfa.py:256` return hashlib.sha256((raw or "").encode("utf-8")).hexdigest | ✅ |
| 21 | `app/notify/mfa.py` 的 `verify_guard` 键改为以 `email_token` 摘要为主键（§3.3） | `backend/app/notify/mfa.py:680` def verify_key(token: str) -> str:<br>`backend/app/notify/mfa.py:699` def verify_guard(token_key: str) -> tuple[bool, int]: | ✅ |
| 22 | `app/api/auth.py:342-358` 注册重名分支执行等量散列（§3.6） | `backend/app/api/auth.py:1042` verify_password(payload.password, _dummy_hash()) | ✅ |
| 23 | `app/core/security.py` 的 `verify_password` 在存量轮数偏低时补算到当前轮数（§3.5） | `backend/app/core/security.py:173` hashlib.pbkdf2_hmac("sha256", raw, salt, _ITERATIONS - iters | ✅ |
| 24 | `app/api/auth.py:528-575` → 原子自增（`UPDATE ... RETURNING`），并把 `lock_level` 的递增一并处理（§3.7） | `backend/app/api/auth.py:599` .with_for_update() | ✅ |
| 25 | `deploy/deploy.sh` → `ROLLBACK_SCOPE=code` 移到 tar 覆盖**之前**；解包改到 `$INCOMING` 再 `rsync --delete`（§3.34） | `deploy/deploy.sh:394` ROLLBACK_SCOPE=none | ✅ |
| 26 | `deploy/esa-mtls.sh:51`、`esa-realip-update.sh:142` → `mktemp` 替代固定 `/tmp` 路径（§3.35） | `deploy/esa-mtls.sh:162` NGINX_T_LOG="$(mktemp)"<br>`deploy/esa-realip-update.sh:246` NGINX_T_LOG="$(mktemp)" | ✅ |
| 27 | `deploy/systemd/cwq-backup.service`、`cwq-oss-backup.service` → 补 `TimeoutStartSec`（§3.36） | `deploy/systemd/cwq-backup.service:30` TimeoutStartSec=1800<br>`deploy/systemd/cwq-oss-backup.service:25` TimeoutStartSec=3600 | ✅ |
| 28 | `deploy/deploy.sh:706` → 改用 `db_backup.py --out`（§3.37） | `deploy/deploy.sh:478` log "         cd $APP/backend && .venv/bin/python scripts/db | ✅ |
| 29 | `deploy/deploy.sh:392,751` → 打印**可执行**的两步恢复流程（先清 schema 再灌）并警告会丢写入（§3.38） | `deploy/deploy.sh:473` print_db_restore_procedure() { | ✅ |
| 30 | `deploy/deploy.sh:673` → 新依赖装进独立 venv，或把 `.venv` 加进回退清单（§3.39） | `deploy/deploy.sh:395` VENV_SNAPSHOT_DIR=""     # 现网 venv 的快照目录（空 = 没快照，不需要回退） | ✅ |
| 31 | `deploy/deploy.sh:1448-1458` → mtime 判据改为 `-mtime "+${keep_days}"`（§3.40） | `deploy/deploy.sh:2000` if [ -n "$(find "$d" -maxdepth 0 -mtime "+${keep_days}" 2>/d | ✅ |
| 32 | `deploy/deploy.sh` 发布后体检 → 补「前端首页可达」断言；nginx 配置纳入渲染范围或在 `APP_DIR != /opt/cwq` 时 die（§3.41） | `deploy/deploy.sh:1477` log "❌ 前端首页取不到：${base}/"<br>`deploy/deploy.sh:359` die "APP_DIR=${APP} 不是 /opt/cwq —— 拒绝发布。 | ✅ |
| 33 | `deploy/esa-realip-update.sh:91-94` → 加前缀长度闸，与 `config.py:819` 口径一致（§3.43） | `deploy/esa-realip-update.sh:219` echo "✗ ${entry} 的前缀长度超过 ${max} —— 不是合法网段。不改任何东西。" >&2 | ✅ |
| 34 | `deploy/deploy.sh:263` → `this_ip` 为空时 die，不放行（§3.45） | `deploy/deploy.sh:282` if [[ -z "$this_ip" ]]; then | ✅ |
| 35 | `TRUSTED_PROXIES` 由告警升级为阻断（应用侧 `config.py` + 部署侧 `deploy.sh:1485-1499`）→ 关闭 §3.3 / §3.44 | `backend/app/core/config.py:1338` if not trusted_proxy_networks(settings.trusted_proxies): | ✅ |
| 36 | `MCP_ALLOWED_HOSTS` 在 `mcp_enabled=true` 时强制要求（§4.3） | `backend/app/core/config.py:1260` if settings.mcp_enabled and not mcp_allowed_host_list(settin<br>`backend/app/core/config.py:1268` "MCP_ENABLED=true 但 MCP_ALLOWED_HOSTS 为空 —— " | ✅ |
| 37 | 配 OSS 备份时强制加密密钥（与第 0 步第 6 项同批）（§3.8） | `backend/app/core/config.py:1302` if _oss_backup_configured(settings): | ✅ |
| 38 | `deploy/esa-mtls.sh` → 自动探测生效的 vhost 路径与 `access_log`（§3.42） | `deploy/esa-mtls.sh:110` done < <(grep -E '^[[:space:]]*access_log[[:space:]]+' "$con | ✅ |
| 39 | `api/client.js` → 网络/超时兜底提示 + 账簿三视图的 `load()` 加 try/catch（§3.25） | `frontend/src/api/client.js:89` ElMessage.error(error.code === 'ECONNABORTED'<br>`frontend/src/views/ledger/GeneralLedger.vue:255` } catch (e) {<br>`frontend/src/views/ledger/DetailLedger.vue:256` } catch (e) {<br>`frontend/src/views/ledger/BalanceSheet.vue:251` } catch (e) { | ✅ |
| 40 | `VoucherOrganize.vue` → 新增 `checkFailed` 态，体检失败时禁用 `RENUM`（§3.27） | `frontend/src/views/voucher/VoucherOrganize.vue:43` v-if="checkFailed" type="warning" :closable="false" show-ico | ✅ |
| 41 | `VoucherEdit.vue` → `autoSave()` 补 `failedSig` 短路；新建请求带幂等键（§3.28） | `frontend/src/views/voucher/VoucherEdit.vue:995` const failedSig = ref('')<br>`frontend/src/views/voucher/VoucherEdit.vue:1037` function newIdempotencyKey() { | ✅ |
| 42 | `VoucherEdit.vue` → `onBeforeUnmount` 的 flush 与新页 `load()` 串行化（§3.24） | `frontend/src/views/voucher/VoucherEdit.vue:2271` onBeforeUnmount(() => { | ✅ |
| 43 | `ReportView.vue` → 报表格不再用 `fmt`（或导出同步 quantize）（§3.26） | `frontend/src/views/report/ReportView.vue:59` 本期结转凭证借方合计 <b class="money">{{ reportNumber(n.amount) }}</b> | ✅ |
| 44 | `VoucherSplitDialog.vue`、`ReportView.vue` 的行/列操作、`SubjectTree.vue` → 补在途守卫（§3.29 / §3.30） | `frontend/src/components/VoucherSplitDialog.vue:87` <el-button type="primary" :disabled="!canConfirm \|\| submitti<br>`frontend/src/views/report/ReportView.vue:123` <el-button link size="small" :disabled="structureBusy" :titl<br>`frontend/src/views/subject/SubjectTree.vue:12` <el-button :disabled="!store.canWrite \|\| busy" @click="impor | ✅ |
| 45 | `VoucherEdit.vue` 的 `rowEls` 改用下标键或在 `removeRow` 里显式 `delete`（§3.31） | `frontend/src/views/voucher/VoucherEdit.vue:1070` const rowEls = new Map() | ✅ |
| 46 | `useCellSelection.js` → 组件卸载/路由切换时清 `current`，`tsvOf()` 校验 `isConnected`（§3.32） | `frontend/src/composables/useCellSelection.js:90` if (!current.rows[0].isConnected) return null<br>`frontend/src/composables/useCellSelection.js:25` let current = null | ✅ |
| 47 | `VoucherEdit.vue` → 把「换账套/换期间」的提示上移到 `MainLayout`（§3.33） | `frontend/src/stores/session.js:410` takePrevBookId() {<br>`frontend/src/layouts/MainLayout.vue:324` store.prevBookId = 0<br>`frontend/src/views/voucher/VoucherEdit.vue:1861` const prevBook = store.takePrevBookId() | ✅ |
| 48 | **首选**：插件后端代码移到独立进程/容器执行，只经 IPC 暴露受限 API（§3.1） | `backend/app/plugins/ipc/host.py:109` class WorkerHandle:<br>`backend/app/plugins/loader.py:148` mode = settings.plugin_isolation | ✅ |
| 49 | **次选（若必须进程内）**：为插件模块注入裁剪过的 `__builtins__`，并禁止包内非 `.py` 文件参与 `exec` | `backend/app/plugins/sandbox.py:144` def _build_restricted_builtins(allowed_roots) -> dict:<br>`backend/app/plugins/sandbox.py:567` if target.suffix != ".py":<br>`backend/app/plugins/sandbox.py:583` module.__dict__["__builtins__"] = ( | ✅ |
| 50 | 无论选哪条，都要修正 `scan_policy.py:3-10` 的模块说明，使它不再声称已闭环那起事故 | `backend/app/plugins/scan_policy.py:14` **这份清单单独用是不够的（不要再把它当闭环）** | ✅ |

---

## 3. 机制偏离：报告建议 ≠ 实际实现

这三处**保证等价**，但机制不同。写清楚，免得日后按报告原文去「修」一遍。

### 3.1 §7 第 24 项：失败计数用行锁，不是 `UPDATE … RETURNING`

- **报告建议**（§7 第 24 项）：`UPDATE sys_user SET failed_logins = failed_logins + 1, … RETURNING …`
- **实际实现**：`SELECT … FOR UPDATE` + `populate_existing=True`，在锁内改写。
- **为什么等价**：两个并发请求里，后到的那个**拿不到锁**，直到前一个提交；
  拿到锁后重新读到的 `failed_logins` 已经是**加过 1 的新值** —— 不存在丢失更新。
- **为什么不用 `UPDATE … RETURNING`**：那条路径要把 `lock_level` 的递进规则
  （第几轮该锁多久、每轮把计数清零）也塞进一个 SQL 表达式，
  而递进规则在 Python 里、且要读回**改写后**的值才能决定下一步 —— 拆成两个语句更不容易写错。
- **守着它的用例**：`backend/tests/test_audit_fix_login_failure_counter.py`（并发实测，不是读码推断）

### 3.1.1 §7 第 48 项的**默认档**要说清楚

插件隔离有四档：`off`（默认）/ `shadow` / `on`，外加部署侧的容器/systemd 形态（未做）。

- **默认是 `off`** —— 现网行为不变。切过去会改变插件的**执行位置**
  （多一次 IPC、同插件并发被串行化），不该由一次升级悄悄带上线。
- `on` 才是报告 §7 第 48 项的「首选」形态：插件后端代码在**独立 worker 进程**里跑，
  宿主只经 Unix socket 派活，插件源码零改动。
- 所以「§7 第 48 项未实施」是**不准确**的：首选路径已实现且可切换，
  只是**默认没打开**。上生产时把 `PLUGIN_ISOLATION=on` 写进 `.env` 即可切过去
  （`.env.example` 里已登记，默认值 `off`）。
- `off` 这一档**不是没有防线** —— 静态扫描 + 运行期能力裁剪仍然在
  （就是 §3.1 那一层，见 §5.4 的实测）。
- **但默认是静默的这件事本轮修掉了**：`loader.startup()` 现在会按档位播报
  「插件代码跑在哪、这一档在防什么、要换档改哪个环境变量」，
  且默认档（`off`）用 **WARNING** 级别 —— 唯一的「插件还在宿主进程里跑」的档
  不该被 INFO 轻轻带过。已固化成用例
  （`test_启动时说清楚插件代码跑在哪`，3 组参数化；把 WARNING 降级即变红）。
- **要不要把默认翻成 `on`，是运维的决定**，不在这份记录里代劳：
  翻默认会改变插件的执行位置与并发特性，不该由一次升级悄悄带上线。

### 3.2 §7 第 48 项：走「独立进程 + 只经 IPC」，不是容器形态

- **报告给了首选与次选**（首选 = 独立进程/容器 + 只经 IPC；次选 = 裁剪 `__builtins__`）。
- **实际实现**：**首选已落地**（`PLUGIN_ISOLATION=on` 时插件后端代码在独立 worker 进程里跑，
  宿主只经 Unix socket 派活）；**次选那两道也顺手补上了**
  （`sandbox.py:144` 裁剪 `__builtins__`、`:567` 拒非 `.py` 参与 `exec`）——
  即便将来退回进程内，沙箱也不是裸的。
- **边界加固（systemd/容器形态）没做**：它要求部署侧把 worker 放进独立 unit
  （宿主就不能自己 spawn 它），本机是 macOS，**写上去也验不了**。
  配方留在 `output/PLUGIN-ISOLATION-PLAN-2026-10-25.md` §8（A/B/C 三档）。

### 3.3 §7 第 30/32 项：部署侧选的是「更保守」的一档

- 第 30 项（依赖）：选了「快照 `.venv`」而不是「新依赖装独立 venv」—— 后者要动安装流程，
  前者把 `.venv` 进回退清单即可达到同一目标（回退时连依赖一起回退）。
- 第 32 项（nginx）：选了「`APP_DIR != /opt/cwq` 即 die」这一档 —— 报告允许
  「nginx 配置纳入渲染范围 **或** 走这一档」，选后者是因为它不依赖生产机上
  nginx 的真实布局（本机无从核实，见审计报告 §6 F 轨）。

### 3.4 §7 第 14 项：护栏**并进**既有的 entry 触发器，不是新建一个

- **报告建议**（§7 第 14 项）：`app/core/db_init.sql` → `fn_posted_voucher_guard`
  增加「entry 侧 DELETE/INSERT 也要看凭证状态」的**触发器**。
- **实际实现**：判据落在 `fn_entry_lock_guard()` 的**第②段**（`db_init.sql:215`），
  挂在**既有的** `trg_entry_lock_guard BEFORE INSERT OR UPDATE OR DELETE ON entry`
  上（`:224`）—— **没有新建触发器**。
- **为什么等价**：护栏照样钉在 `entry` 上、照样在库层生效。实测（新装库，
  绕过应用层直接改库）：已记账凭证的分录 **UPDATE / INSERT / DELETE 三条路全被拒**，
  报「凭证 记0001 已记账，不能修改或删除它的分录；如需改动请先反记账」。
- **为什么不新建一个**（理由写在 `db_init.sql` 里，不是事后找补）：
  迁移 0032（科目表按年度拆开）搬分录时会**临时关掉** `trg_entry_lock_guard`
  —— 它要改的正是**已记账**的历史凭证的分录（改 subject_id，金额一个没动）。
  两道检查放在**同一个函数**里，迁移关掉它时一起让路；
  分成两个触发器，迁移就只关了①、②照样拦 ——
  **迁移会在生产库上当场失败**（跨年账套的次年凭证全是已记账的）。
- **守着它的用例**：`test_audit_fix_voucher_concurrency.py`
  （三条：改金额 / 插分录 / 删分录，各一条）

### 3.5 §7 第 14 项的**恢复路径**风险：护栏与备份恢复的相撞

§3.4 那条护栏是**本轮新加**的，而**恢复就是往库里插 entry 行** ——
所以必须验一次「它会不会在恢复时把整个恢复打死」。实测结论：

| 场景 | 结果 |
|---|---|
| 正常恢复（先清 schema，再灌 plain dump） | **成功**（退出码 0），已记账凭证连分录一起回来，恢复后护栏仍在且仍拦得住 |
| 往**已建好 schema、触发器已武装**的库里灌数据 | **失败**（`fn_entry_sync` 报「凭证不存在」） |

**为什么第一种情况能成**：plain 格式的 pg_dump 把**触发器放在数据段之后**才装
（实测这份 dump：`COPY public.entry` 在第 2100 行、`COPY public.voucher` 在第 2716 行，
而 `CREATE TRIGGER trg_entry_lock_guard` 在**第 3753 行**）——
**灌数据的时候护栏根本还没装上**。这不是运气，是 pg_dump 的格式约定。

**所以审计报告 §3.38（对应 §7 路线图第 29 项）那条「先清 schema 再灌」的理由
要比原来写的更重**：
不只是「`CREATE TABLE already exists`」，更是「**触发器在位时**，
dump 里 entry 排在 voucher 前面的那个数据顺序会撞上 entry 侧的一整排触发器」
（实测先撞上的是 `fn_entry_sync` —— 它要按 voucher_id 回查凭证，
凭证行还没灌进来，直接报「凭证不存在」；已记账护栏紧随其后）。
哪天有人图省事改成 `--data-only` 灌进现成的库，这条退路会在**真出事的那天**才断。

现在这两件事都被用例钉住（`test_db_backup_restore.py`）：
① 已记账凭证能恢复、且恢复后护栏还在；
② dump 里 entry 的触发器**必须在数据段之后**（直接断言 dump 的先后顺序，
不靠人记得）。

### 3.6 恢复路径的**另一半**：恢复一份**旧备份**，护栏会跟着一起消失

§3.5 验的是「护栏会不会把恢复挡死」。这一条是**反方向**，而且更危险 ——
**恢复成功了，护栏没了，而恢复命令返回 0（成功）、日志里一句提示都没有。**

| 步骤 | 事实 |
|---|---|
| 护栏是哪来的 | `uq_entry_voucher_line_no` 唯一约束、「已记账凭证的分录不许增删改」的触发器 —— 都是**迁移**带上去的（`migrations/versions/0038_entry_voucher_line_unique.py`） |
| dump 里有什么 | 只有「备份那一刻库里有什么」。护栏是那次迁移之后才有的 ⇒ **迁移之前的备份里没有护栏** |
| 恢复做了什么 | `db_backup.py --restore` 只跑 psql，**不重跑迁移链**（`deploy/deploy.sh` 印出来的两步恢复流程里也没有这一步） |
| 结果 | 恢复出来的库**没有**唯一约束、**没有**那段「已记账不许动」的触发器；而 `psql` 退出码 0 ⇒ 运维看到的是「恢复成功」 |

**后果不是「恢复失败」，是「恢复出来的库看起来是好的」**（第五轮独立复核实测）：

```
[D] 恢复回来的 fn_entry_lock_guard 里还有「已记账不许动」那段吗： False
[D] 改已记账凭证的分录（只改一边） -> 被拒：CheckViolation 借贷不平衡
[D] 同时改借贷两边的金额 -> **成功**（已记账凭证的金额被静默改写），零报错
[D] 插重复行号 -> **入库成功**，重复组： [(1, 2), (2, 2)]
```

即审计 §3.15 的失败场景在恢复后的库上**原样复现**，而借贷仍然平、所有自检全绿。

**修法**：把「schema 在 head」并进「恢复成功」的判据。
`backend/scripts/db_backup.py` 新增 `_align_schema()`：psql 成功后读库里的
`alembic_version`，落后就补跑迁移（`app/core/schema_version.upgrade_to_head`），
再校验一次；对不上就返回**独立的退出码** `EXIT_SCHEMA_NOT_ALIGNED = 4`。

| 退出码 | 含义 | 要做的事 |
|---|---|---|
| 0 | 数据回来了 **且** schema 在 head | 正常 |
| psql 的非 0 | 恢复失败，库里什么都没有 | 重跑恢复 |
| **4** | **数据回来了，但 schema 没对齐 —— 护栏没在位** | 补迁移 + 查数据冲突 |
| 3 | 输入不是真的 dump，**拒绝执行** | 换备份文件 |

**为什么退出码要分开**：混成一个码的后果是发布流程把「schema 没对齐」当成
「恢复失败」，于是反复重跑恢复，而真正要做的事（补迁移、查数据冲突）没人做。

**用例**（`backend/tests/test_db_backup_restore.py`）：
① `test_恢复旧备份后护栏必须还在` —— 造一个**真的停在 `0037`**（护栏那条迁移
之前）的库，dump 出来，恢复到另一个库，断言恢复后 `alembic_version == head`
**且** `uq_entry_voucher_line_no` 在；
② `test_schema版本认不出来时不能算恢复成功` —— 退出码必须是 4，不能是 0。

**反向验证**：把 `restore()` 里的 `return _align_schema(url)` 换成 `return 0`
（**保留那段解释它的注释**）→ 用例 ① 变红，报
`assert '0037_log_retention_indexes' == '0039_voucher_idempotency'` ——
正是「护栏没装上」这件事本身。

---

## 4. 明确没有做的部分

| 项 | 为什么 | 怎么补 |
|---|---|---|
| §7 第 11 项：**存量数据体检**的**执行** | 「查」已经落成只读命令（`backend/scripts/data_health_check.py`），但**体检本身要在真实库上人工跑一次**；而「哪一条重复该留、金额该怎么修」只有懂业务的人能决定 —— 代码替不了 | 上线前在**生产机上、用生产机的 .env** 跑 `backend/scripts/data_health_check.py`（退出码 2 = 不能上线，见 §1.1）。迁移 `0038` 的前置检查是**兜底**（有重复行号时大声失败），两边**共用同一份判据**（`app/accounting/data_health.py`，`test_audit_fix_data_health_check.py` 钉住） |
| §7 第 48 项的 **systemd / 容器形态** | 要部署侧把 worker 放进独立 unit，本机 macOS **无法验证**，写上去等于没写 | `output/PLUGIN-ISOLATION-PLAN-2026-10-25.md` §8 给了 A/B/C 三档配方与具体 unit 指令 |
| CI 那个「测试文件一个都不能少被发现」的闸，**抓不到「75 个文件里漏了 1 个」** | 它原来在比**量纲不对**的东西（用例数 ≥ 文件数 ⇒ 恒真）。要改成真数文件数，得从 node 的输出里数 —— 而本仓有 **23 个测试文件往 stdout 打自己的分隔行**，那些行会插进 node 的 TAP/spec 流，把文件级结果行冲掉（实测磁盘 75 个、输出里只剩 52 个），按那个数做闸会变成误报 | 本轮把它拆成两件各用可靠来源的事：① 退出码 + 用例条数（抓住「runner 整个没跑起来」，与 P1-27 那个形态直接对应）；② **glob 覆盖**（查磁盘上有没有 `*.test.mjs` 落在 `tests/` 之外 —— 那种文件 runner 扫不到，且**不依赖任何输出解析**）。**剩下的那个缺口在 CI 的注释里明说了**，不让注释替代码吹牛。要彻底补上，得先让那 23 个测试文件不往 stdout 打自定义分隔行 —— 那是**改测试文件**而不是改闸，本轮**没做** |

---

## 5. 本轮补的东西

### 5.1 `SubjectTree.vue` 的在途守卫（§7 第 44 项 —— 最后一个缺口）

报告第 44 项点名的三个文件里，`VoucherSplitDialog` 与 `ReportView` 上一轮已补，
`SubjectTree.vue` 一直空着 —— 双击「删除」会并发两个 `DELETE`，
后端 `delete_subject` **不是幂等的**（先 `db.get` 再查引用，删过就找不到了），
第二个拿到 404「科目不存在」，用户刚看到「已删除」转眼又一条红字。

- `frontend/src/views/subject/SubjectTree.vue`：加 `busy` ref + `withBusy()` 早退；
  删除按钮 `:disabled="busy"`、`:loading="removingId === row.id"`（**只给正在删的那一行**转圈）；
  工具栏「导入并覆盖 / 新增科目」一起按住（导入是**整年覆盖**，跟正在删的科目撞上会残缺）。
- 顺手把 `load()` 换成 `useLatestLoad('subject/tree')`：页内重载（编辑保存 / 导入 / 删除）
  三条路可能在途，慢的那一趟晚到会把**已删掉的科目**又画回树上。
  按 `useLatestLoad.js` 文件头自己写的那条规矩（「页内动作必须自己接守卫」）办。

### 5.2 五条前端修复此前**没有用例钉住**

`§3.27` / `§3.30` / `§3.32` 上一轮只改了代码，没留回归 —— 现在补上
（`frontend/tests/audit_fix_frontend.test.mjs`，18 条），
另外把 `§3.24` / `§3.25` 也一并钉住。

**抠取器先有已知答案的自测**：从源码里抠实现来跑的探针，抠取器自己出错的话，
后面所有「绿」都是假绿 —— 所以第一组用例先验抠取器本身
（字符串里的花括号、注释里的括号都不能带偏配平）。

### 5.3 反向验证（把防线拆掉，确认用例会红）

绿灯本身不作数。八处注入逐一验证：

| 注入 | 结果 |
|---|---|
| 拆掉 `withBusy` 的 `if (busy.value) return` | 红：`双击发了 2 次 DELETE` |
| 拆掉 `load()` 的 `if (isStale(d)) return` | 红：`过期的那趟把树覆盖了` |
| 拆掉工具栏按钮的 `:disabled="!store.canWrite \|\| busy"` | 红：绑定不见了 |
| 拆掉 `loadCheck` 的 `checkFailed.value = true` | 红：`体检失败了却没记 checkFailed` |
| 拆掉 `withStructureBusy` 的早退 | 红：`双击发了 2 次 POST /reports/5/rows` |
| 拆掉 `liveRows` 的 `current.rows[0].isConnected` | 红：`表还在、行被换掉了 → 同样拒绝复制` |
| 拆掉 `liveRows` 的 `current.table.isConnected` | 红：`整张表已经脱离文档 → 拒绝复制` |
| 拆掉 `VoucherEdit` 的 `setPendingFlush(flushIfDirty())` / 先 load 再等 | 红：`旧组件把 flush 挂出去，新组件**先等它**再 load()` |

### 5.4 复审轮次：四条主张按**当前代码**重新取证（全部推翻），并补上真正缺的用例

复审又提了四条关于插件沙箱的主张，引的行号（`sandbox.py:304-308` / `:137`、
`security.py:387`/`:438`、`test_plugin_sandbox.py:182-186`）**属于旧快照**
—— 当前 `sandbox.py` 有 600+ 行、`test_plugin_sandbox.py` 有 1100+ 行。
按主张的**原话**逐条实测：

| 主张 | 实测（当前代码） | 判定 |
|---|---|---|
| `_sanitize(os) is os → True`（白名单外的子模块原样返回） | 抛 `ImportError`：「插件不允许使用模块：os」（`sandbox.py:435`） | **推翻** |
| `__builtins__['__import__'].__globals__['builtins'].__import__('os')` 拿到真 os | 装载期即拒：「插件代码使用了被禁用的双下划线反射」 | **推翻** |
| 「允许模块的命名空间把危险子模块带进来」**没有任何用例覆盖** | `test_plugin_sandbox.py:616` 有 **20 组参数化**运行期用例（`dataclasses.sys` / `contextlib.os` / `uuid.os` / `json.codecs` / `typing.types` …），`:833` 有静态扫描用例 | **推翻** |
| §7 第 48 项（插件独立进程）未实施 | `plugins/ipc/host.py:109` `WorkerHandle` + `loader.py:148` 按 `settings.plugin_isolation` 分流；`PLUGIN_ISOLATION=on` 下 446 条插件用例全绿 | **推翻** |

**但对抗性复现确实挖出两个真缺口**（不是行号问题，是覆盖问题）：

1. **动态拼名形态没有用例钉住**。旧用例只测**字面量形态**
   （`dataclasses.sys`、`operator.attrgetter("__globals__")`）。
   而静态扫描**按形态判定** —— 形态本身就是它的盲区：
   - `operator.attrgetter("sys")(dataclasses)` —— 属性名里**没有双下划线**；
   - `getattr(dataclasses, "s" + "ys")` —— 属性名是**拼出来的**。

   这两条在扫描期都判不到「逃逸模块属性」→ **运行期那一层是唯一防线**。
   实测运行期全部挡住（`attrgetter 对插件不可用` / `name 'getattr' is not defined`），
   现在已固化成用例（`test_动态拼名形态的走私拿不到宿主模块`，6 组参数化）。

2. **「`_sanitize` 必须失败即闭」这条不变量没有用例**。
   实测把 `_sanitize` 改回旧行为（`return module`），**整个测试文件 101 条照样全绿**。
   已补 `test_沙箱的模块清洗对白名单外的模块失败即闭`，
   以及「白名单内的模块拿到的必须是**代理**不是原模块」。

**反向验证（这三条注入都要确认「真的拆掉了防线」才算数）**：

| 注入 | 防线确认 | 结果 |
|---|---|---|
| `_sanitize` 还原成 `return module` | 注入后模块仍可导入 ✓ | **1 条红** |
| 运行期不再拒 `operator.attrgetter` | 注入后模块仍可导入 ✓ | **4 条红** |
| `getattr` 真正变可用（内建白名单**与**黑名单一起拆） | 两处一起拆后模块才导入成功 ✓ | **3 条红** |

> 第三条踩了一个坑：只把 `getattr` 塞进内建白名单时，
> `sandbox.py:187` 的「内建白名单与黑名单冲突」检查**先一步**把模块拦下了
> —— 那次注入**根本没生效**（模块都没加载），不算一次有效验证。
> 把两道闸一起拆掉之后才是真测。**注入没生效的「绿」不能算数。**
> 同理，拆「模块代理拒白名单外模块属性」时第一版改的是**第二道**闸
> （符号守卫），第一道（模块属性清洗）还在 → 照样全绿；
> 改对地方（还原成旧行为「原样返回」）才是 **28 条红**。

**四条主张现在是一条仓库内的命令**，不用人读代码、不用翻聊天记录：

```bash
python3 tools/audit_plugin_sandbox_recheck.py            # 四条主张的探针（快）
python3 tools/audit_plugin_sandbox_recheck.py --mutate   # 加跑变异证明（慢）
```

退出码 0 = 四条主张在当前代码上**都不成立**且**都被用例钉住**。

| 主张 | 探针 | 变异证明（改回旧行为） |
|---|---|---|
| A `_sanitize` 原样返回 | 抛 ImportError | **1 条红** |
| B `__import__.__globals__` 链 | 装载期即拒 | **2 条红** |
| C 命名空间走私无用例覆盖 | 覆盖存在 | **28 条红** |
| D §7 第 48 项未实施 | 已实现（默认 `off`） | **6 条红** |

### 5.5 逃生口是**明示登记**的取舍，不是漏洞

§3.8 / §3.44 的收紧各带一个显式豁免开关
（`BACKUP_ENCRYPTION_KEY_WARN_ONLY=true`、`CWQ_DEPLOY_ALLOW_EMPTY_TRUSTED_PROXIES=1`）。
**默认是阻断**，条目算修好；但生产姿态目标确实会被这些口子削弱 —— 这是明示的取舍。

它们**全部登记在册**（`backend/app/core/escape_hatches.py`）：

| 一侧 | 逃生口 |
|---|---|
| 应用侧 | `TRUSTED_PROXIES_WARN_ONLY`、`MCP_ALLOWED_HOSTS_WARN_ONLY`、`BACKUP_ENCRYPTION_KEY_WARN_ONLY` |
| 部署侧 | `CWQ_ALLOW_ANY_HOST`、`CWQ_DEPLOY_ALLOW_EMPTY_TRUSTED_PROXIES`、`CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR` |

`tests/test_escape_hatches.py` 守着「登记的那张表 = 实际生效的那批」——
**新增一个逃生口而不登记，这条用例立刻红。**

### 5.6 §7 第 11 项：从「人工步骤」变成**一条只读命令**

§7 的 50 项里，第 11 项是唯一一项**「怎么做」也悬着**的：报告只写了
「先做一次存量数据体检：查 `entry` 是否已存在同一 `voucher_id` 下 `line_no`
重复的行」，既没给那条查询，也没说查出来长什么样。验收记录原来写的是
「上线前先在真实库跑一次查询」—— 那等于把 SQL 临场交给运维写。

**临场写的 SQL 与迁移前置检查用的那条，不是同一口径的概率很高**，
而最坏的一种结果是：体检说干净，迁移撞上重复行炸了。

这一轮把它落成：

| 落点 | 职责 |
|---|---|
| `backend/app/accounting/data_health.py` | **判据的唯一实现**：`duplicate_line_no()` / `describe_duplicate()` |
| `backend/scripts/data_health_check.py` | **只读**的体检命令，退出码即结论 |
| `backend/migrations/versions/0038_…py` | 前置检查改为**引用**上面那份（不再自带一份查询） |
| `backend/app/accounting/organize.py` | 新增 `rows_by_period()`：一次查多个期间的体检行 |

**判据只有一份（§4.5）。** 「同一张凭证里行号重复」这个量出现在三处
（迁移前置检查、体检脚本、用例），三处各写一份就会分叉，而分叉出来的那两份
会**安静地**给出不同答案。这里出错的后果不是报错，是**账上的数被记了两遍
却没有任何一处提示**（§3.15：借贷仍平、试算平、科目余额平、总账平、
「整理就绪」体检也过 —— 全绿，但数字是错的）。所以三处都调同一份。

**体检用的是「整理凭证」页面上那个判据**（`organize.check_period`），
不是另写一份 —— 所以上线前体检报的断号，与页面上点「整理凭证」看到的断号，
必然是同一批。

**它只读。** 体检脚本一条写操作都没有，跑完之后库里逐字节不变 ——
这个性质有用：它证明体检本身没有污染被体检的对象。

| 退出码 | 含义 | 动作 |
|---|---|---|
| 0 | 体检通过 | 可以上线 |
| 2 | **阻断**：同一 `voucher_id` 下 `line_no` 重复 | **不能上线**（迁移 `0038` 也会拒绝加约束） |
| 3 | 提示：「整理就绪」体检不干净 | 上线前人工确认；这些期间不能记账、不能结账 |
| 1 | 体检**自己**没跑成 | 修连接串/脚本 —— **不是**「库有问题」 |

最后一行值得单说：把「体检没跑成」与「体检出结果了」混成一个码，
发布流程就会把「体检脚本自己坏了」当成「库里有问题」，然后去**清理生产数据**。

**它确实查得出东西**（不是摆设）：对本机开发库跑一次（只读），
结果是「无重复行号」，但 **25 个期间「整理就绪」不干净**（多为时序/断号）→ 退出码 3。

**反向验证**（三处注入，逐一确认注入真的生效了才看红不红）：

| 注入 | 结果 |
|---|---|
| 拆掉重复判定（`HAVING count(*) > 1` → `> 99`） | 红：4 条用例挂（查不出、点不出名、拦不住） |
| 迁移不再引用 app 侧判据（改回自带一份查询） | 红：`迁移的前置检查没有走 app.accounting.data_health.duplicate_line_no` |
| 体检脚本偷偷写库（`_check_duplicates` 里删一行分录） | 红：`体检改动了库：(2, 200.00, 1, 12) → (0, 0, 1, 12)` |

顺带把 §7 终审脚本里第 11 项的判据**收紧**了（原来只查迁移里有没有那句话，
现在要求「可跑命令 + 唯一实现 + 迁移引用它」三处**都在**）—— 并验证过它有牙齿：
把体检脚本挪走，第 11 项立刻 FAIL（51/51 → 50/51）。

### 5.7 独立复核挖出来的两处：**护栏与恢复路径的相撞**，和一个**说谎的注释**

这一节的东西**不是**从审计报告里来的，是自己复查时挖出来的。

**① 迁移 0038 的 docstring 写了一个不存在的触发器名。**
它说「重放 `db_init.sql`，把新增的 `trg_entry_posted_voucher_guard` 装到库里」——
**库里没有这个触发器**（实测列了 `entry` / `voucher` 上的全部触发器）。
真实机制是：判据在 `fn_entry_lock_guard()` 的第②段，挂在**既有的**
`trg_entry_lock_guard` 上。**护栏行为一直是对的**（UPDATE/INSERT/DELETE 三条路
在已记账凭证上全被拒），错的只是那句注释 —— 但注释说谎的代价是
「日后有人照着这个名字去查，查不到，然后以为护栏没装」。
已改（并把「为什么不新建一个触发器」的理由一并写进去，见 §3.4）。

**② 新加的护栏会不会把「恢复」打死？** —— 这条没人问过，但它很要命：
**恢复就是往库里插 entry 行**，而 §3.15 的护栏正挂在 `entry` 的 INSERT 上。
系统唯一的退路断在需要它的那天，是最坏的结局。

实测结论（细节见 §3.5）：正常恢复（先清 schema 再灌）**成功**，
因为 plain 格式的 pg_dump 把**触发器放在数据段之后**才装 ——
灌数据时护栏还没装上。往**触发器已武装**的库里灌数据则**失败**。

补了两条用例钉住它（`test_db_backup_restore.py`）：

| 用例 | 钉住什么 |
|---|---|
| `test_roundtrip_restores_posted_voucher_and_keeps_the_guard` | 已记账凭证连分录一起能恢复；**恢复之后护栏仍然拦得住**（恢复出来的是**有护栏的库**，不是看起来好的空壳） |
| `test_触发器在数据段之后才装_所以恢复时护栏不会挡` | 直接断言 dump 里 entry 的触发器**在数据段之后** —— 不靠人记得这条格式约定 |

第二条的判据**验过有牙齿**：把 dump 里的触发器段挪到数据段之前，
同一套判据立刻报红。

**顺带把 §7 终审脚本里第 14 项的判据收紧了**：原来只查
「db_init.sql 里出现过 `fn_posted_voucher_guard` 这个词」—— 那命中的是**注释**。
现在要求三处**都在**（守卫函数在 + 真的挂在 entry 的 BEFORE INSERT/UPDATE/DELETE 上
+ 有那句抛错），并验证过有牙齿：把抛错文案改掉，第 14 项立刻 FAIL（51/51 → 50/51）。

**另外**：`§7 终审脚本` 的 `ev(a) + ev(b)` 语义是「**任一**命中就算过」，
不是「都要在」—— 这次一并加了 `ev_all()`（缺任一条即 FAIL，并当场打出缺哪一条），
第 11 项与第 14 项都用它。

### 5.8 第二轮独立复核：判 FAIL，三条确认问题 —— **已全部修完**

第二轮复核代理读的是**当前工作树**，判 **FAIL**，给了三条**确认**问题
（不是「疑似」）。三条都成立，都已修完，并各自配了反向验证。

---

#### A-1【中】§3.8 的启动闸门只看 **env 层**，界面上配 OSS 时不响

审计 §3.8 要求「配了 OSS 备份就必须配备份加密密钥」。修好的那道闸在
`app/core/config.py::validate_production_settings` —— 它**只看 env 层**的
四个 OSS 参数，因为**启动期没有库连接**，看不到界面上填的那一套。

而备份管线真正用的是**生效配置**（`app/backup/config.py::oss_config`：
库里优先、env 兜底）。于是有一条完全走得通的路：

```
运维在「系统管理 → 数据安全」页面上填好 OSS
  → 启动闸门不响（env 层没配）
  → 每晚的备份以明文 gzip 上传到对象存储
```

这条路的特别之处：**它是产品自己指给运维的那条路** ——
`require_ready` 的报错原文就是「请到「系统管理 → 数据安全」填写后重试」。
所以它不是边角情况，是主路。

**修法**：同一道闸搬到**运行期**、按**生效配置**判 ——
`app/backup/config.py::require_encrypted_ready`。一道实现同时覆盖 env 与
界面两条来源；启动闸门**保留**（它挡的是「带着一个必然明文上传的配置启动」，
而且能拦住连不上库就启动的场景）。两道闸覆盖两种来源，谁都绕不过去。

**判据只用生产者本人**（`app/backup/export.py::encryption_key`）：
它返回 None = 没配（不加密），它抛 `ExportRefused` = 配了但不合格。
不另写一份「算不算配了」的判据 —— 启动闸门说「合格」而导出说「拒绝」，
那道闸就是骗人的（而且是最坏的那种骗法：让人以为备份是加密的）。

| 落点 | 改了什么 |
|---|---|
| `app/backup/config.py` | 新增 `EncryptionKeyMissing` / `encryption_ready()` / `require_encrypted_ready()` |
| `app/backup/service.py` | `run_backup` 改用 `require_encrypted_ready` —— 它是 OSS 上传的**唯一收口点**（脚本与界面都汇到它） |
| `app/api/system.py` | `/backup/run` 缺密钥 → **400**；`/backup/test` 仍只验连通（不拦），但响应新增 `encryption_ready`，消息里说清「备份跑不起来」 |
| `backend/scripts/backup_to_oss.py` | 撞上这道闸 → **退出码 2**（原来会落成裸 traceback → exit 1「脚本崩了」，而真相是「配置没配全」）；`--dry-run` 会提示密钥没配 |
| `app/core/config.py` | 改掉两处**说谎的注释**（原文声称运行期 `export.encryption_key()` 会兜底，实际不会） |

**「明文上传」从此只有一个入口**：逃生口 `BACKUP_ENCRYPTION_KEY_WARN_ONLY=true`
（已登记在 `app/core/escape_hatches.py`）。逃生口下**真的**是明文 ——
这条被用例钉住，于是「明文」有一个**明确的、可数出来的**入口，
而不是「配漏了就会发生」的意外。

**用例**：`backend/tests/test_audit_fix_backup_encryption_runtime.py`（16 条）。
其中一条专门把**洞本身**钉住（`test_启动闸门看不到界面上配的OSS_这正是那个洞`）：
它断言启动闸门在「界面上配 OSS」时**不响** —— 防止有人用「删掉启动闸门」
来"修"这个问题。

**反向验证**（三处变异，每处都有**自证探针**先确认「注入真的拆掉了那道闸」）：

| 变异 | 自证 | 变红 |
|---|---|---|
| M1 拆掉 `require_encrypted_ready` 的拒绝分支 | 探针：闸门已拆掉 | **6 条** |
| M2 `encryption_ready()` 恒返回真 | 探针：判定已失真 | **2 条** |
| M3 `run_backup` 不再过那道闸 | 探针：`run_backup` 已放行 | **2 条** |

> 第一次做 M1 时，探针打印的那行**没被 pytest 捕获**（`-q` 且用例通过时
> stdout 不落盘），结果把「注入生效」误判成 False。**注入自证不能靠
> 「看用例红不红」** —— 必须有一句能独立判断「防线真的被拆掉了」的输出，
> 并且那句话本身要能被打出来。

---

#### A-2【低】终审脚本无条件 `sys.exit(0)`，当不了闸门

`tools/audit_roadmap_check.py` 结尾是 `sys.exit(0)` —— 于是**50 项全 FAIL
时它照样返回 0**，挂进 CI 也是绿的。脚本的价值一半在它打印的那张表，
一半在它**能不能让流水线变红** —— 后一半没做，前面那一半就成了
「一张需要人记得去看的表」。

**修法**：退出码改成 `1 if failed else 0`；并把它**接进 CI**
（`.github/workflows/ci.yml` 的 backend job 末尾一步，纯标准库脚本，
不需要数据库与依赖）。

**验证**：把第 51 项的判据改成不可能命中 → 退出码 **1**；还原 → 退出码 **0**。

---

#### A-3【低】两处注释说谎：`VoucherIn` **有** `version` 字段，409 这层是通电的

`app/api/vouchers.py` 与 `app/accounting/vouchers.py` 里各有一段注释，
都写着「请求体模型 `VoucherIn` 里没有 version 字段」「`data.get("version")`
恒为 None」「今天这条是空转的」。

**全是假的**：

* `app/api/schemas.py:223` **有** `version: Optional[str]`；
* 前端**真的在带** —— `frontend/src/views/voucher/VoucherEdit.vue` 保存时送
  `version: voucherVersion.value || null`，并且专门处理 409
  （`e?.response?.status === 409` 时提示「已被他人改动，请刷新」）；
* 后端**早就通电** —— `accounting/vouchers.py::update_voucher` 比对不一致就抛
  `VoucherVersionConflict`，路由层翻成 **409**；用例钉在
  `tests/test_audit_fix_voucher_version.py`（`test_带着过期的令牌回409`）。

**为什么这件事不能只当注释问题。** 一段注释说「这条是空转的」，
下一个人读它就会以为「409 那层没人用，不用测」—— 而实际上前端**正靠它**
提示用户刷新。一段与实现相反的注释，会把「已经做好的保护」说成
「没做」，于是有人会去补一个重复的实现。

两处都改成如实描述，并补上「行锁与乐观锁各管什么」：
**行锁**管写路径上的串行化，**乐观锁**管「人看到的那一版是不是还在」。

---

#### 顺带修掉的：终审判据自己的**假牙**

复核方还提到「约 15/50 项验收记录给的行号指向注释/import/docstring
而非实现」。实测：**63 条判据规格里有 17 条的首条命中是注释或 import**。
于是本轮把终审工具本身修了一遍：

| 问题 | 修法 |
|---|---|
| `ev(a) + ev(b)` 的语义是「**任一**命中就算过」，而十几项的判据是「这几处**都要**在」 | 12 处改用 `ev_all()`（第 3/12/26/27/39/41/44/46/47/48/49/51 项）。实测这 12 项在「都要在」的口径下**全部仍然有证据** —— 收紧不花钱，白赚 12 条真判据 |
| 第 43 项的判据**是反的**：`fmt(` 还在时它塞一条「!! fmt( 还在」的证据，而证据非空 = PASS ⇒ **判据越严，报告反而越亮** | 改成：`fmt(` 在 ⇒ 证据为空 ⇒ FAIL，并把原因写进新增的 `note` 参数 |
| 证据行落在注释上，读起来像「这只是一句说明」 | `ev()` 改为**优先报代码行**（注释/import 排后）；`ev_all()` 原先绕过 `ev()`，一并修掉。修后 63 条里只剩 2 条落在 import/字符串上，而那 2 条**判据要的正是那个 import/那句文案** |

**反向验证（两轮，越到后面越狠）**：

| 轮次 | 怎么拆 | 结果 |
|---|---|---|
| A | 把判据的**全部命中**（含注释）从源文件里抹掉 | 19/19 项变红 |
| B | **只抹代码行、把注释留着** | 19/19 项变红 |

B 轮是 A 轮的加强版：A 轮里「代码和注释一起被抹掉」也算判据被破坏，
所以它证明不了「判据不会只看注释」。B 轮才是真正的问法 ——
**把实现删了、只留那句「必须用 mktemp」的注释，这一项还绿不绿？**

B 轮抓出两处**真·假牙**（A 轮抓不出来的）：

| 项 | 假在哪 | 收紧成 |
|---|---|---|
| 26 | 判据取 `mktemp` 这个**词**，而两个脚本里都有一句注释「nginx -t 的输出落盘：**必须用 mktemp**」在讲这件事 → 把 `NGINX_T_LOG="$(mktemp)"` 那行删掉、只留注释，**照样 PASS** | `NGINX_T_LOG="\$\(mktemp\)"` —— **那行赋值本身** |
| 27 | 判据取 `TimeoutStartSec` 这个**词**，而 unit 文件里有一段注释「TimeoutStartSec：**必须显式给**」在讲这件事 → 把 `TimeoutStartSec=1800` 那行删掉、只留注释，**照样 PASS**。而 systemd **只认指令、不认注释** | `^TimeoutStartSec=\d+` —— **那一条指令本身** |

收紧后两轮都是 19/19，文件全部还原干净。

> **这轮暴露的方法论问题，值得记下来**：
> 「把实现改坏 → 用例变红」这条纪律，**只验过一层**。
> 改坏的方式有多种，**改得不够狠的那种仍然会给出假绿** ——
> 本轮 A 轮（连注释一起抹）就是「不够狠」的一版，它对第 26/27 项
> 完全无感。**判据的牙齿要用「最阴的那种破坏方式」去试**：
> 不是「把这个字符串整个删掉」，而是「删掉实现、留下那句解释它的注释」。

---

### 5.9 第三轮独立复核：PASS_WITH_CAVEATS，三条 caveat 全部修完

第三轮复核代理读的是**当前工作树**，判 **PASS_WITH_CAVEATS**：
A-1 / A-2 / A-3 三条主张**全部成立**，**无本轮引入的回归**，回归数字逐条复现吻合。
它自己写了 12 条**不依赖被审方用例**的探针、21 项终审判据的变异、
以及一条「加密闸被拆掉」的变异 —— 留了三条 caveat。三条都已修完。

#### F-1【低-中】终审第 6 项判据对「§3.8 **强制**」无牙

第 6 项的判据只是「有一行 `key_error = _backup_encryption_key_error`」的**子串命中**，
**不验证这个结果被用来阻断启动**。复核方实测：把 `problems.append(...)` 换成 `pass`、
启动闸门整个作废之后，**终审仍报 51/51、退出码 0**（第 37 项同样照样绿）——
而第 6 项的描述恰恰是「生产校验**强制**」。

**这与本轮刚消灭掉的是同一类病**：判据指向了一行**代码**，但没指向
那一行**起的作用**。§3.8 启动闸门此刻是真的生效的（复核方用真实后端代码验过：
变异后 `problems` 里就没有 BACKUP_ENCRYPTION_KEY 那一条了），坏的只是**判据**。

**修法**：第 6 项与第 37 项都改成要求**阻断点**也在 ——
那句「配了 OSS 备份就必须配备份加密密钥」的报错文案。它在 `config.py` 里
**只出现一次**，且**只在 `problems.append(...)` 里** ——
它出现，就意味着「不配密钥 ⇒ 拒绝启动」真的被写进了启动闸门。

**反向验证**（把 `problems.append(...)` 换成 `pass`）：

```
退出码 = 1
[FAIL]  6. config.py 生产校验强制 BACKUP_ENCRYPTION_KEY（§3.8）—— 判据与**阻断点**都要在 …
[FAIL] 37. 配 OSS 备份时强制加密密钥（§3.8）—— 条件与**阻断点**都要在
```

#### F-2【低】新增用例文件不 hermetic

`backend/tests/test_audit_fix_backup_encryption_runtime.py` 里有两条用例
**没有把 `backup_encryption_key_warn_only` 复位**。而这个逃生口
**只能来自进程环境**（写进 `.env` 会被启动闸门拒），所以「开发机的 shell
profile 里带着它」是完全可能的场景 —— 此时套件会给出 2 条**与实现无关的红**，
**掩盖真问题**。

**修法**：加一个 **autouse** 夹具，每条用例都复位逃生口。
**每个用例只依赖它自己声明的东西**，不依赖跑它的那台机器的环境。

**反向验证**：`BACKUP_ENCRYPTION_KEY_WARN_ONLY=true` 跑整份文件 → **17 passed**（修前是 2 failed）。

#### F-3【低】新用例文件自身缺「配了密钥 ⇒ 上传的是密文」这条

复核方的变异：把 `_prepare_upload` 里的 `encryption_key()` 改成永远返回 None
（于是永远走明文分支）→ **这个新文件 16 条全绿**。
（仓库整体是盖住的 —— 另两个文件会红 —— 但**这一份自己没钉住**。）

**修法**：补一条 `test_配了密钥时传上去的必须是密文`，
与逃生口那条（「明文」）构成**对称断言**。
**只钉一头是不够的**：「配了密钥却传出明文」这件事本来根本没被断言过。

**反向验证**（同一次变异）→ 现在 **1 failed**（修前是全绿）。

> **F-2/F-3 合起来是一条通用教训**：
> **一组对称的事实（明文/密文、拒绝/放行、缺/不缺）只钉住一头，
> 另一头就是没钉。** 复核方正是用「只改一个方向」的方式把它挖出来的。
> 补断言时要问的是「**反过来的那一面，断言了吗？**」

---

### 5.10 第四轮独立复核（§3.1–§3.8）：判 **FAIL**，四条全部修完

复核代理读的是**当前工作树**，判 **FAIL**：§3.2 那道「近期二次验证」闸门
**本身是对的**，但**管得太窄** —— 只挂在 7 条路由上，于是整道闸门从侧翼被击穿。

#### N-1【高】`PUT /api/system/notify` 不挂闸 ⇒ 改收件人即可击穿整道 §3.2

管理员二次验证邮件**没有 per-user 收件人**（`sys_user` 表里没有邮箱列），
统一发给全局 `notify_config.recipients`。于是「改收件人」这个动作
**恰恰就是让二次验证生效的那个开关**，而它当时不需要二次验证。

**实测链条**（修复前）：

```
[旁路 0] 建号(受管控)          -> 403「该操作需要近期完成邮箱二次验证…」
[旁路 1] PUT /api/system/notify -> 200   ← 这条路由没挂二次验证
         现在的收件人 = ['attacker@evil.test']
[旁路 2] POST /api/auth/mfa/reauth -> 200，邮件真的发给了 attacker@evil.test
[旁路 3] 点真链接                -> 200
[旁路 4] POST /api/auth/mfa/poll -> 200，拿到一枚带 mfa_at 的令牌
[旁路 5] 建号(受管控)           -> 201，账号真的建出来了
```

**修法是两道，缺一不可**：

1. 路由挂上 `require_recent_admin_mfa`（挡住旁路 1）；
2. **投递目标一变，作废所有未完成的挑战**（`notify/mfa.py::expire_open_challenges`，
   挡住「先改收件人、再等一个**按旧投递目标投出去的**挑战被点开」这条路径）。

第 2 条**不是可有可无的**：只做第 1 条的话，攻击者可以在**改收件人之前**
先点开一封已经躺在收件箱里的信 —— 那封信是按旧投递目标投出去的，
拦不住。

#### N-2【中高】`POST /api/system/mcp-tokens` 不挂闸 ⇒ 铸出**永不过期**的读凭据

这个模块自己的说明写着「**因为令牌永不过期，吊销是唯一的安全阀**」。
管理员访问令牌 4 小时后失效，这枚 MCP 令牌**永久有效**，且能读该用户能读的一切账套。

**吊销类路由刻意不挂**：吊销是安全阀，令牌被盗时管理员要能立刻吊销，
而那时他很可能正处在「没有 `mfa_at`」的状态上（token 刚被偷）。
**给安全阀加锁是把安全阀焊死。** 判定表见 `api/mcp_admin.py` 的路由注释。

#### N-3【中】§3.7 的 `.with_for_update()` 是**假牙守卫**

复核方实测：把行锁去掉，**HTTP 层那 4 条用例照样全绿** ——
因为它们量的是「最终计数对不对」，而没有并发时最终计数**本来就对**。
**只钉结果、不钉机制**，等于没钉这条并发修复。

**修法**：补 3 条**服务层**守卫 —— 锁语义（确实取到了锁）、
形态（SQL 里真的带 `FOR UPDATE`）、行为（并发数 == 计数增量）。

#### N-4【低】其余未挂闸的管理员写路由

逐条判断后，把**铸造/改写凭据**（OCR 的第三方 token）与**放松安全限制**
（解锁别人的账号）也挂上闸。判据是**「这个动作改没改『别人能不能进来』这件事」**，
而不是「这个动作听起来危不危险」。

#### 本轮钉住的东西

`backend/tests/test_audit_fix_admin_write_mfa_gates.py`（**25 条**）：
**决定要挂的**（403 / 放行**两头**都钉）与**决定不挂的**（安全阀必须仍然好用）
分别列出。后者同样重要 —— 给止损类路由加锁是把安全阀焊死。

---

### 5.11 第五轮独立复核（§3.9–§3.15）：判 **PARTIAL**，三条全部修完

#### N-1【中】`ReportRow.style` 改动**零审计**（双栏表的右栏项目名）

§3.9 修的是「报表模板改动无审计」，但**审计快照只带了行的内容**，
没带 `style` —— 于是「把右栏的项目名换掉」这种改动**不留痕**。
（双栏表：左栏是「本月」、右栏是「本年累计」，两栏的**项目名**是可以单独改的。）

**修法**：`api/reports.py` 的快照里带上 `style`（**拷贝**，不是引用），
并让它参与 `before != after` 的比较。

#### N-2【严重】恢复一份**旧备份**，§3.15 的护栏会跟着一起消失，而恢复**仍然返回 0（成功）**

§3.15 的护栏（`entry` 侧的唯一约束 `uq_entry_voucher_line_no` +
触发器 `trg_entry_lock_guard`）来自**迁移 0038**。而恢复走的是 `pg_dump`/`psql`
这条路径 —— 恢复出来的库是**旧 schema**，护栏**不在里面**。

**更糟的是它静悄悄成功**：`db_backup.py --restore` 的 psql 退出码是 0，
于是恢复脚本报「成功」；而那个库从此**没有护栏** —— 已记账凭证的分录可以被
增删改，**没有任何东西会拦**。

**修法**（`backend/scripts/db_backup.py`）：

1. 新增 `_align_schema(url)`：psql 成功之后读 `alembic_version`，
   落后于 head 就 `upgrade_to_head()`，**然后再校验一次**；
2. 新增**独立的退出码 4**（`EXIT_SCHEMA_NOT_ALIGNED`）——
   **与「恢复失败」分开**：数据回来了但 schema 没对齐，是一种**不同的**状态，
   混进「失败」里会让运维以为数据也丢了。

**判据写在哪**：迁移 0038 的 `_preflight`（重复行号检查）**只在迁移时跑**，
恢复路径**不跑** —— 所以「恢复出来的库」必须自己把 schema 对齐到 head，
否则那套前置检查等于没写过。

#### N-3【低】§3.9 的「补一条审计」只覆盖了**模板**，没覆盖**报表行**

同一条的另一面：改 `ReportRow` 本身的样式，审计记录里看不出来。
与 N-1 一并修（快照带 `style`）。

**用例**：`backend/tests/test_audit_fix_report_template_audit.py`（含 style 一条）。

---

### 5.12 第六轮独立复核（§3.16–§3.22）：判 **PARTIAL**，三条全部修完

#### N-1【中】§3.17 的 M 启发式**不等于**「整理真能补得上」

§3.17 原来把「断号」的判据统一到 `fixable_gaps`（一个 M 启发式）。
复核方实测：**310 组配置里，它把 44 组**判成「断号」，而**整理规则真跑一遍
的话，那些号是补得上的**（补上的意思是：整理之后号是连续的，但**原来的号
变了**）。

**修法**：判据从「M 启发式」换成「**照整理规则模拟一次整理，看这个号之后
还是不是一个断号**」。发号规则因此抽成**唯一实现**
`accounting/organize.py::_assign_sequences`（不再有第二份发号规则），
模拟用 `_organize_resulting_rows`。

**实测**：243 组配置穷举，**不一致从 26 处降到 0 处**。

#### N-2【低】§3.16 的文案**陈述了一件不成立的事实**

结转的提示语里有一句「……科目不存在或已停用」，但**这句在两种情况下都说不对**：
科目其实存在（只是本年度不允许用），或者根本不是「不存在」而是「年度不匹配」。
**文案说了一件假话**，比不写更坏 —— 用户会去查一个不存在的科目。

**修法**：`api/vouchers.py` 抽出 `_missing_subjects_message` 分流。

#### N-3【低】§3.19 的文案**没区分「年度不匹配」**

跨年度科目校验的目标年度错了 —— 报出来的话必须是「**这个科目在目标年度
不存在**」，而不是笼统的「科目不存在」。与 N-2 一并分流。

**用例**：`backend/tests/test_audit_fix_organize_gap.py`、
`test_audit_fix_carry_forward_retained.py`、`test_audit_fix_cross_year_subject.py`。

---

### 5.13 第七轮独立复核（§3.23–§3.33）：判 **FAIL**，两条未修 + 四条新发现 + 8 条缺用例

#### N-1【高】§3.26 **根本没修**：同一个数，四个通道四个值

复核方实测同一份报表：

| 通道 | 显示的值 |
|---|---|
| 屏幕 | **28 位**（原值，不截） |
| Excel 导出 | **16 位**（`safe_string` 走 `"%.16g"`，先转 float） |
| HTML 打印 | 2 位 |
| MCP | 2 位 |

**四个数。** 而审计报告 §3.26 说的就是「屏幕与导出不是同一个数」。

**修法**（这一条改了方向）：**口径定在唯一的生产者 `evaluate()` 上**，
四条通道**全部读同一个两位小数值**。理由：

* 导出侧的两位是**既有事实**（Excel `#,##0.00`、HTML 打印 `_fmt`、
  MCP 一直是两位）—— 把屏幕改成 28 位反而离另外三条通道**更远**；
* 那会拆散「**MCP 与界面逐字一致**」这条已有不变量；
* 审计报告原文允许「导出 quantize 到两位**并在界面注明**」这一支。

于是新建 **`backend/app/core/money.py`**（金额两位小数的**唯一权威实现**）：
`MONEY_SCALE=Decimal("0.01")`、`MONEY_ROUNDING=ROUND_HALF_EVEN`、
`quantize_money()` / `money_str()` / `money_str_or_text()`。

**舍入取 `ROUND_HALF_EVEN` 而不是 `HALF_UP`**：MCP 通道原本就用 Decimal 的
默认舍入，改规则会让**已经在用**的通道变形；`HALF_EVEN` 亦是 IAS 8 在汇总
口径上推荐的写法。已确认**全仓无代码改 decimal 上下文**，
所以显式写死与原行为**逐字一致**。

**口径说明走独立的 `display_scale` 字段，不进 `notices`** ——
两条现有用例断言 `notices == []`（本期没结转时必须为空）：
「常驻事实」与「本期特殊情况」是**两条通道**。

**Excel 侧补了一个坑**：openpyxl 3.1.5 的 `safe_string(Decimal)` 走 `"%.16g"`
（**先转 float**）—— `99999999999999.99` 会被写成 `...98`（**丢一分**），
而 Excel 数值格只保证 **15 位有效数字**。所以超 15 位时**改写成文本**
（精确数字，带千分位）：**可见的错位好过看不出差别的错数**。

**顺带改正了一个说谎的注释**：`printing/excel.py::_num` 的旧 docstring
写着「openpyxl 写十进制字面量」—— 那是**假的**，实测走的是 `"%.16g"`。

**用例**：`backend/tests/test_audit_fix_report_caliber.py`（7 条）+
`frontend/tests/report_display_scale.test.mjs`（6 条）。

**反向验证**（4 处，全部变红）：evaluate 不量化（1 failed）、
Excel 去掉文本兜底（1 failed）、MCP 另写一份实现（1 failed）。
第 4 处（MCP 改回 `str().strip()`）**仍全绿属合理** —— 值已经被 evaluate
量化过了，那一层是空转。

#### N-2【高】§3.33 **根本没修**：`prevBookId` 被 v-model **抢先写废**

§3.33 是「切账套提示永不执行」。修法是「把记号搬进 store」。
但顶栏那行是 `v-model="store.bookId"` —— **v-model 会先把新账套写进 store**，
`@change` 里再调 `setBook` 时 `bookId` **早就是新的了**：
「变了没有」永远答 false，于是「换账套前的那一本」永远记不上。

**修法**：判据改成 **watch 记下来的上一代**（`oldValue`），
跟「谁写的 bookId」**无关** —— v-model 先写、直接调 `setBook`、别的页面改写，
**都判得对**。watch 装在一个 **detached 的 `effectScope`** 里
（装在组件的 setup 里的话，组件一卸载守卫就跟着没了）。
护栏：`ov > 0 && nv > 0 && ov !== nv` 才算「切账套」（登出、账套列表里
没有这一本了 —— 那些路会把 bookId 归 0，不是切账套）。

**用例**：`frontend/tests/session_book_switch.test.mjs` +
`test_recheck_book_switch_prompt.test.mjs`。

#### N-3【中】三视图的「加载失败」渲染成「没有数据」

§3.25 修的是 `api/client.js` 的兜底提示，但**页面这一侧**还有三处：
加载失败时页面把「加载失败」画成「没有数据」，用户打开总账看到一张空表、
零提示，于是以为这个科目一整年没发生额。
**修法**：三视图接 `loadError` 状态 + `client.js` 的 5xx 兜底。

#### N-4【中】凭证保存的**重复 toast**（成功与失败各弹一次）

#### N-5【中】新建凭证**重挂后再存一张**（库里多出一张几乎一样的）

**根因**：「这张凭证已经建出来了」这个事实只活在组件里 → 换页面重挂就丢 →
重挂后又被当成一张**全新的空凭证**，下一趟自动保存**又 POST 一张**。

**修法**：把这件事记进 store（`createdVoucher`，形状 `{bookId, id, createKey}`），
**换页面就清**（MainLayout 的路由 watcher 里跟 `prevBookId` 一起清）。

#### N-6【中】`VoucherOrganize.vue` 在体检失败时照样放开 RENUM

§3.27 原来只有 `checkFailed` 一个布尔，`VoucherOrganize.vue` 里**另写**了一套
判断 —— 两处各判一半，凑出一个「可以重编号」的状态机。
**修法**：抽成 `checkState` 状态机，**只有 `ok` 允许 RENUM**。

#### 8 条缺行为级用例

复核方逐条比对「这条修复守着它的用例」时，发现 **8 条前端修复一条用例都没有**
（修对了，但下次有人改回去不会有人喊）。全部补齐：
`test_recheck_{load_failure,organize_check,row_refs,split_submit,voucher_remount,voucher_save_guard}.test.mjs`
等 7 个新文件、**57 条行为级用例**。

---

### 5.14 第八轮独立复核（§3.34–§3.45）：判 **PASS_WITH_CAVEATS**，四条全部修完

复核方逐条验了 §3.34–§3.45 的 12 条代码修复，结论是**全部为真**
（不是「看起来改了」—— 逐条实测过）。留了 4 条 caveat。

#### N-1【中-高】§3.39 的依赖回退判据错了：**现网会变成「新代码 + 旧依赖」

原来的判据是「回退**失败了没**」。但真正要判的是另一件事：
**现网此刻的代码，是不是已经是新版本了**。

**实测**：发布**已经生效**之后，若回退依赖失败（迁移也一起回退了），
旧判据说「回退失败了」→ 部署脚本报错退出。而此时现网是
**新代码 + 旧依赖** —— 两者版本不匹配，比「回退失败」更糟。

**修法**：`deploy/deploy.sh` 加 `CODE_SWITCHED` + `code_is_old_version()`，
判据从「失败了没」换成「**代码与依赖必须同版本**」
（`CODE_SWITCHED` 与 `ROLLBACK_SCOPE` 合成）。

**复核方建议的「与 `rollback_code` 共用 `ROLLBACK_SCOPE==code`」被实测否掉**：
迁移失败时 `ROLLBACK_SCOPE` 还是 `none`，会打破 §3.39 的原保证。

#### N-2 / N-3 / N-4【低】三处**钉子漏网**

判据/用例没覆盖到本轮新加的三处行为。已补：
`test_deploy.sh` 补三组（21.1 闸门位置 + 覆盖失败行为、
22.1 两个 esa 脚本的符号链接攻击 + 负控、26.1 发布已生效后不许回退依赖），
并**重写 §3.38 的假阳性断言**。

**用例数**：216/0 → **242/0**；`test_dev_sh.sh` **29/0** 不变。

---

### 5.15 前端**撞到闸门之后没有下文** —— 管理员会撞死路

§3.2 的闸门扩到 13 条受管控写路由之后，**前端那一侧出现了一个新的缺口**：
`frontend/src/api/auth.js` 里的 `mfaReauth()` **全仓无调用方**。

**后果**：管理员的令牌一旦超过宽限期（默认 5 分钟），碰这些动作只会收到
一句 403 —— **界面上没有「去重新验证」的引导**。撞死路，而且**没有任何提示**
告诉他该做什么。

**修法**（整条链）：

```
管理员做高危动作
  → 403（detail.code = MFA_REAUTH_REQUIRED）
  → 弹出引导（components/MfaReauthDialog.vue）
  → 发信 → 点链接 → 换到带新鲜 mfa_at 的新令牌
  → **自动重试**那个被拦下的动作
```

**四层，缺一不可**：

| 层 | 落点 | 缺了会怎样 |
|---|---|---|
| 协议 | 后端 403 的 detail 改成**结构化** `{code, message}`（`core/deps.py::MFA_REAUTH_REQUIRED`） | 前端没法判断「这个 403 是不是该去重新验证」—— 只能靠匹配中文，而**文案一改就静默失效** |
| 拦截器 | `api/client.js` 认 `detail.code`，走重新验证，拿到新令牌后**自动重试** | 永远停在 403 |
| store | `stores/session.js` 的 `requestMfaReauth` / `startMfaReauth` / `pollMfaReauth` / `finishMfaReauth` / `cancelMfaReauth` | 没有「等管理员走完」这件事 |
| 界面 | `components/MfaReauthDialog.vue`（挂在 MainLayout） | 管理员看不到「去重新验证」的入口 |

**几个关键取舍**：

* **重试是安全的**：闸门在**依赖**里，FastAPI 先跑依赖再跑路由体 ——
  被 403 拦下的那一刻，路由体**一行都没执行**，没有任何副作用。
  原样重发一遍不会「做两遍」。
* **不会无限重试**：重试那一趟带上 `__cwqMfaRetried` 记号 ——
  没有它，重新验证之后**还是 403**（宽限期是 0、通道坏了……）就会再进一次
  重新验证，每一轮都发一封信。
* **「取消」裁决成 false，不重试**：把「取消」当成「通过」去重试，
  等于把一个管理员**明确拒绝**的动作又做了一遍。
* **超时兜底 10 分钟**：没有它，这一次等待可能**永远不结束** ——
  而拦截器正**等着**这个结果（那个被拦下的请求就悬在那儿不落地）。
* **登出会就地结束**正在等的那一次（裁决 false）：人已经走了，悬着它毫无意义。
* **重新验证换令牌时不动账套列表**：这个流程发生在一次请求的**中途**，
  去拉账套列表既没必要，又可能因为网络抖动把整条链路拖失败。
* **错误文案统一走 `utils/errorText.js`**（**唯一的一份实现**）：
  结构化 detail 直接丢给 `ElMessage` 会渲染成 **`[object Object]`** ——
  而这恰恰是管理员撞闸门时**最需要看懂的一句提示**。

**跨语言的那一环**：后端用例**直接去读前端源码**，核对两边是**同一个字符串**、
前端**按 `detail.code` 判**（不是按文案判）、处理函数**被装上了**、
对话框**挂进了界面**。改了一边忘了另一边，界面上就回到撞死路，
而且**没有任何报错** —— 所以这一环必须被钉住。

**用例**：`backend/tests/test_audit_fix_mfa_reauth_contract.py`（5 条，
含跨语言核对）+ `frontend/tests/mfa_reauth.test.mjs`（10 条，**行为级**：
真的跑 axios 的拦截器，断言「第二趟真的发出去了、而且带的是新令牌」）
+ `frontend/tests/error_text.test.mjs`（12 条）。

**反向验证**（四处，破坏方式都是「**删实现、留注释**」—— 追加字符会让
子串还在，测出来是假绿）：

| 变异 | 结果 |
|---|---|
| 删掉拦截器里「认错误码 → 重新验证 → 自动重试」整段实现（**注释留着**） | `mfa_reauth.test.mjs` → **2 failed** |
| `applyToken` 不把新令牌落进 localStorage（重试会带旧令牌） | 同上 → **2 failed** |
| 后端把错误码换成 `MFA_REAUTH_REQUIRED_V2`（前端还认旧的） | `test_audit_fix_mfa_reauth_contract.py` → **1 failed**（跨语言那条） |
| `cancelMfaReauth` 把裁决写成 `true`（把「取消」当成「通过」） | `mfa_reauth.test.mjs` → **1 failed** |

---

### 5.16 CI 抓到的最后一条：**新用例只在「本机」成立**

第一次 CI（run 40188799518）红了 1 条：

```
FAILED tests/test_plugin_sandbox.py::test_仓库里真实的插件源码能在沙箱里加载
  AssertionError: 示例插件源码不在仓库里
```

**根因**：``/plugins/`` 整目录被 ``.gitignore`` 排除（.gitignore 第 104 行）——
那两份示例插件（``com.cwq.periodic`` / ``com.cwq.reimburse``）**不在版本库里**，
只存在于开发机上。而本轮新写的用例直接 ``assert sources``。

**后果的形态**：这条用例在**本机全绿**（插件源码就在那儿），在 **CI 必红**
（干净的 checkout 里没有它）。也就是说它测的不是这套代码，是「跑它的那台机器」。
这与第三轮复核的 F-2 是**同一类病**：新用例不 hermetic。

**修法**：与既有的 ``test_plugin_security_scan.py`` 同一个口径 ——
本机没有插件源码就**跳过**（并说清为什么），不是失败。

**顺带修掉一条「空转即绿」**：``test_真实插件源码不触发新的扫描规则`` 原来是个
循环，插件源码不在时循环体一次都不跑 —— 什么都没测，却显示通过。
现在它也一起跳过。

**教训（比这次修复本身更值钱）**：**「本地全绿」不等于「CI 会绿」。**
这条用例本地跑了 3554 条全过 —— 因为它测的是**这台机上的文件**。
所以修完之后，本地多做了一步**与 CI 完全同环境**的验证：

```bash
# 开一个干净 checkout（与 CI 的 checkout 一模一样）
git worktree add /tmp/cwq-clean-ci HEAD
cd /tmp/cwq-clean-ci/backend
CWQ_TEST_DATABASE_URL="postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_xxx_test" \
  <原仓库>/backend/.venv/bin/python -m pytest -n 6 -p no:warnings -o addopts="" --tb=line
#   → 修之前：1 failed, 3551 passed, 4 skipped（**与 CI 的失败一模一样**）
#   → 修之后：109 passed, 2 skipped（那两条带理由跳过）
```

**它在本地精确复现了 CI 的失败**（同一条用例、同一个断言）——
于是「是不是还有别的用例依赖本机才有的东西」这件事，一次全量跑就问完了
（答案：只有那一条）。

**验收标准因此补一条**：**新加的用例必须在干净 checkout 上也成立** ——
「本地全绿」不能作为证据。

---

### 5.17 第一次生产发布：**启动闸门把站点按住了**，以及它暴露的两个真缺陷

2026-10-25 19:49 发布 `7ead8ad` 到 `43.138.178.174`。前 5 步（取产物 → 备份 →
解包 → 迁移 → 切产物）全部成功，**第 6 步探活失败**：

```
[19:49:52] ❌ 发布后 30 秒内 /api/health 没有给出 2xx/3xx
[19:49:52]    ⚠️  此时**新版本已经上线**：迁移已执行、产物已切换、服务已重启。
```

**真实原因**（在 `backend/logs/api.err.log` 里，不在 journal 里）：

```
ValueError: 生产配置校验不通过，拒绝启动：
  - MCP_ENABLED=true 但 MCP_ALLOWED_HOSTS 为空 —— MCP 端点的 DNS 重绑定防护未开启
```

**这不是发布失败，是这一轮 §3.44 那类「启动闸门」在生产上第一次真的生效**：
这台机的 `.env` 照抄了 `.env.example`（`MCP_ALLOWED_HOSTS=` 是空的），
而新代码把「MCP 开着 + 白名单空」从「告警」升成了「**拒绝启动**」。

**站点中断约 2 分钟**（19:49:30 服务退出 → 19:51 重启成功），
期间公网 `/api/health` 是 **502**。修法是在这台机的 `.env` 里补上白名单
（先备份，再追加，属主与权限没变），重启后立刻恢复。

**这一轮因此暴露了两个真缺陷**：

#### 缺陷①：报错文案会把人引向一个「启动过了但功能全坏」的配置

原文案让人「设 `MCP_ALLOWED_HOSTS=你的域名（如 acct.example.com:*）`」——
**照着它配是错的**：

* 传输层（MCP SDK 的 `TransportSecurityMiddleware`）的匹配规则是
  「`host in allowed_hosts` 精确命中」**或**「`host.startswith(base + ":")`」，
  而后者**要求带冒号**；
* 所以 `acct.example.com:*` **匹配不到不带端口的 Host**；
* 而 nginx 传给后端的 `Host` 恰恰是 `$host` —— **不带端口**。

于是照着示例配的结果是：**启动闸门过去了、站点起来了、MCP 端点每个请求都被
挡成 421**。最坏的一类 —— 「看着部署成功了」。

**修法**：报错里给出**完整的两种写法**，并说清漏了会怎样：

```
MCP_ALLOWED_HOSTS=acct.example.com,acct.example.com:*
```

**用例**：`test_audit_fix_prod_config_gates.py` 两条 ——

* 一条钉**文案里必须有完整的两种写法**；
* 一条把「闸门放行」与「传输层真能匹配上」**分开**钉：
  只写 `:*` 时闸门放行（配了）但传输层匹配不上裸域名；两种都写时
  裸域名与带端口都匹配得上，而 `evil.example` 与
  `acct.example.com.evil.test` 一律匹配不上。

  **只钉闸门那一半不够** —— 它会得到一个「启动检查过了、但功能是坏的」部署。

**反向验证**（文案改回只给 `:*`）→ **1 failed**。

#### 缺陷②：§3.41 的闸门让 `target-host.md` 里记的调用方式**失效了**

§3.41 的「发布不覆盖 nginx」闸门是这轮新加的：`APP_DIR != /opt/cwq` 即拒绝发布。
而这台机的应用根**恰恰就是** `/www/wwwroot/acct.xins.live` ——
于是 `target-host.md` 里那段「参数一个都不能少」的调用方式**少了必需的一个参数**，
照着它发会被直接拒。

**这台机的 nginx 确实已经手工配好了**（`nginx -T` 实测：
`root /www/wwwroot/acct.xins.live/frontend/dist;`、
`alias /www/wwwroot/acct.xins.live/downloads/;`），
所以放行是安全的 —— 放行后脚本**不会**改 nginx，只会在发布后体检里
**对账线上前端是不是这一版**（`runtime-config.js` 里的 version）。

**修法**：`deploy/target-host.md` 补上 `CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1`
并写清「它是『这台机的 nginx 已经手工配好了』的**断言**，不是绕过检查的开关」；
同时把「这台机的 `.env` 必须有的两行」与「起不来了去哪儿看日志」
（`backend/logs/api.err.log`，**不是** journal —— 发布脚本的提示指向
`/opt/cwq/...`，**这台机上那个路径是错的**）写进去。

#### 发布完成后的验证（实测）

```
1. 所有 unit：cwq-api running，其余 timer waiting，--failed 计数 0
2. 健康检查：本机 200 / 公网 /api/health 200 / 公网 / 200 / 未认证 /api/mcp 401
3. 前端版本：dist -> releases/dist-7ead8ad18db9-…，runtime-config.js 里就是 7ead8ad18db9
4. schema：0039_voucher_idempotency (head)
5. 护栏：uq_entry_voucher_line_no=1、trg_entry_lock_guard=1、voucher_idempotency 表在
6. 存量数据体检（§7 第 11 项）：✓ 通过，退出码 0
7. 数据没丢：voucher 5741 / entry 22006 / sys_user 3
8. 护栏真的拦得住（事务内试改，最后回滚）：
     ERROR: 会计期间已结账，禁止修改凭证 (voucher_id=1)
     CONTEXT: PL/pgSQL function fn_entry_lock_guard() line 21 at RAISE
   → 改完之后那行金额仍是 5264.44
```

**§7 第 11 项的「查」这一半，在生产库上跑完了**：退出码 0，
「没有重复行号」「所有期间都干净」——
所以「哪条重复该留、金额怎么修」那个**只有懂业务的人能做的决定**，
在**当前数据上没有可决策的对象**（没有重复）。

> **第 8 条只验到了「期间已结账」那一支**：库里**没有**「已记账 + 期间未结账」的
> 凭证（全部 5005 张已记账凭证都在已结账期间里），所以 §3.15 真正针对的那一支
> （「已记账凭证的分录不许动」）在生产数据上**验不到** —— 那一支由仓库里的用例覆盖。

---

## 6. 审计报告 §3.1 的四条复核主张：重新取证，**全部推翻**

审计报告 §3.1 里，验证器给出的四条主张与**当前代码**对不上 ——
它读的是旧快照。按现在的代码重新取证：

| 主张 | 重新取证 |
|---|---|
| `_sanitize(os)` 可以拿到真 `os` | **抛 ImportError**：「插件不允许使用模块：os」（`sandbox.py:435`） |
| `scan_file()` 对命名空间走私不设防 | `dataclasses.sys` / `uuid.os` / `contextlib.os` / `json.codecs` / `typing.types` **全部判 `PY_MODULE_ESCAPE`** |
| `__builtins__['__import__'].__globals__['builtins'].__import__('os')` 可用 | **装载期即拒**：「插件代码使用了被禁用的双下划线反射（第 3 行）」 |
| 静态扫描覆盖不足、运行期没有第二层 | 覆盖存在（阶段 1 起为常驻用例），且现在还多一层**独立进程** |

明细见 `docs/plugin-verification.md`。

---

## 7. 回归

```bash
cd backend && CWQ_TEST_DATABASE_URL="postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_mavis_test" \
  .venv/bin/python -m pytest -n 6 -p no:warnings -o addopts="" --tb=line
#   → 3554 passed, 2 skipped（**带上 PG 客户端**：CWQ_PG_DUMP / CWQ_PSQL 指到
#     pg_dump 与 psql。不带客户端时会跳过那几条备份恢复的真实往返用例。
#     CI 装 postgresql-client 并设 CWQ_REQUIRE_PG_CLIENT=1，所以那些**必须**跑）

# 插件专项在**三种隔离模式**下各跑一遍（默认 / shadow / on）
cd backend && CWQ_TEST_DATABASE_URL="postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_mavis_test" \
  .venv/bin/python -m pytest tests/ -k "plugin" -n 4 -p no:warnings -o addopts="" --tb=line
#   → 466 passed（默认档）
cd backend && PLUGIN_ISOLATION=shadow CWQ_TEST_DATABASE_URL="postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_mavis_test" \
  .venv/bin/python -m pytest tests/ -k "plugin" -n 4 -p no:warnings -o addopts="" --tb=line
#   → 466 passed
cd backend && PLUGIN_ISOLATION=on CWQ_TEST_DATABASE_URL="postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_mavis_test" \
  .venv/bin/python -m pytest tests/ -k "plugin" -n 4 -p no:warnings -o addopts="" --tb=line
#   → 466 passed

# **干净 checkout 上的那一遍**（与 CI 同环境，见 §5.16）
git worktree add /tmp/cwq-clean-ci HEAD
cd /tmp/cwq-clean-ci/backend && <原仓库>/backend/.venv/bin/python -m pytest -n 6 ...
#   → 与 CI 完全一致的结果（本地有 plugins/ 时多 2 条会跑，其余一样）

cd frontend && npm test     # → node 汇总 **238 通过 / 0 失败**
                            #   （85 个测试文件；其中一部分用 node:test、
                            #    一部分用自带的断言循环 —— 合计 **859 条**断言用例，
                            #    逐条数法：`npm test | grep -c '^  OK '`）
cd frontend && npm run check# → **187 个文件**语法自检通过

bash deploy/tests/test_deploy.sh   # → **242 / 242**
bash deploy/tests/test_dev_sh.sh   # → 29 / 29

python3 tools/audit_roadmap_check.py            # → 通过 51 / 失败 0（**退出码 0**；有项目 FAIL 时退出码 1）
python3 tools/audit_plugin_sandbox_recheck.py   # → 退出码 0（四条主张都不成立且被钉住）
```

> **前端那个数字原来写的是 711，是错的。** 写进去时**没有实测过**。
> 711 在任何一种数法下都对不上（node 自己的汇总、用例数、文件数）。
> 这正是不该出现的东西：**数字也要有出处**。
> 已在 `docs/plugin-verification.md` 一并改正。
>
> **本轮又核了一遍**，并把「怎么数」写清楚（免得下一个人又写一个对不上的数）：
> node 的汇总是 **238**（它把「一个文件」与「文件里的 node:test 子用例」
> 一起数），测试文件 **85** 个，**断言用例 859 条**。
> 三个数都实测过，也都写了各自的数法。

插件专项在三种隔离模式下各跑一遍全绿（默认 / `shadow` / `on`），
示例插件（`com.cwq.periodic`、`com.cwq.reimburse`）**零改动**通过。

存量数据体检的命令行形态也验过（不只是用例里调 `main()`）：

```bash
cd backend && .venv/bin/python scripts/data_health_check.py
#   干净库 → 退出码 0；库有问题 → 2；只有提示 → 3；体检自己跑不动 → 1
```

---

## 8. 工作区状态

- 改动**已提交并推送**（用户要求：「全部完成修复后提交推送，部署到生产」）。
  提交信息里写明本轮修了哪些章节（§3.1–§3.45 + §7 路线图 50 项）。
- 审计报告 `output/AUDIT-REPORT-2026-10-25.md` **本身未被改动** —— 它是只读的输入
  （md5 `4c93913957ca7fdd3d6e748cf166eabf`）。
- `output/` 整个被 `.gitignore` 排除，所以验收记录放在**受跟踪的** `docs/` 下。
