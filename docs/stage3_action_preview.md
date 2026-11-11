# 阶段 3：行动目录与干运行预演（P2 声明式行动类型）

## 目标

审查报告里的 P2 项「声明式行动类型」的实际缺口不是「加动词要改 if 链」——`app/engines/action_registry.py` 早就是声明式目录（每条 `ActionSpec` 声明 name/label/handler/target/verbs），`RulesEngine.evaluate` 用 `get_action()` 分发。真正的缺口是：

1. 目录只存在于代码里，作者看不到有哪些动词、哪些需要目标；
2. 想知道「这个行动现在能不能做」只能真的提交一次，会消耗 request_id、改存档。

本轮补上这两件，并把它们做成前端可用的能力。

## 后端

### GET /api/v1/actions

返回目录（`all_actions()` 的 `as_dict()`）：

```json
{"name": "take", "label": "拾取", "target": "required",
 "description": "Pick up an item that lies in the current location.",
 "verbs": ["拾取", "捡起", "拿走", "拿起", "拿到", "拿", "拿取", "take", "pick up", "grab"]}
```

### POST /api/v1/campaigns/{id}/actions/preview

请求体 `ActionPreviewBody`（`campaign_id` 可选、`action` 必填、`target_id`、`params`、`actor_id`）——**刻意不接受客户端 request_id**：干运行从不记录，所以不可能与真实回合冲突。

响应：

```json
{"campaign_id": "camp-1", "state_version": 3, "action": "open", "label": "打开",
 "target_id": "warehouse_door", "ok": false, "code": "door_locked",
 "message": "The 仓库门 is locked. You need the 铁钥匙.",
 "effects": [], "changed": false}
```

实现要点：

- 直接调用 `RulesEngine().evaluate(world, state, action, target_id, params)`，与真实回合同一份规则；
- **只读**：不开事务、不写 action_requests、不改状态（测试断言 `version` 与 `doors` 不变）；
- 返回 `state_version`，调用者可以判断「我预演的那个状态是否仍然是最新的」；
- campaign 未绑定世界 → 404；路径与 body 的 campaign_id 不一致 → 400。

## 前端

- `api.js` 新增 `fetchActionCatalogue()`、`previewAction(campaignId, {action, targetId, params})`。
- 新增 `ActionPreviewPanel.jsx`：行动下拉（读目录）、目标 id 输入、预演按钮；结果显示允许/拒绝、code、文案、将产生的效果列表、当前状态版本；说明「不消耗 request_id，也不改变存档」。
- `GameCanvas` 头部在规则引擎接管时显示「预演」入口。

## 测试

`backend/tests/api/test_routes_action_preview.py`（5 条）：

- 目录包含 take/custom，`take.target == "required"`，`look.target == "none"`；
- 无钥匙开门预演返回 `ok=False, code="door_locked"`，且之后状态仍 `version 0`、门仍关；
- 允许的行动列出 `give_item` 效果，且之后库存仍为空；
- campaign_id 不一致 → 400；未绑定世界 → 404。

全量后端：**1278 passed, 8 skipped**。前端构建：`✓ built in 1.93s`（GameCanvas 84.03 kB / gzip 22.32）。

## 已知限制

- 预演只跑规则，不跑效果/事件/任务推进，所以「将产生的效果」是规则层给出的第一层效果，事件二次触发的结果不会预先列出（事件有 `/events/{id}/simulate`）。
- 预演不检查叙事（叙事永远在提交之后/之前但无写权限）。
