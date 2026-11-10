# 自指引擎 Self-Reference Engine

<p align="center">
  <strong>规则决定事实 · 事件记录变化 · 状态保存结果 · 模型只负责叙事</strong>
</p>

<p align="center">
  版本 0.5.0 &middot; MIT License &middot;
  <a href="#快速开始">快速开始</a> &middot;
  <a href="#核心特性">核心特性</a> &middot;
  <a href="#架构">架构</a> &middot;
  <a href="#api-概览">API</a> &middot;
  <a href="#已知限制">已知限制</a>
</p>

> 图片占位：早期 README 使用的 `docs/banner.png` 仍是上游 Project Lunar 的横幅，
> 与现在的项目不符，因此这里改用文字标题。换成新图后，把本段替换为
> `<p align="center"><img src="docs/banner.png" alt="Self-Reference Engine" width="100%" /></p>` 即可。

---

## 这是什么

自指引擎是一个**本地优先**（local-first）的文字冒险与叙事 RPG **引擎**，不是一个具体的游戏。
作者用结构化的世界文档（地点、物品、门、实体、事件、任务、剧情线、知识）写出一个世界，
玩家用自然语言或界面按钮行动，引擎给出确定的结果，语言模型只负责把已经结算的事实写成散文。

它解决的是 AI 文字冒险最常见的失败模式：**模型被当成裁判**。在这里，模型永远是叙述者——
它拿不到写状态的权限，叙事失败也不会让回合回滚。

**自指**指的是引擎的建模方式：世界规则以数据形式存在（条件、效果、事件、任务都是 JSON，
没有脚本、没有 `eval`），而引擎自身的判定过程同样被记录为可回放的事件流；同一回合内
事件、任务奖励与剧情推进按固定点收敛，"引擎如何得出结论"和"世界里发生了什么"是同一套记录。

- 当前版本：**0.5.0**（`backend/app/main.py` 与 `frontend/package.json` 一致）
- 运行时：Python + FastAPI 后端、React + Vite 前端、SQLite 存储
- 语言：界面与内容均为中文优先；叙事语言跟随场景配置
- 范围：以规则链路 `/api/v1` 为准；`/api/game` 旧聊天链路仍在维护，用于未绑定世界的冒险

### 代码地图

| 位置 | 内容 |
|------|------|
| `backend/app/engines/` | 规则与引擎层：行动目录、规则判定、事件与效果、任务、剧情、战斗、记忆、NPC 心智 |
| `backend/app/services/` | 编排层：`action_service` 是唯一写入口，另有文本解析、知识、叙事、世界编辑、AI 生成 |
| `backend/app/api/` | 五个路由模块（规则 / 知识 / 旧链路 / 情景 / 角色） |
| `backend/app/db/` | 四个 SQLite 存储，各自的 `SCHEMA_VERSION` 与迁移 |
| `frontend/src/components/` | 游戏画布、各工坊、面板与浮层 |
| `docs/` | 阶段设计、审阅矩阵与风险登记 |

---

## 架构

替换了旧的 `docs/architecture.png`（那张图描述的是单一聊天链路），下面是当前实现的文字框图：

