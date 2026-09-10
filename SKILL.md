---
name: workbuddy-credits
description: 查询 WorkBuddy/CodeBuddy 账户积分（Credits）余额、资源包批次明细（套餐用量/加量包/权益赠送包）与每日签到状态，支持签到（工作台一键完成，无需打开 WorkBuddy）、余额快照记录、过期浪费分析，并提供可视化积分工作台。当用户询问"我的积分""积分余额""还剩多少积分""积分明细""积分快过期了""签到领积分"等，或需要查询/导出/整理 WorkBuddy 积分时使用。也用于「打开工作台」「打开积分工作台」「启动工作台」「打开积分看板」「看下积分」等打开可视化看板的请求，以及「更新积分 skill」「升级积分工作台」「更新积分工作台」等把本 skill 升级到最新版本的请求。基于本机登录态（accessToken）直接调用官方接口，无需抓包。
agent_created: true
---

# WorkBuddy 积分工作台

## 概述

读取本机 WorkBuddy 登录态文件（含 `accessToken` / `uid`），直接调用官方积分接口，提供积分查询、签到（命令行或工作台按钮，无需打开 WorkBuddy）、余额快照、过期浪费分析，以及一个可视化工作台（`dashboard.html`）。

## 如何打开工作台（常用入口）

用户说「打开工作台」时，按以下流程打开可视化看板：

1. 启动本地服务：后台运行 `python scripts/serve.py`（默认端口 8090）
   - **Windows 沙箱环境排查**：若后台启动后 `/api/data` 报「未找到登录态」（部分 WorkBuddy 沙箱会拦截后台进程读 C 盘登录态），把登录态复制到非 C 盘副本，并用环境变量 `WORKBUDDY_AUTH_FILE` 指向副本后重启：
     ```bash
     cp "$LOCALAPPDATA/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info" "<非C盘副本路径>"
     WORKBUDDY_AUTH_FILE="<非C盘副本路径>" python scripts/serve.py 8090
     ```
2. 首次打开时顺带生成「双击启动器」（幂等，覆盖式无副作用）：
   - 运行 `python scripts/launcher.py --gen-vbs`，探测本机 pythonw 路径，生成 `~/.workbuddy/launchers/start-credits-dashboard.vbs`
   - 生成后明确告知用户：以后无需打开 WorkBuddy，双击该 VBS 即可启动工作台（服务以独立进程常驻，可自行发送到桌面或开始菜单）
3. 用 `present_files` 打开 `http://127.0.0.1:8090/`（内置浏览器预览，可实时刷新）

若只需静态快照（不开服务），则先 `python scripts/workbuddy_credits.py render` 生成数据，再用 `present_files` 打开 `dashboard_inline.html`（单文件、数据内嵌）。

## 如何更新本 skill

用户说「更新积分 skill」「升级积分工作台」时，运行：

```bash
python scripts/update.py           # 检查并更新；若工作台服务在跑则自动重启
python scripts/update.py --check   # 只检查是否有更新，不改动任何文件
```

脚本行为：

1. **优先走 GitHub Releases 包**（本仓库以 Release 作为版本权威）：查 `/releases/latest` → 比对 tag 与本地 `manifest.yaml` 的 `version` → 下载 `workbuddy-credits-v<ver>.zip` → 用同 Release 的 `SHA256SUMS.txt` **校验 sha256**（校验失败拒绝安装）→ 合并式覆盖。
2. **自动检测安装方式**：skill 目录含 `.git` 且有 git → `git pull --ff-only`（增量、可回滚）；否则走上面的 Release 包；Release 不可用时退回源码包覆盖（`git clone --depth 1` → `main.zip`）。
   - 均为**合并式覆盖**，不删除本地已有文件。可用 `--source release|git|archive` 强制指定来源。
3. 对比版本号，报告版本变化、变更文件与 Release 发行说明。
4. **若工作台服务正在运行 → 停掉旧进程并重启**。服务是 DETACHED 常驻进程，不重启不会加载新代码（这是必须的一步）。
5. 重启前会校验监听进程确为 python，避免误杀其它占用 8090 的程序。

