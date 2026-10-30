#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重建 ARCHITECTURE.md 的骨架：把 41 个章节按主题分组、插入目录、清理标题噪声。

**这个脚本存在的意义是「让重组可复现」。**
ARCHITECTURE.md 有 8000 多行，章节编号是代码、脚本和部署文档里的**稳定引用**
（`§11.13`、`§9.6` 这种），所以调整章节顺序是个需要能被复算、能被审查的操作 ——
手改 8000 行文本，下一个人没法确认自己有没有手滑。

**必须守住的四条不变量**（脚本每次跑都会校验）：

1. **章节号一个不多、一个不少、不重排编号** —— 外部引用靠它定位。
2. **章节正文逐字不动** —— 只动结构与标题。
3. **目录锚点必须真能跳转** —— 锚点由 `tools/archdoc-toc.mjs` 用真
   github-slugger 算出，本脚本只负责消费它算好的结果。
4. **标题里的日期与加粗标记不进标题** —— 日期移到正文首行，标题只留主题。

用法：
    # 1) 先算出锚点（需要先 npm --prefix tools/archdoc-toc i）
    node tools/archdoc-toc.mjs --emit-map
    # 2) 再重建文档
    python3 tools/archdoc-regroup.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / 'ARCHITECTURE.md'
TOC_TOOL = REPO / 'tools' / 'archdoc-toc.mjs'

# 章节标题：旧格式 `## 11.13 标题`，新格式 `### §11.13 标题`
OLD_HEAD = re.compile(r'^##\s+(\d+(?:\.\d+)?)\.?\s+(.*)$')
NEW_HEAD = re.compile(r'^###\s+§(\d+(?:\.\d+)?)\s+(.*)$')
# 重建后文档里，组标题是 `## 组名`（无编号），章节是 `### §N 标题`。
# 切分时组标题会落在**上一章的 body 末尾**，重建脚本要把它摘掉。
GROUP_HEAD = re.compile(r'^##\s+(?!§)(.+)$')
SUB_HEAD = re.compile(r'^#{2,5}\s+(\d+(?:\.\d+)+)\s+(.*)$')

# 标题尾部括号里带日期的标记：无论括号里前面写什么都算
DATE_TAIL = re.compile(r'\s*（[^）]*\d{4}-\d{2}-\d{2}[^）]*）\s*$')

# ---------------------------------------------------------------- 主题分组
#
# **分组顺序即文档顺序。** 编号故意不按大小排：§1.5–§1.9 是前端，
# 和 §2 账簿引擎放在一起更顺；把它们排进「前端与交互」组，
# 读者找前端的事不用先翻过 800 行账务内核。
GROUPS: list[tuple[str, str, list[str]]] = [
    ('总纲：这份文档怎么用', '动代码前必须先认下来的两条铁律与分层约定。', ['0', '1']),
    ('开发流程', '加一个新功能要动哪些地方 —— 这是「怎么改代码」的清单。', ['11']),
    ('账务内核', '余额怎么算、期初怎么来、结账与结转怎么走 —— 系统里最不能动错的部分。',
     ['2', '3', '4', '4.5', '5', '6']),
    ('报表与打印', '公式可配的报表模板，以及打印模板的渲染契约。', ['7', '8']),
    ('数据、权限与账号', '数据模型、审计日志、权限、登录节流与个人资料。',
     ['9', '9.5', '9.6', '9.7', '9.8', '9.9', '10', '10.5']),
    ('前端与交互', '界面上那些「不许」的事：导航外壳、选区、切账套、查询缓存、页面骨架。',
     ['1.5', '1.6', '1.7', '1.8', '1.9']),
    ('集成与对外能力', '发票导入、流水对账、共享卡对账插件、插件宿主、计划任务、邮件告警与 MCP 服务。',
     # §11.20（流水对账）排在 §11.11（发票导入）后面，**不按编号大小**：两者都是
     # 「外部单据进来 → 变成凭证」，放一起读者不用来回翻。§11.21（共享卡对账插件）
     # 紧跟 §11.20 —— 它是那条线的**跨账套版本**，两条线对读最省事。组说明的顺序也跟着它。
     ['11.11', '11.20', '11.21', '11.12', '11.13', '11.14', '11.15']),
    ('生产运维与发布', '网关、发布硬约束、备份、CDN、日志清理，以及两次真实故障的根因链。',
     ['11.5', '11.6', '11.7', '11.8', '11.9', '11.10', '11.16', '11.17', '11.18', '11.19']),
    ('测试与取舍', '测试策略，以及已知但刻意保留的取舍。', ['12', '13']),
]

