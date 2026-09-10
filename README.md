# 自·积分工作台（workbuddy-credits）

一个查询、监控、分析 WorkBuddy / CodeBuddy 积分的工具包：积分余额与批次明细、每日签到（工作台一键完成，无需打开 WorkBuddy）、逐包精确消耗追踪、收支账本、可视化工作台、到期提醒、双击启动器与桌面/开始菜单快捷方式。

> 零第三方依赖（仅 Python 3 标准库），基于本机登录态（accessToken）直接调用官方接口，无需抓包。

---

## 🤖 交给 Agent 全流程（安装 → 打开工作台）

**安装**和**打开工作台**都能完全交给 WorkBuddy 的 Agent，不需要你记任何命令。

### ① 让 Agent 完成安装

对 Agent 说（把仓库地址贴进去即可）：

> 帮我安装这个 skill：https://github.com/SkylerCook/workbuddy-credits-ws

Agent 会：

1. 把仓库克隆（或解压仓库内 `dist/workbuddy-credits.zip`）到**用户级 skills 目录**：

   | 系统 | 目标路径 |
   |------|---------|
   | Windows | `C:\Users\<你的用户名>\.workbuddy\skills\workbuddy-credits\` |
   | macOS / Linux | `~/.workbuddy/skills/workbuddy-credits/` |

2. 确认 `SKILL.md` 已就位（最终结构必须含 `~/.workbuddy/skills/workbuddy-credits/SKILL.md`）；
3. 告知安装完成——此后本 skill 的**触发词自动生效**，无需手动指定。

> 前置：已登录 WorkBuddy 客户端（登录态文件存在，`accessToken` 未过期）。验证：对 Agent 说「我的积分」，能返回余额即安装成功。
>
> 若不便让 Agent 安装（或无法联网），手动方式见「一、安装 skill」。

### ② 让 Agent 打开积分工作台

安装完成后，对 Agent 说：

> 打开积分工作台

Agent 会：

1. **后台启动本地服务**（`scripts/serve.py`，默认端口 `8090`）；
2. **首次打开时顺带生成「双击启动器」** `~/.workbuddy/launchers/start-credits-dashboard.vbs`，并提示你：以后**无需打开 WorkBuddy，双击该文件即可启动工作台**（服务以独立进程常驻）；
3. **用内置浏览器打开** `http://127.0.0.1:8090/`，页面可实时刷新。

> 想让入口常驻：对 Agent 说「创建桌面快捷方式」或「创建开始菜单快捷方式」，或直接点工作台顶部工具栏的对应按钮（图标为内置青柠圆角）。

### ③ 让 Agent 更新到最新版

仓库发布新版本后，对 Agent 说：

> 更新积分 skill

Agent 会运行 `scripts/update.py`：自动检测安装方式（Git 克隆 → `git pull`；复制安装 → 下载 zip 覆盖）→ 报告版本变化与变更文件 → **自动重启工作台服务**（服务是常驻进程，不重启不会加载新代码）。

> 只想看有没有新版：说「检查积分 skill 有没有更新」（等价 `update.py --check`，不改动任何文件、不重启服务）。

### Agent 行为速查

| 你说 | Agent 自动执行 | 结果 |
|------|--------------|------|
| 「帮我安装这个 skill：<仓库地址>」 | 克隆/解压到 `~/.workbuddy/skills/workbuddy-credits/`，校验 `SKILL.md` | skill 就绪，触发词生效 |
| 「打开积分工作台」 | 起本地服务 + 生成双击启动器 + 打开看板 | 浏览器显示可视化工作台 |
| 「更新积分 skill」 | 检测安装方式 → `git pull` / zip 覆盖 → 重启服务 | 升级到最新版并生效 |
| 「创建桌面快捷方式」 | 生成指向启动器的 `.lnk`（青柠圆角图标） | 桌面出现可双击的入口 |

---

## ⚡ 快速开始：对 Agent 说这些话

安装后，你**不需要记任何命令**，直接对 WorkBuddy 的 Agent（AI 助手）说人话即可。Agent 会根据触发词自动加载本 skill 并执行对应操作：

| 你想做什么 | 对 Agent 说 | Agent 会做什么 |
|-----------|------------|--------------|
| 查余额 | 「我的积分」「积分余额」「还剩多少积分」 | 拉取并汇总当前可用积分与批次 |
| 查明细 | 「积分明细」「帮我看看各批次」 | 逐批次列出用量/到期时间 |
| 到期提醒 | 「积分快过期了」「哪些积分要到期了」 | 检查并列出近期到期批次 |
| 每日签到 | 「签到领积分」「帮我签到」 | 调用签到接口（幂等，已签则跳过）；日常推荐在工作台一键签到 |
| 打开看板 | 「打开工作台」「打开积分工作台」「启动工作台」「打开积分看板」 | 后台启动本地服务，打开可视化看板（可实时刷新） |
| 消耗分析 | 「分析积分消耗」「消耗趋势」 | 分析过期浪费 / 趋势 / 签到效率 |
| 导出列表 | 「导出积分列表」「导出一份积分清单」 | 生成 Markdown / CSV 清单 |
| 创建快捷方式 | 「创建桌面快捷方式」「创建开始菜单快捷方式」 | 生成 .lnk 快捷方式（指向双击启动器，图标为青柠圆角） |
| 更新 skill | 「更新积分 skill」「升级积分工作台」 | `git pull` / zip 覆盖拉到最新版，自动重启工作台服务 |

