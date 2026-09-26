# AGENTS.md — workbuddy-credits-ws 维护说明

WorkBuddy 积分工作台 skill：查询积分 / 自动签到 / 过期浪费分析 + 本地可视化工作台。

> 本文件只放**每次动手都要用**的内容（它常驻上下文，每行都贵）。
> **项目知识全在 [`references/dev-notes.md`](references/dev-notes.md)** ——
> 那是本项目的单一知识源：数据分层与口径、取数通道、关键约束、环境踩坑、发布流程、当前状态。

## 动手前先读 dev-notes

命中任一条就先去读，别凭印象改：

- 动 `workbuddy_credits.py` / `browser_bridge.py` / `serve.py` / `dashboard.html`
- 遇到：口径对不上、接口报错、401/403、面板转圈或报红
- 准备提交或发版

## 红线（任何改动都不得违反）

1. **提示词永不落盘**：请求明细的 `input` / `inputTrunc` 只活在内存里，**每个落盘通道**
   （`cmd_render` 首行、`serve.py` 的 `/dashboard_data.js` 路由）都必须过 `strip_prompts()`。
   *自查：全局搜 `strip_prompts`，确认每个写盘路径都覆盖到。*
2. **凭据只留在浏览器里**：取数走浏览器桥，`session` cookie 由浏览器自己携带，
   本进程只接收 JSON。任何"读取 / 解密 / 落盘凭据"的代码都不进本仓库。
3. **改完同步到已安装目录**，否则跑的是旧码 —— `~/.workbuddy/skills/workbuddy-credits`
   （复制安装，无 `.git`）。清单 = `SKILL.md` / `README.md` / `manifest.yaml` / `dashboard.html` /
   `.gitignore` / `scripts/*.py` / `assets/*` / `references/*`，**不含** `dist/`。
   **新增的文件也要一并同步。**

## 常用命令

```bash
python scripts/serve.py [端口]        # 本地工作台服务（默认 8090）
python scripts/launcher.py            # 起服务 + 打开浏览器（含版本自检）
python scripts/launcher.py --gen-vbs  # 生成双击启动器
python scripts/workbuddy_credits.py            # 人类可读汇总
python scripts/workbuddy_credits.py status     # 签到状态
python scripts/workbuddy_credits.py render     # 生成 dashboard_data.js
python scripts/workbuddy_credits.py sync --days 31   # 归档请求明细（**窄窗铁律**）
python scripts/build_dist.py          # 打包到 dist/（与 CI 同一个脚本）
```

用**系统 Python**（装了 Playwright）跑；WorkBuddy 内置 Python 没有 —— 见 dev-notes「解释器陷阱」。

## 改动的验收判据

- **数据 / 口径类**：拿真实数据跑通，并确认关键不变式仍成立（如 `today_used == today_used_l5`）
- **前端类**：起服务后用无头浏览器实测页面，确认 **JS 错误 0 条**、关键卡片文案正确
- **交付前用用户的实际入口再验一次**（双击启动器链路），别只测 `serve.py` —— 两者解释器不同

## 提交与发布

- **默认改完不提交**：完成并验证后交给用户验收，等明确指示再 `git add` / `commit`
- 发布顺序不能错：`manifest.yaml` 升版本 → **先写好 README 的 `### vX.Y.Z — 摘要` 段**
  → commit / push → `git tag vX.Y.Z` → 推 tag 触发 CI 建 Release。
  Release 正文取自 README；写晚了就只有一行 Full Changelog

## 约定

**与本项目相关的记忆、规则、踩坑，一律写进 `references/dev-notes.md`**（跟着仓库走，
跨设备可见），不写工作区或宿主的记忆目录。

本仓库是 **public**：dev-notes 与一切提交内容只写可公开的信息 ——
不含凭据 / token / cookie、账号 UIN、个人积分数字、本机绝对路径。
