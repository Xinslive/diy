#!/usr/bin/env python3
"""§7 路线图 50 项终审：逐项找**可复现的代码证据**。

配套文档：``docs/audit-remediation-2026-10-25.md``

用法（仓库根）::

    python3 tools/audit_roadmap_check.py

**退出码** ``0`` = 全过，``1`` = 有项目 FAIL。所以这个脚本可以直接当闸门
挂进 CI —— 退出码就是它的结论，不用人去读那张表。

判据：每项至少一条 (file:line, 命中的代码片段)。找不到就是 FAIL ——
不做「看起来应该改了」的推测。

**判据只认代码，不认注释。** 第一版里有几项（TRUSTED_PROXIES、
this_ip、OSS 加密、vhost 探测…）命中的是**注释里提到的那句话**，
源码其实没改 —— 现在每条判据都指向闸门代码本身。
"""
import os
import re
import sys

# 仓库根 = 本文件所在目录的上一级（output/ 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- 检索工具

_cache = {}


def read(path):
    if path not in _cache:
        full = os.path.join(ROOT, path)
        if not os.path.exists(full):
            _cache[path] = None
        else:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                _cache[path] = f.read()
    return _cache[path]


def find(path, pattern, flags=0):
    """返回 (行号, 命中行) 列表。"""
    txt = read(path)
    if txt is None:
        return []
    out = []
    for i, line in enumerate(txt.splitlines(), 1):
        if re.search(pattern, line, flags):
            out.append((i, line.strip()))
    return out


def one(path, pattern, flags=0):
    """返回第一条命中：(行号, 命中行)；没有就 None。"""
    hits = find(path, pattern, flags)
    return hits[0] if hits else None


RESULTS = []


def check(num, desc, evidence, note=None):
    """evidence: list of (path, lineno, snippet) —— 空列表 = FAIL。

    ``note`` 是**无论成败都要打出来**的一句说明（用来讲「为什么这一项
    没证据」）。它**不参与判定** —— 证据为空就是 FAIL，加一条 note
    不会把 FAIL 变成 PASS。
    """
    RESULTS.append((num, desc, evidence))
    mark = "PASS" if evidence else "FAIL"
    print(f"[{mark}] {num:>2}. {desc}")
    if note:
        print(f"        ⚠️  {note}")
    for path, lineno, snip in evidence:
        print(f"        {path}:{lineno}  {snip[:110]}")


def _是不是注释或导入(snippet):
    """这一行是不是注释 / import / docstring 开头 —— 也就是**不该当证据**的那类行。"""
    t = snippet.strip()
    if t.startswith("#"):
        return True
    if t.startswith("import ") or t.startswith("from "):
        return True
    if t[:1] in (chr(34), chr(39)):
        return True
    return False


def ev(path, pattern, flags=0, limit=1):
    """取证据。**优先报代码行**，注释/import 排在后面。

    **为什么。** 判据的证据如果落在注释上，那行注释说「这里应该有个
    XX」就足以让该项 PASS —— 而那正是审计 §3 那一版判据的老毛病
    （命中的是**注释里提到的那句话**，源码其实没改）。

    实测：63 条规格里有 17 条的**首条**命中是注释或 import
    （``period_prefix`` 命中的是一句注释、``mktemp`` 命中的是
    「必须用 mktemp」那句注释、``TimeoutStartSec`` 命中的也是注释）。

    判据本身**仍然有牙**（那个字符串必须在文件里），但报出来的证据行
    指向注释，会让验收记录读起来像「这只是一句说明」—— 复核方据此
    质疑「实现是真的吗」，是合理的质疑。

    所以这里把**代码行**排到前面。**全路径只有注释命中时仍然算证据**
    （那说明判据写歪了，不是代码没写），只是会排在后面。
    """
    hits = find(path, pattern, flags)
    代码 = [h for h in hits if not _是不是注释或导入(h[1])]
    注释 = [h for h in hits if _是不是注释或导入(h[1])]
    排序 = 代码 + 注释
    return [(path, h[0], h[1]) for h in 排序[:limit]]


