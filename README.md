# 陪伴设备同意与响应

这里保存家庭陪伴设备接入服务的基础入口与数据约定。尚未建立成员授权、情绪事件处置或数据清理能力。

`contracts/device-events.json` 给出设备批量回传的样例。`captured_at` 来自设备时钟，`sequence` 只在一次设备配对周期内递增，设备恢复出厂后会更换 `pairing_id`。原始语音不会进入平台，`signals` 仅包含设备侧形成的摘要。

使用 `python -m service.main` 启动，健康检查位于 `GET /health`。基础检查命令为 `python -m unittest discover -s tests`。