```
浏览器 (React 19 + Vite)
  ├─ GameCanvas ────── 剧情流 / 文字输入 / 选项按钮 / 地图点击 / 背包
  ├─ WorldStudio ───── 地点·物品·门·实体·事件·任务·自定义行动·地图·调试
  ├─ KnowledgeStudio ─ 知识·上下文模式·剧情线·模型预设
  └─ StorylineStudio / ScenarioBuilder / SetupWizard / SettingsPanel ...
        │  REST (axios)
        ▼
FastAPI (backend/app/main.py)
  ├─ /api/v1   …… 规则链路（确定性）      routes_engine.py
  ├─ /api/v1   …… 知识·预设·AI 生成       routes_knowledge.py
  ├─ /api/game …… 旧聊天链路（流式 SSE）  routes_game.py
  ├─ /api/scenarios …… 情景与战役         routes_scenarios.py
  └─ /api       …… 角色卡与外观           routes_characters.py
        │
        ▼
ActionService.execute            唯一的状态写入口 (services/action_service.py)
  1 载荷校验 + request_id 幂等
  2 RulesEngine.evaluate         纯函数判定，无模型参与
  3 应用效果 → 事件固定点 → 任务 → 剧情线
  4 知识上下文（分层/剧情提升/聚焦/预算/NPC 可见性）
  5 叙事                        模型只描述已结算结果；失败只记录
  6 单事务提交 state + request + domain events（版本 CAS）
        │
        ▼
SQLite（路径均可用环境变量覆盖）
  scenarios.db   情景·故事卡·知识·剧情线·模型预设·角色   SCENARIO_DB_PATH
  game_state.db  世界·权威状态·请求记录·领域事件·快照     GAME_STATE_DB_PATH
  events.db      旧链路事件流·记忆晶体                    EVENT_DB_PATH
  traces.db      LLM 调用追踪                             LLM_TRACE_DB_PATH
        │
        └─(可选) Neo4j 知识图谱镜像 —— ENGINE_GRAPH_MIRROR=1 且配置密码时启用
```

**关键不变量**（见 `docs/review-overview.md`）：

- 模型不写状态；叙事失败不回滚回合。
- 条件、效果、后续事件都是校验过的数据，没有脚本或 `eval`。
- 事件、任务奖励、剧情推进在同一回合内按固定点收敛，同一事件每回合最多尝试一次。
- 地图点击、结构化按钮与自然语言共用同一个 `ActionRequest` 管线。

---

## 核心特性

### 确定性规则链路

内置行动是声明式目录（`backend/app/engines/action_registry.py` 的 `ActionSpec`），
每个行动声明 name / label / handler / target / verbs，`RulesEngine` 按动词表分发。

当前 **16 个内置行动 + 1 个世界声明的自定义行动**（`custom` 为文本入口，具体动词由世界文档声明）：
`attack` 攻击、`flee` 逃跑、`defend` 防御、`take` 拾取、`open` 打开、`use` 使用、
`talk` 交谈、`move` 移动、`investigate` 调查、`look` 观察、`equip` 装备、
`unequip` 卸下、`buy` 购买、`sell` 出售、`choose` 选择，加上 `custom`。
行动目录可经 `GET /api/v1/action-types` 读取，供前端渲染按钮与提示。

```bash
# 看当前构建支持的全部行动（含中文动词）
curl http://127.0.0.1:8000/api/v1/action-types
```

### 效果注册表：从分支链变查表

`backend/app/engines/event_engine.py` 的 `apply_effect()` 曾是一条 400 多行的
`if kind == ...` 分支链。现在是 13 行查表：

```python
kind = effect.kind
handler = EFFECT_HANDLERS.get(kind)
if handler is None:
    return AppliedEffect(effect, False, f"unsupported effect kind '{kind}'")
return handler(kind, state, world, effect)
```

每种效果都是独立函数，用 `@register_effect("give_item")` 登记；重复登记直接抛
`ValueError`，未知种类**失败关闭**并给出可读原因。当前 28 个效果种类对应 25 个处理函数
（货币、关系/警戒/技能三类各自合并），见 `docs/stage6_effect_registry.md`。

### 事件、任务与剧情线

- 事件支持触发条件、优先级、`once`/`always`、成功后续、失败后续、冷却、次数上限、
  条件分支（`branches[]`，无条分支兜底且必须在最后）与同回合级联。
- 事件可挂起玩家选项（`offer_choice` / `resolve_choice`），`pending_choice` 写入权威状态，
  `choose` 行动原子应用选项效果并清除挂起状态。
- 任务有前置任务、接受条件、失败条件与奖励；奖励与状态在同一事务内提交。
- 剧情线分主线/支线/独立线，可分组启停，节点带知识。

### 知识与上下文

`backend/app/services/knowledge_service.py` 负责把"作者设定"喂给叙事层：

