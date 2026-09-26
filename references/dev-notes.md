# 开发笔记（项目长期记忆）

> **约定：与本项目相关的记忆、规则、踩坑，一律写在这里** —— 跟着仓库走，换机器、换会话
> 都能看到。不要写在工作区或宿主的记忆目录里（那些不随仓库分发，也不跨设备）。
>
> 日常动手的**常驻说明**（红线、常用命令、提交与发布顺序）见根目录 [`AGENTS.md`](../AGENTS.md)；
> 本文件是**按需查阅的细节库**。
>
> ⚠️ **本仓库是 public**：这份文件只写**可公开的技术内容** ——
> 不写凭据 / token / cookie、账号 UIN、个人积分数字、本机绝对路径、内网信息。

## 一、数据分层与口径

- **权威层**：L5 请求流水（`get-user-request-usage`）+ L6 包生命周期
  （`get-user-resource-{paid,free}-packages`）→ 主导消耗 / 浪费展示
- **观测层**：L2 逐包采样（`usage_history.json`）+ L1 本地会话库（`workbuddy.db`）
  → 骨架与对账基准；**L4 签到是独立来源**（本地，不受服务端接口变动影响）
- **口径**：`usage_daily` / `usage_hourly` / `today_used` 一律 **L5 优先、L2 兜底**
- **对账只有双通路**：L2 的 `used` 就是 L3 的 `CapacityUsedPrecise`（同一字段），
  **不是独立证据**；A = L5 ↔ B = 包级；三态显式（一致 / 有偏差 / 无基准）
- ⚠️ **两张批次表的数据源不同，别混**（v1.6.1 排查时踩到）：
  - 「**积分批次**」表（`#sec-batch`）用 `expiry_list` ← **L3**（`parse_accounts`）→ **含「套餐用量」**
  - 「**包生命周期**」表（`#sec-lifecycle`）用 L6（`api_packages`）→
    **过滤掉了 `CapacityType=4`**，所以**不含套餐那批**
  ⇒ 只有前者能与概览 KPI 的 `at_risk_count` 对齐；后者的「即将到期」行数天然少一批。
  （后者页面的提示文案写着两张表「同源」，与实际不符 —— 待修正）

## 二、取数通道：浏览器桥（v1.5.0 起）

**背景**：客户端把登录凭据**加密落盘**（`auth.accessToken` → `$wbEncrypted` + AES-GCM 封套，
密钥在客户端内），本技能拿不到明文 token，直连 API 的 `Authorization: Bearer` 路径失效。

**方案**：借**官网已登录的浏览器会话**取数 —— Playwright persistent context 在
`www.workbuddy.cn` 页面上下文里发 `fetch(credentials:'include')`，HttpOnly `session`
cookie 由浏览器自动携带。**本技能不接触、不解析、不落盘明文凭据。**

| 项 | 值 |
|---|---|
| 域名 / 路径 | `https://www.workbuddy.cn/billing/meter/*` |
| 与旧域名的关系 | 与 `copilot.tencent.com` **同名同结构** —— 解析层未改 |
| profile | `~/.workbuddy/workbuddy-credits-data/browser_profile` |
| 会话寿命 | `session` / `session_2`（HttpOnly）**7 天绝对过期、不滑动续期**（实测请求前后 `expires` 未变） |
| 进程策略 | **懒加载 + 复用**：首次取数才启动（约 4s），之后 0.1~0.3s/次；进程退出时关闭 |

**接口要点**：

- `paid/free-packages` **必须传 `PackageCodes`**（否则 400 `code:10001`）；可从
  `get-user-resource-summary` 的 `data.Packages[].PackageCode` 动态取，白名单作兜底
- `get-user-resource-summary` 给**包级周期口径汇总**（`CycleTotalCapacity` /
  `CycleRemainCapacity` / `CycleUsedCapacity` / `TotalCount`），可作交叉校验
- 签到接口字段比旧版更多：`today_credit` / `is_streak_day` / `next_streak_day` /
  `streak_bonus_days` / `streak_bonus_credit` / `week_checkin_days` / `total_credits`
- **账号信息（昵称 / 角色）**：`GET /console/account` → `{uid, nickname, uin, type, ...}`
  （`type` 如 `personal`，官网前端渲染成「个人版」）。
  ⚠️ 该路径**不在 `/billing/meter` 前缀下**，是**站点级路径** —— 桥模式下要用完整路径请求。
  凭据加密后昵称/角色只能从这里取（旧版读本机登录态文件的 `account` 段）