网络不通时加 `--proxy <地址>`（默认直连 GitHub）；GitHub API 限流时加 `--token <token>`（或设 `GITHUB_TOKEN`）。用户数据在 `~/.workbuddy/workbuddy-credits-data/`（skill 目录**外**），更新不影响。

`--force` 可在版本号相同时强制重装。发布流程（维护者）见 `README.md`「八、版本与发布」。

> 更新入口同时写在 `README.md`：SKILL.md 里新增的触发词要等**更新之后**才生效（自举问题），README 是用户可复制、Agent 可读的可靠锚点。

## 何时使用

- 用户询问积分余额、剩余积分、积分明细
- 用户想查看各积分批次的到期时间、判断哪些即将过期
- 用户想执行每日签到或查询签到状态
- 用户想统计过期浪费、分析消耗趋势
- 用户想导出积分列表或打开工作台查看

## 使用方式

### 1. 运行脚本

内置脚本 `scripts/workbuddy_credits.py`（零第三方依赖，仅用 Python 标准库，Python 3）：

```bash
python scripts/workbuddy_credits.py                 # 汇总 + 逐批次明细
python scripts/workbuddy_credits.py status          # 每日签到状态
python scripts/workbuddy_credits.py checkin         # 自动签到（幂等，已签则跳过）
python scripts/workbuddy_credits.py record          # 记录当前余额快照
python scripts/workbuddy_credits.py analyze         # 分析（过期浪费/趋势/签到效率）
python scripts/workbuddy_credits.py expire-check 36 # 检查 N 小时内到期批次
python scripts/workbuddy_credits.py usage           # 消耗明细（本地 session_usage，含今日/按天/会话）
python scripts/workbuddy_credits.py render          # 生成工作台数据 dashboard_data.js
python scripts/serve.py [端口]                       # 启动可刷新工作台（浏览器打开 http://127.0.0.1:8090）
python scripts/launcher.py                           # 启动服务（独立进程常驻）+ 打开系统浏览器
python scripts/launcher.py --gen-vbs                 # 生成双击启动器 VBS 到 ~/.workbuddy/launchers/
python scripts/launcher.py --create-shortcut desktop    # 创建桌面快捷方式（指向 VBS）
python scripts/launcher.py --create-shortcut startmenu  # 创建开始菜单「所有应用」快捷方式
python scripts/update.py                             # 更新本 skill 到最新版（自动重启工作台服务）
python scripts/workbuddy_credits.py --export [路径]  # 导出 Markdown 列表（按到期时间升序）
python scripts/workbuddy_credits.py --json          # 原始 JSON
python scripts/workbuddy_credits.py --token         # 登录态摘要（token 脱敏）
```

运行要求：Python 3（`python` 或 `python3`），无需 pip 安装任何依赖。

### 2. 可视化工作台

`dashboard.html` 为工作台页面，展示总览、每日/累计消耗、会话级消耗明细、过期浪费统计与批次到期表。

两种打开方式：

- **可刷新版（推荐）**：运行 `python scripts/serve.py` 启动本地服务，浏览器打开 `http://127.0.0.1:8090`，页面右上角「刷新数据」按钮会实时重新拉取最新积分/消耗。
- **静态版**：运行 `render` 生成 `dashboard_data.js` 后，用浏览器 `file://` 打开 `dashboard.html`（数据为生成时快照）。`dashboard_inline.html` 为自含数据的单文件版，适合预览/分享。

图表库 `assets/echarts.min.js` 已离线内置，无需联网。

工作台顶部工具栏提供「桌面快捷方式」「开始菜单快捷方式」两个按钮：点击后由本地服务在对应位置创建 `.lnk` 快捷方式（指向双击启动器 VBS，等效「双击 VBS」），方便用户把工作台入口固定到桌面或开始屏幕。仅可刷新版（`serve.py`，HTTP）可用；静态版（`file://`）不支持。

工作台内置**到期提醒**：首次加载页面时，若有 7 天内到期的积分批次，会自动弹框提示（列出批次与到期时间，可一键跳转「积分批次」查看）。每次打开工作台检查一次，会话内刷新不重复弹。

