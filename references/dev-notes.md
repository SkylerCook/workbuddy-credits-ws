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
5. **任务栏不监听窗口样式变化**（v1.7.9 踩坑）：给静默窗口加 `WS_EX_TOOLWINDOW` 想让它
   不进任务栏，但**仅改样式 + SetWindowPos(FRAMECHANGED) 不够** —— 任务栏按钮在窗口
   创建时就建好了，必须走一轮 `SW_HIDE → SW_SHOWNA`（重显示不抢焦点）让 shell 重新
   评估才移除。验收判据要看**任务栏图标是否真消失**，不能只看 EXSTYLE 位（样式设了≠图标没了）。

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
- **每个 UI 筛选项必须由它的数据源兑现**（v1.7.20 定案）：L3 资源接口**根本不返回已过期包**
  （过期包只在 L6），所以在 L3 那侧的下拉里放「已过期」就是**假选项** —— 永远命中 0 条，
  用户只会看到「无匹配批次」，误以为丢数据。跨源能力要么跳转，要么合并页签，
  **绝不放宽本表口径**去凑一个筛选项。
- **同一个对象池不得开两张并列的表**（v1.7.21 定案）：曾同时存在「积分批次」（L3 41 条）与
  「包生命周期 · 有效期内」（L6 40 条），按 `(name, cycle_end)` 求交 **40/41 完全重合**，
  列 / 排序 / 到期标记全同 —— 等于把同一批包印了两遍，而且是**两套状态口径**
  （前端按余额推算 vs 服务端状态码），服务端一延迟就会一处「可用」、一处「已过期」。
  现合并为「积分批次与生命周期」一个面板三个页签（待用清单 L3 / 有效期内 L6 / 已过期 L6）。
  **判断标准**：同一批对象、同样列、同样排序 → 只能是同一个面板的不同页签；
  只有当两侧字段与口径都确实不同（如同一批的"调度视角"与"审计视角"）才配各占一个页签，
  且必须在页签下写明数据源徽章。
- **合并后仍要补齐能力差**（v1.7.21）：L6 只返回 `capacity_type=1`，**订阅型（套餐用量）
  只有 L3 给** —— 合并时不能只留下 L6，否则那 500 积分会从页面上静默消失。
  「待用清单」页签因此保留 L3 数据源，并在点行时按 `(name, cycle_end)` 回查 L6：
  有就开完整明细，没有（仅订阅型）开简版并**明说接口不提供**，不要用「—」糊过去。
- **空列表绝不等于没数据 —— 读失败必须显形**（v1.7.22 定案）：`load_usage()` 曾把
  `except` 住的异常一律返回 `[]`，于是「本地库打不开」在页面上长得和「你没有用过量」
  一模一样（都是「暂无记录」）。凡是从**文件 / 数据库**读数的函数，返回空必须能区分
  `不存在 / 打不开 / 打开但为空 / 有数据但被筛选掉`，并把原因暴露到 `sources_health`；
  前端空状态**按原因分别写文案**，读取失败时要明确说「空着不代表没有消耗」并给出替代入口。
- **SQLite WAL 库的只读连接仍需要写权限**（v1.7.22 踩坑）：`mode=ro` 打开 WAL 模式的库时，
  SQLite 仍要创建/写 `-shm`；目录不可写就报 `unable to open database file` —— 文件明明可读、
  `os.path.exists` 也返回 True。回退顺序固定为 `mode=ro` → `mode=ro&immutable=1`
  （只读快照，完全不碰 `-shm`/`-wal`，代价是可能漏掉最近未 checkpoint 的写入）→ 普通连接；
  用了回退必须在健康徽章里注明「只读快照模式」。
- **聚合函数的「空输入」返回值必须有可信度戳**（v1.7.23 定案，比空表更危险的一类）：
  `compute_waste_authoritative([])` 返回的是**字段齐全的全零对象** —— 它和「真的没有浪费」
  在结构上完全一样。L6 挂掉时前端照常渲染，就成了「已过期损失 0.00 · 服务端权威」：
  **一个带着权威口吻的假零**，比一张空表更容易让人相信。规则：
  ① 所有「求和 / 计数」型聚合，输入为空时**必须**同时输出 `degraded`（或有等价的可信度字段）；
  ② 前端遇到 `degraded` 一律显示 `—` ＋原因，**不得**显示 0；
  ③ 任何带「权威 / 服务端直出」字样的位置，必须能证明这一份数据确实来自服务端。
  同理，**别拿错源当兜底**：概览「收支损失 · 已过期未用完」原先取 L3 推算值，
  而 L3 不返回已过期包 ⇒ 这张卡结构上恒为 0，属于永久假零；已统一到 L6。
- **一张空表至少有四种含义，不能合并成一句话**（v1.7.23）：取数失败 / 文件或库不存在 /
  源正常但范围内确实没有 / 有数据但被筛选掉。四者的用户动作完全不同
  （重试 / 修数据源 / 放宽范围 / 清空搜索）。请求流水表已按四态分开，并显示
  `requests_meta.error` 原文 —— **不要把错误原文藏进 hover**。
- **不导出空文件**（v1.7.23）：表格为空时导出会得到一个「只有表头」的 CSV，
  下载成功、打开空白，用户会认为「导出没问题，只是没数据」。空表一律不生成文件并给出提示；
  正常导出回报行数，让「导出了几行」可核对。
- **禁止原生 `alert` / `confirm` / `prompt`**（v1.7.24）：全工作台统一走
  `uiDialog()` / `uiAlert()` / `uiConfirm()`（返回 `Promise<boolean>`）+ `toast()`。
  原因不是好看：① 原生 `alert` **阻塞 JS 绘制** —— 代码里改了按钮文案也刷不出来，
  实测出现过按钮卡死在「保存中…」；② 无法跟随主题、无层级、不能放代码片段与多段说明；
  ③ `confirm` 是同步阻塞，改写过程中会把流程切成回调地狱（改用 `async` + `await` 最省事）。
  换行用 `\n`（`.dlg-msg` 有 `white-space: pre-line`），行内代码用反引号包住（`uiRich()` 转 `<code>`）。
  **轻提示（可忽略的结果）用 `toast`，需要人停下来看或用代码指引的才用 `uiDialog`。**

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

