# 自动化任务配置模板

本 skill 配套 4 个定时任务。自动化任务**不随 skill 打包分发**，需在安装后单独部署。
部署时用 `automation_update` 工具（mode=create），或在 WorkBuddy 自动化界面手动创建。

> 通用运行说明：脚本路径统一为 `~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py`；
> 用系统可用的 Python 运行（`python3` 或 `python`）。Windows 上若 `python` 命令不在 PATH，
> 使用 WorkBuddy 自带的 Python（`~/.workbuddy/binaries/python/versions/` 下最新版本目录中的 `python.exe`）。

## 任务 1：每日自动签到

- name: `WorkBuddy 每日自动签到`
- scheduleType: `recurring`
- rrule: `FREQ=DAILY;BYHOUR=9;BYMINUTE=0`
- prompt:

```
执行 WorkBuddy 每日自动签到。运行脚本 ~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py 的 checkin 子命令（用系统可用的 python3 或 python；Windows 上若 python 命令不存在，用 ~/.workbuddy/binaries/python/versions/ 下最新版本目录中的 python.exe）。脚本会先查询签到状态，若今日已签到则报告"已签到"即可；若未签到则自动执行签到并报告获得积分。把脚本输出作为结果简洁报告。若提示"未找到登录态"或登录失效，则提醒用户需重新登录 WorkBuddy 客户端。
```

## 任务 2：到期巡检（36 小时）

- name: `WorkBuddy 积分到期巡检（36小时）`
- scheduleType: `recurring`
- rrule: `FREQ=HOURLY;INTERVAL=6`
- prompt:

```
检查 WorkBuddy 积分是否有未来 36 小时内到期的批次。运行 ~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py 的 expire-check 36 子命令（用系统可用的 python3 或 python；Windows 上若 python 命令不存在，用 ~/.workbuddy/binaries/python/versions/ 下最新版本目录中的 python.exe）。若输出显示"无到期批次"，则简短回复"当前无 36 小时内到期的积分"；若列出了到期批次，则把这些批次（到期时间、剩余积分、类型）整理成提醒消息报告，提醒用户尽快使用以免过期浪费。
```

## 任务 3：到期巡检（12 小时）

- name: `WorkBuddy 积分到期巡检（12小时）`
- scheduleType: `recurring`
- rrule: `FREQ=HOURLY;INTERVAL=6`
- prompt:

```
检查 WorkBuddy 积分是否有未来 12 小时内到期的批次。运行 ~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py 的 expire-check 12 子命令（用系统可用的 python3 或 python；Windows 上若 python 命令不存在，用 ~/.workbuddy/binaries/python/versions/ 下最新版本目录中的 python.exe）。若输出显示"无到期批次"，则简短回复"当前无 12 小时内到期的积分"；若列出了到期批次，则把这些批次（到期时间、剩余积分、类型）整理成紧急提醒消息报告，强调即将过期需立即使用。
```

## 任务 4：每日到期清单汇总

- name: `WorkBuddy 每日积分到期清单汇总`
- scheduleType: `recurring`
- rrule: `FREQ=DAILY;BYHOUR=9;BYMINUTE=30`
- prompt:

```
生成 WorkBuddy 每日积分汇总清单。依次运行以下脚本命令（用系统可用的 python3 或 python；Windows 上若 python 命令不存在，用 ~/.workbuddy/binaries/python/versions/ 下最新版本目录中的 python.exe）：
1. ~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py record（记录当日余额快照）
2. 同路径 analyze（分析过期浪费、未来7天到期风险、签到效率）
3. 同路径 render（更新工作台 dashboard 数据）
把三部分结果整理成每日汇总报告，并基于当前余额、各批次到期时间和签到可得（每日100积分、连续签到有奖励）给出积分使用建议，例如优先消耗即将到期的批次、保留长周期批次、坚持每日签到补充积分。
```