def ev_any(paths, pattern, flags=0, limit=1):
    out = []
    for p in paths:
        out.extend(ev(p, pattern, flags, limit))
        if out:
            break
    return out


def ev_all(*specs, flags=0):
    """每个 ``(path, pattern)`` 都必须命中 —— 缺一条就当没有证据。

    **为什么要有它。** ``ev(a) + ev(b)`` 是「**任一**命中就算过」，
    而有几项的判据是「这几处**都要**在」—— 用 ``+`` 拼的话，少一处照样 PASS，
    那条判据就是假的（把体检脚本删掉，终审脚本仍然说第 11 项通过）。

    缺哪一条会当场打出来 —— 「没找到证据」本身也要能定位。
    """
    out = []
    missing = []
    for path, pattern in specs:
        # **走 ev() 而不是 find()**：ev() 会把**代码行**排在注释前面。
        # 这一条不是锦上添花 —— 实测 item 27（systemd unit 的
        # TimeoutStartSec）原先报的证据是**注释里那一句**
        # 「TimeoutStartSec：**必须显式给**」，而真正生效的
        # ``TimeoutStartSec=1800`` 在二十行之下。报注释会让验收记录
        # 读起来像「这只是个说明」，而判据本身其实有牙。
        hits = ev(path, pattern, flags, 1)
        if not hits:
            missing.append((path, pattern))
        else:
            out.extend(hits)
    if missing:
        for path, pattern in missing:
            print(f"        ✗ 缺证据：{path} 里找不到 {pattern!r}")
        return []
    return out


def ev_removed(*absent_specs, present_specs=(), flags=0):
    """断言「这些**不在了**，而这些**在**」—— 用来钉「移除」这类改动。

    **为什么不能写成 ``ev_all(...) + ev_absent(...)``**：``+`` 的语义是
    「任一命中就算过」。一组判据缺一条、另一组命中一条，合起来照样 PASS，
    那条判据就是假的（判据本身也要被反向验证过 —— 与
    ``ev_all`` 存在的原因同源）。这里要求**全部**成立才给证据。
    """
    out = []
    for path, pattern in absent_specs:
        if find(path, pattern, flags):
            print(f"        ✗ 还在：{path} 里仍然有 {pattern!r}")
            return []
    for path, pattern in present_specs:
        hits = ev(path, pattern, flags, 1)
        if not hits:
            print(f"        ✗ 缺证据：{path} 里找不到 {pattern!r}")
            return []
        out.extend(hits)
    return out


B = "backend/app"
BE = "backend/app/api"
BF = "backend/app/core"
BA = "backend/app/accounting"
BP = "backend/app/plugins"
BB = "backend/app/backup"
BN = "backend/app/notify"

print("=" * 78)
print("§7 路线图 50 项终审")
print("=" * 78)

# ============================================================ 第 0 步
print("\n--- 第 0 步：立刻可做、无风险 ---")

check(1, "books.py 的 limit 参数改成 Query(100, ge=1, le=1000)（§3.11）",
      ev(f"{BE}/books.py", r"le\s*=\s*1000"))

check(2, "reports.py 改用带类型的入参模型（§3.10）",
      ev(f"{BE}/reports.py", r"class \w+\(BaseModel\)"))

check(3, "reports.py / printing.py 改用 normalize_role（§3.12）—— 两个文件都要改",
      ev_all((f"{BE}/reports.py", r"normalize_role\("),
             (f"{BE}/printing.py", r"normalize_role\(")))

# 8 条 CheckConstraint，分布在多张表
_cc = []
for m in sorted(os.listdir(os.path.join(ROOT, B, "models"))):
    if not m.endswith(".py"):
        continue
    p = f"{B}/models/{m}"
    hits = find(p, r"CheckConstraint")
    for ln, sn in hits:
        _cc.append((p, ln, sn))
_defs = [(p, ln, sn) for (p, ln, sn) in _cc if "CheckConstraint(" in sn]
check(4, f"补齐同名同义的 CheckConstraint（§3.22 / §3.14）—— 实测 {len(_defs)} 条约束定义，"
      f"分布在 {len({c[0] for c in _defs})} 个 model 文件", _defs[:4])

