# 自·积分工作台（workbuddy-credits）

一个查询、监控、分析 WorkBuddy / CodeBuddy 积分的工具包：积分余额与批次明细、每日签到（命令行或工作台按钮，无需打开 WorkBuddy）、逐包精确消耗追踪、收支账本、可视化工作台、到期提醒、双击启动器与桌面/开始菜单快捷方式。

> 零第三方依赖（仅 Python 3 标准库），基于本机登录态（accessToken）直接调用官方接口，无需抓包。

---

## ⚡ 快速开始：对 Agent 说这些话

安装后，你**不需要记任何命令**，直接对 WorkBuddy 的 Agent（AI 助手）说人话即可。Agent 会根据触发词自动加载本 skill 并执行对应操作：

| 你想做什么 | 对 Agent 说 | Agent 会做什么 |
|-----------|------------|--------------|
| 查余额 | 「我的积分」「积分余额」「还剩多少积分」 | 拉取并汇总当前可用积分与批次 |
| 查明细 | 「积分明细」「帮我看看各批次」 | 逐批次列出用量/到期时间 |
| 到期提醒 | 「积分快过期了」「哪些积分要到期了」 | 检查并列出近期到期批次 |
| 自动签到 | 「签到领积分」「帮我签到」 | 调用签到接口（幂等，已签则跳过）；也可在工作台点「立即签到」 |
| 打开看板 | 「打开工作台」「打开积分工作台」「启动工作台」「打开积分看板」 | 后台启动本地服务，打开可视化看板（可实时刷新） |
| 消耗分析 | 「分析积分消耗」「消耗趋势」 | 分析过期浪费 / 趋势 / 签到效率 |
| 导出列表 | 「导出积分列表」「导出一份积分清单」 | 生成 Markdown / CSV 清单 |
| 创建快捷方式 | 「创建桌面快捷方式」「创建开始菜单快捷方式」 | 生成 .lnk 快捷方式（指向双击启动器，图标为青柠圆角） |
| 部署自动化 | 「部署积分自动化」「自动签到」 | 创建「每日自动签到」定时任务（见下） |

> 触发词已写入 `SKILL.md` 的 `description` 字段，Agent 据此自动路由，无需手动指定 skill。

---

## 一、安装 skill

### 方式 A：Git 克隆（推荐）

```bash
git clone https://github.com/SkylerCook/workbuddy-credits-ws.git ~/.workbuddy/skills/workbuddy-credits
```

> 或直接下载仓库内 `dist/workbuddy-credits.zip` 离线解压安装（见方式 B）。

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

前置：**必须先登录 WorkBuddy 客户端**（登录态文件存在、accessToken 未过期；accessToken 约 60 天有效，失效后打开一次 WorkBuddy 即自动刷新登录态，工作台随之恢复）。

在终端运行（用 `python3` 或 `python`）：

```bash
python ~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py        # 应输出余额与批次
python ~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py status # 应输出签到状态
```

两条都正常即安装成功。也可以直接对 Agent 说「我的积分」验证。

---

## 三、部署自动化任务（可选）

**自动化任务不随 skill 打包分发**，安装后需单独部署。

### 一句话部署

对 WorkBuddy 说：

> 帮我部署积分工作台的自动化任务

Agent 会创建「每日自动签到」定时任务（每天 09:00，幂等，已签则跳过）。

> **到期提醒不再靠定时巡检**——已内建到工作台：打开工作台首次加载时，若有即将到期的积分会弹框提示（阈值默认 24 小时，可在工作台「提醒设置」自定义，支持关闭）。零后台开销、零打扰。

### 手动创建

在 WorkBuddy「自动化」界面手动新建，任务名称、调度（rrule）、prompt 完整配置见 `references/automation.md`。

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

- 总览卡片（可用积分、今日已用、预计可用天数、累计消耗、即将到期、过期浪费、签到状态、登录态有效期）
- 每日/累计消耗图、消耗热力图（日期×小时）
- 收支账本（日粒度，自积累）
- 最近会话消耗明细、过期浪费统计、批次到期表
- 顶部使用建议、一键导出（批次 CSV / 消耗 CSV）
- 首次加载到期提醒弹框（阈值默认 24h，可自定义/关闭）
- 顶部工具栏「立即签到」「桌面快捷方式」「开始菜单快捷方式」按钮
  - 「立即签到」未签到时高亮强调（青柠渐变 + 呼吸光晕），点击即签到并自动刷新数据；已签到时自动置灰禁用并显示「今日已签到」，避免重复

> 更省事：直接对 Agent 说「打开工作台」，它会自动起服务 + 打开看板。

### 双击启动 / 快捷方式（脱离 WorkBuddy 桌面端）

工作台服务默认由 Agent 在会话内起，退出 WorkBuddy 桌面端即失效。想要「退出桌面端也能用」，用独立进程方案：

- **双击启动器**：`~/.workbuddy/launchers/start-credits-dashboard.vbs`（首次打开工作台时 Agent 自动生成）。双击即起服务（独立进程常驻，脱离桌面端）+ 打开系统浏览器。
- **桌面 / 开始菜单快捷方式**：
  - 工作台顶部工具栏点「桌面快捷方式」「开始菜单快捷方式」按钮；
  - 或对 Agent 说「创建桌面快捷方式」「创建开始菜单快捷方式」。
  - 快捷方式指向双击启动器 VBS，图标用内置青柠图（多尺寸圆角）。

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
│   ├── serve.py                  # 本地可刷新工作台服务
│   ├── launcher.py               # 双击启动器（独立进程常驻 + 生成 VBS）
│   └── shortcut.py               # 创建桌面/开始菜单快捷方式
├── assets/
│   ├── echarts.min.js            # 离线图表库
│   ├── workbuddy-logo.png        # 工作台 logo（256px）
│   ├── favicon.png               # 浏览器标签栏图标（64px 圆角）
│   └── workbuddy-logo.ico        # 快捷方式图标（多尺寸圆角）
└── references/
    ├── api.md                    # 接口端点、请求头、字段映射
    └── automation.md             # 每日签到自动化任务的完整配置模板
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
