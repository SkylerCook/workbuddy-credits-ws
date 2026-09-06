# 自·积分工作台（workbuddy-credits）

一个查询、监控、分析 WorkBuddy / CodeBuddy 积分的工具包：积分余额与批次明细、每日自动签到、逐包精确消耗追踪、收支账本、可视化工作台、到期提醒自动化。

> 零第三方依赖（仅 Python 3 标准库），基于本机登录态（accessToken）直接调用官方接口，无需抓包。

---

## ⚡ 快速开始：对 Agent 说这些话

安装后，你**不需要记任何命令**，直接对 WorkBuddy 的 Agent（AI 助手）说人话即可。Agent 会根据触发词自动加载本 skill 并执行对应操作：

| 你想做什么 | 对 Agent 说 | Agent 会做什么 |
|-----------|------------|--------------|
| 查余额 | 「我的积分」「积分余额」「还剩多少积分」 | 拉取并汇总当前可用积分与批次 |
| 查明细 | 「积分明细」「帮我看看各批次」 | 逐批次列出用量/到期时间 |
| 到期提醒 | 「积分快过期了」「哪些积分要到期了」 | 检查并列出近期到期批次 |
| 自动签到 | 「签到领积分」「帮我签到」 | 调用签到接口（幂等，已签则跳过） |
| 打开看板 | 「打开工作台」「打开积分工作台」「启动工作台」「打开积分看板」 | 后台启动本地服务，打开可视化看板（可实时刷新） |
| 消耗分析 | 「分析积分消耗」「消耗趋势」 | 分析过期浪费 / 趋势 / 签到效率 |
| 导出列表 | 「导出积分列表」「导出一份积分清单」 | 生成 Markdown / CSV 清单 |
| 部署自动化 | 「部署积分自动化」「设置到期提醒」「自动签到」 | 创建 4 个定时任务（见下） |

> 触发词已写入 `SKILL.md` 的 `description` 字段，Agent 据此自动路由，无需手动指定 skill。

---

## 一、安装 skill

### 方式 A：Git 克隆（推荐）

```bash
git clone <仓库地址> ~/.workbuddy/skills/workbuddy-credits
```

克隆后确认最终结构为 `~/.workbuddy/skills/workbuddy-credits/SKILL.md`，重启 WorkBuddy 客户端（或等它自动刷新）即可。

### 方式 B：解压安装（离线）

1. 解压 `workbuddy-credits.zip`，得到 `workbuddy-credits/` 目录。
2. 放到用户级 skills 目录：

   | 系统 | 路径 |
   |------|------|
   | Windows | `C:\Users\<你的用户名>\.workbuddy\skills\` |
   | macOS / Linux | `~/.workbuddy/skills/` |

3. 确认最终结构为 `~/.workbuddy/skills/workbuddy-credits/SKILL.md`。
4. 重启 WorkBuddy 客户端（或稍等自动刷新），在「技能管理」中确认已启用。

### 方式 C：URL 导入

「技能管理 → 通过 URL 导入」→ 填入仓库地址，自动拉取。

---

## 二、验证安装

前置：**必须先登录 WorkBuddy 客户端**（登录态文件存在、accessToken 未过期）。

在终端运行（用 `python3` 或 `python`）：

```bash
python ~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py        # 应输出余额与批次
python ~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py status # 应输出签到状态
```

两条都正常即安装成功。也可以直接对 Agent 说「我的积分」验证。

---

## 三、部署自动化任务（重要）

**自动化任务不随 skill 打包分发**，安装后需单独部署。

### 方式 A：一句话部署（推荐）

对 WorkBuddy 说：

> 帮我部署积分工作台的自动化任务

Agent 会自动创建以下 4 个定时任务：

| 任务 | 调度 | 作用 |
|------|------|------|
| 每日自动签到 | 每天 09:00 | 自动签到领积分 |
| 到期巡检（36 小时） | 每 6 小时 | 提醒 36h 内到期批次 |
| 到期巡检（12 小时） | 每 6 小时 | 提醒 12h 内到期批次 |
| 每日到期清单汇总 | 每天 09:30 | 每日汇总 + 快照 + 建议 |

### 方式 B：手动创建

在 WorkBuddy「自动化」界面手动新建，各任务的名称、调度（rrule）、prompt 完整配置见 `references/automation.md`。

---

## 四、命令行使用

```bash
python .../workbuddy_credits.py              # 汇总 + 批次明细
python .../workbuddy_credits.py status       # 签到状态
python .../workbuddy_credits.py checkin      # 自动签到（幂等）
python .../workbuddy_credits.py record       # 记录余额快照 + 自积累账本
python .../workbuddy_credits.py analyze      # 分析（浪费/趋势/签到效率）
python .../workbuddy_credits.py usage        # 消耗明细（今日/按天/会话）
python .../workbuddy_credits.py expire-check 36  # 检查 N 小时内到期批次
python .../workbuddy_credits.py render       # 生成工作台数据
python .../workbuddy_credits.py --export     # 导出积分列表 Markdown
python .../workbuddy_credits.py --json       # 原始 JSON
```

运行要求：Python 3（`python` 或 `python3`），无需 pip 安装任何依赖。

---

## 五、可视化工作台

### 可刷新版（推荐）

```bash
python .../serve.py          # 启动本地服务（默认 8090）
```

浏览器打开 `http://127.0.0.1:8090`，页面右上角「刷新数据」按钮可实时重拉最新数据。工作台包含：

