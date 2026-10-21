# tools/uitest —— 一次性 UI 探测脚本

这些是审查期间用来看真实界面状态的一次性探针（截图、点页面、导出文件），
**不是回归测试**，也不进 CI。正式的前端回归测试在 `frontend/tests/`，
由 `cd frontend && npm run test` 执行（Node 内置 test runner）。

## 依赖什么

* `playwright`（声明在**仓库根**的 `package.json`，不在这里 —— 原来这些脚本散在根目录，
  却没有任何清单声明依赖，克隆下来一个都跑不了）
* 一个已经起来的本机环境：后端 `:8000` + 前端 `:5173`（`./dev.sh`）
* 部分脚本还会把截图写到仓库根的 `.shots/`（已被 `.gitignore` 忽略）

## 怎么跑

依赖装在**仓库根**，所以必须在仓库根执行，脚本路径写全：

```bash
npm install                 # 仓库根，装 playwright
npx playwright install chromium
./dev.sh                    # 另开一个终端，把后端和前端起起来
node tools/uitest/ui_voucher.mjs
```

**必须从仓库根跑。** 有脚本按相对路径读仓库里的文件（例如读
`frontend/src/stores/session.js`），换个 cwd 就找不到。

## 关于里面几个路径

* 少数脚本（`ui_list` / `ui_nav` / `ui_subject` / `ui_voucher` / `ui_voucher_insert`）
  用 `import.meta.url` 定位截图目录。这里写的是 `../../.shots/`，为的是搬进
  `tools/uitest/` 之后**输出位置不变**，仍然落在仓库根的 `.shots/`。
* 还有几个脚本（`shot*` / `period_verify` / `final`）把输出目录写成了
  作者机器上的绝对路径。换机器时改脚本里那一行 `const OUT` 即可，
  不影响其他脚本。

## 为什么不逐个修

这些脚本是一次性的调查产物，本身没有回归价值：界面早变了，脚本也早失效了。
它们的价值只有「留个记录，方便复查当时看到了什么」，所以保持原样、不进 CI。
要长期守住某个界面行为，正确做法是在 `frontend/tests/` 里补一个用例。

## 唯一的例外：`ui_year_chart.mjs`

`ui_year_chart.mjs`（年度账套的科目表页实测）是上面那条规矩的**例外** ——
它是**可重复跑**的回归检查，不是一次性探针，而且它守的正是
「每个年度有自己的科目表」这条设计在**真实浏览器**里的行为：

```
✓ 默认显示 2026 年度科目表
✓ 切到 2027 年：工具栏跟着变，科目表是空的
✓ 导入 2027 年度的科目表
✓ 确认框里写明是 2027 年度
✓ 2027 年出现新科目（编码是 2027 自己那套）
✓ 切回 2026 年：科目表一行都没变
```

它**需要一套准备好的环境**（一个已迁移到 head、2027 年还没有科目表的库 +
2027 年的 12 个期间），准备步骤写在脚本头部。它用**独立的验证库**
`cwq_uicheck`，**不碰开发库 `cwq`**。

```bash
cd backend && .venv/bin/python ../tools/uitest/reset_uicheck_2027.py   # 重跑前清场
cd .. && node tools/uitest/ui_year_chart.mjs
```

它没进 CI 的理由与其它脚本不同：CI 里没有浏览器。它进 CI 的条件是
「CI 能起 playwright + 一个可丢弃的库」。