check(5, "reports.py 的模板写路径补审计 log()（§3.9）",
      ev(f"{BE}/reports.py", r"\blog\("))

# §3.8 的那条闸**已于 2026-10-26 移除**：备份制品的静态加密改由 **OSS 服务端
# 加密（SSE）** 承担，客户端不再加密。
#
# 原来这一项钉的是「生产校验强制 BACKUP_ENCRYPTION_KEY」——
# 判据与**阻断点**都要在（阻断点 = 那句报错文案，它只在 problems.append 里）。
# **原来只查判据**，于是把 ``problems.append(...)`` 换成 ``pass``、
# 整个启动闸门作废之后，那一项**照样 PASS** —— 那是一颗假牙。
#
# 现在这一项钉的是**新的事实**，而且是**反向**的：
# ① 客户端加密那层**不在了**（密钥、加密、解密、魔数都没了）；
# ② 桶上的 SSE 状态**看得见**（读得到 + 被调用）。
#
# 写成反向是刻意的：谁把客户端加密加回来（哪怕是"顺手补上"），这一项立刻
# FAIL —— 而它当初正是审计 §3.8 的结论。
check(6, "备份加密改为 OSS 服务端加密（2026-10-26）—— 客户端加密那层不在了，"
      "且桶的 SSE 状态看得见（读得到 + 被调用）",
      ev_removed((f"{BB}/export.py", r"def encryption_key"),
                 (f"{BB}/export.py", r"def encrypt_file"),
                 (f"{BB}/export.py", r"def decrypt_file"),
                 (f"{BF}/config.py", r"^\s*backup_encryption_key\s*:", ),
                 present_specs=((f"{BB}/oss_client.py", r"def server_side_encryption"),
                                (f"{BB}/service.py", r"server_side_encryption")),
                 flags=re.M))

check(7, "system.py 的 entity_id 改用 sha256 截断到 32 位（§3.21）",
      ev(f"{BE}/system.py", r"sha256"))

check(8, "vouchers.py 按约束名区分 IntegrityError（§3.20）",
      ev(f"{BE}/vouchers.py", r"except IntegrityError as"))

check(9, "accounting/vouchers.py 结转用目标期间的年份（§3.19）",
      ev(f"{BA}/vouchers.py", r"period\.year|target_period|period\.year"))

check(10, "accounting/vouchers.py 新建凭证改用 period_prefix（§3.18）",
      ev(f"{BA}/vouchers.py", r"period_prefix"))

# ============================================================ 第 1 步
print("\n--- 第 1 步：账务正确性止血 ---")

# 11 是「上线前在真实库上跑的一次性体检」：判据不是「迁移里有那段话」，
# 而是**三处都在** —— ① 有可跑的只读命令 ② 判据有唯一实现
# ③ 迁移的前置检查引用的是**那一份**实现。
# 少任何一处，第 11 项就不该算通过（§4.5：同一个量只允许一个权威实现）。
check(11, "存量数据体检（同一 voucher_id 下 line_no 重复）—— "
      "可跑的只读命令 + 判据唯一实现 + 迁移前置检查引用同一份",
      ev_all(("backend/scripts/data_health_check.py", r"duplicate_line_no"),
             ("backend/app/accounting/data_health.py", r"def duplicate_line_no"),
             ("backend/migrations/versions/0038_entry_voucher_line_unique.py",
              r"from app\.accounting\.data_health import duplicate_line_no")))

check(12, "vouchers.py / accounting/vouchers.py 对凭证行 with_for_update + 409（§3.15）"
      "—— 两个文件都要有行锁（判据取**调用处**，不是提到这词的注释）",
      ev_all((f"{BE}/vouchers.py", r"\.with_for_update\(\)"),
             (f"{BA}/vouchers.py", r"\.with_for_update\(\)")))