## 七、自动签到（v1.7.24）

**为什么是 Windows 计划任务，而不是服务里的定时器**

服务有闲时看门狗：页面全部关闭约 75 秒后自己退出（v1.7.13 收紧过）。定时器活在服务进程里，
就意味着「用户关掉页面 = 自动签到失效」，而且**不会报错** —— 只表现为开关开着、从没签上。
计划任务是唯一与「工作台开不开」解耦的宿主。代价：系统里多一个任务条目（用户可在任务计划程序里看到）。

**任务定义**（`scripts/auto_checkin.py` → `build_xml`）

| 项 | 值 | 理由 |
|---|---|---|
| 触发 | 每天 `09:30`（可在工作台改） | 上班时段签，奖励当天到账 |
| `StartWhenAvailable` | true | 关机 / 休眠错过 → 下次**登录**后补跑（InteractiveToken 没有会话时不会跑） |
| `RestartOnFailure` | 3 次 / 10 分钟 | 只在**退出码非零**时触发 |
| `LogonType` | InteractiveToken | 需要用户的浏览器会话；不要存密码 |
| `RunOnlyIfNetworkAvailable` | true | 签到必须联网，离线跑只会失败刷重试 |

**退出码是契约**：签成功 / 今日已签 → `exit 0`；失败 → `exit 1`（才会触发重试）。
写代码时若把失败也 `exit 0`，表现为「每天准时什么都不做」。

**四条硬约束**

1. **`pythonw` 下 `sys.stdout` 为 `None`** —— 任何 `print` 都 `AttributeError` 崩进程，
   而计划任务里的崩溃**完全静默**（任务计划程序只留退出码）。入口第一件事是
   `_takeover_stdout()` 把输出接到 `auto_checkin_run.log`；捕获 `cmd_checkin` 输出时
   还要滤掉 `[session]` 这类桥日志，否则会混进弹窗的「上次运行」。
2. **不能与工作台抢浏览器 profile** —— 桥用按浏览器分目录的**持久化** profile。
   故 `run_once` 先读 `serve_state.json` 的 `port` 探活：服务在跑就调它的 `/api/checkin`
   （`via: service`），没在跑才自己拉 Chrome（`via: direct`，完事必须 `bridge_close()`，
   否则计划任务每跑一次留一个 Chrome）。
3. **`schtasks /XML` 只认 UTF-16 带 BOM** —— 用 `open(path, "w", encoding="utf-16")`。
   写成 UTF-8 会报「任务 XML 格式不正确」，错误信息完全不指向编码。
4. **`<Settings>` 里不能出现 `DisallowStartOnRemoteAppSession` / `UseUnifiedSchedulingEngine`**
   —— 本机解析器**直接拒收**：`错误: 任务 XML 包含意外节点。(38,7):DisallowStartOnRemoteAppSession:`
   （补上配套的 `UseUnifiedSchedulingEngine` 后换成它被拒）。这两个节点只影响远程 App 会话，
   跟定时签到无关，已从模板移除。**不要再从网上抄「完整版」任务 XML** —— 抄来就用会卡在这一步。
   校验办法见下（不依赖 schtasks）。

**XML 校验器（schtasks 被拦时也能用）**

```
PowerShell> Register-ScheduledTask -TaskName Probe -Xml ([xml](Get-Content -Raw -Encoding Unicode <xml路径>)).OuterXml -Force
```
`Register-ScheduledTask` 走 Task Scheduler COM，与 `schtasks /Create /XML` **共用同一个 XML 解析器**
（报错文案一模一样，只是行列号指向整个字符串而不是文件行），所以「COM 能注册 = schtasks 也能注册」。
验证完记得 `Unregister-ScheduledTask -TaskName Probe`。想连动作一起验，就 `Start-ScheduledTask`
再去看 `auto_checkin_log.json` 有没有新记录 —— 这条能验出 pythonw 路径 / 参数引号 / 工作目录对不对，
而这三样出错时计划任务是**静默失败**的。

**弹窗里的失败提示不要用原生 `alert`**：`alert` 会阻塞绘制，代码里把按钮文案改回「保存」
也刷不出来，用户看到的是卡在「保存中…」的死按钮。改成弹窗内联的 `.ac-warn.bad` 提示块；
失败时同时把开关 `acResetToggle()` 拨回真实状态，否则「已开启」与「未注册」并列自相矛盾。

**任务存在性必须三态**（`task_state()` → `True / False / None`）

`schtasks /Query` 在权限不足、被安全软件拦、schtasks 不可用时同样返回非零，与
「任务确实不存在」无法区分。第一版用 `rc == 0` 判，界面把「查询失败」写成了「未注册」——
又一次「读不到被画成没有」。现在 `None` 表示查不到并带 `task_query_error` 原文，
前端显示「⚠ 查询失败：<原因>」，`status()` 里 `enabled`（用户意图）与 `verified`（系统实况）分开。

**「工具起不来」≠「工具拒绝」**（`_run_schtasks` → `rc == -1`）

`CreateProcess` 失败（被安全软件/系统策略拦截、组件缺失）时我们只能自己造返回值。
若与 schtasks 真的返回非零混成一个 `rc != 0`，界面就会把「schtasks 根本没启动」写成
「注册被拒」—— 用户照着「权限不足」去折腾，方向完全错。现在 `rc == -1` 表示工具本身
没起来，`_fail_payload()` 单独给措辞与 `hint`；`task_state()` 也必须先判 `rc == -1`
再做关键词匹配，否则「找不到 schtasks.exe」里的「找不到」会被判成「任务不存在」。

