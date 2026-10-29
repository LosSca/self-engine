# 可配置物品：拾取条件与使用效果

物品不再只是「名字 + 标签」。世界作者可以直接在数据里声明**什么条件下才能拿起它**，
以及**使用它会发生什么**。两者都是经过 Pydantic 校验的 `Condition` / `Effect` 数据结构，
没有任何配置会被当作代码执行。

## 数据模型

`backend/app/models/game.py` 的 `ItemDef`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` / `name` / `description` / `tags` / `stackable` | — | 原有字段，含义不变 |
| `pickup_requires` | `List[Condition]` | **全部满足**才能拾取；空表示无条件 |
| `use_effects` | `List[Effect]` | 使用物品时执行的效果；空表示沿用内置行为 |
| `consumable` | `bool` | 使用后是否消耗一个（仅当声明了 `use_effects` 时生效） |

`Condition.kind` 与 `Effect.kind` 与事件系统共用同一份封闭枚举：
`flag / has_item / item_count / door_open / at_location / quest_status / combatant_hp /
combatant_alive / entity_at / entity_alive`，效果则是
`set_flag / give_item / remove_item / move_player / open_door / close_door / quest_progress /
quest_complete / combat_hp / player_damage / move_entity / seed_entity / emit`。
未知的 kind 在反序列化阶段就失败，不会进入运行期。

## 校验时机

`WorldDefinition.validate_references()` 对每个物品做两轮检查，错误信息带前缀：

- `item '<id>' pickup condition: unknown item 'ghost'`
- `item '<id>' use effect: unknown door 'ghost_door'`

`world_editor.upsert_item()` 走同一条校验路径，非法配置不会落库（旧文档保持不变）。

## 规则行为

`backend/app/engines/rules_engine.py`：

- **拾取** `_take`：先判定物品是否在当前地点、是否已在背包，再看 `pickup_requires`。
  任一条件不满足即返回 `code="pickup_refused"`，消息形如
  `You cannot pick up the Silver Charm yet: 'statue_toppled' must be True.`。
  拒绝不会改动状态，也不会消耗问题描述里的任何资源。
- **使用** `_use`：**授权行为优先**。物品声明了 `use_effects` 就执行它们
  （`consumable` 为真时额外追加一条 `remove_item`），返回 `code="used_item"`。
  没有声明时保持原行为：如果这件物品是某扇门的钥匙且玩家就在门所在的地点，
  则开门（`door.consumes_key` 时消耗钥匙）。

因为效果统一交给 `event_engine.apply_effects`，物品效果与事件效果在同一次事务里提交，
所以「使用药水回血」和「触发一个事件」享有同样的原子性与幂等性：
同一个 `request_id` 重放不会重复回血、重复消耗。

## API

物品仍然通过世界编辑接口写入，没有新增路径：

```
PUT    /api/v1/worlds/{world_id}/items/{item_id}
DELETE /api/v1/worlds/{world_id}/items/{item_id}
```

请求体即 `ItemDef`，例如：

```json
{
  "id": "healing_potion",
  "name": "Healing Potion",
  "description": "Bitter, and it smells of pine.",
  "pickup_requires": [{"kind": "flag", "target": "crate_moved", "value": true}],
  "use_effects": [{"kind": "set_flag", "target": "healed", "value": true}],
  "consumable": true
}
```

## 前端

`frontend/src/components/WorldStudio.jsx` 的「物品」标签页新增：

- **拾取条件**：复用 `ConditionRows`，目标从物品/门/地点/任务/实体下拉里选。
- **使用效果**：复用 `EffectRows`。
- **使用后消耗一个**：复选框。

列表行会显示 `拾取条件 N · 使用效果 M · 使用后消耗`，方便快速检查。

## 测试

`backend/tests/engines/test_item_config.py`（11 条）：

| 测试 | 断言 |
| --- | --- |
| 可声明拾取条件与使用效果 | 字段往返正确 |
| 未知物品的拾取条件 | 校验报 `pickup condition: unknown item` |
| 未知门的 use effect | 校验报 `use effect: unknown door` |
| 未知效果 kind | Pydantic 拒绝 |
| 世界编辑器拒绝非法物品 | 抛 `WorldValidationError`，存储不变 |
| 条件未满足 | `pickup_refused`，背包无变化 |
| 条件满足 | `taken`，物品进背包 |
| 使用带效果的可消耗物品 | `used_item`，flag 生效，数量减一 |
| 非消耗物品 | flag 生效，物品仍在 |
| 无 use_effects 的钥匙 | 仍能打开对应门 |
| 被拒绝的拾取重放 | `duplicate` 且不产生第二次变化 |

## 已知限制

- 物品效果不能引用「持有者」自身以外的新实体字段（如「给 NPC 加物品」）。
- `consumable` 只在有 `use_effects` 时生效；纯钥匙物品的消耗仍由 `door.consumes_key` 决定。
- 拾取条件在 `take` 时判定一次，不参与地图上的可见性（物品在未满足条件时仍然可见）。