- 分层、深度、上下文模式、剧情提升、聚焦标签、字符预算共同决定注入哪些条目。
- 排序使用本地 **TF-IDF 余弦相似度**（英文词 + 中文字/二元组），不依赖 embedding 服务。
- 知识条目可设 `audience_npc_ids`：只有当前在场且命中受众的 NPC 才会看到该条目。
- 作者设定（canon）与已提交历史（history）走两条独立通道，防止模型把过去的叙事当成指令。

### 记忆与历史

- **4 层晶体金字塔**（`backend/app/engines/memory_engine.py`）：
  SHORT（约 4 个行动）→ MEDIUM（4 个 SHORT）→ LONG（4 个 MEDIUM）→ MEMORY（4 个 LONG，永久世界事实）。
- 晶体是结构化 JSON，严格保留专有名词与物理描述；NPC 专属事实带见证过滤。
- 规则链路每回合追加**有界滚动摘要**；配置 summary 预设后可进一步用模型压缩。

### NPC、战斗与经济

- NPC 心智记录感受、目标、对玩家看法与秘密计划（`npc_mind_engine.py`）；
  实体拥有阵营、关系、警戒状态；对话按作者台词游标顺序推进。
- 战斗采用**创造性评分**：`final_quality = coherence × 0.4 + creativity × 0.4 + context × 0.2`，
  结果分 `CRIT_SUCCESS` / `SUCCESS` / `FAIL` / `CRIT_FAIL`，并有反"元游戏"拦截
  （`combat_engine.py`）。战斗完全由规则打分决定，模型只描述结果。
- 物品支持拾取条件、使用效果、装备槽与攻击/防御加成、价格；商人实体可配置商品，
  `buy`/`sell` 原子结算货币。

### 世界与地图编辑

- `WorldStudio` 可编辑地点（区域 + 网格坐标）、物品、门（锁与钥匙）、实体、事件、任务、
  自定义行动，并提供事件校验与模拟、执行树、状态诊断、快照回滚与存档导入导出。
- 地图是地理视觉画布：地形分区、道路、地点标记、门锁标记，支持缩放/拖拽与坐标写回；
  `move` 可接受经 BFS 校验的路线逐段移动。
- 内置世界模板（`frontend/src/data/worldTemplates.js`）：**轨道站事故**、**雾镇档案**、
  **荒野营地**；后端另有 `services/starter_worlds.py` 提供奇幻村庄预设。

### AI 生成（需要已配置模型）

`backend/app/services/ai_generator.py` 提供三处一键生成：

| 位置 | 输入 | 输出 | 接口 |
|------|------|------|------|
| 知识库 | 提示词 | 结构化知识草稿 → 预览 → 全部写入 | `POST /api/v1/scenarios/{id}/generate/knowledge` |
| 故事线 | 提示词 | 完整 Storyline 草稿（含校验提示） | `POST /api/v1/scenarios/{id}/generate/storyline` |
| 世界工坊 | 提示词 | 校验引用后直接保存为新世界 | `POST /api/v1/worlds/generate` |

生成接口统一走已配置模型或指定预设；未配置模型返回 **503**，非法 JSON/引用返回 **502**。

### 模型路由

`backend/app/engines/llm_router.py` 支持多个供应商，可在设置面板或模型预设中切换：

- `deepseek` / `anthropic` / `openai`（云端，需对应 API key）
- `local`（本地 OpenAI 兼容服务，即 NInfer；无需云端密钥即可叙事，见 `docs/local_model_ninfer.md`）
- 可选经 CLIProxyAPI 走订阅代理（`proxy/README.md`）

预设为 SillyTavern 风格子集：`top_p` / `top_k` / `min_p`、重复/存在/频率惩罚、`seed`、
停止词、上下文窗口、预设系统提示；叙事/intent/dialogue/summary 四个角色可按场景覆盖。


## 快速开始

### 环境要求