check(13, "entry 加 UniqueConstraint(voucher_id, line_no)（§3.15）"
      "—— 判据是**约束名常量**，不是迁移 docstring 里那句话",
      ev("backend/migrations/versions/0038_entry_voucher_line_unique.py",
         r"CONSTRAINT_NAME = \"uq_entry_voucher_line_no\""))

# 14 原来只查「db_init.sql 里出现过 fn_posted_voucher_guard 这个词」——
# 那命中的是**注释**，不是护栏代码本身。收紧成三处**都在**：
#   ① entry 侧的守卫函数在（fn_entry_lock_guard）；
#   ② 它真的挂在 entry 的 BEFORE INSERT OR UPDATE OR DELETE 上；
#   ③ 里面有「已记账不许动分录」那句抛错。
# 少任何一处，第 14 项就不该算通过。
check(14, "db_init.sql 在 entry 侧钉住「已记账凭证的分录不许增删改」（§3.15）"
      "—— 判据在 fn_entry_lock_guard 第②段，挂在 trg_entry_lock_guard 上",
      ev_all((f"{BF}/db_init.sql", r"CREATE OR REPLACE FUNCTION fn_entry_lock_guard"),
             (f"{BF}/db_init.sql",
              r"CREATE TRIGGER trg_entry_lock_guard BEFORE INSERT OR UPDATE OR DELETE ON entry"),
             (f"{BF}/db_init.sql", r"不能修改或删除它的分录")))

check(15, "periods.py retained is None 时 fail-closed（§3.16）",
      ev(f"{BA}/periods.py", r"retained"))

check(16, "organize.py 把 M 判据抽成唯一共享函数（§3.17）",
      ev(f"{BA}/organize.py", r"def .*(gap|tidy|mismatch|has_)[a-z_]*\("))

check(17, "前端 VoucherEdit.vue 抽出 payableEntries()，显示与提交同一集合（§3.23）"
      "—— 判据是**定义处**，不是提到它的 HTML 注释",
      ev("frontend/src/views/voucher/VoucherEdit.vue", r"const payableEntries = computed"))

# ============================================================ 第 2 步
print("\n--- 第 2 步：把「说明白」变成「挡住」 ---")

check(18, "plugins/api.py 写路径挂 require_recent_admin_mfa（§3.2）",
      ev(f"{BP}/api.py", r"require_recent_admin_mfa"))

check(19, "api/system.py create_user 挂 require_recent_admin_mfa（§3.2）",
      ev(f"{BE}/system.py", r"require_recent_admin_mfa"))

check(20, "notify/mfa.py poll_guard 键含 sha256(client_secret)（§3.4）",
      ev(f"{BN}/mfa.py", r"sha256"))

check(21, "notify/mfa.py verify_guard 键以 email_token 摘要为主键（§3.3）"
      "—— 判据是**摘要函数与限流入口**两处（不是 docstring 里那句 email_token）",
      ev_all((f"{BN}/mfa.py", r"def verify_key\("),
             (f"{BN}/mfa.py", r"def verify_guard\(")))

check(22, "api/auth.py 注册重名分支执行等量散列（§3.6）"
      "—— 判据是**那次调用**，不是模块 docstring 里提到 _dummy_hash",
      ev(f"{BE}/auth.py", r"verify_password\(payload\.password, _dummy_hash\(\)\)"))

check(23, "core/security.py verify_password 在存量轮数偏低时补算（§3.5）"
      "—— 判据是**补算那行**（不是文档里那句「轮数」）",
      ev(f"{BF}/security.py",
         r"hashlib\.pbkdf2_hmac\(\"sha256\", raw, salt, _ITERATIONS - iters\)"))

check(24, "api/auth.py 失败计数用行锁串行化（§3.7）—— 机制是 SELECT…FOR UPDATE，"
      "不是报告建议的 UPDATE…RETURNING（见验收记录「机制偏离」）",
      ev(f"{BE}/auth.py", r"with_for_update"))

# ============================================================ 第 3 步
print("\n--- 第 3 步：部署链路止血 ---")

check(25, "deploy.sh ROLLBACK_SCOPE=code 移到 tar 覆盖之前（§3.34）",
      ev("deploy/deploy.sh", r"ROLLBACK_SCOPE"))