**schtasks 走绝对路径，不靠 PATH**（`_schtasks_exe()`）

PATH 会被父进程改写（工具垫片目录常被插到最前），解析到的可能不是系统那个。
取 `%SystemRoot%\System32\schtasks.exe`，32 位进程回退 `Sysnative`，都找不到才退回裸名。

**真实案例（v1.7.24 交付时）**：用户在页面点「开启」，弹出
`注册计划任务失败（schtasks 返回 1） / 调用 schtasks 失败：[WinError 5] 拒绝访问`。
根因不是权限 —— 当时 8090 上的服务是**从受限执行环境里拉起来的**，继承了拦截垫片，
连 `schtasks.exe` 都起不来。教训：**改完这类依赖系统工具的功能，先确认服务是自己
（用户）启动的进程，再让用户点按钮**，否则验的是测试环境而不是产品。

**关闭的顺序不能反**：先写配置 `enabled=false`，再尽力删任务。反过来会在「查不到任务」
的环境里必然失败，把一次「已关闭」误报成「关闭失败」，用户卡在关不掉。无法确认删除时
返回 `warning`（开关已关，但旧任务可能仍存在）。

**排查**（按顺序看）

1. 弹窗「计划任务」那行：`schtasks 被拦截，无法启动` = 工具本身起不来（安全软件/策略，与权限无关）；
   `查询失败：<原文>` = 调通了但被拒（多为权限）。两者处理方式完全不同，别混着查
2. `~/.workbuddy/workbuddy-credits-data/auto_checkin_run.log` —— 每次运行的完整输出（含桥日志）
3. `auto_checkin_log.json` —— `last` + 最近 60 条 `{at, result, via, message}`
4. 任务计划程序 → 任务名称 `WorkBuddyCreditsAutoCheckin` → 「上次运行结果」
5. 弹窗里「立即试跑一次」走 `schtasks /Run`，与定时触发是同一套动作定义 ——
   注册成功 ≠ 能跑起来，这一步就是为此存在

## 八、工作台渲染踩坑（v1.7.25）

> 这一节记的是**图表库 / CSS / 滚动定位**层面的通用教训，与积分业务无关，
> 但改任何一块面板都会再遇到。改 `dashboard.html` 前值得先扫一遍。

### 8.1 ECharts 坐标系选型：calendar 不适合做点阵

`calendar` 坐标系的横轴按周、纵轴按星期，**两轴刻度宽高完全独立**，
没有任何 `itemStyle` 参数能让格子变方 —— 实测画出来是 33×15 的扁横条。

要做 GitHub 式贡献点阵，正确的是 `scatter` + 两条 `category` 轴：

- `symbolSize: [cell, cell]` 直接是**像素值**，与轴刻度宽度解耦；
- `grid.height = 7 × 列宽`，让 y 方向一个单位等于 x 方向一个单位 ⇒ 格子天然正方；
- 容器高度用 `el.style.height` 精确写入，不要靠 `grid` 自适应（会随列距被撑高）；
- 矩阵必须补成**完整矩形**：起点回退到首周周一（`-((getDay()+6)%7)`），否则首周残缺、
  整块错位；
- 两张图（消耗 / 收入）**共用同一条时间轴**，各自算起点会上下错开半格；
- 色阶上限取 **P90 而非 max**：一个单日尖峰会压平整个梯度，四级色阶退化成两级。

### 8.2 `calc()` 里的比例变量必须无单位

```css
width: calc(var(--w) * 100%);   /* --w: 0.48  ✅  */
width: calc(var(--w) * 100%);   /* --w: 48%   ❌  */
```

带 `%` 的字符串进不了 `calc()` 乘法，**不报错**，只是整条规则失效 ——
表现是「条形长度与轨道对不上」，很容易误判成宽度计算逻辑错了。

### 8.3 滚动高亮靠遍历顺序，不靠坐标

```js
Object.entries(sectionToNav).forEach(([id, nav]) => {
  if (section.getBoundingClientRect().top <= offset) activeNav = nav;  // 最后赋值者胜出
});
```

`sectionToNav` 的**键顺序必须与 DOM 顺序一致**，否则高亮会固定指向上一个区块。
调整面板顺序时，三处要一起改：**DOM 顺序、导航锚点 href、`sectionToNav` 键顺序**，
外加滚动到底时的兜底（`activeNav` 指定最后一项）。

### 8.4 `scrollIntoView` 用的是「那一瞬间」的布局

面板异步加载时，目标面板还是骨架、页面总高不足，`scrollIntoView` 只能滚到当时底部；
数据到齐后面板长高，滚动位置不再对应（实测偏差 1407px）。

修法是短时**锚点守护**而不是加大延时：

- 4 秒窗口，检测到面板仍 `loading` 就续期 1.2 秒；
- 静默 450ms（等平滑滚动走完）后再开始每 100ms 对齐；
- 用户 `wheel / touchstart / 翻页键` 立即 `stop` —— **绝不跟用户抢滚动条**。

### 8.5 CSS 动画时钟在页面繁忙时严重滞后

`opacity: 0` + `animation-delay` 做淡入，实测骨架已显示 600ms 时 `currentTime` 才 200ms，
`opacity` 仍是 0 —— 表现是「骨架/提示长期不可见」，而 DOM 里明明有。
**必须让人看见的东西不要用动画入场**，直接显示。同理，用 `animation` 做进度提示也要设上限。

### 8.6 骨架/占位清理必须限定作用域

```js
document.querySelectorAll('.panel-skeleton').forEach(n => n.remove());  // ❌
```

多面板并行加载时，一个面板完成就会删掉**另一个**的骨架。
应按面板的 section 容器限定范围（`PANEL_SECTIONS[panel]`），只清自己那一块。

另外：往列表里**插入前置元素**后，`.item:nth-child(n)` 序号全错。
宽度这类按位置的样式，用**类名**（`.w2` / `.w3`）而不是 `nth-child`。