- **Python 3.10+**、**Node.js 18+**
- 可选：**Docker**（只为 Neo4j 知识图谱镜像准备，没有也能完整游玩）
- 可选：一个模型 API key，或本机跑一个 OpenAI 兼容推理服务

### 安装

```bash
git clone <本仓库地址>
cd shujia

install.bat      # Windows
./install.sh     # macOS / Linux
```

脚本会建好后端虚拟环境、安装 `backend/requirements.txt` 与前端依赖，
没有 `.env` 时从 `.env.example` 复制一份。

> **Docker / Neo4j 可选**：脚本仍会尝试 `docker-compose up -d neo4j`。
> 不装 Docker 时忽略这一步的失败，并在 `.env` 写 `ENGINE_GRAPH_MIRROR=0`——
> 图谱只是镜像，不会因为图谱不可用而回滚回合。

### 配置

编辑仓库根的 `.env`（字段与 `.env.example` 一一对应）：

```env
# 至少配一个：云端密钥，或下面的本地服务
DEEPSEEK_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

# 本地 OpenAI 兼容推理服务（NInfer 等）
LOCAL_LLM_BASE_URL=http://127.0.0.1:8094/v1
LOCAL_LLM_MODEL=qwen3.8-27b
LOCAL_LLM_CONTEXT_WINDOW=49152

# 叙事：auto = 配了模型就用模型，否则退回模板文本；template = 始终确定性文本
ENGINE_NARRATOR=auto
ENGINE_LLM_PROVIDER=

# 图谱镜像；不接 Neo4j 时设为 0
ENGINE_GRAPH_MIRROR=1
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=

# 可选：设置后 /api 全部需要 Bearer 令牌（或 X-API-Token），健康检查除外
# API_TOKEN=
```

**没有任何密钥也能玩**：`ENGINE_NARRATOR=template` 时由 `TemplateNarrator`
根据已结算的状态与效果生成确定性文本，不联网、零成本。

### 运行

```bash
start.bat           # Windows
./scripts/start.sh  # macOS / Linux
```

两端脚本都只是调用 `launch.py` 的薄封装，所以 `python launch.py` 等价。

只需这一条命令：`launch.py` 探测解释器 → 起后端（`127.0.0.1:8000`）→
起前端（`127.0.0.1:5173`）→ 等健康检查 → 打开浏览器。打开
**http://127.0.0.1:5173** 即可开始，API 文档在 <http://127.0.0.1:8000/docs>。
关掉启动器窗口即停止两端，不需要自己开两个终端跑 `uvicorn` 和 `npm run dev`。

常用参数（传给 `launch.py`）：`--debug`（后端 reload + 调用转储）、
`--backend-only` / `--frontend-only`、`--no-browser`、
`--no-window`（不开新窗口，日志写 `.logs/`，自检/CI 用）。

---

## 界面与工作流

| 路由 | 界面 | 用途 |
|------|------|------|
| `/` | 首页 | 情景卡片、战役列表、进入各个工坊 |
| `/create` | `ScenarioBuilder` | 新建情景（世界类型、语气、设定问答、开场方式） |
| `/play` | `GameCanvas` | 游玩：剧情流、行动输入、战斗浮层、地图、背包、日志 |

游玩界面的主要面板：`ActionInput`（文字/按钮行动与 `@` 提及）、`SuggestionList`、
`ActionPreviewPanel`（干运行预演）、`CombatOverlay`、`WorldMapModal` / `WorldMapGraph`、
`InventoryPanel`、`JournalPanel`、`NpcInspector`、`MemoryInspector`、`CharacterProfilePanel`、
`StoryProgressPanel`、`PlotGeneratorPanel`、`TimeskipModal`、`SettingsPanel`、`DevtoolsPanel`。

典型流程：新建情景 → 设定问答（SetupWizard）→ 绑定世界 → 行动（文字或按钮）→ 看结算与叙事 → 调试面板核对状态、事件执行树与快照。
前端状态用 Zustand（`frontend/src/store.js`），接口在 `frontend/src/api.js`；页面按路由懒加载。

---

## 目录结构