# 判据取**真正的那一行赋值**。
#
# **原来取的是 ``mktemp`` 这个词** —— 而两个脚本里都有一句注释
# 「nginx -t 的输出落盘：**必须用 mktemp**」在讲这件事。
# 于是「把 ``NGINX_T_LOG="$(mktemp)"`` 那行删掉、只留注释」，
# 这一项**照样 PASS** —— 判据被注释满足了。
#
# 这正是「只抹代码行、留着注释」那轮反向验证暴露出来的：
# 判据能被注释满足 = 假牙。判据必须指向**赋值本身**。
check(26, "esa-mtls.sh / esa-realip-update.sh 改用 mktemp（§3.35）—— 两个脚本都要改"
      "（判据是**那行赋值**，不是提到 mktemp 的注释）",
      ev_all(("deploy/esa-mtls.sh", r'NGINX_T_LOG="\$\(mktemp\)"'),
             ("deploy/esa-realip-update.sh", r'NGINX_T_LOG="\$\(mktemp\)"')))

# 判据取**那一条指令**（``TimeoutStartSec=1800``）。
#
# **原来取的是 ``TimeoutStartSec`` 这个词** —— 而 unit 文件里有一段注释
# 「TimeoutStartSec：**必须显式给**，不能靠 systemd 的默认值」在讲这件事。
# 于是「把 ``TimeoutStartSec=1800`` 那行删掉、只留注释」，这一项**照样 PASS**。
# systemd 只认指令、不认注释 —— 判据也该如此。
check(27, "systemd 备份 unit 补 TimeoutStartSec（§3.36）—— 两个 unit 都要有"
      "（判据是**那一条指令**，不是提到 TimeoutStartSec 的注释）",
      ev_all(("deploy/systemd/cwq-backup.service", r"^TimeoutStartSec=\d+"),
             ("deploy/systemd/cwq-oss-backup.service", r"^TimeoutStartSec=\d+")))

check(28, "deploy.sh 用 db_backup.py --out（§3.37）",
      ev("deploy/deploy.sh", r"db_backup\.py"))

check(29, "deploy.sh 打印可执行的两步恢复流程（§3.38）",
      ev("deploy/deploy.sh", r"恢复|restore", re.IGNORECASE))

check(30, "deploy.sh 新依赖装进独立 venv（§3.39）",
      ev("deploy/deploy.sh", r"venv"))

check(31, "deploy.sh 保留策略改用 -mtime（§3.40）",
      ev("deploy/deploy.sh", r"-mtime"))

check(32, "deploy.sh 发布后体检补「前端首页可达」；nginx 纳入渲染范围或 die（§3.41）"
      "—— 判据是**那两处检查本身**（不是提到 nginx 的注释）",
      ev_all(("deploy/deploy.sh", r"前端首页取不到"),
             ("deploy/deploy.sh", r"die \"APP_DIR=\$\{APP\} 不是 /opt/cwq")))

check(33, "esa-realip-update.sh 加前缀长度闸（§3.43）",
      ev("deploy/esa-realip-update.sh", r"len|长度"))

check(34, "deploy.sh this_ip 为空时 die（§3.45）",
      ev("deploy/deploy.sh", r'if \[\[ -z "\$this_ip" \]\]'))

# ============================================================ 第 4 步
print("\n--- 第 4 步：收紧生产姿态 ---")

check(35, "TRUSTED_PROXIES 由告警升级为阻断（§3.44）—— 判据是**闸门代码**，不是注释",
      ev(f"{BF}/config.py", r"if not trusted_proxy_networks\(settings\.trusted_proxies\)"))

check(36, "mcp_enabled=true 时强制要求 MCP_ALLOWED_HOSTS（§4.3）"
      "—— 判据是**闸门那一行**与那条报错文案（两处都要在）",
      ev_all((f"{BF}/config.py",
              r"if settings\.mcp_enabled and not mcp_allowed_host_list"),
             (f"{BF}/config.py", r"MCP_ENABLED=true 但 MCP_ALLOWED_HOSTS 为空")))