### 8.7 状态存 JS 对象，不存 DOM

`reqState` / `lcState` / `panelState` 这类状态对象是唯一真相，渲染只是它的投影。
筛选必须走**单一过滤入口**（`reqFilterFn`），让搜索 / 导出 / 分页共用同一份过滤结果 ——
分别实现必然出现「搜出来的和导出的不一致」。

### 8.8 弹窗不要用原生 `alert` / `confirm`

原生 `alert` 阻塞 JS 绘制，表现为「按钮卡在保存中…」；且无法跟随主题、不能放代码片段。
统一走 `uiDialog / uiAlert / uiConfirm`（Promise 语义）+ `toast`（不打断）。
判断用哪个：**轻提示走 toast，需要人停下来看或要给命令指引的走弹窗**。

## 九、发布流程

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

## 十、当前状态

> 每次**发版**后更新这一节。
> ⚠️ **不要写 commit hash** —— 它每次提交都变，写进来必然过时（写"未提交"同理）。
> 只记「哪些版本已发布」和「哪些功能已提交、待发版」。

- **已发布**（tag + CI + Release）：v1.3.0 / v1.3.1 / v1.4.1（含 v1.4.0）/ v1.4.3（含 v1.4.2）/ v1.7.3 / v1.7.4 / v1.7.5 / v1.7.6 / v1.7.7 / v1.7.8 / v1.7.9 / v1.7.10 / v1.7.11 / v1.7.12 / v1.7.13 / v1.7.14 / v1.7.16（含 v1.7.15）/ v1.7.18（含 v1.7.17）/ v1.7.19 / v1.7.23（含 v1.7.20 / v1.7.21 / v1.7.22）/ v1.7.25（含 v1.7.24）
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
- **已发布（v1.7.7，2026-09-26）**：「预计可用天数」观测窗口可界面配置（`avg_window_days`，
  默认 30 / 限 7~90，存 `config.json`）+ 账号类型空值兜底为「个人版」
- **已发布（v1.7.8，2026-09-26）**：套餐档位显示（`plan_tier`：体验版→Free / 标准版→Plus /
  高级版→Pro / 旗舰版→Max，**Pro 高于 Plus** 按通用英文直觉）+ 彩色渐变徽章 +
  修 `plan_tier` 漏加 `_PANEL_KEYS` 白名单 + 浏览器意外关闭自动重建（`_looks_closed`/`_eval_calls`）
  + 表格字号 13→14
- **已发布（v1.7.9，2026-09-26）**：① 升级进度可见（update.py 输出落 `update_last.log` +
  `/api/update-log` + 提示条实时显示当前步骤）② 静默浏览器窗口移出任务栏/Alt-Tab
  （`WS_EX_TOOLWINDOW` + `SW_HIDE→SW_SHOWNA` 刷新）。
  **升级验证**：本机「回退 1.7.8 → 发版 → 点立即升级」全自动通过（覆盖+重启+服务变 1.7.9），
  任务栏隐藏生效。**注意**：验收任务栏时别把「双击启动器自动弹出的日常 Edge（看工作台）」
  误当桥窗口 —— 桥窗口标题是「WorkBuddy - 个人中心 - 用户配置 1」，日常 Edge 标题是
  「WorkBuddy 积分工作台 + N 个页面」。区分 pid：桥进程命令行含 `browser_profile`。
- **已发布（v1.7.10，2026-09-26）**：① 静默窗口改 **SW_HIDE 彻底隐藏**（TOOLWINDOW+SHOWNA
  对 Edge 不可靠，显示回来图标回归）② 取数浏览器可配置（auto/chrome/msedge，存
  `config.json`；auto 登录后 `confirm_pref_channel` 固化为实际浏览器）③ 修「升级完仍提示
  有新版本」（`_rebase_update_cache` 用当前 VERSION 重算 state）④ 关闭工作台自动退出后台
  （心跳 `/api/heartbeat` + pagehide `sendBeacon('/api/bye')` + `_idle_watchdog` 两路）。
  **踩坑**：sendBeacon 强制 POST，serve.py 只有 do_GET → 补 `do_POST` 接 `/api/bye`。
- **已发布（v1.7.11，2026-09-26）**：① 修「重新登录假成功」（`_collect_session` 判定收紧为
  `KEYCLOAK_SESSION` 存在 或 `session`/`session_2` 值长度 ≥1000；匿名 session 339B 不再误判——
  否则切到未登录 profile / 全新安装 / 7 天过期后，relogin 第一次就命中并提前关窗，死锁）② 会话
  过期粘性标志加 60s TTL（`_session_short_circuit`），到期放行真实重试、成功即清标志 → 会话恢复后
  页面自愈，失败态不再比失败本身持久 ③ 浏览器偏好 launch 即固化（老会话用户不再永远停在 auto）
  ④ 登录体验文案（白屏提示 + 切换浏览器未登录引导）。**来源**：另一台电脑报的 issue（假阳性）+ 评论（粘性）。
- **已发布（v1.7.12，2026-09-28）**：① 顶栏「检查更新」按钮（`force_update_check` 绕过缓存）；
  自动检查 TTL 1h→24h ② 修 relogin 假成功：判定接上**服务端探针** `SESSION_PROBE`
  （`_probe_page`，cookie 仅作快速筛；`KEYCLOAK_SESSION` 是 SSO 层痕迹，SSO 在 ≠ 业务会话在）
  ③ 修「登录后未自动刷新」：relogin **无论成败都清粘性标志**（原来只在成功时清 → 前端兜底探测
  被短路）+ 前端探测重试 6 次 ④ 修任务栏残留图标：`SW_HIDE` 后补 `ITaskbarList::DeleteTab`
  显式删按钮（+枚举重试，窗口创建滞后于进程）⑤ 服务日志 `serve.log` + 会话事件 `_log_session`。
  **发现**：cookie 的 7 天 ≠ 服务端会话寿命（实测 9-27 08:55 成功 → 9-28 08:51 失效，~24h；
  cookie 到期戳 = 登录时刻+7 天且不随 API 刷新）→ 判有效只能信服务端探针，日志用于累积分析。