```
shujia/
├── launch.py                    # 通用启动器（前后端编排、健康检查、浏览器）
├── start.bat                    # Windows 入口（纯 ASCII，调用 launch.py）
├── scripts/start.sh             # macOS / Linux 启动入口（调用 launch.py）
├── scripts/start-dev.ps1        # PowerShell 开发启动脚本
├── install.bat / install.sh     # 一键安装（含可选 Neo4j 容器）
├── docker-compose.yml           # Neo4j 5 容器（可选）
├── .env.example                 # 环境变量样例，安装脚本会复制为 .env
├── README.md / CHANGELOG.md
├── docs/
│   ├── review-overview.md       # 运行时结构、13 项验收矩阵、风险登记
│   ├── stage6_effect_registry.md # 效果注册表与相关测试记录
│   ├── local_model_ninfer.md    # 本地模型接入
│   ├── postgres-migration.md    # SQLite → PostgreSQL 一次性导出
│   └── ...                      # 各阶段设计与审计记录
├── backend/
│   ├── requirements.txt / pytest.ini
│   ├── app/
│   │   ├── main.py              # FastAPI 入口、中间件、/api/settings
│   │   ├── config.py            # pydantic-settings，读取 .env
│   │   ├── api/                 # routes_engine / routes_knowledge / routes_game
│   │   │                        # routes_scenarios / routes_characters
│   │   ├── db/                  # scenario_store / game_state_store / event_store / trace_store
│   │   ├── engines/             # 规则与引擎层
│   │   ├── models/              # game.py / knowledge.py（世界、状态、条件）
│   │   ├── services/            # 编排与服务层
│   │   └── utils/
│   ├── scripts/sqlite_to_postgres.py
│   └── tests/                   # pytest，asyncio_mode = auto
├── frontend/
│   ├── package.json             # version 0.5.0
│   ├── scripts/check-hook-order.mjs
│   └── src/
│       ├── App.jsx / api.js / store.js
│       ├── components/          # 游戏画布与各个工坊
│       ├── data/worldTemplates.js
│       └── lib/                 # interpolate / theme / scenarioTypes
└── proxy/                       # 可选的 CLIProxyAPI 订阅代理
```

