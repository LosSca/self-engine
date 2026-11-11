# 起步世界：四种作者预设 + 别名解析

## 问题

审查报告 P0-2：从场景新建、但场景里没有世界的战役，会整条落到旧 LLM 链路——
旧链路的权威状态在进程内存的 `GameSession` 里，规则引擎看不见，于是两套系统
对同一个玩家给出互相矛盾的答案（`/api/game/{id}/inventory` 说空，规则层说没有状态）。

上一轮已经做了兜底：`TextActionService.bindable_world` 找不到世界时会生成一个
"通用起步世界"并绑定。但它对任何 `world_type` 都返回同一张"落脚处 / 里侧 / 倒落的门"，
场景明明写着科幻空间站，玩家开局却站在泥路上。

## 现在

`backend/app/services/starter_worlds.py` 里有四个作者预设，全部是纯数据，
和任何世界一样经过 `validate_world`：

| `world_type` | 名称 | 形状 | 别名示例 |
| --- | --- | --- | --- |
| `fantasy` | 边境村庄 / Border Village | 广场、仓库（上锁）、林边；铁钥匙在广场 | `medieval`, `village`, `rural` |
| `orbital_station` | 轨道站事故 / Orbital Station Incident | 对接舱、环形走廊、反应堆（上锁）、医疗舱；钥匙卡在对接舱 | `sci-fi`, `space station accident`, `科幻`, `空间站` |
| `fog_town` | 雾镇档案 / Fog Town Files | 广场、旅店、档案室、地窖（上锁）；黄铜钥匙在旅店 | `mystery`, `fog town`, `雾镇` |
| `wild_camp` | 荒野营地 / Wild Camp | 营地、河岸（灰狼）、洞窟；无门，靠补给箱推进 | `survival`, `wilderness`, `荒野生存` |

每个预设都满足"能从起点走到钥匙、钥匙能开门"这条纵向切片，中英双语各一份，
并且带一个真实的 `quest`——所以玩家第一回合就不是空场景。

解析顺序（`preset_for`）：

1. `_slug(world_type)` 精确命中（`orbital_station`）；
2. 命中 `PRESET_ALIASES` 里的别名（`sci-fi`、`space station`、`科幻`）；
3. 原文包含预设 key 或别名（`"orbital station accident"`、`"雾镇档案"`）；
4. 都不命中 → 返回 `None`，由 `generic_world` 兜底。**未知类型不会被悄悄归到 fantasy。**

## API

`GET /api/v1/world-presets` → `List[StarterWorldPresetView]`

```json
[{"world_type": "orbital_station", "name": "Orbital Station Incident",
  "summary": "A station out of contact: ...", "locations": 4, "items": 3,
  "doors": 2, "quests": 1}]
```

只读：它描述"这个 `world_type` 会把战役落在哪个世界"，不创建任何状态。
绑定仍然只发生在正常的战役流程里（`bindable_world` → `_provision_starter_world`），
生成的世界依然走 `validate_world`，id 冲突依然用后缀避开、绝不覆盖作者改过的世界。

## 测试

- `backend/tests/services/test_starter_worlds.py` 新增 3 条：
  每个预设在两种语言下都通过 `validate_world`、起点存在、锁门引用的钥匙确实是世界里
  真实存在的物品且开头就在某处；别名与原文匹配都能选中对应预设、未知类型返回 `None`；
  一个 `world_type="orbital_station"` 的战役绑定后得到作者预设，起点是 `dock`、
  钥匙卡在 `dock`、反应堆门是关着的。
- `backend/tests/api/test_routes_world_presets.py` 2 条：预设清单的四条记录与字段；
  调用该接口不会写入任何世界（读前读后 `list_worlds()` 长度一致）。
- `backend/tests/api/test_routes_contracts.py` 的 `TYPED_PATHS` 增加 `GET /world-presets`。

## 已知限制

- 预设仍是手写数据，不是从场景文本生成；`world_type` 之外的场景细节（语气、开场白）
  不进入世界结构，只进入叙事上下文。
- 前端还没有"选择起步世界"的界面，只能通过场景的 `world_type` 间接影响。