- **已发布（v1.7.13，2026-10-10）**：① 页面失效自动重建（`_looks_closed` 纳入 `Failed to fetch`
  / 导航中断等信号 —— 原来只认 `closed`，页面加载失败时桥不重建、卡死一直报错）
  ② 启动器识别「服务在跑但取数异常」：新增 `/api/health`（只读内存、不联网）+ `--restart`；
  `_ensure_running` 在版本相同时再看健康，degraded 即自动重启（修「以为重启了其实没有」）
  ③ **去掉心跳超时兜底**：实测 Edge「睡眠标签页」（本机 `edge/sleeping_tabs.threshold: 300`）
  会让**不活跃标签的 JS 整体停掉** —— 心跳停、页面却没关；据此判"已关闭"必然误退
  （用户切走一会儿回来，服务已经没了、点刷新报「本地服务未运行」）。心跳无法区分
  「睡眠 / 冻结」与「关闭」，故**只认 `pagehide` 告别**；`IDLE_TIMEOUT` 常量保留但停用。
  **已知取舍**：极端情况（浏览器崩溃 / 关闭瞬间信号丢失）可能残留服务，用户可双击启动器复用。
- **已发布（v1.7.14，2026-10-11）**：**「clone 即用」—— Playwright 依赖自动准备 + 落点与
  客户端更新解耦**。起因：桌面问题报告（另一台**无系统 Python** 的机器实测）—— 客户端更新把
  内置 Python 换成指向安装目录（`resources\vendor\python`）的**符号链接**，装在里面的
  Playwright 被一并清除；而 launcher 候选只有「`sys.executable` 同目录 / PATH / 注册表」，在
  无系统 Python 的机器上**全部指向内置** → 依赖一清，桥起不来（面板一直转圈）。
  修复（仅 `launcher.py` + manifest + README）：① 依赖落点改到**数据目录持久 venv**
  （`~/.workbuddy/workbuddy-credits-data/.venv`，客户端更新不碰）② 候选解释器把它**排最前**
  ③ `_ensure_playwright_env()` 首次**自动建 venv + `pip install playwright`**（清华镜像 +
  锁版本 `playwright==1.58.0`）+ 弹窗提示「首次约 1–2 分钟」 ④ 内置目录告警（`_warn_if_volatile`）。
  **实测（本机）**：首次准备 **28.2s**、venv **114.1MB**、`python 3.13.12` + `playwright 1.58.0`；
  端到端用 venv 解释器起 serve → **1.5s 就绪** → `/api/overview` **200 / 4.6s / 57KB**。
  **实测后补的加固**：依赖探测改走**真实调用链** `from playwright.sync_api import sync_playwright`
  （原 `import playwright` 在「base 换了 Python 大版本、site-packages 里旧 ABI 的 greenlet 还在」
  时会**假就绪** —— 探测过、起桥才 ImportError）；`pip install` 加 **`-U`** 让绑版本的依赖按新
  解释器重装；新增 `_volatile_rank()` 让 base **优先路径稳定的解释器**、内置的排最后。
  **维护成本实测（「只装一次」的依据）**：对已存在 venv 再执行 `python -m venv <dir>` →
  `site-packages` **完整保留**（marker/six.py 都在）→ 断链只需**就地重建（几秒），不重下 100MB**；
  本机 venv 的 `pyvenv.cfg` `home = D:\Program\Python`（稳定路径）→ **永不因客户端更新失效**。
  **沙箱踩坑**：沙箱外 `Start-Process` 起的 serve **仍会被回收**（11:15:18 起、11:16:28 后消失）
  → 服务类验证必须**单次调用内「起 → 测 → 停」**（本次用 subprocess 起 + urllib 轮询 + terminate）。
- **已发布（v1.7.16，2026-10-11；含未单独发版的 v1.7.15）**：★ **「服务静默消失」真因 —— 服务自己的控制台窗口**。
  **现象史**：多台机器反馈「工作台正常取数后，服务在无人操作时从 8090 消失，无退出日志、无 traceback，
  端口直接释放」，用户点刷新才知「本地服务未运行」。另一台机器的 issue
  （`issue-service-silent-death-v1.7.14.md`）用逐条排除法推断为「外部 `TerminateProcess` 强杀」——
  **推断正确，但那个"外部"就是用户自己关掉的那个窗口**。
  **真因（实测）**：服务以 `python.exe`（**console 子系统**）启动时，`CREATE_NO_WINDOW` **与
  `DETACHED_PROCESS` 同用会被 Windows 静默忽略**（MSDN 明文）→ 进程仍**得到一个控制台**；
  Win11 默认终端是 **Windows Terminal**，于是**弹出一个命令行窗口**，而**关掉它 = 杀服务**
  （强杀不触发 atexit、不留任何日志）。证据：`conhost.exe` 的父进程 = **服务进程本身**；
  `WindowsTerminal.exe` 由 **svchost(-Embedding)** 拉起、是**独立进程** —— 所以"枚举服务进程的
  可见窗口"看不到它（这正是早期误判「服务无窗口」的原因，记牢这个坑）。
  **修复**：① `launcher._popen_target()` → `_spawn` 改用同目录 **`pythonw.exe`**（GUI 子系统，
  **机制上不创建控制台**），缺失时回退 `python.exe`；② `serve._release_console()`：启动时
  `GetConsoleWindow()` + `FreeConsole()` 兜底；③ `serve._setup_log()` 加固（pythonw 下
  `sys.stdout/stderr` 为 **None**，重定向失败兜底 devnull，否则任何 `print` 崩服务）。
  **对比实测**：`python.exe` → 有 `conhost`（复现现象）；`pythonw.exe` → **无任何控制台宿主** ✓
  **副产物**：venv 的 `python.exe`/`pythonw.exe` 是**重定向器**，会再派生一个真实进程 →
  这就是「Popen 返回的 pid ≠ 监听 pid」的由来。
  **★ 教训（高价值）**：**「子进程不要控制台」只能靠 GUI 子系统（pythonw）保证**；
  `CREATE_NO_WINDOW` 与 `DETACHED_PROCESS` 同用时会被忽略 —— 光靠 creationflags 靠不住。
