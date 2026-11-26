# 意图建议器（Intent Advisor）

确定性解析器覆盖了所有「动词 + 目标」的说法。当一条说法完全无法映射时，规格允许由 LLM 辅助理解玩家意图——
但绝不能让它改权威状态。这个模块就是那条边界。

## 什么时候需要 LLM，什么时候不需要

**默认不需要。** 解析器是确定性的：动词表 + 地点/物品/人物名的精确与别名匹配，中文前缀
（"我去…"、"帮我…"）、`去铁匠铺` 这类简称、`[SAY]` 说话模式都在代码里处理，不花一次调用。
加上 `improvise` 之后，认不出动词的句子也不再是错句：它变成一次不改变状态的尝试。

只有一种句子值得花模型调用：**它对不上任何规则**（既没有动词表里的动词，也不是说话，还
没提到任何已知实体）——例如 "the thing with the key, you know, do that"、"按门把手"。
反过来，下面这些**不会**去问模型：

- 规则已经认出动词、只是拒绝了的（"你拿不动月亮"、目标不在眼前）——那已经是答案；
- 只写了一个名字的（"iron"）——玩家只是还没说要做什么；
- 动词拼错的（"openly iron_key"）——那是输入错误，不是一个意思。

判定在 `TextActionService.execute`：`parse(..., improvise=False)` 返回空后，先查一次**学习表**（见下文，
命中就不再问模型），没有再 `_advise`。

## 开关

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `ENGINE_INTENT_ADVISOR` | `0`（关） | 打开后才允许向模型提问 |
| `ENGINE_INTENT_TIMEOUT` | `8.0` 秒 | 超过这个时间直接放弃，这句话退化成一次尝试 |

超时默认值为什么是 8 秒而不是 3 秒：托管模型一次往返常在 3–6 秒，3 秒会让每一次提问都被跳过，
开关看起来像没打开。

默认关闭，所以没有模型的机器行为与之前完全一致：能映射的照常走规则，映射不了的是一次
`improvise` 尝试。打开它，只是让"映射不了"的那一类多了第二次被理解的机会。

## 一次建议的完整流程

1. 确定性解析器 `TextActionParser.parse(..., improvise=False)` 返回 `None`（连猜测都没命中，
   并且这句话没有被任何规则解释过）。
2. 若开关打开且已接上 router（走 `narrator_factory.build_router()`，与叙事用同一个 provider），把
   **当前可用的行动白名单 + 玩家原话** 发给模型，要求只返回
   `{"action": "<白名单内>", "target": "<原话中的目标词>", "confidence": 0..1}`。
3. 回复必须是 JSON（允许 ```json 围栏```）；`action` 不在白名单、解析失败、超时、抛异常 →
   全部返回 `None`，这句话随后退化成一次 `improvise` 尝试（不再直接拒绝）。
4. 建议被**重新拼装成一条动词 + 目标的说法**（动作用注册表里的首个动词），交回同一个
   `TextActionParser.parse`。因此模型只能决定「试哪一行动词表」，目标必须能在世界数据里真实匹配。
5. 解析成功才构造 `ActionRequest`，照常进入 `ActionService`：前置条件、消耗、副作用全部由规则引擎判定。
6. 结果里 `parsed.confidence = "advised"`、`parsed.note` 追加 `intent advisor`，调试时可一眼看出这一步是模型帮的忙。

任何一步失败都会落回"这是一次尝试"，不会写入任何状态改变。

## 学到的说法留在表里（`intent_learnings`）

同一个玩家常会反复用同一句话试同一个东西（"我凑近看看那个歪掉的小药柜"）。每次失败都再问一次
模型既慢又贵，所以**顾问回答有效的那一次会被记下来**：

