# 陪伴设备同意与响应

独眼星球陪伴机器人家庭试用的“同意与响应中枢”。负责把家庭成员、儿童监护与临时访客的
授权分开管理，并在每次触摸、语音情绪摘要或安全信号到达时，按 **事件发生当刻** 有效的
同意版本决定留存粒度与可执行动作。

## 数据约定（沿用 `contracts/device-events.json`）

- `captured_at`：设备时钟下的事件发生时刻，内部统一归一为 UTC；同时记录与接收时刻的
  `clock_skew_seconds`，时钟漂移不影响授权裁决。
- `sequence`：仅在一次设备配对周期（`pairing_id`）内递增；设备恢复出厂更换
  `pairing_id` 后序号可重新从小开始。去重键为 `event_id` 与 `(pairing_id, sequence)`。
- `signals`：只含设备侧形成的摘要（mood/risk/touch/confidence 等），原始语音不进入平台。

## 核心原则

1. **三类授权分开、版本不可变**：`family` / `child`（可到期，需监护人授予）/ `guest`
   （临时访客）。调整设置只追加新版本（`effective_from`），历史事件永远指向当时版本。
2. **事件时刻裁决**：留存粒度（full/summary/minimal）与可执行动作由 `captured_at`
   当刻有效的授权决定，不套用接收时的新设置；断网延迟到达的事件也只按旧版本处置一次。
3. **三级响应**
   - 普通低落：只能从家庭批准的 `allowed_actions` 中选动作（触摸 hold 优先轻抱回应）。
   - 连续异常（默认 10 分钟内 ≥2 次）：生成最少信息关怀提醒，仅含次数与时间窗；
     窗口按 `captured_at` 重算，乱序、重传、重启都只通知一次。
   - 明确危险信号：即使无授权/已撤回/已转借，仍只保留最小事实并进入人工确认升级
     （open → confirmed/false_alarm → resolved）。
4. **撤回与转借立即停止新个性化处理**：撤回后/转借后的非危险事件只留不含信号的操作级
   最小记录；危险事件仍走安全升级。
5. **清理与保留分开**：删除请求返回清理结果（删除的事件/动作数），危险升级所需最小
   事实单列保留并逐条给出保留依据（`retain_reason`、授权/规则版本、案件号）。
6. **两种视图**：客服只能查看脱敏解释（无身份、无置信度、无信号细节）；监护人可导出
   本人权限内的授权版本与处置记录，每条处置都能指回 `consent_id@version` 与
   `rules_version`。

规则版本号见 `service/engine.py` 的 `RULES_VERSION`。

## HTTP 接口

| 方法 | 路径 | 角色 |
| --- | --- | --- |
| GET | `/health` | 公开 |
| POST | `/admin/consents` | admin |
| POST | `/admin/consents/{id}/versions` | admin |
| POST | `/admin/consents/{id}/revoke` | admin |
| POST | `/admin/devices/{id}/transfer` | admin |
| POST | `/events/batch` | 设备回传 |
| GET | `/events/{id}/explain` | support（`X-Role: support`） |
| POST | `/escalations/{case_id}/decision` | safety_officer |
| GET | `/escalations`、`/audit/facts`、`/notifications` | safety_officer |
| POST | `/privacy/deletions` | admin |
| GET | `/guardians/{guardian_id}/export?household_id=...` | guardian（`X-Identity` 须一致） |

## 运行与测试

```bash
python3 -m service.main            # 默认 :8080，可用 PORT / CONSENT_DB 覆盖
python3 -m unittest discover -s tests
```

服务零第三方依赖（仅标准库 + SQLite），全部状态落盘，重启后幂等去重、升级流程、
通知记录与清理进度均可恢复。
