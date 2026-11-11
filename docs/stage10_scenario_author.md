# 阶段 10：一句话生成剧本

## 为什么这样切

一条提示词 → 一份可玩的剧本。一次模型调用返回三件东西：

| 件 | 落到哪 | 说明 |
| --- | --- | --- |
| world | game_state 的 world 文档 | 地点、物品、门、实体、任务、事件、自定义行动 |
| knowledge | scenario 的 knowledge 条目 | 世界/地点/人物分层知识 |
| storyline | scenario_play_config.storyline_json | 主线、分线、节点与前置 |

三件各自独立校验、独立保存。**一件不合法不会连带丢掉另外两件**：有四个好地点加一条坏知识，比什么都没有强。被丢的部分进 `warnings`，界面上原样列出来。只有当三件全抽不出来时才整体失败（`GenerationError` → 502）。

脚本从不写权威游戏状态：不碰库存、不碰门、不推进任务。它只是在场景名下写好配置，玩家进入后的每一步照旧走行动管线。

## 新增文件

- `backend/app/services/scenario_author.py`
- `backend/tests/services/test_scenario_author.py`（10 项）
- `backend/tests/api/test_routes_scenario_author.py`（3 项）
- `backend/tests/api/test_routes_scenario_author_saved.py`（2 项）

## 服务层

`build_prompt(prompt, language, location_count=6)` 在提示里写死六条硬性要求，其中三条是可验证的：

- 必须有一处需要钥匙的门：`locked=true`，`key_item_id` 指向本世界已定义物品；
- 主线首节点没有前置；
- 只写数据不写代码。

`_clean_world` / `_clean_knowledge` / `_clean_storyline` 分别复用了既有的 `WorldDefinition` + `validate_references()`、`KnowledgeEntryCreate`、`Storyline` + `validate_storyline()`。校验失败一律转成中文可读句子，不往上层抛异常。

`author_scenario` 用 `asyncio.wait_for` 包一层 120 秒上限，超时给的是「生成超时，请稍后重试或换用更快的模型」。

`apply_authored_scenario` 的剧情线必须走 `get_play_config` → 改 `.storyline` → `set_play_config` 写回：storyline 存在 `scenario_play_config.storyline_json` 里，直接覆盖整份 play config 会把 mode 与 model preset 一起重置掉。

## 接口

`POST /api/v1/scenarios/author`

请求：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| prompt | 必填，1–4000 | 一句话描述 |
| scenario_id | "" | 给了就必须存在（否则 404）；不给且 save=true 时新建场景 |
| title | "" | 空则取 prompt 前 40 字 |
| preset_id | "" | 模型预设，未知预设 400 |
| language | "中文" | |
| location_count | 6（3–12） | 建议给模型的地点数量 |
| save | true | **false 时只校验不落盘**，返回 status="draft" |

返回：

```json
{
  "status": "saved",
  "scenario_id": "…",
  "scenario_title": "雾港",
  "world_id": "ai_1a2b3c4d",
  "world_saved": true,
  "storyline_saved": false,
  "knowledge_ids": [],
  "warnings": ["第 2 条知识未通过校验：…"],
  "counts": {"locations": 6, "items": 3, "entities": 2, "events": 1, "knowledge": 5, "story_nodes": 4}
}
```

状态码：`503` 未配置模型，`502` 模型失败或整体无效，`404` 未知场景，`400` 未知模型预设。

## 前端

`WorldStudio` 新增标签页「剧本生成」：提示词框、`只试运行，不保存` 开关、按钮（试运行 / 生成并保存）、以及结果卡片（counts 徽章、被拒绝的部分逐条列出、保存后可一键打开该世界）。没选场景时按钮禁用并提示先选场景。

`frontend/src/api.js` 新增 `authorScenario({ prompt, scenarioId, title, presetId, language, locationCount, save })`。

## 测试

```
pytest tests/services/test_scenario_author.py tests/api/test_routes_scenario_author.py \
       tests/api/test_routes_scenario_author_saved.py
```

- 服务的 10 项覆盖提示词要求、三件套各自的接受/拒绝路径、部分失败与整体失败。
- 接口的 5 项覆盖：无模型 503；dry run 不建场景行且传入 scenario_id='draft'；全无效 502；**保存路径用真实 GameStateStore 读回 ai_ 世界并核对 scenario_id**；未知场景 404。
- 全量后端：1421 passed, 8 skipped。

## 未做

- 未做真正的联网检索：「一句话」只靠提示词，不查外部资料。理由见 `docs/stage9_world_crossover.md`：本环境外网受限，且模型对具体作品的记忆不稳定；策展数据可测。
- 生成结果不能直接一键开新战役 —— 先保存剧本，再走已有的创建战役流程。
- 前端在本沙箱无法执行 `vite build`（`Cannot find module E:\shujia\.dsh-tmp\config-module.mjs`），只做了 esbuild 语法检查。

## 本地验证

```
# 后端
cd backend && .venv/Scripts/python -m pytest -q
# 前端
cd frontend && npm run dev
```

在「世界工坊 → 剧本生成」里写一句话，先点「试运行」看 counts 与警告；再选好场景、去掉试运行开关、点「生成并保存」；然后到「世界地图」页确认新世界的连通性，进入战役走一遍原有的行动管线。