> 触发词已写入 `SKILL.md` 的 `description` 字段，Agent 据此自动路由，无需手动指定 skill。

---

## 一、安装与更新 skill（手动方式）

> **一般不需要看这节**——直接把仓库地址发给 Agent 让它装、说「更新积分 skill」让它更新即可（见文首「交给 Agent 全流程」）。以下为手动/离线场景的备用方式。

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

### 更新已有安装

更新统一由安装目录里的 `scripts/update.py` 完成，对两种安装形态都适用：

```bash
python ~/.workbuddy/skills/workbuddy-credits/scripts/update.py          # 更新 + 自动重启工作台服务
python ~/.workbuddy/skills/workbuddy-credits/scripts/update.py --check  # 只检查，不改动
```

| 安装方式 | 更新行为 |
|---|---|
| Git 克隆（目录含 `.git`） | `git pull --ff-only` —— 增量更新，可用 `git log` 回滚 |
| 解压 / URL 导入（无 `.git`） | 优先 `git clone --depth 1` 取最新源码覆盖；无 git 时退回下载 `main.zip`。均为**合并式覆盖**，不删除本地已有文件 |

要点：

- **复制安装优先走 git**：国内部分网络可访问 `github.com` 但 `codeload.github.com`（archive 域名）不通，故 zip 仅作兜底。
- **网络不通时**加 `--proxy <地址>`；或改用 `--repo <镜像地址>`。
- 本地手改过 skill 目录里的文件会阻止 `--ff-only` 更新（Git 方式），请先处理改动。
- **用户数据不受影响**：数据在 `~/.workbuddy/workbuddy-credits-data/`，位于 skill 目录之外。
- **更新后服务会自动重启** —— 服务是 DETACHED 常驻进程，不重启不会加载新代码。
- 其它参数：`--no-restart`（不重启服务）、`--branch <分支>`。

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

## 三、每日签到（工作台一键完成）

签到已**统一收敛到工作台**，**无需部署任何定时任务**。

打开工作台后：

1. 当天**未签到** → 首次加载自动弹框引导，点「立即签到」即完成（也可直接点顶部工具栏的「立即签到」按钮）。
2. 当天**已签到** → 按钮自动置灰显示「今日已签到」，不会重复签到。
3. 签到成功后**自动刷新数据**。

命令行的等价用法见「四、命令行使用」（`status` / `checkin`，幂等）。

> **为什么不用定时任务**：定时任务只在 WorkBuddy 运行时可执行，本质是替用户点一下按钮；且每次运行会新建一个任务会话，长期累积成会话垃圾。改用工作台后路径更短、结果可见、无后台残留。详见 `references/checkin.md`（含 Windows 任务计划程序替代方案）。
>
> **到期提醒同理不再靠定时巡检**——已内建到工作台：首次加载时若有即将到期的积分会弹框提示（阈值默认 24 小时，可在工作台「提醒设置」自定义，支持关闭）。零后台开销、零打扰。

---

## 四、命令行使用

```bash
python .../workbuddy_credits.py              # 汇总 + 批次明细
python .../workbuddy_credits.py status       # 签到状态
python .../workbuddy_credits.py checkin      # 签到（幂等）
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
- 首次加载**未签到提醒**弹框（当天未签到 → 引导点击「立即签到」，弹框内可直接签到）
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
│   ├── shortcut.py               # 创建桌面/开始菜单快捷方式
│   └── update.py                 # 更新器（git pull / zip 覆盖 + 重启服务）
├── assets/
│   ├── echarts.min.js            # 离线图表库
│   ├── workbuddy-logo.png        # 工作台 logo（256px）
│   ├── favicon.png               # 浏览器标签栏图标（64px 圆角）
│   └── workbuddy-logo.ico        # 快捷方式图标（多尺寸圆角）
└── references/
    ├── api.md                    # 接口端点、请求头、字段映射
    └── checkin.md                # 签到机制与自动化说明（为何不用定时任务）
```

---

## 七、常见问题

**Q：提示「未找到登录态文件」？**
先登录 WorkBuddy 客户端，再运行脚本。

**Q：accessToken 失效（401 / 登录态失效）？**
重新登录 WorkBuddy 客户端即可自动刷新 token（约 90 天有效）。

**Q：工作台图表空白？**
若用 `serve.py` 方式，直接点「刷新数据」；若静态打开，先运行 `render` 生成 `dashboard_data.js` 再刷新。

**Q：更新完了，工作台还是老样子？**
`update.py` 会自动重启工作台服务；如果你是自己手动覆盖文件安装的（没走 `update.py`），服务是常驻进程不会自动加载新代码——双击启动器重启，或先关掉旧服务再打开工作台。

**Q：更新提示连不上 GitHub？**
本机需代理时加 `--proxy <地址>`；或改用镜像：`--repo <镜像地址>`。

**Q：消耗趋势图为什么只有最近约 17 天？**
逐日消耗采用「逐包追踪」口径（从安装后开始自积累，突破 17 天窗口）。历史消耗仅能从本地 `session_usage` 表近似回溯（会话级时间戳、约 17 天），属正常。

**Q：签到收入和裂变包会不会重复计算？**
不会。签到奖励本身以「size==daily_credit 的裂变包」形式发放，账本已做去重（`build_ledger` 排除签到奖励包），只计一次。

---

## 许可

自用工具，代码按现状提供，不附带任何担保。使用前请确认你所在地区对相关接口调用的合规性。
