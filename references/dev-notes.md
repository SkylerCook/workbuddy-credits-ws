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

## 五、环境与踩坑

- **⚠️ 解释器陷阱（v1.5.1，代价高）**：双击启动器走的是 **WorkBuddy 内置 Python**
  （`~/.workbuddy/binaries/python/versions/...`），它**没有 Playwright**；
  Playwright 通常装在系统 Python 里。
  ⇒ **"开发机能跑"≠"用户那边能跑"**：交付前**必须用与用户相同的入口验证**
  （用内置 Python 跑 `launcher.py`，而不是直接跑 `serve.py`）。
  `launcher.py` 现已自动探测解释器（同目录 → PATH → 注册表 → py launcher），
  挑装了 Playwright 的那个；桥在缺依赖时**秒级报错**（原来会卡满 120s）
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

## 六、发布流程

1. `manifest.yaml` 升 version
2. **先写好 README 的 `### vX.Y.Z — 摘要` 段**（Release 正文取自它，见下）
3. `git commit` → `git push origin main`
4. `git tag vX.Y.Z && git push origin vX.Y.Z`
5. CI（`.github/workflows/release.yml`）打包并建 Release

- **Release 正文取自 README 更新日志**（v1.4.3 起）：CI 调
  `scripts/extract_changelog.py <tag>` 生成 `--notes-file`，取不到才回退 `--generate-notes`。
  ⚠️ **务必先写好 README 段再打 tag** —— 本项目无 PR，自动 notes 只有一行 Full Changelog
- `build_dist.py` 白名单式收集：`SKILL.md` / `manifest.yaml` / `README.md` / `dashboard.html` /
  `.gitignore` / `.gitattributes` / `assets/` / `references/` / `scripts/*.py`（排除自身）
  —— 新增顶层目录/文件时须确认在收集范围内

## 七、当前状态

> 每次提交 / 发版后更新这一节。

- **已发布**（tag + CI + Release）：v1.3.0 / v1.3.1 / v1.4.1（含 v1.4.0）/ v1.4.3（含 v1.4.2）
- **已提交、未发版**（commit `730fa6b`，11 文件 +1653/-34）：
  v1.4.4 / v1.4.5 / v1.5.0 / v1.5.1 / v1.5.2 / v1.5.3 / v1.6.0
  - v1.4.4 —— 签到卡区分「真实连续」与「活动周期内」＋ 可用天数改 30 天窗口
  - v1.4.5 —— 凭据加密后优雅降级
  - v1.5.0 —— 取数通道改「浏览器桥」（凭据不落本进程）
  - v1.5.1 —— 修「双击启动器后面板一直转圈」（解释器探测 + 桥快速失败）
  - v1.5.2 —— 恢复「昵称 / 角色」（新增 `GET /console/account`）
  - v1.5.3 —— 修「服务掉线时点刷新」的误导提示
  - v1.6.0 —— 体感优化（服务端预热 + 首屏缓存 + 桥攒批并发）
- **未提交**（按「提交与发版解耦」的节奏，攒着等下次提交）：
  - v1.6.1 —— 「积分批次」表标出临近到期（`expiry_list` 补 `exp_ts`；
    ≤3 天红 / ≤7 天橙；行底色 + 左侧色条 + 状态列小标签）
- 同一提交还带上了 `AGENTS.md` 与本文档（项目资产）
- **发版提示**：`extract_changelog.py` 只提取**单个**版本段，所以 v1.6.0 的 README 段里
  必须写明「本版包含 v1.4.4 ~ v1.6.0 的全部改动」，否则 Release 正文会漏掉前六批
- **版本语义**：v1.4.0 首屏并发提速 + 分面板异步/局部刷新；v1.4.1 启动器版本自检 +
  前后端能力探测；v1.4.2 订阅型容量改用周期口径；v1.4.3 修「今日已使用」口径错位 + 全面口径自查
