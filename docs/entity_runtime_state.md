# 实体运行时状态（Entity runtime state）

世界文档（WorldDefinition）描述实体**从哪里开始**、是什么；战役状态
（GameState）描述实体**现在在哪里**、是否活着。两者分开保存，这样同一份世界
可以被多个战役使用，而每个战役里 NPC 的位置和生死各自独立、可存档。

## 数据模型

```python
class EntityState(BaseModel):     # backend/app/models/game.py
    entity_id: str
    location_id: str = ""         # 当前所在地点
    hp: int = 1
    max_hp: int = 1
    alive: bool = True
    flags: Dict[str, Any] = {}

class GameState(BaseModel):
    entities: Dict[str, EntityState] = {}   # 新增；旧存档没有这个键也能加载
```

- 记录是**惰性**的：只有当某个回合把实体"落账"之后才会出现。
- 一旦记录存在，`EntityState.location_id` 覆盖 `EntityDef.location_id`。
- 旧存档（没有 `entities` 键）读取后为空字典，投影仍然回落到世界文档，行为不变。

## 落账（seeding）

规则引擎在**任何行动被判定成功**时，会先给"此刻与玩家同处一地"的人/生物补一条记录：

```
evaluate() -> handler(...) -> outcome.ok
    -> changed = 当前地点里还没有记录的实体（跳过 kind == "object" 的布景）
    -> effects = [Effect(kind="seed_entity", target=eid), ...] + 原有 effects
```

- 用效果表达，而不是在规则里直接改状态：这样整回合仍在同一个事务里提交，
  事件引擎仍能看到完整的效果列表，重放请求也不会重复落账（`seed_entity` 幂等）。
- `object` 类型（可交互布景）永远不落账，它们不会移动也不会死。

## 新增的条件与效果

| 条件 kind | 语义 |
| --- | --- |
| `entity_at` | `target`（实体）当前在 `value`（地点） |
| `entity_alive` | `target`（实体）存活；`value` 默认 true |
| `combatant_hp` | 已有：实体当前 HP 与 `value` 按 `op` 比较 |

| 效果 kind | 语义 |
| --- | --- |
| `move_entity` | `target` 实体移动到 `value` 地点（会创建记录） |
| `seed_entity` | 内部使用：为 `target` 实体创建（或忽略已存在的）运行时记录 |
| `combat_hp` | 已有：改 HP；同时把 `alive` 同步写进实体记录 |

两类都走 `WorldDefinition._validate_condition` / `_validate_effect`：
未知实体、未知地点在**授权期**就被拒绝，并给出可读错误，例如

```
event 'bad' effect: unknown entity 'ghost'
event 'bad' effect: unknown location 'nowhere'
event 'bad2' condition: unknown location 'moon'
```

## 规则层

- `look`：`People here` 使用运行时位置；实体走近会出现在名单里，走开会消失。
- `talk`：目标不在当前地点 → `entity_not_here`；实体已死 → `entity_dead`。
- `attack`：敌人已离开当前地点 → `entity_not_here`。
- 判定顺序：`state.entities[eid].location_id` 优先，缺失时回落到 `EntityDef.location_id`
  （`app.engines.event_engine.entity_location`）。

## API 投影

`GET /api/v1/worlds/{world_id}/map` 新增可选查询参数 `campaign_id`：

- 不带：显示世界文档的**初始布局**（编辑器看到的就是作者写的内容）。
- 带：用该战役的运行时位置投影 `locations[].npcs` 与 `entities[].location_id/hp/alive`，
  世界文档本身不被修改。
- 传了但战役不存在或不属于该世界：安全回落到初始布局。

位置为 `LocationDef.npc_ids`（旧式展示名）的历史条目仍会显示，除非同名实体
已有运行时记录并走进了别的地点。

## 前端编辑器

`WorldStudio` 的条件/效果行现在按 kind 提供下拉选择（物品/门/地点/任务/实体），
新增的条件与效果无需手写 ID：

- 条件：实体位于地点、实体存活、实体生命值
- 效果：移动实体（实体 → 地点）、设置实体生命值、玩家受伤

## 测试

- `backend/tests/engines/test_entity_runtime_state.py`（13 条）：落账、只在场的实体落账、
  走开后不可交谈、走近后可交谈、死亡后不可交谈、运行时位置覆盖作者位置、落账幂等、
  不可达实体不出现、事件移动实体且版本号递增、重复请求不重复移动、旧存档兼容、
  校验拒绝未知实体/地点。
- `backend/tests/api/test_routes_entity_state.py`（5 条）：地图不带 campaign 为初始布局、
  带 campaign 为实时位置且世界文档不变、未知 campaign 回落、state 持久化实体记录、
  事件执行日志可见。

## 已知限制

- 没有实体之间的自主行动（NPC 只在事件/规则显式驱动下移动）。
- `EntityState.flags` 目前没有专属的效果种类，可用 `set_flag` 记录全局标记。
- 实体记录不参与 `look` 的物品可见性判断（物品仍由 `item_locations` 管理）。