**三条铁律**：

1. **绝不用 headless** —— 同一 profile 混用 headful/headless 会让**会话凭空消失**
   （实测 cookie 直接没了、全部 401，极易误判成"服务端踢了会话"）。
   "无头"用 `--window-position=-32000,-32000`
2. **Playwright sync API 有线程亲和性** → 桥跑在专用线程里，其他线程经队列提交（actor 模式）
3. **网关层 HTML 401 = 会话过期**（openresty 返回），≠ 业务错误（业务错误是 JSON `code:xxxx`）
4. **持久化 profile 会「记住」窗口坐标**（v1.7.6 踩坑）：静默窗口用 `--window-position=-32000,-32000`
   移出屏幕，坐标会在 `ctx.close()` 时写进 profile；登录/可见窗口若**不给** `--window-position`
   就会恢复这个屏幕外坐标 → 登录弹窗「消失」在屏幕外。故 `LAUNCH_ARGS_VISIBLE` 必须显式
   `--window-position=80,80`（命令行参数优先级高于 profile 记住的位置，实测生效）。

**浏览器来源**：用 `channel` 别名驱动**系统浏览器**（不是 Playwright 自带内核，故**无需**
`playwright install` 下载内核）。候选顺序：**上次登录成功的浏览器最优先**（记在数据目录
`browser_channel.txt`，relogin 成功时写入 —— 它的 profile 才有有效 cookie），
其次 Edge（Windows 默认自带）、Chrome 回退。
channel 别名不存在时在 launch 阶段即抛异常（此时尚未创建进程 / 锁 profile），换下一个候选是安全的。
Firefox 不兼容 —— Playwright 的 Firefox 是自带 Gecko 内核，且本桥的会话机制只针对 Chromium 系实测。

**登录完成判定用 cookie、不用页面探针**（v1.7.6）：`_relogin_flow` 原靠 `page.evaluate`
发 `get-user-resource-summary` 探针轮询 `code==0`，登录后页面跳转/导航会让 `evaluate`
反复失败 → 误判超时 → 前端不刷新（实测「登录成功但没自动刷新」）。改为读
`ctx.cookies()` 的 session cookie 到期时间（登录后 Set-Cookie 更新为未来 7 天），
与页面状态无关、可靠。

**⚠️ 禁止 Chrome/Edge 共用同一个 profile 目录（v1.7.6 踩坑，实测）**：两者对同一
user-data-dir 的 cookie 加密互不兼容（各自持有/轮换 `os_crypt` 密钥，跨应用解密失败）——
实测早上 Chrome 登录、下午桥切 Edge 接管同一 profile 后会话即「凭空失效」
（与"混用 headful/headless 会话消失"同类）。故 `profile_dir_for(channel)` 按浏览器
分目录：`browser_profile`（chrome 沿用）/ `browser_profile-msedge`；切换浏览器后
首次需重新登录一次。

## 三、性能设计（v1.6.0）

桥模式把取数交给浏览器后，**首屏要等 Chrome 冷启动**（3~5s）。三处针对性优化：

1. **服务端预热**（`serve.py::_warmup`）：服务起来 2s 后，后台线程拉起浏览器并预取四源
   填进 SourceHub。用户双击启动器后本来就要切窗口，趁这几秒热好 —— 失败静默、不阻塞服务
2. **首屏缓存**（`dashboard.html`）：overview 切片存 `localStorage`，再次打开**立即渲染**
   再后台刷新。⚠️ 只能缓存 **overview** —— 它的字段里**没有提示词**；
   `requests` 切片有 `input`，**绝不能进缓存**（「提示词永不落盘」红线）
3. **攒批并发**（`browser_bridge.py::_worker_loop`）：worker 单线程，Python 侧的"四源并发"
   到桥里会**排成队**。改为把此刻已在队列里的请求攒成一批，一次 `page.evaluate` +
   `Promise.all` 并发发出

| 场景 | 优化前 | 优化后 |
|---|---|---|
| 首屏（服务端耗时） | 4.59s | 1.48s |
| 再次打开（命中缓存） | ~4.6s | 0.42s |
| 点「刷新数据」 | 3.19s | 1.76s |

## 四、关键约束（勿推翻）

