"""响应规则：信号分类与动作选择。

规则内容发生任何变化都必须更新 RULES_VERSION，
处置记录通过该版本号回溯"设备为什么这样回应"。
"""

RULES_VERSION = "rules.v1"

# 普通低落时按优先级尝试的安抚动作，最终只能落在家庭批准的动作集合内。
COMFORT_ACTION_PRIORITY = ("breathing_light", "play_music", "tell_story")

# 同一配对周期内连续多少个异常信号触发一次最少信息关怀提醒。
ANOMALY_STREAK_THRESHOLD = 2

# 信号分类：danger 明确危险；anomaly 需要关注；comfort 普通低落；observe 仅记录。
CLASSIFICATIONS = ("danger", "anomaly", "comfort", "observe")


def classify(signals):
    """按设备侧摘要信号分类，安全类别优先。"""
    if signals.get("risk") == "danger":
        return "danger"
    if signals.get("risk") == "needs_attention":
        return "anomaly"
    if signals.get("mood") == "low":
        return "comfort"
    return "observe"


def select_comfort_action(approved_actions):
    """从家庭批准的动作中按规则优先级选择一个，未批准任何动作时返回 None。"""
    approved = set(approved_actions)
    for action in COMFORT_ACTION_PRIORITY:
        if action in approved:
            return action
    return None
