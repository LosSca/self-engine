# 意图建议器（Intent Advisor）

确定性解析器覆盖了所有「动词 + 目标」的说法。当一条说法完全无法映射时，规格允许由 LLM 辅助理解玩家意图——
但绝不能让它改权威状态。这个模块就是那条边界。

## 开关

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `ENGINE_INTENT_ADVISOR` | `0`（关） | 打开后才允许向模型提问 |
| `ENGINE_INTENT_TIMEOUT` | `3.0` 秒 | 超过这个时间直接放弃，保持原有拒绝 |

默认关闭，所以没有模型的机器行为与之前完全一致。

## 一次建议的完整流程

1. 确定性解析器 `TextActionParser.parse` 返回 `None`（连猜测都没命中）。
2. 若开关打开且已接上 router（走 `narrator_factory.build_router()`，与叙事用同一个 provider），把
   **当前可用的行动白名单 + 玩家原话** 发给模型，要求只返回
   `{"action": "<白名单内>", "target": "<原话中的目标词>", "confidence": 0..1}`。
3. 回复必须是 JSON（允许 ```json 围栏```）；`action` 不在白名单、解析失败、超时、抛异常 →
   全部返回 `None`，后续走原来的 `unparsed_action` 拒绝。
4. 建议被**重新拼装成一条动词 + 目标的说法**（动作用注册表里的首个动词），交回同一个
   `TextActionParser.parse`。因此模型只能决定「试哪一行动词表」，目标必须能在世界数据里真实匹配。
5. 解析成功才构造 `ActionRequest`，照常进入 `ActionService`：前置条件、消耗、副作用全部由规则引擎判定。
6. 结果里 `parsed.confidence = "advised"`、`parsed.note` 追加 `intent advisor`，调试时可一眼看出这一步是模型帮的忙。

任何一步失败都不会改变「这回合被拒绝」这个结论，也不会写入状态。

## 为什么不是「让模型直接给结构化行动」

因为那样模型就能绕开前置条件。现在模型无法指定 `target_id`，也无法宣布结果；
它给出的目标词还要经过 `_resolve` 与 `RulesEngine` 的双重确认。

## 代码位置

- `backend/app/services/intent_advisor.py`：`enabled()`、`timeout_seconds()`、`parse_reply()`、`IntentAdvisor.suggest()`。
- `backend/app/services/text_action_service.py`：`TextActionService._advise()` 与 `_VERBS_PRIMARY`。
- `backend/app/api/routes_engine.py`：`_build_advisor()`，`POST /api/v1/campaigns/{id}/text-actions` 会带上它。

## 测试

- `backend/tests/services/test_intent_advisor.py`（10 条）：开关解析、超时边界、围栏 JSON、白名单外行动被拒、垃圾输出、缺 confidence、慢模型被跳过、模型报错降级。
- `backend/tests/services/test_text_action_advisor.py`（7 条）：关闭时根本不提问、开启后可救回无法映射的说法、编造目标仍被拒、规则仍然拒绝无钥匙开门、开启但未接线保持拒绝、已识别的说法不会走建议器。

## 已知限制

- 建议器只在文本入口（`text-actions`）生效，结构化 `/actions` 本来就带行动名，不需要它。
- 不做多轮澄清：一次建议失败就回到拒绝，玩家可以换个说法。
- 自定义行动只会出现在白名单里，但拼装时使用内置动词，所以「自定义一词 + 目标」仍依赖世界自己声明的动词被解析器命中。