> 其中「提示词永不落盘」「凭据只留浏览器」属于**红线**，执行口径见 [`AGENTS.md`](../AGENTS.md)；
> 本节记录它们的**机制与实测依据**，以及其余全部约束。

- **提示词永不落盘**：`_sanitize_request` 只留 `REQ_STORE_FIELDS` + `fetched_at`，
  `input` / `inputTrunc` 硬丢弃；`strip_prompts()` 是**落盘通道硬约束**
  （`cmd_render` 首行 + `serve.py` 的 `/dashboard_data.js` 路由，**漏一处即泄密**）
- **L5 窄窗铁律**：查询窗口宽度 **≤31 天**（含首尾）；**32 天会静默剥离提示词**
  （HTTP 200 + `code:0`，无任何报错）。未来日期 / 起止倒置 → 静默 `total:0`；
  页码越界 → 400。clamp 用 `e - (REQ_MAX_WINDOW_DAYS - 1)`
- **L6 静默陷阱**：付费码传 free 端点会被**静默过滤成空**（须按白名单分派）；
  缺 `User-Agent` → 403 `code:10085`；「已过期」查询必带
  `PackageEndTimeRangeBegin`（−101 年，等价不限时间）
- **「今日已使用」一律 L5 优先**（v1.4.3 定案）：`build_dashboard_data()` 里 `today_used`
  **不得**再用 `compute_today_used(usage_hist)` 覆盖 —— L2 逐包采样只算「今日首个采样点之后」
  的增量，而采样点由页面刷新产生，当天首次打开工作台前的消耗**没有基准**，会**系统性低估**
- **小时维度同源**：只走 `req_hourly` / `compute_usage_hourly`（同构 list，L5 优先）；
  `usage_heatmap`（L1 口径）已废弃
- **容量字段有两套口径，订阅型必须用周期口径**（v1.4.2 定案）：接口对每条资源同时返回累计
  `Capacity*` 与周期 `CycleCapacity*`。`CapacityType == 4`（套餐用量）两组会分叉，且
  **累计字段不随周期消耗更新**（实测出现 `CapacityUsed = 0` 而周期已用 > 0；因周期 ⊂ 累计，
  累计已用不可能小于周期已用 ⇒ 累计口径失真）。统一走 `_pick_capacity()`：
  type=4 取周期、其余取累计、某组缺失则回退。
  ⚠️ `RemainCycles = 0` **不等于**周期额度用尽，勿据此判定
- **两个「连续签到天数」别混用**（v1.4.4 定案）：接口的 `streak_days` 是**当前活动周期内**
  的签到天数（实测 `checkin_dates` 起点与 `start_time` 重合、长度等于 `streak_days`，
  活动一换即归零重算）；跨周期**真实连续**由 `compute_real_streak()` 逐日回溯得出，
  输出 `checkin.real_streak_days`。页面：「今日签到」卡用真实连续，「签到活动」卡用周期内天数。
  **签到日期三源合并**：① 接口当期 `checkin_dates` ② 本地累积 `checkin_history.json`
  ③ **从 L6 资源包反推**（`derive_checkin_dates()`：签到奖励 = size 等于当日奖励额的散包，
  `CycleStartTime` 即签到日；历史档位登记在 `CHECKIN_CREDIT_ALIASES` —— 奖励额调整过，
  两个档位的日期**互不重叠**）。③ 是关键：**只有它能跨设备回溯更早历史**。
  **局限**：奖励包被清理后不可回溯 ⇒ 该值是**下界**（只会少算，不会多算）
- **「预计可用天数」用 30 天窗口 + 不估算护栏**（v1.4.4 定案）：
  `DAILY_AVG_WINDOW_DAYS = 30`（7 天窗口会被「最近一周恰好没怎么用」主导，实测估算出
  近 5 位数的荒谬天数）；`DAYS_LEFT_MIN_AVG = 1.0` 以下不估算、页面显示 `—`，
  副标题给 `active_days_30d` 说明是样本稀。
  **不要改成「剔除低消耗日再平均」** —— 会引入主观阈值，且实测样本常为空
- **每源独立 try/except**：L5/L6 任一失败不得连累整页，降级 + 徽章标红

## 五、工作日历（v1.7.0）

**要解决的**：批次若在**周末或长假**到期，那几天用户不会打开工作台 —— 等他回来时可能已经过期。
原来的提醒只按"小时窗口"（默认 24h）算，覆盖不到这个盲区。