| 列 | 含义 |
| --- | --- |
| `scope_key` | 世界 id——说法与房间里的东西有关，换个世界不该复用 |
| `lang` | `zh` / 空串；中文与英文各自一套 |
| `phrase` | 归一化后的原话：去掉 `[DO]`/`[SAY]` 信封、NFKC、小写、压空白、剥首尾标点 |
| `action` | 顾问给出的白名单行动名（`talk` 表示说话） |
| `fragment` | 原话里的目标词，用来重新拼「动词 + 目标」 |
| `hits` / `source` / `confidence` | 被复用次数、来源（`advisor` 或 `manual`）、当时的置信度 |

下一次同一句话（归一化后相同）先查这张表：命中就把 `hits` 加一，用同一套「动词 + 目标」拼法交回
解析器复核，成功后 `note` 写 `intent memory`、`confidence` 仍是 `advised`，**不再调用模型**。表中
找不到、或那条记录已经解析不通（世界换了、物品被拿走），才回到顾问。

实测（Spark X2.6）：第一次 4.30 秒、写表；第二次同一句 2.25 秒、日志/结果都显示 `intent memory`。

表可以手工维护，也可以让作者预先教引擎几句：

```
GET    /api/intent?scope=<world_id>&limit=200   # 列表（附命中次数）
POST   /api/intent                              # 手写一条（source=manual）
PATCH  /api/intent/{id}                         # 改 action / fragment / confidence
DELETE /api/intent/{id}                         # 删一条
DELETE /api/intent?scope=<world_id>             # 清空一个世界
```

界面入口在侧栏「叙事与选项」→ 学习表页签：可切世界作用域、刷新、手写、逐条修改与删除。

## 为什么不是「让模型直接给结构化行动」

因为那样模型就能绕开前置条件。现在模型无法指定 `target_id`，也无法宣布结果；
它给出的目标词还要经过 `_resolve` 与 `RulesEngine` 的双重确认。

## 代码位置

- `backend/app/services/intent_advisor.py`：`enabled()`、`timeout_seconds()`、`parse_reply()`、`IntentAdvisor.suggest()`。
- `backend/app/services/text_action_service.py`：`TextActionService._advise()`、`_recall_learned()`、`_parse_learned()`、
  `_remember_learned()` 与 `_VERBS_PRIMARY`。
- `backend/app/db/intent_memory_store.py`：学习表 `intent_learnings` 与 `normalise_phrase()`。
- `backend/app/api/routes_intent_memory.py`：`/api/intent` 的增删改查。
- `backend/app/api/routes_engine.py`：`_build_advisor()`，`POST /api/v1/campaigns/{id}/text-actions` 会带上它。

## 测试

- `backend/tests/services/test_intent_advisor.py`（10 条）：开关解析、超时边界、围栏 JSON、白名单外行动被拒、垃圾输出、缺 confidence、慢模型被跳过、模型报错降级。
- `backend/tests/services/test_text_action_advisor.py`（7 条）：关闭时根本不提问、开启后可救回无法映射的说法、编造目标仍被拒、规则仍然拒绝无钥匙开门、开启但未接线保持拒绝、已识别的说法不会走建议器。
- `backend/tests/services/test_intent_memory.py`（8 条）：信封与大小写归一、跨世界不串、命中次数递增、顾问结果
  被写进表、第二次不再问模型、关掉顾问后学习记录仍生效、过期记录不被采用、`[SAY]` 不复用 `look` 的记录。

## 已知限制

- 建议器只在文本入口（`text-actions`）生效，结构化 `/actions` 本来就带行动名，不需要它。
- 不做多轮澄清：一次建议失败就回到拒绝，玩家可以换个说法。
- 自定义行动只会出现在白名单里，但拼装时使用内置动词，所以「自定义一词 + 目标」仍依赖世界自己声明的动词被解析器命中。
- 学习表按归一化短语**精确匹配**，不做泛化：把"歪掉的小药柜"换成"那个药柜"仍要重新问一次顾问。
- 学习记录绑定世界：同一条短语换个世界不会复用，以免把 A 世界的物品名套进 B 世界。