# 与第 6 项同一条事情（§3.8 在 §7 里出现两次：第 0 步与第 4 步）。
# 原来这一项钉的是「条件与**阻断点**都要在」—— 只查 ``if _oss_backup_configured(settings)``
# 这一行的话，把 ``problems.append(...)`` 换成 ``pass`` 照样 PASS。
#
# **2026-10-26：客户端加密那一层整段移除了**（静态加密改由 OSS 服务端加密
# 承担），所以「阻断点」不存在了 —— 留着这条判据就等于要求一个**不存在**的
# 闸门存在，它会永远 FAIL，而那不是「有洞」，是「判据过期了」。
#
# 改成与第 6 项同向的反向判据：客户端加密那层**不在**，且桶的 SSE 状态
# **看得见**。谁把客户端加密加回来，这两项一起 FAIL。
check(37, "备份加密已改为 OSS 服务端加密（2026-10-26）—— 客户端加密那层不在了，"
      "且桶的 SSE 状态看得见",
      ev_removed((f"{BB}/export.py", r"def encryption_key"),
                 (f"{BB}/export.py", r"def encrypt_file"),
                 (f"{BB}/export.py", r"def decrypt_file"),
                 (f"{BF}/config.py", r"^\s*backup_encryption_key\s*:", ),
                 present_specs=((f"{BB}/oss_client.py", r"def server_side_encryption"),
                                (f"{BB}/service.py", r"server_side_encryption")),
                 flags=re.M))

check(38, "esa-mtls.sh 自动探测 vhost 路径与 access_log（§3.42）",
      ev("deploy/esa-mtls.sh", r"grep -E .*access_log"))

# ============================================================ 第 5 步
print("\n--- 第 5 步：前端健壮性 ---")

check(39, "client.js 网络/超时兜底提示 + 账簿三视图 load() 加 try/catch（§3.25）"
      "—— 四处都要在",
      ev_all(("frontend/src/api/client.js", r"ECONNABORTED|网络异常"),
             ("frontend/src/views/ledger/GeneralLedger.vue", r"catch"),
             ("frontend/src/views/ledger/DetailLedger.vue", r"catch"),
             ("frontend/src/views/ledger/BalanceSheet.vue", r"catch")))

check(40, "VoucherOrganize.vue 新增 checkFailed 态，体检失败禁用 RENUM（§3.27）",
      ev("frontend/src/views/voucher/VoucherOrganize.vue", r"checkFailed"))

check(41, "VoucherEdit.vue autoSave 补 failedSig 短路；新建带幂等键（§3.28）—— 两处都要在",
      ev_all(("frontend/src/views/voucher/VoucherEdit.vue", r"failedSig"),
             ("frontend/src/views/voucher/VoucherEdit.vue", r"Idempotency|idempotency")))

check(42, "VoucherEdit.vue onBeforeUnmount 的 flush 与新页 load() 串行化（§3.24）",
      ev("frontend/src/views/voucher/VoucherEdit.vue", r"onBeforeUnmount"))

# 判据是**两条都要成立**：① fmt( 不再出现 ② 改用 reportNumber。
#
# **原来这条判据是反的。** 它写成
# 「有 fmt( 就塞一条『!! fmt( 还在』的证据」，而证据非空 = PASS ——
# 于是「fmt( 又回来了」恰恰会让这一项**显示通过**。判据越严，报告
# 反而越亮，这是最坏的一种假牙。
# 现在改成：fmt( 在 ⇒ 证据为空 ⇒ FAIL，并把原因写进 note。
_fmt_left = find("frontend/src/views/report/ReportView.vue", r"\bfmt\(")
check(43, "ReportView.vue 报表格不再用 fmt（§3.26）—— "
      "判据是**两条都要成立**：fmt( 不再出现 + 改用 reportNumber",
      ev_all(("frontend/src/views/report/ReportView.vue", r"reportNumber"))
      if not _fmt_left else [],
      note=("fmt( 又出现在 ReportView.vue 里（行 "
            + ", ".join(str(h[0]) for h in _fmt_left[:3]) + "）—— 报表格必须用 reportNumber"
            if _fmt_left else None))