**设计取舍（重要，别推翻）**：

- 工作日历**只用于决定「什么时候提醒」**，**不参与任何消耗口径计算**
- 为什么不用"工作日日均"算「预计可用天数」：那会算错 ——
  `剩余 ÷ 工作日日均` 得到的是"还能用多少个**工作日**"，而用户关心的是"**到哪天**用完"，
  日子是按**日历天**流逝的。用工作日口径，等于把"工作日强度"错当成"日历流逝速度"
  （例：1000 分、工作日日均 20 → 说 50 天，实际能用 68 天）

**判断顺序**：**调休上班 → 法定放假 → 用户勾选的星期几**
（调休必须排在放假判断之前，否则"落在放假区间里的调休上班日"会判错）

**配置**（存浏览器 localStorage —— 提醒本来就是纯前端行为，配置跟着它走才一致）：

| 键 | 含义 |
|---|---|
| `credits_work_weekdays` | `"1,2,3,4,5"`（ISO：1=周一 … 7=周日） |
| `credits_use_holidays` | `'1'` = 按国务院安排顺延 |

**触发规则**：`checkExpiryAlert()` 的筛选从「① 小时窗口内」扩展为
「① 小时窗口内 **或** ② 剩余工作日 ≤ 阈值（默认 1）」。
② 让"10/8 到期"的批次在 **9/30** 就能提醒到（中间隔着国庆 7 天假，工作日数为 0）。

**调试**：页面暴露了只读的 `window.CREDITS_CAL`（`isWorkday` / `workdaysUntil`），
可以在控制台直接核对"为什么这天没提醒"。

**节假日数据维护（每年一次）与「到点提醒」**：

- 数据文件：`assets/holidays.json`
- 来源：国务院办公厅《关于XXXX年部分节假日安排的通知》（通常**前一年 11 月**发布）
- **次年安排未公布时**：`load_holiday_calendar()` 该年为缺失 → `isWorkday()`
  降级为**只判周六周日**（不会误判成"天天上班"）
- ⇒ **每年 11 月前后需更新一次**，否则次年会出现"假期被当成工作日"的静默失准
- **兜底机制（v1.7.0）**：漏更新是**静默**的（用户察觉不到），所以到点主动提醒 ——
  `holiday_data_status(today=None)` 算出「覆盖到哪年 / 该有哪年 / 缺哪年」，
  随 `/api/overview` 的 `holiday_meta` 下发：
  - **当年恒需**；进入 **11 月**后连**次年**一起需要（此时次年安排按惯例已公布）
  - 前端 `renderHolidayNote()` 据此显示顶部提示条，**两档措辞**：
    只缺次年 = 常规「该更新了」；当年也缺 = 已降级，措辞加严
  - 两个分寸：**用户关掉了「按国务院安排顺延」就不打扰**（这份表对他不起作用）；
    关闭状态按**缺失年份集合**记（`credits_holiday_notice_dismissed`），
    年份集合变化会**重新提醒**，不会点一次就永久沉默
  - 「提醒设置」弹窗里另常驻一行数据状态（覆盖到哪年 / 更新于何时）
  - ⚠️ 判断"该有哪年"用**服务器当前时间**（后端），"是不是当年缺"用**浏览器当前时间**
    （前端 `thisYear`）—— 同机同刻，二者一致；若将来做跨时区部署需重新审视
- **这张表为什么值得这样兜**：它一年只动一次，最容易忘；而忘了的后果是**静默降级**，
  没有任何报错能提示你 —— 只能靠主动提醒

**数据更新走「发版」而非「本机维护」（2026-09-26 定案）**

一度想过让用户在本机补全年份数据（「数据目录优先的覆写层」）。**决定不做**：

- 年更数据由**发版**带出：维护者更新仓库 `assets/holidays.json` → 发布新版本 →
  用户升级 skill 得到它
- 因此 `holidays.json` 放在 `assets/`（skill 包内）**是正确的** —— 它就是**出厂数据**，
  随版本分发给所有使用者；不必再引入"用户覆写层"（少一个分层、少一处口径）
- 配套正好闭环：**v1.7.1 的到点提醒**告诉维护者"该发版了"；
  **v1.7.2 的新版本提示**告诉用户"该升级了"
