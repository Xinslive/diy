#!/usr/bin/env python3
"""生成 tools/archdoc-external.json —— 外部编号注册表。

**为什么需要它。** 段号契约闸门要判断「仓库里出现的 §N 能不能解析」。
本仓库有好几套并行的编号体系，其中**审计报告那一套定义在 `output/` 下的文档里，
而 `output/` 是 gitignore 的** —— 也就是说：

  · 本地有 output/ → 闸门全绿；
  · CI 的干净 checkout 没有 output/ → 几百处合法引用全判成断链。

这正是**依赖未跟踪文件的飘忽闸门**：本地过了、CI 红了，谁也说不清哪边对。
（实测踩过：CI run 40723257346 的「段号引用契约必须成立」在 runner 上红，
而同样的命令在本机是绿的。）

修法：把那些「属于仓库里没有的文档」的编号**显式登记进版本控制**。
登记之后，编号全集 = 本文档自己的编号 + git 跟踪的 .md 的编号 + 本注册表，
在任何 checkout 上都一模一样。
"""
import json
import re
from pathlib import Path

REPO = Path('/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统')
OUT = REPO / 'tools' / 'archdoc-external.json'

HEAD_RE = re.compile(r'^#{2,6}\s+(?:§\s*)?(\d+(?:\.\d+)*)\.?(?:\s+(.*))?$')

# 仓库里没有、但代码/脚本确实在引用的文档目录
SOURCES = [
    'output/AUDIT-REPORT-2026-10-25.md',
    'output/AUDIT-FIX-VERIFICATION-2026-10-25.md',
    'output/PLUGIN-ISOLATION-PLAN-2026-10-25.md',
    'output/CUTOVER-RUNBOOK.md',
]


def main():
    sources = {}
    for rel in SOURCES:
        p = REPO / rel
        if not p.exists():
            print(f'⚠ 不存在，跳过：{rel}')
            continue
        nums, in_code = [], False
        for line in p.read_text(encoding='utf-8').splitlines():
            if line.startswith('```'):
                in_code = not in_code
                continue
            if in_code:
                continue
            m = HEAD_RE.match(line.strip())
            if m:
                nums.append(m.group(1))
        sources[rel] = sorted(set(nums), key=lambda n: [int(x) for x in n.split('.')])
        print(f'  {rel}: {len(sources[rel])} 个编号')

    data = {
        '_说明': ('段号引用契约闸门的外部编号注册表。这些编号属于**仓库里没有**的文档'
                  '（output/ 被 gitignore），但代码、脚本、部署文档里确实在引用它们。'
                  '登记在这里，闸门的判定才不会依赖「本地恰好有 output/」这种运气。'),
        '_维护': '这些文档新增/删除章节后，跑 python3 tools/archdoc-external.py 重写本文件并提交。',
        'sources': sources,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    total = sum(len(v) for v in sources.values())
    print(f'✓ 已写入 {OUT.relative_to(REPO)}：{len(sources)} 份文档，{total} 个编号')


if __name__ == '__main__':
    main()