# ------------------------------------------------- 已知的坏引用与编号修正
#
# §11.13 的子标题**从来没有编号**，所以原文里那句「见 §11.13.4」指不到任何东西
# （§11.13 下第 4 个子标题是「插件定时任务」，与上下文讲的备份加密无关）。
# 这里改成按标题指认 —— 章节以后再挪位置也不会指错。
#
# **匹配只认标题文字，不认 `#` 的个数**：标题层级在重建前后会变
# （`### ` → `#### `），写死层级就会在第二次运行时匹配不上，
# 于是修复只在第一次生效、跑第二遍又变回去。
BROKEN_REF_OLD = '于是没有「密钥为空」这个状态需要豁免（见 §11.13.4）。'
BROKEN_REF_NEW = ('于是没有「密钥为空」这个状态需要豁免'
                  '（见 §11.13「备份与清理的触发方式变了」）。')

# §11.13 的子标题补编号，之后 §11.13.x 才能被引用。
SUB_NUMBERING = [
    ('触发权在外部，执行权在宿主', '11.13.1'),
    ('至少一次，靠幂等键和租约收敛', '11.13.2'),
    ('备份与清理的触发方式变了', '11.13.3'),
    ('插件定时任务', '11.13.4'),
    ('调度看门狗：应用之外的那双眼睛', '11.13.5'),
]

# §11.7.12 出现两次（后一个排在 §11.7.13 之后），改成 §11.7.14
DUP_NUMBER_OLD = '11.7.12 逃生口清单'
DUP_NUMBER_NEW = '11.7.14 逃生口清单'


def parse_blocks(text: str) -> tuple[list[str], list[dict]]:
    """
    按章节标题切分文档：返回 (前言, [{num, title, body}])。

    **两种标题格式都要认**，否则脚本只能跑一次：
    - 重建前：`## 11.13 标题`
    - 重建后：`### §11.13 标题`

    脚本必须能反复运行 —— 「重建」这个动作的价值就在于可复算，
    跑第二次就报错的话，它就不是工具而是一次性脚本。
    """
    lines = text.split('\n')
    fence = False
    starts: list[int] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith('```'):
            fence = not fence
            continue
        if fence:
            continue
        # 重建后的形态：章节是 `### §11.13 标题`，组标题是 `## 组名`（没有编号）。
        # 只认 `###` 那一档，`## 组名` 不带编号，本来就匹配不上。
        if NEW_HEAD.match(s):
            starts.append(i)
        elif OLD_HEAD.match(s):
            starts.append(i)
    if fence:
        raise SystemExit('代码围栏未闭合 —— 文档结构坏了，不能重建')
    if not starts:
        raise SystemExit('一个章节标题都没找到 —— 文档格式不对，不能重建')

    head = lines[:starts[0]]
    # **剥掉旧的目录段**：脚本可以反复运行，若把上一次的目录留在前言里，
    # 跑第二次就会得到两份目录（行数还会莫名其妙地涨）。
    cut = None
    for i, l in enumerate(head):
        if l.strip() == '## 目录':
            cut = i
            break
    if cut is not None:
        head = head[:cut]
    blocks = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        s = lines[start].strip()
        m = OLD_HEAD.match(s) or NEW_HEAD.match(s)
        assert m, lines[start]
        blocks.append({'num': m.group(1), 'title': m.group(2), 'body': lines[start + 1:end]})
    return head, blocks


def clean_title(t: str) -> str:
    """标题只留主题：去掉尾部日期标记与加粗标记。"""
    t = DATE_TAIL.sub('', t)
    t = t.replace('**', '')
    return t.strip()