- ⇒ 所有提示文案的行动指引统一到「**升级**」（对 Agent 说「更新积分 skill」），
  **不要**教用户去改 skill 包内的文件 —— 否则他改了，下一次同步 / 升级又被覆盖

**教训：能力做完 ≠ 用上了（v1.7.3）**

v1.7.0 做出了工作日历，但只用在「到期提醒」的顺延上；批次表的到期标识**照旧按日历天**
（≤3 红 / ≤7 橙）—— 于是出现反直觉的结果：

> 10/4 到期（8 天）**不标**，9/30 到期（5 天）**标了** —— 而两者**实际都只剩 3 个工作日**
>（中间隔着国庆 7 天假，那几天不产生行动窗口）。

⇒ v1.7.3 把日历能力**接到用户会看的界面元素上**：给"超出 7 天但落在法定节假日"的批次
加了独立一档（紫色「假期内到期」）+ 批次表上方汇总 + 概览区**独立紫色 KPI 卡**
（最初只写在「即将到期」的副标题里，一行灰字存在感太弱，遂升级为独立卡）。
**新增能力必须同时接到界面上，否则对用户等于不存在。**

两条设计约束（勿推翻）：

- 新档**只看国务院法定节假日、不看普通周末** —— 约四成批次落在周末，全标出来会淹没重点
- 三档**互斥**：`due-soon` / `due-urgent` 仍是**日历天**口径、仍与 `at_risk_count` 严格对齐
  （这条对账判据不能被打乱）；新档只补"日历天够不着、而假期要吃掉"的那一段

**测试约定**：这套逻辑的产出是**前端渲染**（提示条显不显示、文案、关闭记忆），
与取数通道无关 —— 所以验证时用**本地静态数据 + 拦截 `/api/*`** 喂数，
既不受浏览器桥/会话状态影响，也能构造"到了 11 月但次年数据缺失"这类**真实时间里测不到**的场景。

无头验证的**可复现手段**（已跑通）：用 **jsdom**（Node 工作区已装）渲染 `dashboard.html`，
`beforeParse(window)` 里注入三样：
① `echarts` 桩（`init→{setOption,resize,dispose}`、`getInstanceByDom→null`）；
② `fetch` 桩 —— `/api/version`→`{version}`（进 panels 模式）、`/api/overview`→mock 数据、
`/api/requests|lifecycle`→空表；③ `url` 用 `http://localhost:8090/`（**不能 file://**：
file 协议下 jsdom 的 localStorage 是 opaque origin、且 fetch 不存在，两条路都断）。
开/关「按节假日顺延」用 `localStorage.setItem('credits_use_holidays','0|1')` 在 beforeParse 预置，
每个用例新起一个 JSDOM。断言只数数字（卡 value == 表内紫行数 == 汇总批数；
soon+urgent 行数 == `at_risk_count`），不靠目测。

## 六、环境与踩坑

- **⚠️ 解释器陷阱（v1.5.1，代价高）**：双击启动器走的是 **WorkBuddy 内置 Python**
  （`~/.workbuddy/binaries/python/versions/...`），它**没有 Playwright**；
  Playwright 通常装在系统 Python 里。
  ⇒ **"开发机能跑"≠"用户那边能跑"**：交付前**必须用与用户相同的入口验证**
  （用内置 Python 跑 `launcher.py`，而不是直接跑 `serve.py`）。
  `launcher.py` 现已自动探测解释器（同目录 → PATH → 注册表 → py launcher），
  挑装了 Playwright 的那个；桥在缺依赖时**秒级报错**（原来会卡满 120s）
- **VBS 直接写系统 Python**（2026-09-26）：`gen_vbs()` 不再照搬内置 pythonw 进 VBS，
  而是用 `_find_launcher_pythonw()` 探测装了 Playwright 的解释器、取其 `pythonw.exe` 写进 VBS
  —— 双击一步到位用系统 Python（默认即「playwright 用的那个」），省去「内置冷启动 + 探测系统」中转。
  重新生成 VBS 后要读一下确认它指向的是系统 pythonw 而非内置 pythonw
- **凡"等待就绪"的同步原语，每条退出路径（含异常分支）都必须置位** ——
  漏一个 `_ready.set()` 就把「立即报错」变成「无限转圈」，用户完全无从判断
