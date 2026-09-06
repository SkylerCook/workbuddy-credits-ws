---
name: workbuddy-credits
description: 查询 WorkBuddy/CodeBuddy 账户积分（Credits）余额、资源包批次明细（套餐用量/加量包/权益赠送包）与每日签到状态，支持自动签到、余额快照记录、过期浪费分析，并提供可视化积分工作台。当用户询问"我的积分""积分余额""还剩多少积分""积分明细""积分快过期了""签到领积分"等，或需要查询/导出/整理 WorkBuddy 积分时使用。也用于「打开工作台」「打开积分工作台」「启动工作台」「打开积分看板」「看下积分」等打开可视化看板的请求。基于本机登录态（accessToken）直接调用官方接口，无需抓包。
agent_created: true
---

# WorkBuddy 积分工作台

## 概述

读取本机 WorkBuddy 登录态文件（含 `accessToken` / `uid`），直接调用官方积分接口，提供积分查询、自动签到、余额快照、过期浪费分析，以及一个可视化工作台（`dashboard.html`）。

## 如何打开工作台（常用入口）

用户说「打开工作台」时，按以下流程打开可视化看板：

1. 启动本地服务：后台运行 `python scripts/serve.py`（默认端口 8090）
2. 用 `present_files` 打开 `http://127.0.0.1:8090/`（内置浏览器预览，可实时刷新）

若只需静态快照（不开服务），则先 `python scripts/workbuddy_credits.py render` 生成数据，再用 `present_files` 打开 `dashboard_inline.html`（单文件、数据内嵌）。

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

### 3. 关键前提

- 已登录 WorkBuddy 客户端，登录态文件存在且 `accessToken` 未过期（JWT 约 90 天有效，失效后需重新登录再查）。
- 脚本按平台自动定位登录态文件（Windows / macOS / Linux）。

### 4. 安全约束（重要）

- `accessToken` 等同于登录密码：严禁打印明文、提交到代码仓库、贴群、写进公开文章。
- 脚本默认只输出脱敏摘要；快照数据仅存本机 `~/.workbuddy/workbuddy-credits-data/`。

## 自动化任务部署

本 skill 配套 4 个定时任务（自动化**不随 skill 分发**，安装后需单独部署）。当用户要求「部署积分自动化」「设置到期提醒」「自动签到」时，用 `automation_update` 工具（mode=create）创建以下 4 个任务；完整配置（name / rrule / prompt 模板）见 `references/automation.md`。

| 任务 | 调度 |
|------|------|
| 每日自动签到 | 每天 09:00 |
| 到期巡检 36h | 每 6 小时 |
| 到期巡检 12h | 每 6 小时 |
| 每日到期清单汇总 | 每天 09:30 |

注意：prompt 中的脚本路径用 `~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py`，运行用系统 `python3`/`python`，不写死本机绝对路径。

## 接口细节

完整接口文档（端点、请求头、字段映射、关键坑）见 `references/api.md`。排查 401/403 或扩展字段时，先读该文件。

## 资源

- `scripts/workbuddy_credits.py` —— 核心脚本（查询/签到/快照/分析/渲染/账本自积累）
- `scripts/serve.py` —— 本地可刷新工作台服务
- `dashboard.html` + `dashboard_data.js` —— 可视化工作台（静态版）
- `dashboard_inline.html` —— 自含数据的单文件工作台（预览/分享用）
- `assets/echarts.min.js` —— 离线图表库
- `references/api.md` —— 接口端点、请求头、响应字段与分类逻辑说明
- `references/automation.md` —— 4 个自动化任务的 name/rrule/prompt 模板
