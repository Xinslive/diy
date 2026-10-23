"""CI 里 PostgreSQL 客户端必须**用上** 16.x，不是只装上。

2026-10-23：GitHub runner 镜像（ubuntu-latest 迁到 Ubuntu 26）开始自带
PostgreSQL 客户端 18 —— /usr/bin/pg_dump 是 pg_wrapper，指向**最新装上的
那个**。workflow 从 PGDG 源装的是 16，可它的二进制在
/usr/lib/postgresql/16/bin，**不在 PATH 上**。于是：

  · 版本断言查到的是 18.6，`grep -qE " 16\\."` 不匹配 → 这一步直接
    exit 1，后面的全量 pytest 根本没跑（backend 闸门是「未验证」，不是
    「代码红」）；
  · 就算断言被改松，备份/恢复测试（test_db_backup_restore.py）调到的
    仍然是 18 生成的 dump，PG 16 服务端不认里面的版本特有参数。

所以修法是**把 16 顶到 PATH 最前面**，并用 $GITHUB_PATH 让**后续步骤**
（全量 pytest）也用上它 —— 钉版本的真正含义是「用上」，不是「装上」。

这里守住四件事：PATH 顶在前面、$GITHUB_PATH 写给后续步骤、版本断言仍然
在（不许为了「让它变绿」把断言删掉）、客户端主版本跟服务端镜像一致。
"""
from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# run: | 块的正文缩进（与 ci.yml 里的写法一致：run: | 在 8 列，正文 10 列）
_RUN_BODY_INDENT = 10


def _ci_text() -> str:
    assert CI_YML.exists(), f"找不到 {CI_YML} —— 仓库结构变了？"
    return CI_YML.read_text(encoding="utf-8")


def _step_run_block() -> str:
    """CI 里「装 PostgreSQL 客户端」这一步的 run 脚本正文。"""
    lines = _ci_text().splitlines()
    hits = [n for n, l in enumerate(lines) if l.strip().startswith("- name: 装 PostgreSQL 客户端")]
    assert hits, "ci.yml 里找不到「装 PostgreSQL 客户端」这一步"
    i = hits[0]
    j = next(n for n in range(i, len(lines)) if lines[n].strip() == "run: |")
    body: list[str] = []
    for line in lines[j + 1:]:
        if line.strip() == "":
            body.append(line)
            continue
        if len(line) - len(line.lstrip()) < _RUN_BODY_INDENT:
            break
        body.append(line)
    assert body, "「装 PostgreSQL 客户端」这一步没有 run 脚本"
    return "\n".join(body)


def test_pg16_bin_dir_is_prepended_to_path():
    """16 必须在 PATH 最前面 —— 否则断言与后续测试用的都是镜像自带的 18。"""
    body = _step_run_block()
    assert 'PG16_BIN="/usr/lib/postgresql/16/bin"' in body,         "没钉住 16 客户端的目录"
    assert 'export PATH="$PG16_BIN:${PATH}"' in body,         "16 客户端没有顶到 PATH 最前面 —— 镜像自带的 18 仍然排在前面"


def test_pg16_bin_is_exported_to_later_steps():
    """全量 pytest 是**后续步骤**，它也得用 16：备份/恢复测试要真的调 pg_dump。"""
    body = _step_run_block()
    assert 'echo "$PG16_BIN" >> "$GITHUB_PATH"' in body,         "只在本步改了 PATH，后续步骤（pytest）用的还是 18"


def test_version_assertions_are_still_there():
    """不许为了「让它变绿」把版本断言删掉或放松。"""
    body = _step_run_block()
    assert 'pg_dump --version | grep -qE " 16\\."' in body, "pg_dump 的 16.x 断言没了"
    assert 'psql --version | grep -qE " 16\\."' in body, "psql 的 16.x 断言没了"


def test_client_major_matches_service_image():
    """客户端主版本必须跟服务端镜像同一个 —— 钉错了照样出事。"""
    text = _ci_text()
    images = re.findall(r"image:\s*postgres:(\d+)", text)
    assert images, "ci.yml 里找不到 postgres 服务镜像"
    body = _step_run_block()
    for major in set(images):
        assert f'PG16_BIN="/usr/lib/postgresql/{major}/bin"' in body, \
            f"服务端是 postgres:{major}，客户端却钉在别的版本上"
