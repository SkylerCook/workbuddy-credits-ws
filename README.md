# 自·积分工作台（workbuddy-credits）

一个查询、监控、分析 WorkBuddy / CodeBuddy 积分的工具包：积分余额与批次明细、每日签到（工作台一键完成，无需打开 WorkBuddy）、逐包精确消耗追踪、收支账本、可视化工作台、到期提醒、双击启动器与桌面/开始菜单快捷方式。

> 零第三方依赖（仅 Python 3 标准库），基于本机登录态（accessToken）直接调用官方接口，无需抓包。
>
> **版本与分发**：按 Release 管理版本，安装/更新用 `https://github.com/SkylerCook/workbuddy-credits-ws/releases/latest/download/workbuddy-credits.zip`（永久直链）。发版流程见「八、版本与发布」。

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

Agent 会运行 `scripts/update.py`：

1. 查仓库最新的 **GitHub Release**，比对版本号（`manifest.yaml` 的 `version` ↔ Release tag）；
2. 下载对应的 `workbuddy-credits-v<版本>.zip`，并用同 Release 提供的 `SHA256SUMS.txt` **校验 sha256**（校验失败拒绝安装）；
3. **合并式覆盖**安装（不删除本地已有文件）→ 报告版本变化与发行说明；
4. **自动重启工作台服务**（服务是常驻进程，不重启不会加载新代码）。

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
| 请求明细 | 「同步消耗明细」「拉取请求级流水」 | 同步 L5 请求级流水到本地存档（窄窗口保提示词） |
| 包生命周期 | 「看下积分包」「哪些包过期了」 | 拉取 L6 权威包清单（有效期内 / 已过期）与浪费归因 |
| 导出列表 | 「导出积分列表」「导出一份积分清单」 | 生成 Markdown / CSV 清单 |
| 创建快捷方式 | 「创建桌面快捷方式」「创建开始菜单快捷方式」 | 生成 .lnk 快捷方式（指向双击启动器，图标为青柠圆角） |
| 更新 skill | 「更新积分 skill」「升级积分工作台」 | 拉取最新 Release 包（sha256 校验）覆盖并重启工作台服务 |

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

