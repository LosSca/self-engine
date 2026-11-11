# 阶段 3：战斗规则化（确定性判定）

## 目标

把战斗从 `game_session.py` 的 LLM 裁定改成**规则引擎判定、LLM 只描述**，同时不破坏既有的结构化行动链路（ActionService / 同一事务提交 / request_id 幂等 / 状态版本）。

之前：`CombatEngine.evaluate_action` 让模型给行动的"连贯性/创造性/情境"打分，再用分数算成功率（`final_quality = coherence*0.4 + creativity*0.4 + context*0.2`）。模型输出就是战斗事实。

现在：`RulesEngine` 用校验过的数据结构与确定性种子计算命中、伤害、反击、逃跑；`combat_round` 作为行动结果的一部分，和状态变化在**同一个 SQLite 事务**里提交。

## 数据模型（backend/app/models/game.py）

```python
class CombatantState(_ConfigModel):
    entity_id: str
    name: str = ""
    hp: int = 1
    max_hp: int = 1
    alive: bool = True
    attack: int = 1
    defense: int = 0
    flee_chance: float = 0.5
```

- `GameState` 新增 `combatants: Dict[str, CombatantState]` 与 `combat: Dict[str, Any]`。
- `ActionRequest.action` 扩展为 9 种：`move/look/take/open/use/talk/attack/defend/flee`。
- `ActionResult.combat_round` 记录本回合判定结果。
- `Condition` 新增 `combatant_hp` / `combatant_alive`；`Effect` 新增 `combat_hp` / `player_damage`，因此事件和任务可以按血量/存活触发。
- 敌人数值仍写在 `EntityDef.attributes`（`max_hp/hp/attack/defense/flee_chance`），旧场景 JSON 无需改动即可继续加载。

## 规则（backend/app/engines/rules_engine.py）

- 种子：`sha256(f"{campaign_id}|{turn}|{action}|{target}")[:16]`，同一回合同一行动可复现；规则函数是纯函数，不修改 state。
- 命中率：`min(0.95, max(0.05, 0.5 + (attack - defense) * 0.1))`。
- 伤害：`max(1, attack - defense // 2) + rand`，反击概率与命中同式。
- 防御：本回合护甲 `2 + player_defense`。
- 逃跑：按 `flee_chance` 判定，失败则承受 `max(1, attack // 2)` 伤害。
- 玩家数值来自 flags：`player_attack`（默认 4）、`player_defense`（默认 1）、`player_hp`（默认 `player_max_hp`/20）。

## 事件系统（backend/app/engines/event_engine.py）

- `combat_hp` 效果按实体 attributes 种入战斗者（与规则引擎同一套数字来源），血量归零时设置 `alive=False`、`combat.active=False`、`flags.combat_active=False`，并产出 `combatant_defeated` 事实。
- `player_damage` 递减 `player_hp`，小于等于 0 时产出 `player_defeated`。
- `validate_event` 传入 `entity_ids`，未知实体在保存阶段就会被拒绝。

## 行动服务（backend/app/services/action_service.py）

`_combat_round()` 在效果应用之后、事件触发之前生成：

```json
{
  "round": 2, "action": "attack", "target_id": "goblin",
  "target_hp_before": 6, "target_hp_after": 3, "target_alive": true,
  "player_hp_before": 20, "player_hp_after": 17, "player_defeated": false,
  "in_combat": true, "facts": ["combat_hp", "flag"]
}
```

它随 `commit_action` 一起写入 action_requests / campaign_state / snapshots / domain_events，重复 request_id 会原样回放同一个 combat_round，不会二次掷骰。

## 自然语言入口（backend/app/services/text_action_service.py）

新增动词：攻击/打击/砍/劈/刺/揍/attack/strike/fight/kill/stab/slash；防御/格挡/防守/defend/block/guard/parry；逃跑/逃走/撤离/撤退/run away/retreat/escape。

目标候选只包含当前地点的 `kind == "creature"` 或 `hostile` 实体，以及正在交战的 `combat.target_id`。没有目标时会话提示"没有可以攻击的目标。"，只有唯一敌对目标时自动锁定。

注意："打" 不单独作为 attack 动词（`打开` 会被误判），"刺前需有目标语义"同理已改为 `刺向/刺` 与 `hit the`。

## API（backend/app/api/routes_engine.py）

```
POST /api/v1/campaigns/{campaign_id}/combat
{ "action": "attack|defend|flee", "target_id": "goblin", "request_id": "...", "narrate": true }
-> ActionResult（含 combat_round）
```

与 `/actions`、`/text-actions` 走同一个 ActionService，不产生第二套规则。