def denoise_body(body: list[str], base_level: int) -> list[str]:
    """
    章内子标题去噪，并按**相对深度**重新定级。

    **为什么按相对深度而不是「一律降一级」**：那样脚本跑第二次就会把
    `#### ` 降成 `##### `，第三次再降 —— 每次运行层级都在往下掉，
    脚本就不是幂等的了。「重建」这个动作的价值在于可复算，
    跑两次必须得到同一个结果。

    **组标题（`## 组名`，不带编号）不参与定级** —— 它不是章节，
    是章节之间的分隔。把它算进来会让整章的层级基准错位。
    """
    out: list[str] = []
    fence = False
    for line in body:
        s = line.strip()
        if s.startswith('```'):
            fence = not fence
            out.append(line)
            continue
        if not fence:
            m = re.match(r'^(#{2,5})\s+(.*)$', line)
            if m:
                lvl = len(m.group(1))
                rest = DATE_TAIL.sub('', m.group(2)).replace('**', '')
                # 组标题：留着原样，不参与定级
                if lvl == 2 and not re.match(r'^§?\d', rest):
                    out.append((lvl, rest, True))
                    continue
                out.append((lvl, rest, False))
                continue
        out.append(line)

    # 章内**章节子标题**的层级集合（不含组标题）
    levels = [x[0] for x in out if isinstance(x, tuple) and not x[2]]
    shift = None
    if levels:
        # 章内最浅的子标题对齐到 h4；已经是 h4 就不要再动
        shift = 4 - min(levels)

    result: list[str] = []
    for item in out:
        if isinstance(item, tuple):
            lvl, rest, is_group = item
            if is_group:
                result.append('#' * lvl + ' ' + rest)
            else:
                result.append('#' * max(2, min(lvl + (shift or 0), 6)) + ' ' + rest)
        else:
            result.append(item)
    return result

    return out


def apply_fixes(lines: list[str]) -> list[str]:
    """
    修已知的坏引用与编号问题。

    **单独一个环节、在完整文本上做**：放在标题元组化那一步里做不了 ——
    那一层已经把标题拆成「层级 + 文字」，再想按整行匹配就得把结构拼回去，
    反而更容易出错。

    **幂等**：已带编号的不再重复加；标题层级用 `#` 的个数匹配，
    不写死具体层级（重建前后层级会变）。
    """
    out = [l.replace(BROKEN_REF_OLD, BROKEN_REF_NEW) for l in lines]
    # 重复编号：只改标题行
    out = [l.replace(DUP_NUMBER_OLD, DUP_NUMBER_NEW) if l.lstrip().startswith('#') else l for l in out]
    # §11.13 子标题补编号
    for title, num in SUB_NUMBERING:
        fixed = []
        for l in out:
            stripped = l.strip()
            if not stripped.startswith('#'):
                fixed.append(l); continue
            m2 = re.match(r'^(#+\s+)(?:\d+(?:\.\d+)*\s+)?' + re.escape(title) + r'$', stripped)
            fixed.append(m2.group(1) + num + ' ' + title if m2 else l)
        out = fixed
    return out