1. 从 [Releases 页面](https://github.com/SkylerCook/workbuddy-credits-ws/releases/latest) 下载 `workbuddy-credits-v<版本>.zip`（**永久直链**：`https://github.com/SkylerCook/workbuddy-credits-ws/releases/latest/download/workbuddy-credits.zip`，部分网络下直链可能不通，改用页面下载即可），并核对同页 `SHA256SUMS.txt` 的 sha256。
2. 解压得到 `workbuddy-credits/` 目录。
3. 放到用户级 skills 目录：

   | 系统 | 路径 |
   |------|------|
   | Windows | `C:\Users\<你的用户名>\.workbuddy\skills\` |
   | macOS / Linux | `~/.workbuddy/skills/` |

4. 确认最终结构为 `~/.workbuddy/skills/workbuddy-credits/SKILL.md`。
5. 重启 WorkBuddy 客户端（或稍等自动刷新），在「技能管理」中确认已启用。

### 方式 C：URL 导入

「技能管理 → 通过 URL 导入」→ 填入仓库地址，自动拉取。

### 更新已有安装

更新统一由安装目录里的 `scripts/update.py` 完成，对两种安装形态都适用：

```bash
python ~/.workbuddy/skills/workbuddy-credits/scripts/update.py            # 更新 + 自动重启工作台服务
python ~/.workbuddy/skills/workbuddy-credits/scripts/update.py --check    # 只检查，不改动
python ~/.workbuddy/skills/workbuddy-credits/scripts/update.py --force    # 同版本也重装
```

更新来源（`--source auto`，默认）：

| 安装方式 | 更新行为 |
|---|---|
| Git 克隆（目录含 `.git`） | `git pull --ff-only` —— 增量更新，可用 `git log` 回滚 |
| 解压 / URL 导入（无 `.git`） | 走 **GitHub Releases 包**：查最新 Release → 比对版本 → 下载 zip → `SHA256SUMS.txt` 校验 sha256 → 合并式覆盖 |

Release 不可用时自动退回源码包覆盖（`git clone --depth 1` 优先，`main.zip` 兜底），同样为**合并式覆盖**，不删除本地已有文件。

要点：

- **版本权威是 Release tag**：与 `manifest.yaml` 的 `version` 比对；`--check` 会同时显示 Release 版本、发布时间与发行说明。
- **完整性校验**：Release 附带 `SHA256SUMS.txt` 时**强制校验**，不通过则拒绝安装，不留半成品。
- **网络不通时**加 `--proxy <地址>`；GitHub API 限流时加 `--token <token>`（或设 `GITHUB_TOKEN`）；也可 `--repo <镜像地址>`。
- 本地手改过 skill 目录里的文件会阻止 `--ff-only` 更新（Git 方式），请先处理改动。
- **用户数据不受影响**：数据在 `~/.workbuddy/workbuddy-credits-data/`，位于 skill 目录之外。
- **更新后服务会自动重启** —— 服务是 DETACHED 常驻进程，不重启不会加载新代码。
- 其它参数：`--source release|git|archive`（强制来源）、`--no-restart`（不重启服务）、`--branch <分支>`。

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
python .../workbuddy_credits.py sync         # 同步请求级流水（L5，默认近 31 天）
python .../workbuddy_credits.py sync --days 7   # 窄窗口（**≤31，否则提示词被剥离**）
python .../workbuddy_credits.py requests     # 查看请求级流水（本地存档）
python .../workbuddy_credits.py packages     # 包生命周期（有效期内）
python .../workbuddy_credits.py packages --expired  # 包生命周期（已过期）
python .../workbuddy_credits.py waste        # 过期浪费（权威口径，按到期月）
python .../workbuddy_credits.py expire-check 36  # 检查 N 小时内到期批次
python .../workbuddy_credits.py render       # 生成工作台数据
python .../workbuddy_credits.py --export     # 导出积分列表 Markdown
python .../workbuddy_credits.py --json       # 原始 JSON
```

运行要求：Python 3（`python` 或 `python3`），无需 pip 安装任何依赖。

> `sync` 的窗口宽度**必须 ≤31 天**：服务端在窗口 ≥32 天时会静默剥离提示词字段（HTTP 200 且 `code:0`，无报错）。提示词仅保留约 32 天，过期不可回补 —— 想留着就得勤跑窄窗。

---

## 五、可视化工作台

### 可刷新版（推荐）

```bash
python .../serve.py          # 启动本地服务（默认 8090）
```

浏览器打开 `http://127.0.0.1:8090`，首屏即走实时接口，右上角「刷新数据」按钮可重拉最新数据。工作台包含：

- **页头版本号**：标题右侧显示当前 skill 版本（取自 `manifest.yaml`），一眼确认跑的是哪一版
- **面板状态条**：概览 / 消耗明细 / 包生命周期三个面板**各自独立异步加载**，各自显示耗时与更新时间
  - 点面板名称 → 跳到该面板；点 `↻` → **只刷新这一个面板**（只重取它自己的数据源，不全量重拉）
  - 顶部「刷新数据」= 三个面板一起强制刷新
- **数据源健康徽章**（三态：正常/降级/缺失/无基准），任一接口变动立即显形
- 总览卡片（可用积分、今日已用、预计可用天数、累计消耗、即将到期、过期浪费、签到状态、登录态有效期）
- 每日/累计消耗图、消耗热力图（日期×小时）
- **消耗明细（请求级）**：服务端权威流水表 + 按模型/客户端/用途的构成占比 + 口径对账提示
  - **提示词列实时可见**（HTTP 模式），但**永不落盘**；`file://` 静态版该列显示「—」
- 收支账本（日粒度，自积累）
- 最近会话消耗明细
- **包生命周期**：`有效期内` / `已过期` 双页签，含浪费归因与按到期月分布
- 过期浪费统计、批次到期表
- 顶部使用建议、一键导出（批次 / 消耗 / 明细 / 生命周期 CSV）
- 首次加载到期提醒弹框（阈值默认 24h，可自定义/关闭）
- 首次加载**未签到提醒**弹框（当天未签到 → 引导点击「立即签到」，弹框内可直接签到）
- 顶部工具栏「立即签到」「桌面快捷方式」「开始菜单快捷方式」按钮
  - 「立即签到」未签到时高亮强调（青柠渐变 + 呼吸光晕），点击即签到并自动刷新数据；已签到时自动置灰禁用并显示「今日已签到」，避免重复

> 首屏速度：四路服务端数据**并发**拉取 + 进程内缓存去重，冷启动约 1 秒（此前约 3.9 秒），
> 之后刷新约 0.3 秒。单个数据源挂掉只影响它自己那个面板，其余照常可用。

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

自积累数据存 `~/.workbuddy/workbuddy-credits-data/`：

| 文件 | 内容 | 可回补 |
|------|------|--------|
| `snapshots.jsonl` | 余额快照 | ✅ |
| `usage_history.json` | 逐包余量采样（含状态/到期） | ❌ 衰减轨迹不可回补 |
| `income_events.json` | 到账/签到事件（对账归因用） | ✅ |
| `checkin_history.json` | 签到记录 | ✅ |
| `requests_history.jsonl` | 请求级消耗流水（按 `requestId` 去重合并） | 消耗可回补；**提示词不可** |

**不写** WorkBuddy 客户端自己的 `workbuddy.db`。提示词字段（`input`）**永不落盘**：`render` 与静态通道写入前都会强制剥离。

---

## 六、目录结构

仓库结构（`dist/` 为打包产物，不入库；skill 安装目录即上表 `workbuddy-credits/` 部分）：

```
workbuddy-credits-ws/
├── SKILL.md                      # skill 定义（触发词、操作流程）
├── README.md                     # 本文件
├── manifest.yaml                 # 版本号与市场清单（Release tag 以此为版本权威）
├── dashboard.html                # 工作台页面（可刷新版模板）
├── .gitignore                    # 忽略 dist/、用户数据、缓存
├── .gitattributes                # 行尾统一为 LF（覆盖 Windows 的 core.autocrlf）
├── scripts/
│   ├── workbuddy_credits.py      # 核心脚本（查询/签到/快照/分析/渲染/L5 流水/L6 包生命周期/对账）
│   ├── serve.py                  # 本地可刷新工作台服务
│   ├── launcher.py               # 双击启动器（独立进程常驻 + 生成 VBS）
│   ├── shortcut.py               # 创建桌面/开始菜单快捷方式
│   ├── update.py                 # 更新器（Release 包 → git/源码包 → 重启服务）
│   └── build_dist.py             # 打包发布物（生成 dist/*.zip + SHA256SUMS.txt）
├── assets/
│   ├── echarts.min.js            # 离线图表库
│   ├── workbuddy-logo.png        # 工作台 logo（256px）
│   ├── favicon.png               # 浏览器标签栏图标（64px 圆角）
│   └── workbuddy-logo.ico        # 快捷方式图标（多尺寸圆角）
├── references/
│   ├── api.md                    # 接口端点、请求头、字段映射、分层口径与关键坑
│   └── checkin.md                # 签到机制与自动化说明（为何不用定时任务）
├── .github/workflows/release.yml  # 推 v* tag 自动打包并发布 Release
└── dist/                         # 打包产物（git 忽略，经 Releases 分发）
```

---

## 七、常见问题

**Q：提示「未找到登录态文件」？**
先登录 WorkBuddy 客户端，再运行脚本。

**Q：accessToken 失效（401 / 登录态失效）？**
重新登录 WorkBuddy 客户端即可自动刷新 token（约 90 天有效）。

**Q：工作台图表空白？**
若用 `serve.py` 方式，直接点「刷新数据」；若静态打开，先运行 `render` 生成 `dashboard_data.js` 再刷新。

**Q：后台起的服务报「未找到登录态」，但前台脚本正常？**
部分沙箱环境会拦截**后台进程**读 C 盘登录态。把登录态复制一份到非 C 盘，用环境变量指过去再起服务：

```bash
cp "$LOCALAPPDATA/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info" "D:/somewhere/workbuddy-desktop.info"
WORKBUDDY_AUTH_FILE="D:/somewhere/workbuddy-desktop.info" python scripts/serve.py 8090
```

**Q：消耗明细里的「提示词」列是空的？**
两种情况：① 该条记录已超过服务端约 **32 天**保留期（服务端本身不再返回，不可回补）；② 你是用 `file://` 静态版打开的 —— 提示词**永不落盘**，静态版该列刻意留空。走 `serve.py`（HTTP）即可实时看到。

**Q：`sync` 拉回来的提示词怎么是空的？**
窗口宽度 **≥32 天**时服务端会**静默剥离**提示词（HTTP 200 且 `code:0`，没有任何报错）。改用窄窗口：`sync --days 7` 或按单日拉取。

**Q：为什么「已过期损失」以前是 0，现在有了具体数字？**
老接口 `get-user-resource` **完全不返回已过期包**，所以旧口径的过期浪费恒为 0（只能靠本地快照差分推算）。现在改用包生命周期接口（L6）权威直出，无需再依赖差分。

**Q：更新完了，工作台还是老样子？**
`update.py` 会自动重启工作台服务；如果你是自己手动覆盖文件安装的（没走 `update.py`），服务是常驻进程不会自动加载新代码——双击启动器重启，或先关掉旧服务再打开工作台。

**Q：不开代理就报「连接被拒绝（WinError 10061）」，开着代理反而一切正常？**
这是 urllib 的**代理冻结**：CPython 把「进程首次发请求时的代理配置」固化在全局 opener 里（`ProxyHandler.__init__` 当场调用 `getproxies()`，此后永不刷新）。于是 `serve.py` 若在代理软件开着的时候启动，它就一直把请求发往那个代理端口——你后来关掉代理软件、端口失效，它仍照发不误，报 `10061` 且**永不自愈**。这也解释了为什么"开着代理就好"。
现版本已改为**默认直连**：显式传入空代理表，完全不读取系统与环境代理，从根上消除该问题（与 `update.py` 的既定约定一致）。**注意修复只对重启后的进程生效**——正在跑的老进程仍是旧代码，关掉它重新打开工作台即可；如果你撞上这个报错，先重启一次工作台。

**Q：更新提示连不上 GitHub？**
按优先级：① 本机需代理时加 `--proxy <地址>`；② 改用镜像 `--repo <镜像地址>`；③ 强制走源码包 `--source archive`（走 `github.com`，通常比 API 域名更容易连通）。

**Q：提示「sha256 校验失败，已中止安装」？**
说明下载到的包与 Release 声明的不一致（下载被截断、被代理/网关改写，或极端情况下被替换）。脚本会**拒绝安装**，本地文件不受影响。重试即可；若反复失败，改用 `--source archive`，或手动从 Release 页下载并核对 sha256。

**Q：如何回滚到旧版本？**
Git 安装：在 skill 目录 `git checkout v<旧版本>` 后重启服务。Release 安装：从 [Releases 页面](https://github.com/SkylerCook/workbuddy-credits-ws/releases) 下载对应旧版 zip 覆盖安装目录。覆盖不会删除本地文件，但**不会自动降级**——`update.py` 检测到本地版本高于远端时会跳过（可用 `--force` 强制覆盖）。

**Q：怎么确认自己装的是哪个版本？**
看 skill 目录下 `manifest.yaml` 的 `version`，或运行 `python scripts/update.py --check`（会同时显示本地版本、Release 最新版本与发布时间）。

**Q：更新提示「尚无 Release」？**
本仓库改用 Release 作为版本权威，若还没有任何 Release（或网络访问不到 `api.github.com`），脚本会自动回退到 Git / 源码包方式更新，不影响使用。

**Q：消耗趋势图为什么只有最近约 17 天？**
逐日消耗采用「逐包追踪」口径（从安装后开始自积累，突破 17 天窗口）。历史消耗仅能从本地 `session_usage` 表近似回溯（会话级时间戳、约 17 天），属正常。

**Q：签到收入和裂变包会不会重复计算？**
不会。签到奖励本身以「size==daily_credit 的裂变包」形式发放，账本已做去重（`build_ledger` 排除签到奖励包），只计一次。

**Q：怎么知道页面上跑的是哪个版本？**
页头标题右侧有版本号徽标（如 `v1.4.0`），取自 `manifest.yaml`，无需翻文件。静态单文件版同样会显示。

**Q：首屏还要等多久？慢在哪？**
四路服务端数据（账户 / 签到 / 请求流水 / 包生命周期）**并发**拉取，冷启动约 1 秒，之后刷新约 0.3 秒（此前是四路串行，约 3.9 秒）。耗时几乎全在网络往返，本机 CPU 只占约 0.03 秒。三个面板各自独立加载：谁先回来谁先上屏，某个面板失败也不影响其他面板。

**Q：只想刷新一个面板，不想整页重拉？**
点该面板在「面板」状态条上的 `↻` 即可——它只重取这个面板自己的数据源（概览→账户/签到，消耗明细→请求流水，包生命周期→包明细），并只重绘该面板。想全部刷新就用右上角「刷新数据」。
（注意：这个优化需要**重启工作台服务**才生效——`serve.py` 是常驻进程，不重启不会加载新代码。）

---

## 八、更新日志

### v1.4.3 — 修复「今日已使用」取了错误口径：被逐包采样值覆盖，严重低估

**背景**：用户发现「今日已使用」显示 **0.74**，与直觉不符。核对三个口径（2026-09-16 13:16 实测）：

| 值 | 实测 |
|---|---|
| 前端显示的 `today_used` | **0.74** |
| `today_used_l5`（服务端流水，权威） | **9.66** |
| `today_used_l2`（逐包采样） | 0.74 |

L5 当日确有两笔请求（合计 9.66），**显示值不到它的 1/13**。

**根因（两层）**：

1. **口径被覆盖（真 bug）** —— `build_dashboard_data()` 先按「L5 优先、L2 兜底」算出 `today_used`，
   随后在构造 data 时**又用 `compute_today_used(usage_hist)` 覆盖了一次**。于是 KPI 显示的是 L2 采样值，
   副标题却按 `usage_source` 标成「服务端流水口径」—— **口径与标注不一致**
2. **L2 本身会低估** —— 逐包差值只统计「今日**首个采样点**之后」的增量，而采样点由页面刷新/服务请求产生。
   当天第一次打开工作台之前的消耗没有基准。实测首个采样点是 13:12（服务启动那一刻），此前消耗全被漏掉
   （对账状态也如实报 `no_baseline`）

**改动**：删掉那处覆盖，`today_used` 统一沿用「L5 优先、L2 兜底」，并在原地写明**为什么不能再覆盖**（附实测数字，防止后人重蹈）。前端副标题不变 —— 修复后它如实表达「流水 9.66 · 采样 0.74」，两个口径的差异反而一目了然。

**实测**：

- 修复后 `today_used = 9.66 = today_used_l5`，与流水一致
- 新增不变式断言：`usage_source == "L5"` 时必有 `today_used == today_used_l5`

**同批完成的口径自查**（起因：用户追问「其他面板还有没有同类问题」）—— 逐项核对每个对外指标
「值取自哪套口径 ↔ 界面标注说的是哪套」：

| 指标 | 实际口径 | 结论 |
|---|---|---|
| 可用积分 `total_remain` | 包级 remain（订阅型本周期） | ✅ 正确 |
| 累计消耗 `total_used` | 包级 used 求和 | ✅ 正确 |
| 预计可用天数 `days_left` | total_remain ÷ daily_avg(L5) | ✅ 正确 |
| 每日·累计趋势、日历热力图 | `usage_daily` / `income_daily`（L5 优先） | ⚠️ **说明文案过时**，已订正 |
| 即将到期 / 已过期损失 | L3 at_risk + L6 权威浪费 | ✅ 正确 |
| 消耗明细 + 三项构成 | L5 请求级 | ✅ 正确 |
| 双通路对账 | L5 ↔ L2 | ✅ 正确 |
| 会话消耗 | L1 本地会话库（页面已注明「相互独立、可用于对账」） | ✅ 正确 |

据此另修 3 处 + 1 处健壮性缺陷：

1. **`usage_hourly` 字段类型随分支变化（埋雷）** —— L5 可用时 `req_hourly()` 返回
   `{日期: [24 小时]}` 的 dict，不可用时返回 `[{date,hour,credit}]` 的 list。前端目前未消费该字段
   所以没爆，但下游一旦接上就会崩。已把 `req_hourly()` 改为与 `compute_usage_hourly()` **同构**
2. **小时维度口径分歧** —— `usage_heatmap` 走 L1 本地会话库（按「会话最后更新时刻」归小时，跨小时会话
   会把消耗全记在最后一刻，且受本地库「到账伪增量」影响），与 `usage_daily`/`usage_hourly` 的 L5 口径
   不一致。现统一为 `heatmap = uhourly`（L5 优先、L2 兜底）；`usage_heatmap()` 标注废弃、保留以兼容外部调用
3. **过时的界面说明** —— 「消耗分析」的日历说明仍写「各资源包已用积分的逐日增量…自今日起每日记录、
   逐步积累」（L2 时代口径），而数据早已是 L5 优先。订正为「优先取服务端请求级流水（覆盖最近 31 天，
   精确到每次调用），更早的日期由逐包采样补足」；同类过时注释共修 4 处
4. **小时字段只验「能转整数」不验范围** —— `hour=25` 会被照收并产生非法小时格（24 格热力图错位）。
   `req_hourly` / `compute_usage_hourly` 均已加 0–23 范围校验

### v1.4.2 — 修复「套餐用量」与官网口径不一致：订阅型改用本周期口径

**背景**（另一台电脑发现）：工作台「套餐用量」显示剩余 **500**，官网控制台却显示「已用 500/500、**0 剩余**」，两边对不上，且工作台的「可用积分」合计被系统性**高估**。

**根因**：同一条接口响应里**存在两套容量口径**，工作台与官网各取一套：

| 字段组 | 工作台（修复前） | 官网控制台 |
|---|---|---|
| 累计 `CapacitySize / CapacityUsed / CapacityRemain` | ✅ 取这组 | — |
| 周期 `CycleCapacitySize / CycleCapacityUsed / CycleCapacityRemain` | ❌ 全脚本从未读取 | ✅ 取这组 |

**为什么累计口径不可信**：实测同一个资源对象 `CapacityUsed=0` 而 `CycleCapacityUsed=500`。而**周期区间是累计区间的子集 —— 累计已用不可能小于周期已用**，两者自相矛盾，说明服务端对订阅型的累计字段**不随周期消耗更新**，长期停在「重置后的初值」。旁证：官网「下次权益周期更新时间」正好等于 `CycleEndTime`，说明官网那一区块就是周期维度。

**改动**：

- `parse_accounts()` / `parse_pkg_lifecycle()` 共用新的 `_pick_capacity()`：`CapacityType == 4`（套餐用量）以**周期口径**为当前可用；其余类型实测两组一致，沿用累计口径；某组字段整体缺失时回退另一组（故对「两组一致的包」行为零变化）
- 随行留档 `diverged` / `remain_cum` / `remain_cyc`，供页面标注与对账
- 概览「可用积分」在出现分叉时标注「已按本周期口径修正 X」（X = 若沿用累计会多算的量）
- 「积分批次」表对订阅型行标注「· 本周期」，鼠标悬浮可看两套口径的原始值
- 「收支损失统计」的说明文案同步更新；`references/api.md` 补录 `CycleCapacity*` 三兄弟与「订阅型双口径」这一坑，并注明 `RemainCycles=0` **不代表**周期额度用尽

**实测**：

- 构造报告场景（累计 0/500、周期 500/0）：生效剩余 **0**（与官网一致）、合计修正 500，**21 项断言全过**
- 真实数据回归：本机该套餐周期用量为 0（两组本就一致），修复前后**可用合计完全一致 4695.98**，**零回归**
- 赠送包 42 条两组数值一致，不受影响

### v1.4.1 — 修复「点刷新三个面板全报红」：启动器版本自检 + 前端后端能力探测

**背景**：点「刷新数据」后，三个面板齐刷刷显示「失败 · 点此重试」。根因既不在前端也不在数据 —— **8090 上跑的一直是 v1.3.x 的旧进程**，它没有 `/api/version`、`/api/overview` 这些面板接口，新前端打过去全是 404。

**为什么重启也没用**：`launcher.py` 过去只用「端口是否被占用」判断服务是否在跑。旧进程一直占着 8090，启动器每次都判定「已在运行」，直接打开浏览器 —— 于是**永远打在旧版本上**，代码更新再多也进不去。

**改动**：

- **启动器改为「端口 + 版本」双校验**（`scripts/launcher.py`）：启动前先探 `/api/version` 并与 `manifest.yaml` 的当前版本比对，不一致就结束占用 8090 的旧进程再拉起新版。只对 python 系进程动手；端口被其他程序占用时绝不误杀，只给出明确提示
- **前端先探后端能力再选通道**（`dashboard.html`）：
  - 拿到版本号 → 面板模式（三面板并行、局部刷新、耗时统计）
  - 返回 404（服务在、但是旧版）→ **直接走全量接口**，不再发三个注定 404 的请求；页面显示提示条说明原因与升级方法，并给「重新检测」按钮一键切回
  - 连不上 → 逐级兜底（全量接口 → 静态数据）
- **面板状态如实标注「全量模式」**：旧版服务下不再是刺眼的「失败」，而是说明当前走的通道
- **请求加超时**（面板 60s / 全量 90s）：上游卡住时不会让按钮永远停在「刷新中…」
- **旧版服务下点 `↻`** 自动退化为全量刷新，不再报错

**实测**（8090 旧服务 + 新前端）：三面板由「失败」变为「全量模式 · 时间」，控制台报错由 7 条降到 2 条（探测本身），无 JS 异常；启动器接管链路实测 `1.4.0 进程 → 判定版本不符 → 结束 → 新版就绪`。面板模式下三面板 0.53~0.83s、点 `↻` 只发一个请求，均未受影响。

### v1.4.0 — 首屏并发提速 + 分面板异步加载 / 局部刷新 + 页头版本号

**背景**：工作台首屏加载慢。实测把耗时拆开看，答案很干净——**四路服务端数据串行往返 3.84 秒，本机纯计算只占 0.03 秒**。也就是说，慢的完全是网络排队，不是数据处理。

**改动**：

- **四源并发拉取**：账户（L3）/ 签到（L4）/ 请求流水（L5）/ 包生命周期（L6）改为并行，首屏耗时从「四路之和」变成「最慢的一路」。L6 内部的分页（付费×免费 × 有效期内×已过期 共 4 次请求）同样并发。实测 **3.87s → 0.86s（约 4.5 倍）**
- **分面板接口 + 局部刷新**：新增 `/api/overview`、`/api/requests`、`/api/lifecycle` 三个接口，每个只返回自己渲染需要的字段（概览不再背着 250 行请求流水与 140 行包明细）。前端三面板并行请求、各自独立渲染与失败隔离；「面板」状态条显示每个面板的耗时与更新时间，点 `↻` 只刷新那一个面板
- **进程内缓存 + single-flight（`SourceHub`）**：页面同时发三个面板请求时，同一个数据源只会真正打一次上游，其余等待复用。实测首屏后两个面板的接口耗时降到约 28ms
- **修掉一处解析阻塞**：静态数据脚本加 `async`。此前它作为同步脚本挡在面板请求之前，冷启动实测阻塞 869ms（DCL=890ms）；现在 DCL 降到约 176ms，面板请求立即发出
- **页头版本号**：标题右侧显示 `v1.4.0`（取自 `manifest.yaml`），HTTP 与静态单文件两种模式都显示
- **降级链更稳**：面板接口不可用（例如还在跑旧版服务进程）→ 自动退回 `/api/data` → 再退回静态数据，页面始终有内容（已实测：旧服务下仍正常出全页）

> 常驻服务不重启不加载新代码——**升级后请重启工作台**（关掉旧的 `pythonw.exe` 再双击启动器）。

### v1.3.1 — 接口访问改为默认直连

**背景**：常驻的 `serve.py` 会出现「不开代理就报 `WinError 10061（连接被拒绝）`、开着代理反而一切正常」，且**重启前永不自愈**。

**根因**：CPython 的 urllib 把「进程首次请求时的代理配置」**冻结**在模块级全局 opener 里（`request.py` 的 `_opener` 每进程只构建一次，`ProxyHandler.__init__` 构造那一刻就调用 `getproxies()` 固化结果）。于是服务若在代理软件开着的时候启动，之后代理软件退出、端口关闭，它**仍会**向那个死端口发 `CONNECT`——用户侧的症状就是「开代理就好、不开就报错」。

**改动**：

- **所有接口调用一律默认直连**：`_api_call` 改用显式空代理表的 opener（`build_opener(ProxyHandler({}))`），**完全不读取系统与环境代理**；与 `update.py` 的既定约定统一（代理仅通过 `--proxy` 显式开启）
- **报错说人话**：`10061` / `10060` / DNS 失败分别翻译成可操作提示，不再抛裸英文异常
- 文档同步：`references/api.md` 新增「网络与代理」章节（含冻结机制与排查手段），`README.md` 新增对应 FAQ

> 修复只对**重启后**的进程生效——常驻服务不加载新代码，升级后请重启工作台。

### v1.3.0 — 三层数据架构重构

**背景**：此前工作台完全依赖**客户端可见**的本地数据（逐包采样快照 + 本地会话库），而网页控制台的接口数据拿不到，导致「已过期浪费」「请求级明细」「真实消耗口径」都只能靠推算，缺陷明显。本版把数据源重构为**三层架构**，让**服务端权威数据主导展示**。

**新增数据源**：

- **L5 请求级消耗流水**（`/get-user-request-usage`）——服务端权威、可回溯至 2026-05（窄窗口）。含模型 / 客户端 / 用途 / 单次积分 / **实时提示词**。
- **L6 包生命周期**（`/get-user-resource-{paid,free}-packages`）——权威包清单，**首次能看到已过期包**，过期浪费由直出取代推算。

**工作台新增**：

- **数据源健康徽章**：四条链路（消耗明细 / 包生命周期 / 逐包采样 / 签到）三态标示，接口一挂当天就能看见
- **消耗明细（请求级）**面板：权威流水的表格 + 模型/客户端/用途构成条 + 口径对账提示
- **包生命周期**面板：`有效期内` / `已过期` 双页签，含浪费归因与按到期月分布
- 口径切换：`今日已使用` / `每日消耗` 改为 **L5 优先、L2 兜底**，卡片上标注实际来源

**关键设计**：

- **提示词永不落盘**：`input` 只存在于内存中供实时展示；`render` 与静态通道写入前强制剥离（`strip_prompts` 硬约束）。`file://` 静态版该列留空
- **窄窗口铁律**：L5 查询窗口宽度 **≥32 天会静默剥离提示词**（HTTP 200 且 `code:0`，无报错）。`sync` 默认 31 天并自动收敛
- **双通路对账**：A = L5 请求级（有服务端历史）↔ B = L3→L2 包级采样。三态返回（`已对账` / `无基准` / `精度不足`），显式呈现从不静默跳过
- **优雅降级**：每个新数据源独立 try/except 隔离，任一挂掉页面照常可用并显式降级

**新增命令**：`sync` / `requests` / `packages [--expired]` / `waste`

### v1.2.2 及更早

- 行尾统一 LF（`.gitattributes`），打包自动清理旧版本包
- 基于 GitHub Releases 的更新链路（sha256 校验）、双击启动器与桌面/开始菜单快捷方式
- 工作台与签到流程（无需打开 WorkBuddy 即可签到）

---

## 九、版本与发布（维护者）

本仓库采用 **tag = 版本，Release = 发行** 的包管理方式（与常见开源项目一致）。

### 发版流程

1. 改 `manifest.yaml` 的 `version`（如 `1.2.0`），提交推送到 `main`；
2. 打同名 tag 并推送：

   ```bash
   git tag v1.2.0
   git push origin v1.2.0
   ```

3. GitHub Actions（`.github/workflows/release.yml`）自动：
   - 校验 tag 与 `manifest.yaml` 的版本一致（不一致直接失败，避免版本错配）；
   - 运行 `python scripts/build_dist.py` 打包——**白名单收集**，用户数据、`.git`、`dist`、`__pycache__` 永不入包；
   - 创建 Release，附下表三个资产，发行说明由 commit 自动生成。

4. 用户侧执行 `update.py` 即更新到该版本（带 sha256 校验）。

### Release 资产

| 资产 | 用途 |
|---|---|
| `workbuddy-credits-v<版本>.zip` | 版本化包，`update.py` 首选下载对象 |
| `workbuddy-credits.zip` | 同名稳定包，`releases/latest/download/` 永久直链 |
| `SHA256SUMS.txt` | 两者的 sha256，`update.py` 强制校验 |

### 本地打包（不发版时）

```bash
python scripts/build_dist.py                  # 输出 dist/ 三件套
python scripts/build_dist.py --print-version  # 只打印 manifest.yaml 里的版本号（CI 用它校验 tag）
```

- **自动清理旧包**：打包后自动删除 `dist/` 内其它版本的 `workbuddy-credits-v*.zip`，只留当前版本 + 稳定包 + 校验文件，避免目录堆旧包。
- **文本一律 LF**：文本文件写入 zip 前统一转 LF（与 `.gitattributes` 的 `eol=lf` 一致），二进制资源（png/ico 等）原样保留 → 产物不依赖本地检出状态，Windows 的 `core.autocrlf=true` 也影响不到。

> 打包可复现：zip 条目按名称排序、时间戳统一取当次 git 提交时间。同一次提交、相同 Python/zlib 版本下重复打包得到相同字节；跨环境不保证字节一致（**sha256 以随包发布的 `SHA256SUMS.txt` 为准**）。

### 下载域名说明

`update.py` 下载 Release 资产时**优先走 API 资产端点**（`api.github.com` → 302 → `release-assets.githubusercontent.com`），失败才退回 `browser_download_url`（`github.com`）。实测国内网络下 `github.com` 会间歇性不可达，而 API 域名与 CDN 稳定，故 API 端点优先。同理，`releases/latest/download/...` 永久直链依赖 `github.com`，偶发不通时改用 `update.py` 或 Release 页面手动下载。

### 行尾约定（`.gitattributes`）

本仓库所有文本文件统一 **LF**（`.gitattributes` 里 `* text=auto eol=lf`）。原因：Windows 上 git 默认 `core.autocrlf=true`，会让「git 检出的文件」变 CRLF，而 CI 在 Linux 打的包与用户安装目录是 LF，导致 `git clone` 安装与 Release 安装**行尾不一致**（内容相同，但直接 md5 比对会误报差异）。

要点：

- `.gitattributes` 优先级**高于** `core.autocrlf`，无需让贡献者改本地 git 配置。
- `*.png / *.ico / *.jpg / *.zip` 等声明为 `binary`，不做任何转换。
- `*.bat / *.cmd / *.ps1` 例外保留 **CRLF**（cmd.exe 与 Windows PowerShell 5.1 最稳）。
- 若在加入 `.gitattributes` **之前**就已克隆，执行一次 `git add --renormalize .` 并重新检出（删除后用 `git checkout -- .` 恢复）即可拉平；或用 `git clone` 重新克隆。
- `build_dist.py` 打包时还会再把文本统一转 LF 作为兜底，所以**即使本地检出是 CRLF，产物也是 LF**。

---

## 许可

自用工具，代码按现状提供，不附带任何担保。使用前请确认你所在地区对相关接口调用的合规性。
