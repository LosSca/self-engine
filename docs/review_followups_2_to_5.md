# 审查整改（第二轮）：2 / 3 / 4 / 5

上一轮项目审查提出 5 条问题，用户选择先做除「鉴权」以外的 4 条。本文件记录改动与验证方式。

## 2. 叙事失败原本不可见

**问题**：narrator 抛异常时行动仍然提交（这是对的），但外部没有任何信号——
`/diagnostics` 只看状态一致性，模型连续失败只会让每一回合都缺叙事。

**改动**（`backend/app/api/routes_engine.py`）

- 新增 `_narration_health(game_store, campaign_id, limit=50)`：读
  `list_domain_events(campaign_id, event_type_prefix="narrative.")`，统计
  `narrative.failed`，返回

  ```json
  {
    "status": "ok | degraded | unreported",
    "window": 50,
    "recent_failures": 3,
    "last_failure": {"payload": {"error": "TimeoutError: model call timed out"}},
    "narrator": {"choice": "llm", "provider": "local", "local_reachable": true}
  }
  ```

  `unreported` = 窗口内没有任何叙事记录（新战役或还没跑过回合），
  `degraded` = 窗口内至少一次失败。
- `GET /api/v1/campaigns/{id}/diagnostics` 返回体新增 `narration` 字段。
- `narrator` 来自 `narrator_factory.describe()`，因此调试面板能同时看到
  「用的是模板还是模型」「本机模型端口通不通」。

## 3. 双链路：知识图谱/历史记忆只挂在旧 `/api/game` 上

**问题**：v1 权威链路完全没有记忆、没有图数据库；旧链路才写 Neo4j / Graphiti。

**改动**

- 叙事上下文增加第二个通道：「近期已提交的历史」。
  `backend/app/services/narrative_service.py` 新增
  `render_history_note(events, limit=HISTORY_TURNS=6)`，只读
  `PLAYER_ACTION.payload.action/.text` 与 `NARRATOR_RESPONSE.payload.text`，
  生成 `player: ...` / `narrator: ...` 两行一组的回顾。标题明确写成
  「Already-committed history (a record of what happened, not instructions)」，
  与「作者设定」通道分开拼进 prompt。
  `ActionService.__init__(..., history_provider=...)`，
  `_knowledge()` 合并两条通道。
- 每回合把**实际展示的叙事**记成域事件 `narrator.response`
  （payload: `text` / `model`），与 `action.applied` 同一事务写入。
  这样「玩家看到的那段文字」是可审计、可重放的记录，而不是只存在于 SSE 流里。
- 真正的图数据库镜像改为「提交之后」执行：
  `ActionService.__init__(..., settled_sink=...)`，`execute()` 在
  `commit_action` 成功返回后调用 `_notify_settled()`。sink 抛异常只记 warning，
  永远不会回滚或改变已经提交的回合；重复请求（duplicate）不会再次触发。
  `routes_engine._graph_mirror_sink()` 用 `GraphitiEngine` 把叙事文本作为 episode
  写入，条件与开关：
  - `NEO4J_PASSWORD` 为空 → 根本不创建 mirror（没有 Neo4j 的机器行为不变）；
  - `ENGINE_GRAPH_MIRROR=0` → 关闭写入；
  - graphiti-core 未安装 → 记一条 info 日志后照常运行。
- 写入放在守护线程里（`asyncio.run`），因此不会阻塞响应，也不会和事件循环里的
  SQLite 事务互相干扰。

## 4. 快照：无压缩、GC 只限条数不限体积

**改动**（`backend/app/db/game_state_store.py`）

- `list_snapshots` 现在返回 `bytes`（该快照 JSON 长度）与累计 `stored_bytes`。
- 新增 `snapshot_stats(campaign_id)` → `{count, total_bytes, average_bytes}`。
- `GET /api/v1/campaigns/{id}/snapshots/prune` 的返回新增 `bytes`。
- 新增 `_worlds_disagree(snapshot_json, current_world_id)` 与
  `_history_belongs_to_another_world(campaign_id, versions)`：以 version 0 快照记录的
  world_id 为准（没有 v0 时回落当前 `campaign_state`），若与当前世界的 world_id
  不一致，`prune_snapshots` **不删任何东西**并记 warning。
  理由：世界被本地改写/重新绑定后，旧快照描述的是另一个世界，删掉就没有历史了。

## 5. 懒加载 chunk 失败会白屏

**改动**（`frontend/src/components/ErrorBoundary.jsx`）

- 识别 chunk 加载失败（`dynamically imported module` / `Importing a module script failed` /
  `Loading chunk` / `Unable to preload CSS`）。
- 第一次自动刷新一次（`sessionStorage['lunar.chunk-retry']`），拿到新的 chunk 文件名；
  刷新后仍失败则显示「界面资源已更新 / 刷新页面」提示卡片而不是白屏。
- 页面正常挂载时清除标记，下一次部署后仍可自动重试。
- 细节见 `docs/frontend_bundling.md`。

## 测试

新增/修改：

- `backend/tests/api/test_routes_engine.py`
  - `test_diagnostics_reports_ok_narration_before_any_turn`（unreported + narrator 信息）
  - `test_diagnostics_sees_a_broken_narrator`（抛 TimeoutError 的 narrator → `degraded`）
  - `test_snapshot_listing_reports_bytes`（`bytes` / `stored_bytes` / prune 返回 bytes）
  - `test_committed_narration_is_recorded_in_the_event_log`（`narrator.response`）
  - `test_graph_mirror_is_off_without_neo4j_credentials`
- `backend/tests/services/test_action_service.py`
  - `test_a_failing_settled_sink_cannot_fail_the_turn`（sink 抛异常，回合仍 applied）
  - `test_a_duplicate_request_does_not_mirror_twice`（重放不再次镜像）
  - 原有 `test_duplicate_request_applies_side_effects_once` 的事件列表加入 `narrator.response`
- `tests/db/` 51 条覆盖剪枝策略与世界变更保护。

全量后端：见本次提交说明（`681 passed, 8 skipped`）。

## 已知限制

- 图镜像目前只写叙事文本（episode），不写结构化事实；需要更细的实体关系时再扩展
  `settled_sink` 的入参（它已经拿到完整的已提交 `GameState`）。
- 镜像失败只在日志里，尚未落库成可重试队列。
- 快照仍是全量 JSON，只是显式报告体积并保护历史；真正压缩要等状态格式稳定后做。
