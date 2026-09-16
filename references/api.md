# WorkBuddy 积分接口参考

本文档记录 WorkBuddy/CodeBuddy 积分查询相关接口的端点、请求头、响应字段与关键坑。所有接口均使用本机登录态 Bearer Token 调用，无需抓包。

## 登录态文件位置

| 系统 | 路径 |
|------|------|
| Windows | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| macOS | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` |
| Linux | `~/.config/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` 或 `~/.workbuddy/auth/...` |

文件为明文 JSON，关键字段：

- `auth.accessToken`：JWT（Bearer Token，约 90 天有效）
- `auth.domain`：`www.codebuddy.cn`（仅用于签到类接口）
- `account.uid`：用户唯一 ID（请求头 `X-User-Id` 用）

## 接口清单

### 1. 积分资源包查询（核心）

```
POST https://copilot.tencent.com/billing/meter/get-user-resource
请求体：{}
```

返回账户下全部资源包（套餐用量 / 加量包 / 权益赠送包）及其批次明细。

### 2. 每日签到状态

```
POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status
请求体：{}
```

返回 `active`、`today_checked_in`、`streak_days`、`daily_credit`、`checkin_dates` 等。

### 3. 执行签到

```
POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin
请求体：{}
```

成功 `code=0`；当日已签返回 `code=10001`（幂等，不重复发奖）。

### 4. 消耗明细（请求级流水）

```
POST https://copilot.tencent.com/billing/meter/get-user-request-usage
请求体：{"startTime":"YYYY-MM-DD HH:MM:SS","endTime":"YYYY-MM-DD HH:MM:SS","pageNum":1,"pageSize":500}
```

返回账户在指定时间窗口内的**逐次请求**消耗流水。企业版另有 v2（`timezone`/`version:2`/`pageToken` + 头 `X-Enterprise-Id`）；个人版走 v1。
仅网页控制台在用（客户端 asar 无此端点）→ 是重构最频繁、最不稳定的一环，需按「挂了当天要知道」的思路做对账。

响应 `data.total` + `data.data[]`，字段：

| 字段 | 含义 |
|------|------|
| `requestId` | 请求唯一 ID（可作去重主键，也可 join 本地库 `credit_json` 的 key） |
| `requestTime` | 请求时间 |
| `credit` | 本次消耗积分 |
| `model` | 模型（如 `deepseek-v4-pro`、`kimi-k2.7`） |
| `client` | 客户端（`WorkBuddy` / `VSCode` / `JetBrainsAndroidStudio`） |
| `input` | **用户原始输入（提示词）** |
| `inputTrunc` | 截断后的输入 |
| `agentPurpose` | 用途（`conversation`、`conversation:compact`、`subagent:Explore` 等） |

**⭐ 最关键的坑：窗口宽度 ≥32 天 → `input`/`inputTrunc`/`agentPurpose` 被整批静默剥离**（HTTP 200 + `code:0`，无任何报错）。实测同一批记录：31 天窗口 226/230 有值，32 天窗口 0/233 全空。**必须用窄窗口（建议 ≤7 天，最稳单日）**。

其余静默/显式失败模式：

| 场景 | HTTP | 表现 |
|------|------|------|
| 单次窗口宽度 ≥32 天 | 200 | `code:0`，但 `input`/`agentPurpose` 整批剥离（**静默**） |
| 未来日期 / 起止倒置 | 200 | `code:0` + `total:0`（**静默**，须调用侧自行 clamp） |
| 页码越界 | 400 | `code:10001 invalid params`（不能「翻到空为止」，须按 `total` 收敛） |
| body 字段名写错 | 400 | `code:10001`（显式） |
| 路径改名 | 404 | `404 page not found`（显式） |
| 坏 token | 401 | openresty HTML（显式） |

- **无需 `User-Agent`**（去掉仍 200）
- `pageSize` 无静默截断（实测 5000 返回全部）；`pageSize=500` 单次请求约 0.4s
- 稳定性实测：60/60 成功、median 0.40s、5 路并发无 429
- **真正的约束是「单次查询窗口宽度 ≤31 天」，与「距今天数」无关** —— 窄窗口实测可回溯到 2026-05（更早应为账号本身无数据）
- **提示词只保留约 32 天**：更早的记录即使窄窗也全空 → 这是唯一不可回补项，要提示词就必须勤跑窄窗

### 5. 资源包生命周期（有效期内 / 已过期）

```
POST https://copilot.tencent.com/billing/meter/get-user-resource-paid-packages  # 付费包/加量包
POST https://copilot.tencent.com/billing/meter/get-user-resource-free-packages  # 免费包/权益赠送
请求体：{"PageNumber":1,"PageSize":200,"Status":[...],"IsDisplayTotalInfo":true,"PackageCodes":[...]}
```

- **已过期**：`Status:[2,3]` + `PackageEndTimeRangeBegin`（now−101 年）+ `PackageEndTimeRangeEnd`（now）+ `OrderBy:"endTime"` + `SortBy:"desc"`
- **有效期内**：`Status:[0,3]` + `OnlyValidPeriod:true`（不带时间/排序）
- `Status`：`0`=有效、`1`=已退款（用户不可见）、`2`=已过期、`3`=已用完；**`3` 同时属于两个页签**
- `PackageCodes` **必填**，且必须**按白名单分派** —— 付费码传给 free 端点会被**静默过滤成空**（`TotalCount:0`）
- **缺 `User-Agent` → HTTP 403 `code:10085`「请求不合法」**（连老接口也一样）
- `PageSize` 上限 200；过滤 `CapacityType == 4`

响应 `data.TotalCount` + `data.Accounts[]`；字段 `PackageName` / `PackageCode` / `CapacitySize` / `CapacityUsed` / `CapacityRemain` / `CycleCapacitySize` / `CycleCapacityUsed` / `CycleCapacityRemain` / `Status` / `DeductionEndTime` / `ExpiredTime` / `CycleEndTime`。

> 容量字段有两套口径（累计 / 本周期），订阅型（`CapacityType=4`）会分叉，详见「两套容量口径」一节。

**意义**：过期浪费分析改由该权威接口直出，不再依赖本地快照差分推算。老接口 `get-user-resource` **完全不返回已过期包**，因此旧口径的「已过期损失」恒为 0。

包码白名单（个人版）：

| 端点 | 包码 |
|------|------|
| paid | `free`(code_002)、`proMon`(005)、`proMonPlus`(003)、`proYear`(023)、`youth`(026)、`advanced`(027)、`flagship`(009)、`extra`(038)、`extra38`(036)、`extraIntl` |
| free | `freeMon`(001)、`freeMonIntl`(008)、`gift`(035)、`activity`(006)、`proTrialMon`(039)、`proTrialYear`(040)、`bonus28`(007)、`bonus29`(028)、`bonus30`(030)、`bonusIntl`(037) |

## 请求头（四处必带）

```
Content-Type: application/json
Accept: application/json
Authorization: Bearer <accessToken>
X-User-Id: <uid>
User-Agent: Mozilla/5.0        ← 关键：缺此头部分接口返回 403 code=10085
```

## 关键坑（务必注意）

1. **域名区分**：`get-user-resource` 必须用 `copilot.tencent.com`；用 `www.codebuddy.cn` 同路径返回 403（`code:10085`）。签到类接口才用 `www.codebuddy.cn`。
2. **路径前缀不对称**：积分接口 `/billing/meter/get-user-resource` **无** `/v2/` 前缀；签到接口 `/v2/billing/meter/...` **有** `/v2/` 前缀。
3. **必须带 `User-Agent: Mozilla/5.0`**：Python urllib 默认 UA 会被网关拒（403）。
4. 历史教程里的 `copilot.tencent.com` 签到接口已失效（404），签到以 `www.codebuddy.cn` 为准。

## 网络与代理（默认直连）

**所有接口调用一律显式直连，完全不读取系统代理与环境变量代理。** 实现方式：`_api_call` 用
一个显式空代理表的 opener（`build_opener(ProxyHandler({}))`），而非裸
`urllib.request.urlopen`。`update.py`（GitHub 通道）遵循同一约定，代理只能通过 `--proxy`
显式开启。

**为什么不能用裸 urlopen？** CPython 的 urllib 会把「进程首次请求时的代理配置」**冻结**在
模块级全局 opener 里：

- `urllib/request.py` 的 `_opener = None`（模块级变量）
- `elif _opener is None: _opener = build_opener()` —— **每进程只构建一次**
- `ProxyHandler.__init__` 内 `proxies = getproxies()` —— 构造那一刻即固化

后果：常驻的 `serve.py` 若在代理软件开着的时候启动，此后代理软件退出、端口关闭，它**仍会**
向那个死端口发 `CONNECT`，报 `WinError 10061（连接被拒绝）`，且**永不自愈**（只能重启进程）。
用户侧的典型症状是「开着代理就正常、不开就一直报错」——端口复活后，被缓存的代理又生效了。

顺带两点实测经验（2026-09-13）：

- **确证手段**：在代理端口上临时放一个监听器，驱动工作台刷新，可直接抓到该进程发来的
  `CONNECT copilot.tencent.com:443 HTTP/1.1`，即证明它仍在使用已关闭的代理。
- **测网络要出沙箱**：沙箱/容器内可能存在透明代理并接管流量，使「直连」看起来成功，据此
  判断会得出错误结论。

## get-user-resource 响应结构

```json
{
  "code": 0,
  "data": {
    "Response": {
      "Data": {
        "TotalCount": 35,
        "TotalDosage": 4647,
        "Accounts": [ { ...资源包对象... } ]
      }
    }
  }
}
```

## 资源包对象关键字段

| 字段 | 含义 |
|------|------|
| `CapacityType` | 类型：`4`=个人体验版（套餐用量）、`1`=权益赠送包（运营裂变包）、其余=加量包等 |
| `PackageName` | 包名（如 `CodeBuddy个人体验版`、`CodeBuddy个人版国内运营裂变包`） |
| `CapacityRemainPrecise` | **累计**剩余积分（精确字符串，优先用；Fallback `CapacityRemain`） |
| `CapacitySizePrecise` | **累计**总量（精确字符串；Fallback `CapacitySize`） |
| `CapacityUsedPrecise` | **累计**已用（精确字符串；Fallback `CapacityUsed`） |
| `CycleCapacityRemainPrecise` | **本周期**剩余积分（精确字符串；Fallback `CycleCapacityRemain`） |
| `CycleCapacitySizePrecise` | **本周期**总量（精确字符串；Fallback `CycleCapacitySize`） |
| `CycleCapacityUsedPrecise` | **本周期**已用（精确字符串；Fallback `CycleCapacityUsed`） |
| `CycleStartTime` | 周期开始（字符串 `YYYY-MM-DD HH:MM:SS`） |
| `CycleEndTime` | 到期时间（字符串 `YYYY-MM-DD HH:MM:SS`） |
| `DeductionEndTime` | 扣减结束时间戳（毫秒，可作到期时间兜底） |
| `TotalCycles` / `RemainCycles` | 总周期数 / 剩余周期数（**注意**：`RemainCycles=0` 不等于「周期额度用尽」，实测有套餐剩余 500 但该字段为 0，勿据此判定） |
| `ResourceId` | 资源包唯一 ID |

### ⚠️ 两套容量口径：订阅型必须用周期口径

同一条记录会**同时**返回上面两组容量字段。对**订阅型资源**（`CapacityType == 4`，套餐用量）
两组会**分叉**，且服务端的累计字段不可信：

| 实测样本（`CodeBuddy个人体验版`） | 累计口径 | 周期口径 |
|---|---|---|
| 总量 / 已用 / 剩余 | 500 / **0** / **500** | 500 / **500** / **0** |
| 官网「套餐与用量」显示 | — | 已用 500/500、**0 剩余** |

判据：**周期区间是累计区间的子集，累计已用不可能小于周期已用**。上面 `0 < 500` 自相矛盾，
说明服务端对订阅型的累计字段**不随周期消耗更新**（停留在重置后的初值），会把已用光的套餐
算成满额。官网按周期口径渲染（旁证：官网「下次权益周期更新时间」= `CycleEndTime`）。

结论：**`CapacityType == 4` 取 `CycleCapacity*` 作为当前可用**；其余类型（赠送包/加量包）
实测两组一致，沿用累计口径。若一组字段整体缺失则回退到另一组。

## 分类逻辑

- `CapacityType == 4` → 套餐用量（个人体验版）
- `CapacityType == 1` → 权益赠送包（裂变包）
- 其他（含包名含"加量/叠加"）→ 加量包/其他

## 状态判断

- **可用**：`剩余 > 0` 且 `到期时间 > 当前时间`
- **已用完**：`剩余 <= 0`
- **已过期**：`到期时间 < 当前时间`

扣减顺序：系统优先消耗最先到期的资源包；到期时间相同则先耗基础积分、再耗赠送积分。

## 本地数据源（session_usage）

除云端接口外，本地 SQLite `~/.workbuddy/workbuddy.db` 的 `session_usage` 表存有**会话级积分消耗明细**：

| 字段 | 含义 |
|------|------|
| `session_id` | 会话 ID（可 JOIN `sessions.id` 拿标题/模型） |
| `updated_at` | 会话最后更新时间（毫秒） |
| `credit_json` | JSON：key=**服务端 requestId**（32位hex），value=该次调用消耗的积分 |

- `credit_json` 的 key **就是** L5 接口的 `requestId` → 可做 `服务端流水 ⋈ 本地会话库` 的 join，把「会话标题/上下文」与「模型/客户端/提示词/单次积分」缝成一张请求级表。
- 该表**会被客户端滚动清理**（实测仅保留很少行），因此**不能**作为历史消耗的唯一依据。
- 该表**不含**「已过期积分包」清单 —— 过期包一律走 L6 接口。

## 数据分层与口径

| 层 | 来源 | 角色 | 能否回补历史 |
|----|------|------|--------------|
| **L5** | `/get-user-request-usage` | **权威层**：请求级消耗流水 | ✅ 窄窗口可回溯至 2026-05（但**提示词仅 ~32 天**） |
| **L6** | `/get-user-resource-{paid,free}-packages` | **权威层**：包生命周期（含过期） | ✅ 包清单/终值可一次性回补（**余量衰减轨迹不可回补**） |
| **L3** | `/get-user-resource` | 观测层：有效包清单（**余额基准**，不可缺） | — |
| **L2** | `usage_history.json` | 观测层：逐包余量采样存档（**是 L3 的采样存档，非独立证据**） | ❌ 从安装/启用那刻起才有 |
| **L1** | `workbuddy.db` | 观测层：会话标题/模型（与 L5 join） | ❌ 会被滚动清理 |
| **L4** | 签到接口 | 独立：签到状态 | — |

**口径切换**：`usage_daily` / `usage_hourly` / `today_used` 一律 **L5 优先、L2 兜底**（`usage_source` 字段标注实际来源）。

**对账是「双通路」而非「三方」**：A = L5 请求级（有服务端历史）；B = L3→L2 包级（L2 只是 L3 同一字段的采样存档，**两者是同一个数，不构成独立证据**）。对账即 A ↔ B。

**已知偏差**：`compute_usage_daily`（L2 差分）会把「新包到账（0→满额）」误记为消耗。项目内已记录该缺陷，并在对账时按 `income_events` 的 `ResourceId` 归因剔除。剔除 L2 首个采样日（无跨天基线）后，实测差异可收敛到 0.25%。

## 本地服务接口（serve.py，非上游）

工作台的分面板加载/局部刷新走以下本地路由（均仅监听 `127.0.0.1`）：

| 路由 | 内容 | 底层源 | `?fresh=1` 重取的源 |
|------|------|--------|---------------------|
| `/api/overview` | 概览：KPI / 摘要 / 收支统计 / 图表 / 账本 / 会话 / 批次 / 签到 / 健康徽章 | L3 + L4 + L5 + L6 | `resource`, `checkin` |
| `/api/requests` | 消耗明细：请求级大表 + 模型/客户端/用途构成 + 对账（**提示词只在此处实时返回**） | L5（+L1 join、L2 基准） | `l5` |
| `/api/lifecycle` | 包生命周期：有效期内/已过期 + 权威浪费 | L6 | `l6` |
| `/api/version` | skill 版本号（来自 `manifest.yaml`，瞬时返回） | — | — |
| `/api/data` | 全量（兼容旧调用方 / 导出） | 全部 | 全部四个 |
| `/dashboard_data.js` | 静态数据，**已剥离提示词** | 全部 | — |

**并发与去重**：四源在一个请求内用线程池并发拉取（`_run_parallel`），首屏由「四路之和」变为「最慢的一路」。
`SourceHub` 提供进程内 TTL 缓存（10s）+ single-flight：页面同时发三个面板请求时，同一源只真正打一次上游，其余等待复用。

**失败隔离**：任一源失败只让依赖它的面板降级（该块为空 + 健康徽章标红），不连累其余面板。