## 前端（frontend/src/components/CombatOverlay.jsx）

战斗 HUD 增加攻击/防御/逃跑三个按钮，调用 `runCombatAction`，用返回的 `combat_round` 显示血量变化与回合数；击败目标后自动退出战斗态。叙事文本仍来自服务端已提交结果。

## 测试

- `backend/tests/services/test_combat.py`（9 个）：规则纯函数可重复、缺目标/未知目标拒绝、攻击提交后战斗者落库、两个独立库结果一致（确定性）、连续攻击直到击败后再次攻击返回 `target_defeated`、重复 request_id 不重复掷骰、防御+逃跑、事件可响应战斗事实。
- `backend/tests/api/test_routes_combat.py`（3 个）：HTTP 提交并返回 combat_round、幂等、未知目标拒绝。
- 全量：`511 passed, 9 failed, 8 skipped`。9 个失败与上游基线 `fc0d73d` 完全一致（journal 直注入 3、npc_mind 1、game_session mock 5），非本次改动引入。

## 已知限制

- 尚未实现进入战斗的显式判定（现在由玩家发起 attack 即开始交战）；主动敌对（怪物先攻击）依赖事件系统 `player_damage` 效果。
- 战斗无网格/距离，属于叙事级数值战斗；需要在阶段 2 地图坐标落地后再加距离规则。
- 前端战斗 HUD 依赖 `/combat` 接口，旧 `/api/game/action` 流式链路中的 LLM 战斗评分仍保留，但不再是结构化链路的一部分。


---

# 阶段 3b：声明式行动目录（action registry）

## 问题

行动类型原本散在三处硬编码：`rules_engine.evaluate` 的 if 链、`text_action_service.ACTIONS`、
`ActionRequest.action` 的 Literal。新增一种行动要同时改三处，漏一处就出现"接口接受但规则不认识"
或"解析器能识别但接口 422"。

## 改动

新增 `backend/app/engines/action_registry.py`：每个行动是一条 `ActionSpec`。

```python
@dataclass(frozen=True)
class ActionSpec:
    name: str            # 稳定 id，等于 ActionRequest.action 的取值
    label: str           # 中文显示名，给编辑器和调试台用
    handler: str         # 规则方法名，如 "_take"
    target: TargetNeed   # "none" | "required" | "optional"
    description: str = ""
    verbs: Tuple[str, ...] = ()   # 文本解析用的动词表
    verb_priority: int = 100      # 同一条话里多个动词都能匹配时的顺序
```

- `register_action(spec)` 幂等注册（同名替换，位置不变）。
- `get_action(name)` / `action_names()` / `all_actions()` / `verb_table()`。
- `RulesEngine.evaluate` 改为查目录后 `getattr(self, spec.handler)(world, state, target_id, params)`，
  所有规则方法签名统一为 `(world, state, target_id=None, params=None)`。
- `text_action_service.ACTIONS = action_names()`，`_VERBS = verb_table()`，解析器与规则表不可能再漂移。
- 新端点 `GET /api/v1/action-types` 返回目录（name/label/target/description/verbs），编辑器与调试台读它。

## 刻意保留的两条约束

1. **目录不含代码**：`ActionSpec` 只有数据，`handler` 只是本类方法名，配置无法在运行时注入实现。
   可配置性通过"新增 ActionSpec + 新增规则方法"实现，仍然走代码评审与测试。
2. **动词顺序是作者写的，不是算出来的**：中文短语无法按字符数排序（`打开` 只有 2 字却必须
   先于 `开`，`unlock` 有 6 个字符却要排在后面），所以 `verb_table()` 原样保留声明顺序。

## 测试

`backend/tests/engines/test_action_registry.py`（7 条）与 `backend/tests/api/test_routes_action_types.py`（1 条）：

- 每条目录项都指向真实存在的规则方法；
- 目录名字集合 == `ActionRequest.action` 的 Literal 取值（防止接口与目录漂移）；
- 未知行动返回 `action_not_supported` 且消息带行动名；
- 目录项确实被 `evaluate` 派发（monkeypatch 规则方法后观察调用参数）；
- target 需求元数据正确；动词顺序保持声明顺序；
- 重复注册不产生重复项，可回滚；
- API 返回顺序与目录一致。

## 已知限制

- 行动仍是代码里声明的；世界配置暂时只能引用这 9 种行动，还不能定义"场景自定义行动"
  （需要先有参数模式校验，见后续阶段）。
- `ActionRequest.action` 仍是静态 Literal（Pydantic 需要），由测试保证与目录同步。