- **「端口占用」≠「服务可用」**（v1.4.1 定案）：旧进程会一直占着端口，启动器若只判端口
  就永远打开旧版页面。`launcher.py::_ensure_running()` 必须做**端口 + 版本双校验**
  （探 `/api/version` 与 `manifest.yaml` 比对，不符则结束旧进程再拉起）；
  杀进程前必须确认是 python 系进程，非 python 占用只提示、不误杀
- **排查入口：面板转圈 / 刷新报「无法加载数据」** → 先看服务是否还在
  （`GET /api/version` 是否响应）。服务不在时前端会一路兜底到静态文件，
  报错文案要能指向「启动服务」而不是「跑 render」（v1.5.3 修正过这个误导）
- **serve.py 常驻，不重启不加载新码**；**自更新陷阱**：Release 包内 `update.py` 会覆盖
  本地版本，修复须先发新版
- **技能默认直连、不读系统与环境代理**：`_api_call` 用 `build_opener(ProxyHandler({}))`；
  `update.py` 同约定。根因：urllib 把「进程首次请求时的代理」冻在模块级 `_opener`
  → 服务经历「代理开着启动 → 后关掉」必 `WinError 10061` 且不自愈；**改完须重启服务**
- **git push 的代理是 URL 特定键**（`.gitconfig` 的 `http.https://github.com.proxy`）：
  `unset` 与 `-c http.proxy=` **都无效** → 须 `git -c "http.https://github.com.proxy=" push`
- **改完须同步到已安装目录**（否则跑旧码）：`SKILL.md` / `README.md` / `manifest.yaml` /
  `dashboard.html` / `.gitignore` / `scripts/*.py` / `assets/*` / `references/*`，**不含** `dist/`。
  **新增的文件也要进同步清单**
- 行尾权威是 `.gitattributes`（eol=lf）；需要时用
  `git add --renormalize .` + 检出刷新

## 七、发布流程

1. `manifest.yaml` 升 version（**必须升** —— 启动器的版本自检靠它，见「环境与踩坑」）
2. **先写好 README 的 `### vX.Y.Z — 摘要` 段**（Release 正文取自它，见下）
3. `git commit` → `git push origin main`
4. `git tag vX.Y.Z && git push origin vX.Y.Z`
5. CI（`.github/workflows/release.yml`）打包并建 Release

- **Release 正文取自 README 更新日志**（v1.4.3 起）：CI 调
  `scripts/extract_changelog.py <tag>` 生成 `--notes-file`，取不到才回退 `--generate-notes`。
  ⚠️ **务必先写好 README 段再打 tag** —— 本项目无 PR，自动 notes 只有一行 Full Changelog
- **正文范围由 tag 历史自动定界**（v1.7.2 起）：本项目是"改完攒着、验收后一起发"的节奏，
  一个 tag 常涵盖**多个**版本段。CI 用 `git describe --tags --abbrev=0 HEAD^` 取上一个
  已发布 tag，传给 `extract_changelog.py --since <prev>` → 一次带出所有未发布的版本段。
  **因此 README 里不再需要任何"本版包含 X~Y"的人工说明**（写过两次，每次都要手工搬、且必过期）
- `build_dist.py` 白名单式收集：`SKILL.md` / `manifest.yaml` / `README.md` / `dashboard.html` /
  `.gitignore` / `.gitattributes` / `assets/` / `references/` / `scripts/*.py`（排除自身）
  —— 新增顶层目录/文件时须确认在收集范围内

**版本检查（`/api/update`，v1.7.2）**——让用户端知道"该升级了"：

- 后端 `update_status()` 查 GitHub Release 最新版本，与 `manifest.yaml` 比对。
  **取数与版本比较复用 `update.py`**（`latest_release` / `cmp_version`），不写第二套逻辑
- **联网永远在后台线程**：请求线程只读缓存，缓存空也立即返回（绝不让一次 GitHub 不通卡住页面）；
  服务启动时预热一次，用户打开页面时通常已有结果
- 缓存 `~/.workbuddy/workbuddy-credits-data/update_check.json`：**成功 12h / 失败 30min** 过期
  —— 失败也缓存，是为了避免每次开页面都去撞一个不通的网络
- 前端延迟 1.5s 发起、失败**静默**（不因"查更新失败"打扰用户）；
  「知道了」按**远端版本号**记忆，出了更新的版本会重新提示
- ⚠️ **只提示、不改任何本地文件** —— 升级始终由用户显式跑 `python update.py`
  （Release 包 + sha256 校验 + 自动重启服务）。这是刻意的：改动本地文件必须用户触发