- **已发布（v1.7.15，2026-10-11，随 v1.7.16 一起发）**：**让「异常终止」可留痕**（可观测性）。
  ① **运行标记** `~/.workbuddy/workbuddy-credits-data/serve_state.json`：启动写（pid / 起始 / 版本）、
  正常退出删；**下次启动若发现残留** → 记一行 `⚠ 上次服务未走正常退出：pid=… 存活 …`
  （正常退出**必留日志行**，故"标记残留"是比日志更强的信号）。含 `_pid_alive`（Win:
  OpenProcess + STILL_ACTIVE）、`_fmt_dur`。② **退出观测**：`atexit`（清标记）、`signal`
  （SIGTERM / SIGINT / SIGBREAK）、`threading.excepthook`（后台线程异常带线程名落日志）。
  ③ **`POST /api/quit`**（仅本机回环）：`launcher._ask_server_quit()` / `update._ask_quit()`
  在重启前**先请旧服务正常退出**，避免 `taskkill /F` 把「正常重启」误报成「异常终止」
  （旧版服务无该接口 → 404 → 回退强杀）。④ **`CREATE_BREAKAWAY_FROM_JOB`**：
  **`DETACHED_PROCESS` 不脱离 Job Object** —— 父环境带 `KILL_ON_JOB_CLOSE` 的 Job
  （IDE 任务 / CI / 脚本包装器 / 沙箱）时，服务会随 Job 关闭被一并回收（实测：受限父环境下
  30–60 秒内静默消失）；该 flag 不被允许时自动回退，不影响启动。⑤ `SKILL.md`：启动方式改为
  **优先 launcher / 用户双击启动器**，并明确「**不要**用前台 `python scripts/serve.py` ——
  它占住当前终端，**终端一关服务就没了**」。
  **端到端实测**：`/api/quit` → 200 + 日志「收到 /api/quit，正常退出…」+ 标记清除 → 再启动**无告警**；
  `taskkill /F` 强杀 → **标记残留** → 再启动**正确告警**（`pid=… 存活 4 秒`）。
- **已发布（v1.7.17，2026-10-16；随 v1.7.18 tag 一同发版）**：**「连续签到 N 天」口径分裂 —— 弹框与卡片各说各的**。
  两个口径并存已久：接口 `streak_days` = **当前活动周期内**天数（周期一换即归零重算）；
  `real_streak_days` = **跨周期真实连续**（`compute_real_streak` 基于本地累积自算）。
  **病灶**：卡片早已改用真实连续，而**弹框文案**（`serve.do_checkin` 的两个 message）与
  **未签到引导框**（`dashboard.html`）仍在用接口口径 → 用户同屏看到两个不同的「连续 N 天」。
  **修法（重点是一条纪律，不是四行代码）**：新增 `wc.real_streak_days(checkin_dates,
  extra_dates, today, merged)` 作为**全项目唯一口径入口**（内部 = `accumulate_checkin` +
  `compute_real_streak`），四处出口统一改走它 —— `build_dashboard_data` / `serve.do_checkin`
  两个分支 / 前端未签到引导框 / CLI（`cmd_checkin`、`cmd_analyze`）。
  签到成功分支**直接并入今天**（`real_streak_days([today])`）—— 签成了则今天必在集合里，
  无需再查一次接口。`/api/checkin` 保留 `streak_days` 并**新增** `real_streak_days`（向后兼容）。
  **有意保留的双口径**：工作台「签到活动」卡的「本周期已签 N 天」仍用周期口径（它已标注"本周期"，
  与卡片的真实连续并列展示，便于一眼看出差别）—— 这不是漏改。
  **★ 教训**：同一个**用户可见概念**只允许有**一个算法入口**。接口口径（系统视角）与业务口径
  （用户认知）不一致时，必须**显式选择并全局统一**；分散在多处各算各的，早晚漂移 ——
  这次就是卡片改了、弹框漏改。
  **验证的两个技巧（值得复用）**：① 服务端文案用**桩测**（stub 掉 `api_checkin_status` /
  `api_daily_checkin`，让两个口径**故意分叉**，断言输出取哪一个）—— 比起服务快得多；
  ② 前端弹框必须**伪造状态**才测得到：签到引导框只在「未签到」时渲染，当天已签到就永远不出现
  → 用 Playwright `page.route` 拦截 `/api/overview`，**只改 `checkin` 段**、其余字段沿用服务真实
  返回，页面即走完真实弹框路径（headless + `channel="msedge"`，实测 JS 错误 0 条）。
  **环境补充（服务类验证必读）**：工具环境里起的服务**一律会被回收**（受限 Job，见 v1.7.15 段）；
  `explorer.exe <vbs>` 代跑**无效**（服务没起来）；`schtasks` 已被 WorkBuddy 安全策略
  **列入程序黑名单**（不可绕过，需用户在「安全中心 → 命令安全 → 程序黑名单」移除）。
  → 只做**单次调用内「起 → 测 → 停」**，验证完**请用户自己双击启动器**恢复常驻服务。