**engines/**：`action_registry`（行动目录）、`rules_engine`（纯函数判定）、
`event_engine`（事件与效果注册表）、`quest_engine`、`story_engine`、`combat_engine`、
`journal_engine`、`narrator_engine`、`memory_engine`、`npc_mind_engine`、`plot_generator`、
`world_reactor`、`inventory_engine`、`item_proposals`、`opening_generator`、`llm_router`、
`graph_engine`、`graphiti_engine`、`auditor_engine`。

**services/**：`action_service`（唯一写入口）、`text_action_service`（自然语言 → `ActionRequest`）、
`knowledge_service`、`narrative_service`、`narrator_factory`、`intent_advisor`、`world_service`、
`world_editor`、`starter_worlds`、`summary_service`、`state_diagnostics`、`ai_generator`、
`card_images`、`scenario_service`、`scenario_interpolation`、`game_session`。

---

## API 概览

全部接口挂在 `/api` 下；设置 `API_TOKEN` 后除 `/api/health` 外都要求
`Authorization: Bearer <token>` 或 `X-API-Token`。五个路由模块共注册 111 个路由，
下面只列主干，完整清单见 <http://127.0.0.1:8000/docs>。

### 规则链路 `/api/v1`（确定性游玩）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/action-types` | 内置行动目录（16 个内置 + 世界声明的 `custom`） |
| GET | `/actions` | 同上，兼容入口 |
| POST | `/campaigns/{id}/world` | 为战役绑定世界 |
| POST | `/campaigns/{id}/actions` | 提交结构化行动（唯一写入口） |
| POST | `/campaigns/{id}/text-actions` | 自然语言行动，解析后走同一管线 |
| POST | `/campaigns/{id}/combat` | 战斗回合 |
| GET | `/campaigns/{id}/state` | 权威状态 |
| GET | `/campaigns/{id}/events` | 领域事件（可回放） |
| GET | `/campaigns/{id}/lane` | 时间线/调试视图 |
| GET | `/campaigns/{id}/diagnostics` | 状态一致性诊断 |
| GET | `/campaigns/{id}/snapshots` | 快照列表；`/{version}` 取单份 |
| GET | `/campaigns/{id}/state-diff` | 状态差异 |
| POST | `/campaigns/{id}/rollback` | 回滚到快照（受 `ENGINE_DEBUG_TOOLS` 限制） |
| POST | `/campaigns/{id}/repair` | 修复（同上） |
| GET/POST | `/campaigns/{id}/export`、`/import` | 存档导出/导入（覆盖需 `reset=true`） |
| GET | `/actions/{request_id}` | 按幂等键查请求记录 |
| GET/POST | `/worlds`、`/worlds/{id}` | 世界读取与写入 |
| POST | `/worlds/validate` | 校验引用与条件/效果 |
| POST | `/worlds/generate` | AI 生成世界 |
| POST | `/worlds/{wid}/events/{eid}/simulate` | 事件模拟 |
| PUT/DELETE | `/worlds/{wid}/{locations,items,doors,entities,events,quests}/{item_id}` | 资源级编辑 |
| GET | `/worlds/{id}/map` | 地图数据 |
| GET | `/local-model` | 本地模型可用性 |

### 知识、预设与生成 `/api/v1`

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/knowledge/layers` | 知识分层 |
| PATCH / DELETE | `/knowledge/{entry_id}` | 编辑/删除知识条目（含 `audience_npc_ids`） |
| GET/PUT | `/scenarios/{id}/play-config` | 游玩配置（模式、预算、预设） |
| POST | `/scenarios/{id}/generate/knowledge` | AI 生成知识草稿 |
| POST | `/scenarios/{id}/generate/storyline` | AI 生成故事线草稿 |
| GET/POST | `/model-presets` | 模型预设列表与新建 |
| PATCH / DELETE | `/model-presets/{preset_id}` | 修改/删除预设 |

### 旧聊天链路 `/api/game`（流式）

绑定世界的战役会转交规则链路；未绑定的冒险仍走这条链路，用于场景卡、记忆晶体与追踪。

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/action` | 流式提交行动（SSE） |
| GET | `/{cid}/history` | 对话历史 |
| GET | `/{cid}/memory-crystals` | 四层记忆晶体 |
| POST | `/{cid}/crystallize` | 手动触发合并 |
| GET/PUT/DELETE | `/{cid}/npc-minds` 与 `/{cid}/npc-minds/{npc_name}` | NPC 心智查看与编辑 |
| GET | `/{cid}/journal` | 日志条目 |
| GET | `/{cid}/inventory`、POST 同路径 | 背包读写 |
| GET | `/{cid}/world-graph`、`/{cid}/graph-search` | 图谱快照与搜索 |
| GET/DELETE | `/{cid}/traces` | LLM 调用追踪 |
| POST | `/{cid}/timeskip`、`/{cid}/rewind`、`/{cid}/regenerate-opening` | 时间跳转、回退、重生成开场 |

### 情景与战役 `/api/scenarios`

| 方法 | 端点 | 说明 |
|------|------|------|
| GET / POST | `/` | 情景列表与新建 |
| GET / DELETE | `/{id}` | 情景详情与删除 |
| POST | `/import` | 从 JSON 导入情景 |
| GET | `/{id}/export` | 导出为 JSON |
| POST / GET | `/{id}/story-cards` | 故事卡（NPC/地点/阵营/物品） |
| POST / GET | `/{id}/campaigns` | 战役新建与列表 |
| DELETE | `/{id}/campaigns/{cid}` | 删除战役 |
| POST | `/preview-opening` | 预演开场白，不落库 |

角色卡与外观在 `/api` 下：`/api/scenarios/{id}/characters`、`/api/characters/{id}`
（含 `/looks` 外观变体与 `/api/characters/upload` 上传）、`/api/campaigns/{cid}/character`。

全局设置：`GET/POST /api/settings`（供应商、模型、temperature、max_tokens）、
`GET /api/health`、`GET /api/health/neo4j`。

---

## 测试与质量

```bash
# 后端全量（pytest，asyncio_mode = auto）
backend/.venv/Scripts/python.exe -m pytest backend/tests -q

# 前端构建（含 hook 顺序检查）
npm --prefix frontend run build
```

- 最近一次全量为 **1304 passed / 8 skipped**（跳过项为未启动 Neo4j 的图谱测试），
  见 `docs/stage6_effect_registry.md`；CHANGELOG 0.5.0 里的 1238 passed 是发版当时的统计口径，两者不矛盾。
- 测试按 `backend/tests/{api,db,engines,models,services,scripts}` 分层，
  包括效果注册表对齐、事件分支、NPC 知识可见性、并发 CAS、幂等重放等。
- 前端构建会先执行 `frontend/scripts/check-hook-order.mjs`，防止 `useCallback` 未声明就进依赖数组；
  页面全部懒加载，发布包由 `vite build` 产出到 `frontend/dist`。
- 数据层：`scenarios.db` / `game_state.db` / `events.db` / `traces.db`，
  `SCHEMA_VERSION` 分别为 12 / 2 / 2 / 1，打开旧库时按版本迁移；路径可用环境变量覆盖。

---

## 已知限制

取自 `docs/review-overview.md` 的 13 项验收矩阵中标记为"部分"的缺口，以及风险登记：

| 系统 | 状态 | 主要缺口 |
|------|------|----------|
| 世界与地图 | 部分 | 无可走格子、碰撞与自由寻路；跨区域需逐段移动 |
| 实体与 NPC | 部分 | NPC 无自主行为；无关系网络界面 |
| 行动与交互 | 部分 | 无内置潜行、键盘方向移动、自由寻路 |
| 物品与属性 | 部分 | 无独立商店/交易面板；装备只加攻防 |
| LLM 调度 | 部分 | 判定由确定性规则承担，没有 LLM judge；超时/重试未按功能独立配置 |
| 记忆与历史 | 部分 | 规则链路没有自动 LLM 摘要（仅确定性滚动摘要）；无统一 NPC 知识库视图 |
| 模型配置 | 部分 | 预设的角色覆盖仍以叙事为主 |

风险登记里的三条 P1：服务默认无鉴权（本机使用，需要共享时设 `API_TOKEN`）、
SQLite 单写者（高并发回以版本冲突重试）、知识为词法相似度而非语义召回。
此外 `/api/game` 旧聊天链路仍保留独立的流式副作用路径，未绑定世界的冒险行为与绑定世界不同。

---

## 来源与致谢

本项目 fork 并演进自 **[horizonfps/project-lunar](https://github.com/horizonfps/project-lunar)**
（Project Lunar）。世界文档模型、四层记忆晶体、NPC 心智与叙事引擎的原始设计来自上游；
本仓库在此基础上把判定与叙事彻底分离，改为规则链路优先的架构，并重写了编辑器与启动流程。
当前远端：<https://github.com/LosSca/self-engine>。

同时致谢：

- **litellm**、**FastAPI**、**React / Vite**、**Zustand** —— 底层框架
- **Graphiti** —— 时序知识图谱概念（可选镜像）
- **CLIProxyAPI** —— Anthropic / OpenAI 兼容订阅代理
- **Inner-Self** —— NPC 内心与人格系统的灵感来源
- **AI Dungeon** —— 交互式 AI 小说与故事卡的先驱

---

## 贡献

欢迎提交 Issue 与 Pull Request。请遵守一条原则：**这是一个引擎，不是一个游戏。**
不要在引擎里硬编码某个世界的专有逻辑（题材关键词、等级体系、具体角色名）；
所有世界相关的信息都应该来自作者定义的世界文档、知识条目与语气指令。

---

## 许可证

MIT