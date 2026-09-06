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

## 请求头（三处必带）

```
Content-Type: application/json
Accept: application/json
Authorization: Bearer <accessToken>
X-User-Id: <uid>
User-Agent: Mozilla/5.0        ← 关键：缺此头返回 403 code=10085
```

## 关键坑（务必注意）

1. **域名区分**：`get-user-resource` 必须用 `copilot.tencent.com`；用 `www.codebuddy.cn` 同路径返回 403（`code:10085`）。签到类接口才用 `www.codebuddy.cn`。
2. **路径前缀不对称**：积分接口 `/billing/meter/get-user-resource` **无** `/v2/` 前缀；签到接口 `/v2/billing/meter/...` **有** `/v2/` 前缀。
3. **必须带 `User-Agent: Mozilla/5.0`**：Python urllib 默认 UA 会被网关拒（403）。
4. 历史教程里的 `copilot.tencent.com` 签到接口已失效（404），签到以 `www.codebuddy.cn` 为准。

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
| `CapacityRemainPrecise` | 剩余积分（精确字符串，优先用；Fallback `CapacityRemain`） |
| `CapacitySizePrecise` | 总量（精确字符串；Fallback `CapacitySize`） |
| `CapacityUsedPrecise` | 已用（精确字符串；Fallback `CapacityUsed`） |
| `CycleStartTime` | 周期开始（字符串 `YYYY-MM-DD HH:MM:SS`） |
| `CycleEndTime` | 到期时间（字符串 `YYYY-MM-DD HH:MM:SS`） |
| `DeductionEndTime` | 扣减结束时间戳（毫秒，可作到期时间兜底） |
| `ResourceId` | 资源包唯一 ID |

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

除云端接口外，本地 SQLite `~/.workbuddy/workbuddy.db` 的 `session_usage` 表存有**会话级积分消耗明细**（约近 17 天）：

| 字段 | 含义 |
|------|------|
| `session_id` | 会话 ID（可 JOIN `sessions.id` 拿标题/模型） |
| `updated_at` | 会话最后更新时间（毫秒） |
| `credit_json` | JSON：key=32位hex（task/message id），value=该次调用消耗的积分 |

- 该表是**消耗维度**（每次会话耗多少积分），可精确算「今日已使用」「日/周消耗趋势」，替代快照差值估算。
- 该表**不含**「已过期积分包」清单（那是资源包维度，本地无缓存，仅网页端 Cookie 接口）。
- 脚本 `usage` 子命令读取此表；`render` 时把按天聚合结果写入工作台数据。
