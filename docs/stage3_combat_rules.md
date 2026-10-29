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
