# 陪伴设备同意与响应

家庭陪伴设备的同意与响应中枢：把家庭成员、儿童监护、临时访客的授权分开管理，
机器人每次处置触摸、语音情绪摘要或安全信号时，都使用**事件发生当刻**有效的同意版本，
并让每个响应都能回溯到对应的授权与规则版本。

## 数据约定

`contracts/device-events.json` 给出设备批量回传的样例。`captured_at` 来自设备时钟，
`sequence` 只在一次设备配对周期内递增，设备恢复出厂或转借后会更换 `pairing_id`。
原始语音不会进入平台，`signals` 仅包含设备侧形成的摘要。

## 核心语义

- **同意版本化**：每次授权变更产生新版本（`effective_from` 起生效，可被取代、到期 `expires_at`
  或撤回 `revoked_at`）。处置按事件的 `captured_at` 解析当时有效的版本，而不是接收时的设置；
  撤回与到期只对之后发生的事件生效，不回溯。
- **主授权优先级**：同一设备同一配对周期内有多份有效授权时，按 儿童监护 > 家庭成员 > 临时访客
  取主授权，处置记录同时保留全部有效授权的版本引用。
- **响应分级**（规则版本见 `service/rules.py` 的 `RULES_VERSION`）：
  - 普通低落：只能从家庭批准的动作清单中选择安抚动作；
  - 连续异常（同一配对周期内按 `sequence` 连续达到阈值）：生成一次最少信息关怀提醒；
  - 明确危险信号：无论授权状态都进入人工确认的升级流程。
- **幂等处置**：断网重传、重复到达按 `event_id` 去重（载荷不一致报 `conflict`）；
  时钟漂移不拒收事件，仅在配对关闭边界留 5 分钟容忍；通知按去重键只发一次。
- **转借与删除**：转借关闭旧配对周期，旧配对新事件（超出漂移容忍）拒绝处置；
  删除任务清除可删资料（事件载荷、处置细节），保留安全审计与同意审计要求的最小事实，
  删除进度与保留依据可通过任务查询。
- **视图分离**：客服只能查看脱敏解释（无主体身份、无信号原文）；
  监护人可以导出自己权限内的同意与处置记录。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/v1/consents` | 登记授权（同一主体重复登记产生新版本） |
| POST | `/v1/consents/{id}/revoke` | 撤回授权，即刻生效 |
| GET | `/v1/consents?device_id=&pairing_id=` | 查看授权链与版本 |
| POST | `/v1/events:batch` | 批量事件回传（数组或 `{"events": [...]}`），幂等 |
| GET | `/v1/dispositions/{event_id}` | 处置记录（含授权与规则版本） |
| GET | `/v1/support/dispositions/{event_id}` | 客服脱敏解释 |
| POST | `/v1/escalations/{id}/confirm` | 升级单人工确认 |
| GET | `/v1/escalations?device_id=&status=` | 升级单列表 |
| POST | `/v1/devices/{id}/transfer` | 设备转借，关闭当前配对周期 |
| POST | `/v1/devices/{id}/deletions` | 启动删除任务（`pairing_id`、`requested_by`） |
| GET | `/v1/deletions/{job_id}` | 删除进度与保留依据 |
| GET | `/v1/guardians/{id}/export` | 监护人导出权限内的同意与处置记录 |
| GET | `/v1/notifications?device_id=` | 通知列表（关怀提醒 / 升级） |

## 运行与测试

```bash
python3 -m service.main                 # 启动服务，STORE_PATH 指定状态文件（默认 data/store.json）
python3 -m unittest discover -s tests   # 全部测试
```

状态持久化在 JSON 文件中（原子写入），服务重启后处置幂等、通知不重复。
`tests/test_end_to_end.py` 把两个家庭转借、儿童授权到期、乱序回传、
撤回与危险事件相撞、服务重启连成一条完整流程。