工作台顶部另有「立即签到」按钮：**无需打开 WorkBuddy**，页面直接调用官方签到接口完成每日签到（幂等）。**未签到时按钮高亮**（青柠渐变 + 呼吸光晕），点击即完成签到并自动刷新数据；**已签到时按钮自动置灰禁用**，文案变为「今日已签到」，避免重复签到。仅可刷新版（`serve.py`，HTTP）可用；静态版（`file://`）不支持。

工作台**首次加载自动检查签到状态**：若当天未签到，弹框引导点击「立即签到」（弹框内可直接签到）。每次打开工作台检查一次，会话内刷新不重复弹；静态版（`file://`）不弹，避免误导。

**签到入口已统一收敛到工作台**，本 skill 不再部署任何定时任务。详见 `references/checkin.md`。

工作台概览区显示**登录态有效期**（`accessToken` 剩余天数）：剩余 ≤7 天时卡片转警示色并提示「打开一次 WorkBuddy 可续期」；已过期时提示「打开一次 WorkBuddy 即可恢复」。登录态来自本机文件，只要 token 未过期，工作台即可脱离 WorkBuddy 独立完成查询与签到。

### 3. 关键前提

- 已登录 WorkBuddy 客户端，登录态文件存在且 `accessToken` 未过期。
- 有效期：`accessToken` 约 **60 天**，`refreshToken` 约 **90 天**且随每次刷新滚动。失效后**打开一次 WorkBuddy**（启动即自动刷新登录态并写回文件）即可恢复，无需重新扫码；仅当 `refreshToken` 也已过期才需重新登录。
- 脚本按平台自动定位登录态文件（Windows / macOS / Linux）。

### 4. 安全约束（重要）

- `accessToken` 等同于登录密码：严禁打印明文、提交到代码仓库、贴群、写进公开文章。
- 脚本默认只输出脱敏摘要；快照数据仅存本机 `~/.workbuddy/workbuddy-credits-data/`。

## 签到与提醒机制

**不再部署定时任务。** 签到与到期提醒全部收敛到工作台：打开工作台 → 未签到则弹框引导 → 点「立即签到」即完成并自动刷新；到期批次同样在首次加载时弹框提示。

命令行等价用法（脚本保留，供手动或外部调度调用）：

| 命令 | 用途 |
|------|------|
| `workbuddy_credits.py status` | 查询签到状态 |
| `workbuddy_credits.py checkin` | 执行签到（幂等，已签则跳过） |

弃用定时任务的原因与可选替代方案（Windows 任务计划程序）见 `references/checkin.md`。

> 到期提醒**不**用定时任务推送（体感价值低、有后台开销、会累积任务会话），改为工作台内置弹框提示。主动打开工作台即可掌握到期风险，无需被动巡检。

## 接口细节

完整接口文档（端点、请求头、字段映射、关键坑）见 `references/api.md`。排查 401/403 或扩展字段时，先读该文件。

## 资源

- `scripts/workbuddy_credits.py` —— 核心脚本（查询/签到/快照/分析/渲染/账本自积累）
- `scripts/serve.py` —— 本地可刷新工作台服务
- `scripts/launcher.py` —— 通用启动器（独立进程常驻 + 生成双击启动器 VBS）
- `scripts/shortcut.py` —— 创建 .lnk 快捷方式到桌面/开始菜单（零依赖，subprocess 调 PowerShell）
- `scripts/update.py` —— 更新器（GitHub Releases 包 → git pull / 源码包覆盖 → 重启工作台服务）
- `scripts/build_dist.py` —— 打包发布物（本地与 CI 共用；生成 `dist/*.zip` + `SHA256SUMS.txt`）
- `dashboard.html` + `dashboard_data.js` —— 可视化工作台（静态版）
- `dashboard_inline.html` —— 自含数据的单文件工作台（预览/分享用）
- `assets/echarts.min.js` —— 离线图表库
- `references/api.md` —— 接口端点、请求头、响应字段与分类逻辑说明
- `references/checkin.md` —— 签到机制与自动化说明（为何不用定时任务、可选替代方案）