## 八、当前状态

> 每次**发版**后更新这一节。
> ⚠️ **不要写 commit hash** —— 它每次提交都变，写进来必然过时（写"未提交"同理）。
> 只记「哪些版本已发布」和「哪些功能已提交、待发版」。

- **已发布**（tag + CI + Release）：v1.3.0 / v1.3.1 / v1.4.1（含 v1.4.0）/ v1.4.3（含 v1.4.2）/ v1.7.3 / v1.7.4 / v1.7.5 / v1.7.6
- **已发布（2026-09-26 一次攒批发，tag v1.7.3 涵盖）**：v1.4.4 ~ v1.7.3
  - v1.4.4 —— 签到卡区分「真实连续」与「活动周期内」＋ 可用天数改 30 天窗口
  - v1.4.5 —— 凭据加密后优雅降级
  - v1.5.0 —— 取数通道改「浏览器桥」（凭据不落本进程）
  - v1.5.1 —— 修「双击启动器后面板一直转圈」（解释器探测 + 桥快速失败）
  - v1.5.2 —— 恢复「昵称 / 角色」（新增 `GET /console/account`）
  - v1.5.3 —— 修「服务掉线时点刷新」的误导提示
  - v1.6.0 —— 体感优化（服务端预热 + 首屏缓存 + 桥攒批并发）
  - v1.6.1 —— 「积分批次」表标出临近到期（`expiry_list` 补 `exp_ts`；
    ≤3 天红 / ≤7 天橙；行底色 + 左侧色条 + 状态列小标签）
  - v1.7.0 —— 到期提醒按「工作日历」顺延（可配置上班日 + 内置法定节假日含调休；
    触发条件加「剩余工作日 ≤ 阈值」；新增 `assets/holidays.json`）
  - v1.7.1 —— 节假日数据「该更新了」到点提醒（`holiday_data_status()` +
    `/api/overview` 的 `holiday_meta` + 顶部提示条；兜住"次年数据忘更新"的静默降级）
  - v1.7.2 —— 工作台提示「有新版本」（`/api/update` + 12h/30min 缓存 + 后台联网 + 失败静默）；
    `extract_changelog.py --since` 让 Release 正文范围由 tag 历史自动定界
  - v1.7.3 —— 「积分批次」标出落在法定节假日的批次（`due-holiday` 独立一档 +
    批次表汇总 + 概览区独立紫色 KPI 卡；把 v1.7.0 的工作日历真正接到界面上）
- **已发布（v1.7.4，2026-09-26）**：浏览器桥兼容 Edge（Edge 优先、Chrome 回退）；
  双击启动器 VBS 直接用系统 Python；SKILL.md 新增「安装前置」清单
- **已发布（v1.7.5，2026-09-26）**：新版本检查成功缓存 12h→1h（修「发新版后最多 12h 才提示」）；
  提示条新增「立即升级」一键升级（`/api/update-run` 子进程跑 update.py + 前端轮询版本自动刷新）
- **已发布（v1.7.6，2026-09-26）**：修三个问题 —— ① Chrome/Edge 混用同一 profile 导致会话失效
  （profile 按浏览器分目录 + 记住上次登录浏览器）② 登录弹窗弹屏幕外（`--window-position=80,80`）
  ③ 登录成功后不自动刷新（判定改读 session cookie 到期时间 + 前端兜底）
- **发版提示（v1.7.2 起已简化）**：正文范围由 CI 用 `git describe --tags --abbrev=0 HEAD^`
  自动定界，**README 里不再需要写「本版包含 X~Y」**。
  仍需注意的唯一一件事：**先写好 README 段再打 tag**（否则只能回退到自动 notes）
- **版本号是启动器的自检依据**：`launcher.py` 用「端口 + 版本」双校验决定是否结束旧进程
  重拉服务。所以**代码变了就必须升 `manifest.yaml` 的版本号** ——
  否则用户的旧进程会被判为"版本一致"而**不被替换**，改动看不到效果（v1.4.1 定案）
- **版本语义**：v1.4.0 首屏并发提速 + 分面板异步/局部刷新；v1.4.1 启动器版本自检 +
  前后端能力探测；v1.4.2 订阅型容量改用周期口径；v1.4.3 修「今日已使用」口径错位 + 全面口径自查
