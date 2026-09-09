# 自动化任务配置模板

本 skill 配套 1 个定时任务（每日自动签到）。自动化任务**不随 skill 打包分发**，需在安装后单独部署。
部署时用 `automation_update` 工具（mode=create），或在 WorkBuddy 自动化界面手动创建。

> 通用运行说明：脚本路径统一为 `~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py`；
> 用系统可用的 Python 运行（`python3` 或 `python`）。Windows 上若 `python` 命令不在 PATH，
> 使用 WorkBuddy 自带的 Python（`~/.workbuddy/binaries/python/versions/` 下最新版本目录中的 `python.exe`）。

## 到期提醒说明（重要）

到期提醒**不用定时任务**：定时巡检体感价值低、有后台执行开销，且无法做到"零打扰"。
改为**工作台内置弹框**——用户主动打开工作台时，若有 7 天内到期的积分批次，页面会自动弹框提示（可一键跳转「积分批次」查看）。主动查看即可掌握到期风险，无需被动推送。

## 任务 1：每日自动签到

- name: `WorkBuddy 每日自动签到`
- scheduleType: `recurring`
- rrule: `FREQ=DAILY;BYHOUR=9;BYMINUTE=0`
- prompt:

```
执行 WorkBuddy 每日自动签到。运行脚本 ~/.workbuddy/skills/workbuddy-credits/scripts/workbuddy_credits.py 的 checkin 子命令（用系统可用的 python3 或 python；Windows 上若 python 命令不存在，用 ~/.workbuddy/binaries/python/versions/ 下最新版本目录中的 python.exe）。脚本会先查询签到状态，若今日已签到则报告"已签到"即可；若未签到则自动执行签到并报告获得积分。把脚本输出作为结果简洁报告。若提示"未找到登录态"或登录失效，则提醒用户需重新登录 WorkBuddy 客户端。
```