- **已发布（v1.7.18，2026-10-16）**：**工作台 UI 改版 ——「深色计量舱」主题**（纯表现层，
  用户提的「美化 UI、提升视觉冲击力」）。**改动边界**：只动 `dashboard.html` 的 CSS 层 +
  ECharts 配置；JS 取数逻辑 / 数据结构 / 页签结构 / `strip_prompts` 提示词剥离链路一律未碰。
  **设计令牌**：暖黑底 `#0B0E13` + 卡面 `#131720` + 单一电光青强调色 `#22D3EE`（原先 5 色
  平摊改为一主色 + 语义色只表状态）；KPI 数字换 JetBrains Mono（CDN + `ui-monospace` 回退）
  28px/800 仪表读数；`color-scheme: dark` 让原生控件（日期选择器/滚动条）同步深色。
  氛围层 = `body::before` 双径向青辉光 + `body::after` 内联 SVG feTurbulence 噪点（3.5%）。
  **深色适配的关键经验**：① 浅色的**实色状态块**在深底上会"炸"成白斑 —— 行状态 / 徽章 /
  提示条全部改**语义色半透明叠底**（`rgba` 约 .07–.14 + 同色系描边）；② ECharts 深色不是
  换个 `backgroundColor` 就完事 —— 坐标轴发丝线 / tooltip / 图例要一套主题，新增
  `echarts.registerTheme('credits-cockpit')` 并让 `initChart()` 统一传入，热力图色阶与
  柱/线图渐变再在 `setOption` 里单独覆盖；③ 热力图色阶在深底要**压暗低值档**
  （收入日历几乎每天 +100 → 一片亮绿过曝，低值档从 `#15803D` 压到 `#15653A` 才不抢戏）。
  **★ 踩坑（截图验收必读）**：`serve.py` 每请求现读 `SKILL_DIR/dashboard.html`，而常驻服务
  是从**已安装目录**（`~/.workbuddy/skills/workbuddy-credits`）启动的 —— 只改项目根、
  不同步已安装目录，页面**纹丝不动**（首轮截图全浅色即此因）。同步文件后**无需重启服务**
  即生效（每请求现读）。验收：无头 Edge 7 屏 + 弹层全扫，JS 错误 0 条。
- **已发布（v1.7.19，2026-10-17）**：**「消耗明细」三列构成区重做 + 「仅看今日实时提示词」勾选框空转修复**。

  **A. 三列构成区（按模型 / 按客户端 / 按用途）为何显得乱** —— 不是配色问题，是**排版失序**，
  三条叠加：① 数值与「· N 次」挤在同一个 `grid-template-columns: 1fr auto` 单元格里，
  各行数字**右边界不对齐**（这是"乱"的主因，扫读时找不到参照点）；② 占比画成每行下方
  **6px 细条**，5 行散成 5 根断条，行高 = 文字 + 条 + 两段 gap，节奏碎且条细得像残影；
  ③ 三列**无分隔、无表头**，纯文本堆叠，看不出是"三张并列小表"。
  **改法**：表格化 `grid-template-columns: minmax(0,1fr) 74px 34px`（名称截断 / 积分右对齐等宽 /
  次数右对齐），上方加 10.5px 极小表头建立对齐锚点；占比改为**整行背景条**
  （`--w` CSS 变量 + `linear-gradient` 硬切），不占纵向空间，行高从约 34px 降到 24px。
  **★ 关键权衡**：第一版试过「左亮右渐隐」的条，观感柔和但**终点判读不出来** ——
  42% 与 37% 的长度差在视觉上消失，条形失去存在意义；改回**实心硬边**后比例立刻清楚。
  可视化里"柔和"与"准确"冲突时，准确优先。
  **口径修正**：条长由「相对第 1 名的相对值」改为「占该维度**全量**总和」——前者第一名永远满格，
  读不出垄断结构（实测 kimi-k3-1 占模型维度 42%、WorkBuddy 占客户端 90%）；下限 clamp 0.6%
  保证极小项仍可见。列头右上角补「前 5 / 共 N 项」（原先被 `slice(0,5)` 静默截断）。
  积分走 `toLocaleString('en-US')` 千分位，新增 `fmtCredit()`（文件内原无千分位工具）。
  竖分隔用 `--border-strong`（`--border` 太淡等于没有）。响应式 `auto-fit minmax(272px,1fr)`，
  **<900px 去掉所有竖线**；2 列临界约 866px 被 900 媒体查询覆盖，不会出现换行后悬空竖线。

  **B. 勾选框空转（真 bug）**：`#req-only-prompt` 全文件只出现**一次**（就是 `<input>` 本身），
  没有 `change` 监听、没有任何代码读它的 `checked` —— 属于某次改版留下的**断头控件**。
  修复接在 `reqFilterFn` 这**单一过滤入口**上：`has_input === false` 或
  `r.time.slice(0,10) !== todayStr()` 即剔除（复用文件内已有 `todayStr()`，不另造日期工具）。
  **筛选态存 `reqState` 而非 DOM** → `renderAll` 重渲染后勾选仍生效；因为是单一入口，
  搜索 AND、**导出 CSV 自动跟随筛选**、分页总数三处一并正确，无需另改。
  空态文案分叉：勾选态下提示「提示词只在实时响应中存在，历史行不可回溯；取消勾选可看全部流水」
  （`file://` 静态模式全部 `has_input=false` 时正好命中，避免被误判为坏掉）。
  顺带 `.chk-label` + `accent-color: var(--primary)` 把原生蓝勾换成主色。
  实测：537 条 / 跨 4 天 → 勾选后 3 条 / 仅今日 / 页码 1-1 → 取消恢复 537 条。
  **★ 教训（控件验收必读）**：纯装饰控件在**视觉上完全正常**，只有真去点它才会暴露。
  验收清单里必须有「控件是否真的改变了数据」这一条，不能只看「画面是否正常」。
