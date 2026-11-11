# 无世界战役的自动绑定与起步世界生成

## 问题

新建场景 + 战役时，该场景通常没有绑定任何世界。此时 `routes_game._unified_service` 找不到世界，返回 `None`，`POST /api/game/action` 于是回落到旧的 LLM 会话：

- 旧会话的权威状态在内存里（`_sessions`），`GameStateStore` 完全看不见它；
- `GET /api/v1/campaigns/{id}/state` 返回空、`/inventory` 返回 `[]`，而玩家在聊天界面里明明拿到了东西；
- 两套系统对同一个玩家给出**互相矛盾**的答案。

## 方案

在规则链路里补一步"起步世界"：任何战役只要能读到场景，就一定能进入规则链路。

解析顺序（全部确定性）：

1. 战役已经绑定世界 → 直接用（**永不重新绑定**，不会覆盖进度）。
2. 该场景下有作者写好的世界 → 按创建顺序取第一个并绑定（原有行为，保留）。
3. 都没有 → 生成一个校验通过的起步世界，存库并绑定。

### 生成来源

`backend/app/services/starter_worlds.py`

| 入口 | 说明 |
|---|---|
| `preset_for(world_type, language)` | 按场景的 `world_type` 取作者预设；目前 `fantasy`（含 medieval/rural/village 别名）。 |
| `generic_world(title, summary, language)` | 场景没有预设时，用场景自己的标题与描述造一个三地点小世界。 |
| `build_world_document(...)` | 组装完整 `WorldDefinition` 载荷。 |
| `starter_world_id(campaign_id)` | 由战役 id 推出的稳定 id，重试不会造出第二个世界。 |
| `alternate_world_id(world_id, n)` | 第 n 次尝试的 id，**绝不覆盖**已存在的世界。 |

生成的世界一定包含一个可玩的闭环：起点地点 + 一扇锁着的门（`inner_door`）+ 能打开它的钥匙（`traveler_key`）+ 一条任务 `q_first_steps`（obtain_item → open_door）。语言跟随场景：`zh`、`zh-CN`、`中文` 都走中文模板。

### 落点

`backend/app/services/text_action_service.py`

- `bindable_world()`：第 3 步改为调用 `_provision_starter_world()`。
- `_provision_starter_world(campaign_id, campaign)`：生成 → `WorldDefinition` 校验 → `world_service.validate_world` 校验 → 保存 → 绑定。**任何一步失败都不保存、不绑定**，只记日志，调用方仍然安全回落。
- `_world_for_state(state, campaign_id)`：状态指向的世界被删掉时，用同样的方式补一个，而不是让回合失败。

`backend/app/api/routes_game.py`

- `_unified_service()` 把上面两种异常路径都收敛为"没有可用世界才返回 None"。

## 兼容性

- 已有世界、已有战役状态**一律不动**：`bindable_world` 只在没有状态时绑定，`_provision_starter_world` 跳过任何已存在的 world_id。
- 生成的世界与作者世界的形状完全一致，可以在世界工作室里继续编辑、删除、导出。
- 删除世界后该战役不会丢进度：下次行动会给它补一个新的起步世界。

## 顺带修复

`frontend/src/api.js` 的 `streamAction` 以前硬编码 `provider: 'deepseek', model: 'deepseek-v4-flash'`，这正好撞上审查里 P0-3 的 401（"Authentication Fails"），并让每次叙事都悄悄降级。现在默认传空，交给服务端配置决定。

## 测试

- `backend/tests/services/test_starter_worlds.py`（9 条）：四种形状都能通过 `validate_world`、生成确定性、绑定幂等、作者世界优先、同 id 不覆盖、世界被删后仍能补、生成世界能打完一个完整回合、中文场景得到中文世界。
- `backend/tests/api/test_routes_starter_world.py`（3 条）：HTTP 层 —— 新战役调用聊天接口后 `/lane` 变为 `engine` 且绑定 `starter_*`、无钥匙开门被拒 / 有钥匙开门成功 / 重复请求幂等、生成的世界出现在 `/api/v1/worlds` 且可继续编辑。

## 已知限制

- 预设只有一个 `fantasy`；其它 `world_type` 落到通用模板。
- 通用模板是刻意的极简结构（三地点），它的作用是保证"能玩、可校验、可编辑"，不是替代作者世界。
- 世界工作室的"游玩进度"下拉仍只列出已绑定世界的战役；新战役在第一次行动后才有世界。
