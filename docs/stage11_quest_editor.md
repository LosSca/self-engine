# Stage 11 — 任务编辑与手动推进

## 这一阶段解决什么

任务是可配置、可持久化的数据，而不是模型临时编出来的文本。本阶段把任务从“能存进世界文档”推进到“能在编辑器里完整增删改，并且能在调试面板里看到并手动推进”：

- 世界文档里的任务定义通过 REST 增删改查（含目标、奖励两个子资源）。
- 进度永远从权威状态读取，不从叙事或请求日志反推。
- 手动推进是一次真实提交（新版本号 + 领域事件 + 幂等请求行），不是绕过写入路径的内存修改。

## 后端

### 任务 REST（`backend/app/api/routes_engine.py`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/worlds/{world_id}/quests` | 列出任务 |
| POST | `/api/v1/worlds/{world_id}/quests` | 新增任务（201，返回整个世界） |
| GET | `/api/v1/worlds/{world_id}/quests/{quest_id}` | 单个任务（未知 404 `unknown quest 'x'`） |
| PATCH | `/api/v1/worlds/{world_id}/quests/{quest_id}` | 整体替换任务（路径 id 权威，body id 不一致 400） |
| PUT/DELETE | `.../quests/{quest_id}/objectives/{objective_id}` | 增改/删除一个目标 |
| PUT/DELETE | `.../quests/{quest_id}/rewards/{reward_id}` | 增改/删除一个奖励 |
| POST | `/api/v1/campaigns/{campaign_id}/quests/{quest_id}/objectives/{objective_id}/progress` | 手动推进一个目标 |

所有写操作都经由 `backend/app/services/world_editor.py` 的 `upsert_objective` / `delete_objective` / `upsert_reward` / `delete_reward`，它们在返回前对整份世界做结构与引用校验：目标指向不存在的物品/门/地点、奖励未知，都会被拒绝并且不落盘。

奖励没有 id 字段，所以路径里的 `reward_id` 就是效果的 `kind`（例如 `add_xp`）；同一个任务里重复的同 kind 奖励不会通过这个接口加进去。

### 手动推进（`ActionService.record_quest_progress`）

`POST .../objectives/{objective_id}/progress` body：`{"amount": 1, "request_id": ""}`。

它不做规则判定（这是作者在操作，不是玩家在行动），但保留了所有让一次写入可信的机制：

1. 目标进度不是由事实（fact）推进的，而是由 `quest_progress` 效果推进的——这和配置事件推进进度走的是同一条注册表路径（`backend/app/engines/event_engine.py:358`）。所以工具复用了这个效果，只把效果产生的 fact 交给事件/任务流水线。
2. fact 之后照常走 `_settle_facts`：事件照常触发，任务照常判定完成，奖励照常通过 `complete_quest` 发放。
3. 提交是**一个事务**，带 `expected_version` 的比较写入；失败回滚，请求行不残留。
4. 同一个 `request_id` 重放不会二次推进；不传 `request_id` 时服务端生成一个 `author-progress-*` 随机 id（这种调用本身不幂等，调试用）。
5. 领域事件：`debug.asserted` 一条说明是谁推进了什么，外加常规的 `event.*` / `quest.progressed` / `quest.completed`。
6. 目标不存在时返回 `rejected`（`effect_failed`），世界状态版本与 turn 都不变。
7. 任务不是 `active` 时返回 409，说明原因（`quest 'x' is completed; only an active quest can advance`）。

**开关**：和回滚、修复等会改状态的调试工具一样，受 `_require_debug_tools()` 保护——`ENGINE_DEBUG_TOOLS` 显式设置时以其为准，未设置时跟随 `settings.debug`。生产安装下返回 403，不会开放给任何能连到端口的人。

### 读侧

`GET /api/v1/campaigns/{campaign_id}/quest-progress` 保持不变：任务定义 + 本战役已提交的进度（`status`、每个目标的 `count/required_count/done`、`active/completed/failed` 列表、当前 `version`）。

## 前端

- `frontend/src/components/WorldStudio.jsx`：任务编辑器（`kind === 'quest'`）支持 id、标题、描述、启用、自动接受、前置任务、接受/失败条件、目标行、奖励行。
- 调试面板「任务进度」卡片读取 `fetchQuestProgress`，每个未完成目标旁有「手动推进」按钮（只在任务 `active` 时出现），调用 `progressQuestObjective` 后重新拉取进度并刷新面板。
- `frontend/src/api.js` 新增 `progressQuestObjective(campaignId, questId, objectiveId, amount, requestId)`。

## 测试

- `backend/tests/api/test_routes_quests.py`（8 项）：任务 REST 生命周期、可读的拒绝、进度读自状态、手动推进提交、幂等、未知 id、完成发奖励一次、生产环境 403。
- `backend/tests/services/test_action_service.py` 新增 5 项：作者事实进入任务/事件/历史、幂等、未知目标不写盘、非法数量、`reach` 事实推进 `reach_location` 目标。

## 未做

- 还没有“任务面板”式的玩家界面（只在调试面板里显示）。
- 手动推进不产生叙事（调试动作不调模型，省时省钱）。
- 前端仍只通过 esbuild 语法检查过，本沙箱无法运行 `vite build`。

## 本地验证

```bash
cd backend
ENGINE_DEBUG_TOOLS=1 .venv/Scripts/python.exe -m pytest tests/api/test_routes_quests.py tests/services/test_action_service.py -q
```

手动验证：在 World Studio 里给世界加一个任务，绑定战役后到调试标签页，点目标右侧的「手动推进」，观察版本号 +1、目标 `count` 增加、事件历史里出现 `debug.asserted` 与 `quest.progressed`。