- **已发布（v1.7.25，2026-10-24）**：**工作台可读性八项**（纯前端，全在 `dashboard.html`）。
  ① 消耗/收入热力图从 ECharts `calendar` 改 `scatter` 点阵（正方形格子、共用时间轴、P90 上限）；
  ② 三列构成区并排 → 竖排，占比条移到独立弹性列（`--w` 传无单位比例）；
  ③ 明细表提示词单行截断 + 悬浮气泡，路径类降为灰色等宽；
  ④ 「待用清单 / 有效期内」合并为「待用 / 已结束」（按数据源切，状态按码值分，排序用 `expired_time`）；
  ⑤⑥ 会话消耗下移、消耗明细置底（DOM / 导航 / `sectionToNav` 三处同步）；
  ⑦ 导航锚点守护（4s 窗口 + loading 续期 + 用户操作即停）修「点完偏 1407px」；
  ⑧ 骨架屏补加载说明（按面板分流 + ≥2s 显示秒数）。
  渲染层通用教训见「八、工作台渲染踩坑」。**验证**：1500×1000 Playwright，七区块高亮逐个点过、
  JS 错误 0、骨架期点导航最终偏差 0。
- **已发布（v1.7.24，2026-10-24，随 tag v1.7.25 一同发版）**：自动签到 —— 开关在工作台（设置菜单 → 自动签到），
  宿主是 **Windows 计划任务**（每天 09:30，`StartWhenAvailable` 补跑、`RestartOnFailure` 3 次）。
  新增 `scripts/auto_checkin.py`（run / install / uninstall / status / run_task_now）、
  `/api/auto-checkin`、`/api/auto-checkin-set`、`/api/auto-checkin-run`；
  `serve_state.json` 增写 `port`（计划任务靠它找到服务、复用桥）。
  详见「七、自动签到」。**本机沙箱拦 schtasks，注册链路待用户实测。**
- **已发（v1.7.23）**：全量排查「读不到被画成没有」，四处已修：
  ① `waste_authoritative` 补 `degraded`/`error` —— L6 挂掉时 KPI 显示「—」而不是「0.00 · 服务端权威」；
  ② 概览「收支损失 · 已过期未用完」由 L3 推算（结构上恒 0）改为 L6 权威（7617.34 / 62 批）；
  ③ 请求流水空表按四态分文案并显示 `requests_meta.error` 原文；
  ④ 空表导出不再生成「只有表头」的 CSV，改轻提示；正常导出回报行数。
  另加 `/api/health.files`（快照 / 到账 / 签到历史的存在性、坏行数、解析错误）。
  ⑤ 静态快照时间精确到天：`file://` / 分享单文件里数据是冻结的，原先只写「静态数据」无时间，
  12 天前的快照会被读成当前余额（实测静态文件是 v1.7.16 / 2026-10-11）；现在面板条显示日期，
  超 1 天在顶部横幅明确「N 天前，不是实时数据」。
  实测：L6 全挂 → 「—」+ 原因 + 徽章标红；L5 挂 → 「没取到：<错误原文>」；空表导出 → 不生成文件；
  正常态 7617.34 / 62 批 / 158 条回归，JS 错误 0。
- **已发（v1.7.21）**：「积分批次」与「包生命周期」合并为**一个面板三个页签**
  （待用清单 L3 / 有效期内 L6 / 已过期 L6），删掉独立面板与导航项。
  依据：两侧按 `(name, cycle_end)` 求交 **40/41 完全重合**，列 / 排序 / 到期标记全同，
  却是**两套状态口径**（前端推算 vs 服务端码值）。合并后 `/api/lifecycle` 增补
  `expiry_list` / `holidays` / `holiday_meta`（待用清单不依赖概览先到），
  `PANEL_FRESH_KEYS["lifecycle"]` 加 `resource`。
  踩坑记录：① 合并时**只留 L6 会静默丢「套餐用量」500 积分**（L6 只返回 `capacity_type=1`），
  所以待用清单必须保留 L3 源；② 两个源的「类型」字段不同名（L3 `label` 分类 / L6 `name` 包名），
  同一行在两张表里显示成两个词，用户会以为是两批 —— 统一以包名为主、分类作后缀；
  ③ 切页签要清空搜索，否则旧关键词（如「套餐」）在新页签恒 0 命中，看起来像坏了。
- **已发（v1.7.20）**：① 「积分批次」筛「已过期」恒为空 —— 假选项（数据源 L3 不返回过期包）→
  移除该选项；② 「包生命周期」表格支持**点行看单批完整明细**（包码 / 容量类型 / 计量口径 /
  服务端失效时刻 / 扣减结束 / 资源 ID，含 Esc、点遮罩、复制包码）。
  实测 158 批中 151 批有 `ExpiredTime`，**到期作废（Status 2）的那批通常为空、
  已用完（Status 3）的有值**（能看出何时用光）—— 弹窗对空值给明确文案而非留白
- **已发（v1.7.22）**：「最近会话消耗」整面板空 → 真因是本地库 WAL 的 `mode=ro` 需要写 `-shm`，
  目录不可写即 `unable to open database file`，异常被 `except` 吞成 `[]`，页面显示成「暂无记录」。
  已加三级打开回退（ro → ro&immutable=1 → 普通连接）+ `_L1_DIAG` 诊断（进 `/api/health.l1`
  与 `sources_health.l1`）+ 面板头数据源徽章 + 三态空文案。实测回退后 36 条全部读出，
  与 `ro` 模式行数/最新时间一致
- **发版提示（v1.7.2 起已简化）**：正文范围由 CI 用 `git describe --tags --abbrev=0 HEAD^`
  自动定界，**README 里不再需要写「本版包含 X~Y」**。
  仍需注意的唯一一件事：**先写好 README 段再打 tag**（否则只能回退到自动 notes）
- **版本号是启动器的自检依据**：`launcher.py` 用「端口 + 版本」双校验决定是否结束旧进程
  重拉服务。所以**代码变了就必须升 `manifest.yaml` 的版本号** ——
  否则用户的旧进程会被判为"版本一致"而**不被替换**，改动看不到效果（v1.4.1 定案）
- **版本语义**：v1.4.0 首屏并发提速 + 分面板异步/局部刷新；v1.4.1 启动器版本自检 +
  前后端能力探测；v1.4.2 订阅型容量改用周期口径；v1.4.3 修「今日已使用」口径错位 + 全面口径自查