- 总览卡片（可用积分、今日已用、预计可用天数、累计消耗、即将到期、过期浪费、签到状态）
- 每日/累计消耗图、消耗热力图（日期×小时）
- 收支账本（日粒度，自积累）
- 最近会话消耗明细、过期浪费统计、批次到期表
- 顶部使用建议、一键导出（批次 CSV / 消耗 CSV）

> 更省事：直接对 Agent 说「打开工作台」，它会自动起服务 + 打开看板。

### 静态版

先 `render` 生成数据，再用浏览器打开 `dashboard.html`；`dashboard_inline.html` 为自含数据的单文件版，适合预览/分享。

### 数据存储

自积累数据存 `~/.workbuddy/workbuddy-credits-data/`（余额快照、逐包消耗历史、签到历史、包到账事件）。**不写** WorkBuddy 客户端自己的 `workbuddy.db`。

---

## 六、目录结构

```
workbuddy-credits/
├── SKILL.md                      # skill 定义（触发词、操作流程）
├── README.md                     # 本文件
├── manifest.yaml                 # 市场清单（备选）
├── dashboard.html                # 工作台页面（可刷新版模板）
├── scripts/
│   ├── workbuddy_credits.py      # 核心脚本（查询/签到/快照/分析/渲染/账本）
│   └── serve.py                  # 本地可刷新工作台服务
├── assets/
│   ├── echarts.min.js            # 离线图表库
│   └── workbuddy-logo.jpg        # 工作台图标
└── references/
    ├── api.md                    # 接口端点、请求头、字段映射
    └── automation.md             # 4 个自动化任务的完整配置模板
```

---

## 七、常见问题

**Q：提示「未找到登录态文件」？**
先登录 WorkBuddy 客户端，再运行脚本。

**Q：accessToken 失效（401 / 登录态失效）？**
重新登录 WorkBuddy 客户端即可自动刷新 token（约 90 天有效）。

**Q：工作台图表空白？**
若用 `serve.py` 方式，直接点「刷新数据」；若静态打开，先运行 `render` 生成 `dashboard_data.js` 再刷新。

**Q：自动化任务里 python 找不到？**
用 WorkBuddy 自带的 Python（`~/.workbuddy/binaries/python/versions/` 下最新版本目录中的 `python.exe`），或安装系统 Python 3。

**Q：消耗趋势图为什么只有最近约 17 天？**
逐日消耗采用「逐包追踪」口径（从安装后开始自积累，突破 17 天窗口）。历史消耗仅能从本地 `session_usage` 表近似回溯（会话级时间戳、约 17 天），属正常。

**Q：签到收入和裂变包会不会重复计算？**
不会。签到奖励本身以「size==daily_credit 的裂变包」形式发放，账本已做去重（`build_ledger` 排除签到奖励包），只计一次。

---

## 许可

自用工具，代码按现状提供，不附带任何担保。使用前请确认你所在地区对相关接口调用的合规性。