check(44, "VoucherSplitDialog / ReportView / SubjectTree 补在途守卫（§3.29 / §3.30）"
      "—— 三个都要在",
      ev_all(("frontend/src/components/VoucherSplitDialog.vue", r"submitting"),
             ("frontend/src/views/report/ReportView.vue", r"structureBusy"),
             ("frontend/src/views/subject/SubjectTree.vue", r"busy")))

check(45, "VoucherEdit.vue rowEls 改用下标键（§3.31）",
      ev("frontend/src/views/voucher/VoucherEdit.vue", r"rowEls"))

check(46, "useCellSelection.js 清 current + tsvOf() 校验 isConnected（§3.32）—— 两处都要在",
      ev_all(("frontend/src/composables/useCellSelection.js", r"isConnected"),
             ("frontend/src/composables/useCellSelection.js", r"current\s*=\s*null")))

check(47, "「换账套/换期间」的提示上移到 MainLayout（§3.33）—— 记号存在 store，重挂后取"
      "—— 三处都要在",
      ev_all(("frontend/src/stores/session.js", r"takePrevBookId\("),
             ("frontend/src/layouts/MainLayout.vue", r"store\.prevBookId = 0"),
             ("frontend/src/views/voucher/VoucherEdit.vue", r"store\.takePrevBookId\(\)")))

# ============================================================ 第 6 步
print("\n--- 第 6 步：插件隔离模型改造 ---")

check(48, "插件后端代码移到独立进程执行，只经 IPC（§3.1）—— 进程句柄与隔离加载都要在",
      ev_all((f"{BP}/ipc/host.py", r"class WorkerHandle"),
             (f"{BP}/loader.py", r"plugin_isolation|_load_isolated")))

check(49, "（次选）裁剪过的 __builtins__ + 禁止包内非 .py 文件参与 exec —— "
      "即便走了首选（独立进程），沙箱里这两道也补上了 —— 三处都要在",
      ev_all((f"{BP}/sandbox.py", r"def _build_restricted_builtins"),
             (f"{BP}/sandbox.py", r'if target\.suffix != "\.py"'),
             (f"{BP}/sandbox.py", r'module\.__dict__\["__builtins__"\]')))

check(50, "scan_policy.py 的模块说明不再声称已闭环那起事故",
      ev(f"{BP}/scan_policy.py", r"不要再|不再声称|事故"))

# ============================================================ 第 7 步
print("\n--- 第 7 步：把「同一个量只允许有一个权威实现」变成 CI 规则 ---")

check(51, "CI 规则：单一权威实现（后端 + 前端各一条结构性防回归用例）—— 两条都要在"
      "（前端判据取**真正的 test( 调用**，不是文件里提到 test 的注释）",
      ev_all(("backend/tests/test_single_source_of_truth.py", r"def test_"),
             ("frontend/tests/single_source.test.mjs", r"^test\(")))

# ============================================================ 汇总
print("\n" + "=" * 78)
passed = [r for r in RESULTS if r[2]]
failed = [r for r in RESULTS if not r[2]]
print(f"通过 {len(passed)} / 失败 {len(failed)}")
if failed:
    print("\nFAIL 明细：")
    for num, desc, _ in failed:
        print(f"  {num}. {desc}")

# **退出码必须反映结论。** 原来这里无条件 ``sys.exit(0)`` —— 于是这个脚本
# 只能**给人看**，不能当闸门：50 项全 FAIL 时它照样返回 0，
# CI 里挂上去也是绿的。脚本的价值一半在它打印的那张表，一半在它**能不能
# 让流水线变红** —— 后一半没做，前面那一半就成了「一张需要人记得去看的表」。
#
# 0 = 全过；1 = 有项目 FAIL。退出码是这份脚本对外的**唯一机器可读结论**，
# 跟表里的 PASS/FAIL 是同一件事的两种表示，不该各说各话。
sys.exit(1 if failed else 0)