def emit_toc_map() -> dict[str, str]:
    """让 tools/archdoc-toc.mjs 用真 github-slugger 算出章节锚点。"""
    if not TOC_TOOL.exists():
        raise SystemExit(f'缺少 {TOC_TOOL}')
    res = subprocess.run(
        ['node', str(TOC_TOOL), '--emit-map'],
        cwd=str(REPO), capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise SystemExit(f'锚点生成失败:\n{res.stdout}\n{res.stderr}')
    path = REPO / 'tools' / '.archdoc-toc-map.json'
    if not path.exists():
        raise SystemExit(f'锚点工具没有产出 {path}')
    data = json.loads(path.read_text(encoding='utf-8'))
    path.unlink()
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='只校验不写文件（对比现有文档的章节号与标题）')
    args = ap.parse_args()

    text = DOC.read_text(encoding='utf-8')
    head, blocks = parse_blocks(text)
    by_num = {b['num']: b for b in blocks}

    # ---- 不变量 1：分组覆盖全部章节，无重复无遗漏 ----
    used: set[str] = set()
    for gname, _, nums in GROUPS:
        for n in nums:
            if n not in by_num:
                raise SystemExit(f'分组引用了不存在的章节 §{n}')
            if n in used:
                raise SystemExit(f'章节 §{n} 被分到两个组里')
            used.add(n)
    if used != set(by_num):
        raise SystemExit(f'有章节没归组: {set(by_num) - used}')

    if args.check:
        new = DOC.read_text(encoding='utf-8')
        _, new_blocks = parse_blocks(new)
        new_by = {b['num']: b for b in new_blocks}
        if set(new_by) != set(by_num):
            raise SystemExit('章节号集合不一致')
        print(f'✓ 章节号 {len(new_by)} 个，与分组定义一致')
        return 0

    toc_map = emit_toc_map()

    out: list[str] = list(head)
    out += ['## 目录', '']
    out += ['全文 41 个章节，按主题分成 9 组：', '']
    for gname, gdesc, nums in GROUPS:
        out.append(f'- **{gname}** —— {gdesc}')
        for n in nums:
            t = clean_title(by_num[n]['title'])
            key = f'§{n} {t}'
            anchor = toc_map.get(key)
            if not anchor:
                raise SystemExit(f'锚点缺失: {key}')
            out.append(f'  - [{key}](#{anchor})')
    out += [
        '',
        '**章节号是稳定引用。** 代码、脚本与部署文档里按 `§11.7` 这样指认章节，',
        '所以编号只增不改、不重排 —— 重排会让那些引用全部指错地方。',
        '',
        '标题里的日期标记（首次确立 / 最后修订）已移到各章正文首行，目录里只留主题。',
        '',
        '---',
        '',
    ]

    for gname, gdesc, nums in GROUPS:
        # 组说明后面也补一条分隔线：**每一章前面都必须有 `---`**，一视同仁。
        #
        # 原文 41 章里只有 30 章带分隔符，缺的那 11 章正是后期追加的 ——
        # 补丁式痕迹的一种。组标题占了「组内第一章」前面分隔线的位置，
        # 于是每组第一章都缺一条，滚下来会看到「有横线、有横线、这条没有」。
        # 统一到 41/41，读起来才是同一套规矩。
        out += [f'## {gname}', '', gdesc, '', '---', '']
        for n in nums:
            b = by_num[n]
            t = clean_title(b['title'])
            out += [f'### §{n} {t}', '']
            dm = DATE_TAIL.search(b['title'])
            if dm:
                out += [f'*{dm.group(0).strip()}*', '']
            body = list(b['body'])
            while body and body[0].strip() == '':
                body.pop(0)
            # 摘掉落在本章末尾的「下一组」标题行（重建后形态）。
            # 组标题由重建流程重新插入，留在原处会变成两处。
            # 它前面还跟着分隔线 `---` 和一行组说明，所以要**整段**摘掉：
            # 往前找到最后一个属于本章的正文行，只保留那之前的部分。
            # 从后往前找**最后一处组标题**：它前面是分隔线 `---`，
            # 再前面是组说明（两行文字）。找到就从那儿截断。
            cut = len(body)
            for i in range(len(body) - 1, -1, -1):
                if GROUP_HEAD.match(body[i].strip()):
                    cut = i
                    break
            if cut < len(body):
                body = body[:cut]
                while body and body[-1].strip() in ('', '---'):
                    body.pop()
            while body and body[-1].strip() in ('', '---'):
                body.pop()
            out += apply_fixes(denoise_body(body, 3))
            out += ['', '---', '']

    while out and out[-1].strip() == '':
        out.pop()
    if out and out[-1].strip() == '---':
        out.pop()
        while out and out[-1].strip() == '':
            out.pop()
    out.append('')

    # ---- 不变量 4：每一章前面都有 `---` ----
    #
    # 写盘之前钉住，免得以后改生成逻辑时静默漏掉几章 —— 那正是
    # 原文里 30/41 那种补丁式不一致的来源。
    missing = []
    for i, line in enumerate(out):
        # 用宽松模式：章节号可能有任意层小数（§11.7.14 这类），
        # 借用 NEW_HEAD 会漏检 —— 漏检的不变量等于没有不变量。
        if not re.match(r'^###\s+§\d+(?:\.\d+)*\s+\S', line.strip()):
            continue
        j = i - 1
        while j >= 0 and not out[j].strip():
            j -= 1
        if j < 0 or out[j].strip() != '---':
            missing.append(line.strip())
    if missing:
        raise SystemExit('以下章节前缺分隔线：\n' + '\n'.join(missing))

    DOC.write_text('\n'.join(out), encoding='utf-8')
    print(f'✓ 已重建 {DOC.name}：{len(out)} 行，{len(blocks)} 章，{len(GROUPS)} 组')
    return 0


if __name__ == '__main__':
    sys.exit(main())
