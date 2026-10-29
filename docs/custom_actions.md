# 场景自定义行动（custom actions）

世界不再只能使用引擎内置的 9 种行动。世界定义里可以声明自己的行动，包含
"什么时候允许"（条件）和"允许之后发生什么"（效果）。两者都是**经过校验的数据**，
由规则引擎解释执行，**绝不执行任意代码**（没有 eval、没有 Python 片段、没有字符串脚本）。

## 数据模型

`backend/app/models/game.py`：

```python
class CustomActionDef(_ConfigModel):   # extra="forbid"
    id: str                              # 1..64 字符，不能与内置行动同名
    label: str = ""                      # 界面/叙事里显示的名字，如「祈祷」
    description: str = ""                # 规则通过后作为行动结果说明
    verbs: List[str]                     # 玩家会说的触发词，如 ["祈祷", "pray"]
    target: Literal["none", "required", "optional"] = "none"
    requires: List[Condition]            # 全部满足才允许执行
    effects: List[Effect]                # 通过后由规则引擎施加
```

挂在 `WorldDefinition.custom_actions` 上。条件和效果复用事件系统同一套 `Condition` /
`Effect`，因此能用的能力与事件完全一致（flag、has_item、door_open、quest_status、
combatant_hp…；set_flag、give_item、open_door、quest_progress…）。

## 授权期校验（保存世界时）

`WorldDefinition.validate_references()` → `_validate_custom_actions(...)` 会拒绝：

| 情况 | 报错 |
| --- | --- |
| id 重复 | `custom action 'x' is duplicated` |
| id 与内置行动同名 | `collides with a built-in action name` |
| 没有任何触发词 | `declares no verbs, so no player utterance could ever reach it` |
| 触发词与内置动词重复 | `is already mapped to a built-in action` |
| 两个自定义行动抢同一个触发词 | `is already used by custom action 'owner'` |
| 条件/效果引用了不存在的物品、门、任务、实体 | `custom action 'x' condition: unknown item 'y'` 等 |

保存接口（`PUT /api/v1/worlds/{world_id}/custom-actions/{action_id}`）走的是同一套校验，
所以非法配置在写入时就返回 400，不会落库。

## 判定流程

1. 玩家输入 → `TextActionService.parse()`：**内置动词优先**匹配；没有命中才匹配世界声明的
   触发词，产出 `action="custom"` + `params={"action_id": "pray"}`。
2. `RulesEngine._custom()`：
   - 缺 `action_id` → `custom_action_missing`
   - 世界里找不到 → `unknown_custom_action`
   - 目标无法识别 → `unknown_target`
   - `target="required"` 但没给目标 → `custom_target_required`
   - 任一条件不成立 → `condition_failed`，消息形如 `you need 'iron_key'`
   - 全部通过 → `RuleOutcome(ok=True, code="custom_action", effects=...)`
3. `ActionService` 照常把效果写进状态、推进事件与任务、写快照与事件记录 —— 与内置行动
   走完全相同的同事务提交、`request_id` 幂等、状态版本路径。

也就是说：自定义行动不是"在叙事里假装做过"，而是真正走确定性规则和事务。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/api/v1/worlds/{world_id}/custom-actions/{action_id}` | 新建/更新，body 为 `CustomActionDef` |
| DELETE | `/api/v1/worlds/{world_id}/custom-actions/{action_id}` | 删除 |
| GET | `/api/v1/action-types` | 内置行动目录（name/label/target/verbs），供前端展示 |

## 前端

"世界工作室"（WorldStudio）新增「行动」标签页：

- 列出世界里已配置的自定义行动，显示触发词、目标要求、条件数、效果数；
- 表单支持行动 ID、显示名称、目标要求、描述、触发词（逗号分隔）、前置条件行、行动效果行；
- 同一页顶部展示引擎内置行动目录（只读，来自 `GET /api/v1/action-types`），避免与内置动词撞车。

## 测试

- `backend/tests/engines/test_custom_actions.py`（20 条）：授权期校验、规则层各拒绝码、
  条件满足后真实生效、同 `request_id` 重复只结算一次、解析层内置优先。
- `backend/tests/api/test_routes_custom_actions.py`（7 条）：CRUD、路径 id 不匹配、非法引用、
  未知 effect kind（`run_python` → 422）、`/action-types`、`text-actions` 端到端。

## 已知限制

- 不支持带参数的行动（如「给 X 100 金币」）；`params` 目前只承载 `action_id`。
- 不具备战斗回合、剧情节点跳转等复合语义；复杂流程请用事件 + 任务组合。
- 触发词只在 `TextActionService` 里参与解析；结构化 API（`/actions`）需要显式传
  `action="custom"` + `params.action_id`。
