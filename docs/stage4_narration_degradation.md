# 阶段 4：叙事降级与模型输出校验

系统必须做到「叙事失败不影响事实」。这条验收（m00001 第 11 条）在本次改造前只做了一半：
narrator 抛异常会被捕获，但回合会以 `narrative.status = "failed"`、**空正文**返回，
玩家看到的是一个「失败」的回合，只能自己去翻状态面板。同时，模型编造奖励（例如
常规返回 "You gain 50 gold" 而规则没发任何东西）没有任何拦截。

## 现在的行为

```
规则判定 → 应用效果/事件/任务（在副本上）
        → narrator 生成散文（只读已结算状态）
        → 输出校验 _verified()
             ├─ 合规 → status=ok, model=llm
             └─ 不合规/异常/超时 → status=failed + error
        → 降级 render_fallback()：用规则结果拼确定性描述
             └─ status=ok, model=fallback, error=<原因>
        → 一个事务提交状态 + 行动记录 + 域事件
```

关键点：

1. **叙事永远不是交易的一部分。** 状态、行动记录、域事件的提交与叙事成败无关。
2. **降级后依然是"ok 回合"。** `NarrativeResult.model = "fallback"` 是界面和调试器区分
   "模型写的" 与 "规则拼的" 的唯一信号；`error` 字段保留原始失败原因。
3. **域事件照记。** 只要 `narrative.error` 非空就写一条 `narrative.failed`，payload 增加
   `degraded`（本次是否降级）与 `model`，便于统计"模型不可用"的频率。
4. **输出校验看的是规则，不是文字风格。** `_verified()` 只拒绝"宣称获得了规则没发的东西"。

## 输出校验的实现

`backend/app/services/narrative_service.py`

- `_issued_things(world, effects)`：这一回合**真正**发放或解锁的东西（`give_item` /
  `add_currency` / `add_xp` / `skill_change` / `open_door` / `equip_item`），取物品名、
  门名与 id 的小写形式。事件效果也算（`ActionService` 把它们作为 `executions_effects` 传入）。
- `_reward_claims(text)`：散文里"玩家获得了什么"的说法，返回 `(phrase, offset)`。
  英文用 `<you> ... <动词>` 的窗口扫描（动词表 `_REWARD_WORDS`），中文用
  `获得/得到/拿到/拾取/捡到/收下/入手`。
- `_unbacked_reward(text, issued)`：逐个说法判断，附近（60 字符窗口）没有提到真实发放的
  东西就算编造。所以 "You take the key and gain 50 gold" 会因 gold 被拒，而
  "You take the key" 不会。
- `_verified(world, outcome, text, executions_effects)`：合规返回 `model="llm"`，
  否则 `status="failed"` + 可读错误 `narrator announced a reward the rules did not award
  (state is unaffected)`。

### 这台机器上 re 的坑（写在代码注释里）

本机 Python 3.12 上，**较长的正则**（带 lookbehind、或多分支交替）会**静默不匹配**：
`r"(?<![\w])(?:^|[\s,.!?;])you\s+(?:now\s+)?(?:gain|...|earned)\b"` 对
"You take the key and gain 50 gold." 返回 `None`，而 `re.search(r"gain", ...)` 正常。
`_REWARD_SCAN` 因此只保留最朴素的形式 `(?:you)\s+(?:now\s+)?`，其余判断全在 Python 里做。
**不要把它们合并回一个大正则。**

## 界面表现

`frontend/src/components/GameCanvas.jsx` 的 `applyEngineResult`：

- `narrative.model === "fallback"` 时，正文后面追加
  「（叙事模型不可用，以上为规则结果的机器描述，状态已提交。）」；
- 降级正文由 `effects_applied`（「获得 X。」「X 打开了。」「你来到了 X。」）与
  `events_fired`（「（触发了事件：name）」）拼出；
- `narrative.status === "failed"` 但回合成功时，优先显示降级正文而不是错误行。

## 测试

- `backend/tests/services/test_narrative_fallback.py`（7 条）：失败 narrator 仍描述已提交回合、
  异常 narrator 在被拒行动上降级、`render_fallback` 含效果与事件名、中英奖励话术检测、
  编造奖励被拒、提到真实发放物品被接受、事件效果算作已发放。
- `backend/tests/services/test_action_service.py`：叙事异常不回滚状态，断言降级为
  `ok` + `model="fallback"` + `error` 保留。
- `backend/tests/api/test_acceptance_story.py::test_acceptance_narrative_failure_keeps_committed_state`：
  验收第 11 条的端到端版本。

全量后端 1285 passed / 8 skipped；前端构建通过。

## 已知限制

- 校验只针对"编造奖励"这一类越界。模型若描述出与规则**冲突但不涉及奖励**的细节
  （例如说门已经打开而规则说还锁着），目前不会被拦截，只是不会被采纳为状态。
- 中文判定以整个段落为窗口（中文没有词间空格），比英文宽松。
- 降级描述是模板拼接，不带 NPC 语气；这是刻意的——它必须确定可复现。
