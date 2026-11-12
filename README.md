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
  <a href="#测试与质量">测试</a> &middot;
  <a href="#已知限制">已知限制</a>
</p>

> 这里刻意不使用横幅图片：早期使用的 `docs/banner.png` 是上游 Project Lunar 的旧图，
> 与现在的项目不符，已移除。要加新图时把本段替换为
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
- 语言：界面与内容中文优先；**叙事语言跟随世界文档的 `language` 字段**，未声明时为英文
- 规模：146 个路由，其中 `/api/v1` 引擎链路 92 个，其余为旧聊天链路（27）、情景、角色与全局设置
- 范围：以规则链路 `/api/v1` 为准；`/api/game` 旧聊天链路仍在维护，用于未绑定世界的冒险
- 模型：**不配置任何密钥也能完整游玩**——确定性叙事器按世界语言产出文本

### 一句话能做的事

| 你想要的 | 怎么做 | 需要模型吗 |
|----------|--------|-----------|
| 一句话生成世界、知识、主线 | 世界工坊「剧本生成」页签，或 `POST /api/v1/scenarios/author` | 需要 |
| 借用已有作品的世界与角色 | 世界工坊「作品穿越」页签，六部内置作品，也可让模型补写 | 内置作品不需要 |
| 从零手做一套世界 | 世界工坊十三个页签：地图 / 剧本生成 / 物品 / 门 / 实体 / 事件 / 任务 / 行动 / 调试 / 作品穿越 / 记忆提炼 / 对话补全 / 世界演化 | 不需要 |
| 让玩家一句话被写进正史 | 世界演化页签：断言 → 提案 → 采纳，校验不过的不进世界 | 不需要 |
| 换个模型只换一个角色 | `AGENTS_NARRATIVE_MODEL` 等环境变量，或调试台「模型调用」页签看路由 | 不需要 |
| 换一个起步世界 | `GET /api/v1/world-presets`：奇幻村庄、轨道站、雾镇、荒野营地 | 不需要 |
| 让模型写得更好 | 设置面板选供应商与预设；上游拒绝时可自动改走本地模型 | 需要 |
| 用别家的兼容接口 | 设置面板「自定义兼容接口」，地址与密钥放 `.env` 的 `CUSTOM_LLM_*` | 需要 |

### 代码地图

| 位置 | 内容 |
|------|------|
| `backend/app/engines/` | 规则与引擎层：行动目录、规则判定、事件与效果、任务、剧情、战斗、记忆、NPC 心智 |
| `backend/app/services/` | 编排层：`action_service` 是唯一写入口，另有文本解析、知识、叙事、世界编辑、AI 生成、作品库 |
| `backend/app/api/` | 六个路由模块（引擎 / 知识 / 旧链路 / 情景 / 角色 / 作品） |
| `backend/app/db/` | 四个 SQLite 存储，各自的 `SCHEMA_VERSION` 与迁移 |
| `backend/tests/` | pytest 1640 passed / 8 skipped，验收走查在 `tests/api/test_acceptance_story.py` |
| `frontend/src/components/` | 游戏画布、各工坊、面板与浮层 |
| `docs/` | 阶段设计、审阅矩阵、风险登记与交付审查（`docs/delivery_review.md`） |

---

## 架构

旧的 `docs/architecture.png` 描述的是单一聊天链路，已与实现不符并移除；下面是当前的文字框图：

```
浏览器 (React 19 + Vite)
  ├─ GameCanvas ────── 剧情流 / 文字输入 / 选项按钮 / 地图点击 / 背包
  ├─ WorldStudio ───── 地图·剧本生成·物品·门·实体·事件·任务·行动·调试·作品穿越
  ├─ KnowledgeStudio ─ 知识·上下文模式·剧情线·模型预设
  └─ StorylineStudio / ScenarioBuilder / SetupWizard / SettingsPanel ...
        │  REST (axios)
        ▼
FastAPI (backend/app/main.py)
  ├─ /api/v1   …… 规则链路（确定性）      routes_engine.py
  ├─ /api/v1   …… 知识·预设·AI 生成       routes_knowledge.py
  ├─ /api/v1   …… 作品库·世界穿越·角色挑选 routes_works.py
  ├─ /api/game …… 旧聊天链路（流式 SSE）  routes_game.py
  ├─ /api/scenarios …… 情景与战役         routes_scenarios.py
  └─ /api       …… 角色卡与外观           routes_characters.py
  └─ /api       …… 全局设置·健康检查       main.py
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

**关键不变量**（见 `docs/review-overview.md` 与 `docs/delivery_review.md`）：

- 模型不写状态；叙事失败不回滚回合。
- 条件、效果、后续事件都是校验过的数据，没有脚本或 `eval`。
- 事件、任务奖励、剧情推进在同一回合内按固定点收敛，同一事件每回合最多尝试一次。
- 地图点击、结构化按钮与自然语言共用同一个 `ActionRequest` 管线。
- 同一 `request_id` 重放返回同一份结果，不重复扣物品、不重复发奖励。
- 叙事、图谱镜像、摘要的失败只记录，不撤销已提交的回合。

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

- `WorldStudio` 有十二个页签：地图、剧本生成、物品、门、实体、事件、任务、行动、记忆提炼、对话补全、调试、作品穿越。
  每次保存都跑完整世界校验；不通过的编辑不会落库。调试页提供事件校验与模拟、执行树、
  状态诊断、快照回滚与存档导入导出。
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

### 一句话生成剧本：世界 + 知识 + 主线

`POST /api/v1/scenarios/author`（`backend/app/services/scenario_author.py`）用**一次模型调用**返回三件套，
但**逐件独立校验**：世界过 `validate_references`，知识逐条过 `KnowledgeEntryCreate`，
剧情线过 `validate_storyline`。哪件不合法就进 `warnings` 而不是整单失败——一份能玩的世界
不该因为其中一条知识少写了标题就整个作废；三件全废才返回 502。

`save=false` 是**试运行**：只校验、不落盘，用于在作者确认前预览模型会给出什么。
落盘时世界走 `world_service.save_world`，剧情线必须经 `get_play_config → 改 .storyline → set_play_config`
写回（直接覆盖会重置 mode 与模型预设）。**它从不写权威游戏状态**——只创建配置。

### 作品穿越：借一个世界的骨架

`backend/app/services/work_catalogue.py` 内置六部作品档案（西游记、福尔摩斯、三体、流浪地球、
哈利·波特、指环王），每部带梗概、语气、地点（含地点注释）、开场提示与 2–4 个可选角色。
`POST /api/v1/works/enter` 会按档案生成一个**链式连通**的世界骨架（相邻地点双向出口），
把角色作为起点 NPC 写入，并记到战役的角色选择上。

这是**策展优先**设计而不是联网搜索：档案是确定性的、可测试的，模型只在内置作品未命中时才补写
（`allow_model=false` 时直接 404，不花模型调用）。选角色是「换一个视角看这个世界」，
不是绕过规则——进入后的每一步仍然走同一个规则链路。

### 对话式补全：一句话长出一个地方

`POST /api/v1/worlds/{w}/authoring/propose` 把一句「在广场东边加一条通往河边的小路，并写一条关于这条河的设定」
变成**可审核的编辑提案**：`reply` 一句回复、`ops` 通过校验的操作、`changes` 各地类数量前后对比、
`warnings` 被拒绝的部分及原因。**这一步不写任何东西。**

确认后 `POST .../authoring/commit` 才落盘。服务端不信任客户端回传的提案：世界部分重新按 `WorldDefinition`
解析并再跑一次引用校验，知识逐条重验，不合法返回 400。写入只涉及世界文档与知识库——
**游戏状态、任务进度、存档一律不动**。

每条操作都在世界文档的**副本**上走 `world_editor` 的校验读改写，所以一个会让引用悬空的提案当场被拒，
原因直接来自校验代码而不是模型的自述：

```text
add_door 未通过校验：door 'ghost_door' requires unknown key item 'nope'
```

提示词里附上当前世界已有的地点/物品/门/实体/事件/任务 id 与最近 12 轮对话，让模型复用已有命名而不是另起一套。
世界工坊的「对话补全」页签就是这个界面（共 12 个页签）。当前只支持新增，不支持改字段与删除；
**故事线不在这条通道里**，见 `docs/delivery_review.md`。

### 从叙事里抽记忆：抽取是确定性的，模型只改写

`POST /api/v1/scenarios/{s}/knowledge/extract` 读已提交的叙事回合（`narrator.response` 的 `payload.text`），
把句子分类成人物/事件，并识别 `X 是 Y`、`X 持有 Y` 这类整句事实。**先确定性抽取，模型只做改写**（`refine=true`），
模型挂了就退回原文加一条 warning，不会让整批丢失。

它是**提案通道**：`apply=false` 时完全只读且幂等，`apply=true` 只写 `scenario_knowledge`。
没有任何规则读知识库，所以一条抽错的记忆只会让叙事偏一点，不会污染存档。

### 交互预演：先看这一回合会怎样

`POST /api/v1/worlds/{w}/plan` 列出当前房间每个候选动作与真实判定结果（放行/拒绝及原因）；
`POST .../preview-actions` 预演一条动作链，逐步给出效果、触发事件与状态字段前后值；
`POST .../explain` 回答「为什么不行」，并给出可行路径（例如「先到森林边缘取钥匙，距离 1 步」）。

判定全部来自真实的 `RulesEngine.evaluate` 与 `event_engine`，状态是深拷贝，**没有任何写回**。
预演若自己实现一套规则，迟早会和真实链路漂移；若真写进去，就违反「只有行动服务能改权威状态」。
所以它只读。
### 世界演化：断言变成提案，对话长成世界

玩家或模型说出的「事实」不再直接进正史。`backend/app/services/world_evolution.py` 把一句话变成
一条**提案**（`kind` + `confidence` + `payload` + `evidence`），先做冲突检查（世界里已有同名地点就不再建一个），
再把通过检查的正史类提案丢给 `authoring_session.stage_ops` 在**世界副本**上校验读改写；校验不过的提案
保持 `pending` 或转 `rejected`，**不会**被记进「已采纳」。传闻、猜测这类个人类断言永远不进正史。

```text
POST /api/v1/worlds/{w}/evolution/propose            只草案，commit 默认 false
GET  /api/v1/worlds/{w}/evolution/proposals          审计轨迹：每条断言与它的下场
POST /api/v1/worlds/{w}/evolution/proposals/{p}/approve
POST /api/v1/worlds/{w}/evolution/proposals/{p}/reject?reason=...
```

「已采纳」只在 `applied` 里真的出现这条提案时才写，所以报告不会把一次失败说成成功；
采纳时会重新校验整份世界文档，引用悬空就 400。世界工坊的「世界演化」页签就是这套接口的界面：
提交一句话、看提案、逐条采纳或驳回，驳回原因就留在记录里。

### 角色路由：模型按角色选，不写死在代码里

`backend/app/engines/agent_router.py` 让引擎按**角色**（`narrative` / `system` / `memory`）找模型：

```env
AGENTS_SYSTEM_MODEL=...        # 回答 system 角色的模型
AGENTS_SYSTEM_PROVIDER=...     # 可选供应商覆盖
AGENTS_SYSTEM_ENABLED=0        # 整个角色关掉
AGENTS_SYSTEM_TIMEOUT=45       # 单次调用秒数
AGENTS_NARRATIVE_* / AGENTS_MEMORY_*   # 同样的开关
```

每读一次环境、每个角色独立，未配置的角色退回主模型——所以一个从没听过「角色」的部署行为不变。
`AgentRuntime.ask()` 永不抛异常：调用失败返回带 `error` 的空答案并降级到下一个角色，
一回合不会因为模型挂了而失败。每次调用写进 `game_state.db` 的 `agent_calls` 表，
调试台的「模型调用」页签读它，`GET /api/v1/agents` 只看配置与可用性，**从不回显密钥**。

### 时间只在玩家行动时走

`backend/app/engines/game_clock.py` 的 `Clock` 由 `ActionService` 在应用效果时推进：
每个行动带 `duration_minutes`，回合结果多出 `minutes_spent` 与 `clock`，`GameState` 多出 `day` / `minute`。
暂停的战役不花时间，恢复时也不补算——因为没有墙钟时间可供追赶，离线世界模拟在结构上就不存在。

### 任务：定义可编辑，进度由事实推进

任务有目标、前置任务、接受条件、失败条件与奖励。`GET|POST|PATCH /api/v1/worlds/{w}/quests`
到目标与奖励级（`.../objectives/{id}`、`.../rewards/{id}`）都可编辑，每次编辑都跑完整世界校验，
**不通过的编辑不会落盘**。奖励没有 id，用效果种类当路径 id（同一任务同一效果种类只有一条）。

推进目标的是 `quest_progress` **效果**，不是任何一段文本。调试时的手动推进
（`POST /campaigns/{c}/quests/{q}/objectives/{o}/progress`）走的仍是 `ActionService` 的提交路径：
幂等、版本 CAS、单事务、领域事件齐全，只是在判定环节跳过规则。它受 `ENGINE_DEBUG_TOOLS` 保护，
关闭时返回 403。

### 叙事诊断：模型到底看到了什么

每回合的 `narrative.diagnostics` 记录**只读旁路**信息，不进入提示词、不影响提交：

- 路由：`provider` / `model` / `temperature` / `max_tokens` / `timeout` / `language` / `content_policy`
- 上下文：`blocks[]`（id、层、标题、160 字符摘要）与 `fact_counts`（按来源计数）
- 结局：`skipped`（未调用模型）或实际生效的策略；超时、异常、空文本、被拒**都带着这份记录**

失败时最需要诊断，所以 `narrative.failed` 事件也带 `diagnostics`。前端调试台「叙事与上下文」页签
把它读成卡片，「行动记录」页签列出该战役每个 request_id 的 `applied/rejected/duplicate`。

### 内容策略与本地回退

`content_policy` 有 `standard` / `nsfw` 两档（`POST /api/settings` 可改）。有两种路径：

- **主动**：直接选 `nsfw`，整条请求走本地模型（`nsfw_provider` / `nsfw_model`）。
- **被动**：上游返回拒绝时，路由器不把拒绝文本当正常输出转发，而是带 `⚠⚠REFUSAL⚠⚠` 哨兵返回；
  叙事层发现哨兵后整条重发到本地模型一次。本地也拒绝则本回合没有叙事，**状态照常提交**。

流式路径同样处理：`stream()` 累积后判定，命中就不再逐帧转发——否则玩家会看到半句拒绝文本。

### 模型路由

`backend/app/engines/llm_router.py` 支持多个供应商，可在设置面板或模型预设中切换：

- `deepseek` / `anthropic` / `openai`（云端，需对应 API key）
- `local`（本地 OpenAI 兼容服务，即 NInfer；无需云端密钥即可叙事，见 `docs/local_model_ninfer.md`）
- 可选经 CLIProxyAPI 走订阅代理（`proxy/README.md`）

预设为 SillyTavern 风格子集：`top_p` / `top_k` / `min_p`、重复/存在/频率惩罚、`seed`、
停止词、上下文窗口、预设系统提示；叙事/intent/dialogue/summary 四个角色可按场景覆盖。

### 叙事语言

`WorldDefinition.language`（`"zh"` / `"en"`，留空视为英文）决定**描述语言**，规则引擎从不读它。
起步世界自动写入，AI 生成的世界按提示词语言写入。三处都遵守它：

1. 确定性叙事器（`TemplateNarrator`）的句子与连接符；
2. 效果描述（拾取/失去/移动/开门/关门/标记）；
3. 交给模型时的 `language` 选项（场景预设已指定则预设优先）。

没有这一层时，中文世界会得到 `You arrive at 林间小路.` 这种句子——地点名是中文，句子是英文。

### 叙事不会带控制标记

旧聊天链路曾靠解析模型输出里的 `[ITEM_ADD:name|category|source]` 改库存。那条路现在默认不写状态
（`LEGACY_APPLY_ITEM_TAGS=0`），但标记能出现在引擎散文里本身就是风险：任何一处放宽的正则都会
把它变成副作用。`strip_prose_tags()` 因此在两条叙事路径上都把这类标记剥掉。

### 编辑器与调试台

- **世界工坊**（十三个页签）：世界地图、剧本生成、物品、门、实体、事件、任务、行动、调试、作品穿越、记忆提炼、对话补全、世界演化——
  每次保存都跑完整世界校验；事件可校验、可模拟（模拟不改真实状态）；调试页有状态差异与快照回滚。
- **调试台**（四个页签）：调用追踪（旧链路的 trace）、叙事与上下文（`narrative.diagnostics`）、
  行动记录（request 列表与单条详情）、模型调用（哪个角色由哪个模型回答 + 每次调用记录）。
- 所有能改状态的调试能力都受 `ENGINE_DEBUG_TOOLS` 限制，生产环境默认关闭并返回 403。

---

## 快速开始

### 环境要求

- **Python 3.10+**、**Node.js 18+**
- 可选：**Docker**（只为 Neo4j 知识图谱镜像准备，没有也能完整游玩）
- 可选：一个模型 API key，或本机跑一个 OpenAI 兼容推理服务

最短路径（不需要任何配置）：装好依赖 → `python launch.py` → 首页选一个情景 → 绑定起步世界 → 直接玩。
叙事由 `TemplateNarrator` 按已结算效果生成，引擎的判定、事件、任务、存档全部照常工作。

### 安装

```bash
git clone <本仓库地址>
cd shujia

install.bat      # Windows
./install.sh     # macOS / Linux
```

脚本会建好后端虚拟环境、安装 `backend/requirements.txt` 与前端依赖，
没有 `.env` 时从 `.env.example` 复制一份。

> **Neo4j 可选，且脚本不再替你起容器**：安装脚本只装 Python 与前端依赖。
> 需要实体关系图谱时自己起一个 Neo4j 5 并把 `NEO4J_URI` 填进 `.env`；不想用就把
> `ENGINE_GRAPH_MIRROR` 设为 0——图谱只是镜像，不会因为不可用而回滚回合。

起图谱可以直接用自带的检查工具，它会告诉你当前状态并给出下一步命令：

```bash
python launch.py --neo4j               # 只看状态：连得上 / 认证失败 / 没在跑，各自给出修复命令
python launch.py --neo4j start         # 有 Docker 时起一个 neo4j:5 容器（首次会生成随机密码写回 .env）
python launch.py --neo4j password xxx  # 改密码后同步写回 .env
python launch.py --neo4j stop          # 停容器
```

没有 Docker 也可以：用 Neo4j Desktop 或官方压缩包起一个 5.x，把 `bolt://localhost:7687`
与密码填进 `.env` 即可；不改 `.env` 时把 `ENGINE_GRAPH_MIRROR` 设为 0 关掉镜像。

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

# 内容策略：standard = 正常；nsfw = 整条请求走本地模型
# 也可在设置面板切换，或 POST /api/settings
# CONTENT_POLICY=standard
# NSFW_MODEL=
# 上游拒绝时自动改走本地模型重试一次
# LOCAL_FALLBACK_ON_REFUSAL=1

# 角色路由：让不同角色由不同模型回答（不写就全部退回主模型）
# AGENTS_NARRATIVE_MODEL=
# AGENTS_SYSTEM_MODEL=
# AGENTS_MEMORY_MODEL=
# AGENTS_SYSTEM_ENABLED=0
# AGENTS_SYSTEM_TIMEOUT=45

# 调试/作者工具：手动推进任务目标、回滚快照、修复状态都受它控制
# ENGINE_DEBUG_TOOLS=0

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
起前端 → 等健康检查 → 打开浏览器。API 文档在 <http://127.0.0.1:8000/docs>。
关掉启动器窗口即停止两端，不需要自己开两个终端跑 `uvicorn` 和 `npm run dev`。

两种前端模式：

| 命令 | 前端 | 端口 | 适合 |
|------|------|------|------|
| `start.bat` / `python launch.py` | vite dev（热更新） | 5173 | 改前端代码时 |
| `start.bat --prod` | `npm run build` + 自带静态服务器托管 `frontend/dist` | 4173 | 演示、桌面快捷方式 |

**桌面快捷方式默认走 `--prod`**：某些受限环境里 vite dev 无法在浏览器端预打包依赖，
裸导入会 404 导致白屏；`--prod` 交付的是一份已经打包好的 js，在任何环境下都能渲染。
想回到热更新就在参数里加 `--dev`。细节见 [`docs/launch.md`](docs/launch.md)。

### 桌面端（可选）

`desktop/` 是一个 Tauri 2 壳：原生窗口里跑的就是上面这套前端，后端仍由 `launch.py` 拉起。
需要 Rust stable + Node 18+，构建与限制见 [`desktop/README.md`](desktop/README.md)。

```bash
cd frontend && npm install && npm run build
cd ../desktop && npm install
npm run build        # 产物在 desktop/src-tauri/target/release/bundle/
```

不想装 Rust 的话，用 `start.bat --prod` + Chrome「创建快捷方式（以窗口形式打开）」也是同一效果。

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

典型流程：新建情景 → 设定问答（SetupWizard）→ 绑定世界 → 行动（文字或按钮）→
看结算与叙事 → 调试台核对叙事上下文、行动记录与状态差异。

几条值得知道的界面行为：

- **行动预演**：提交前可先看判定结果（`POST /campaigns/{id}/actions/preview`），预演不写状态。
- **自然语言**：文字行动由 `text_action_service` 解析成同一个 `ActionRequest`，解析失败回到按钮，
  不会「模型理解成什么就是什么」。
- **调试台四个页签**：调用追踪（旧链路 trace）、叙事与上下文（`narrative.diagnostics`）、
  行动记录（request 列表与单条详情含状态差异）、模型调用（角色路由与调用记录）。
- **内容策略**在设置面板切换；切到「NSFW（本地）」后叙事整条走本地模型。

前端状态用 Zustand（`frontend/src/store.js`），接口在 `frontend/src/api.js`；页面按路由懒加载。

---

## 目录结构

```
shujia/
├── launch.py                    # 通用启动器（前后端编排、健康检查、浏览器）
├── start.bat                    # Windows 入口（纯 ASCII，调用 launch.py）
├── scripts/serve-dist.mjs       # --prod 的前端静态托管 + /api 反代
├── scripts/start.sh             # macOS / Linux 启动入口（调用 launch.py）
├── scripts/start-dev.ps1        # PowerShell 开发启动脚本
├── install.bat / install.sh     # 一键安装（Python venv + 前端依赖）
├── desktop/                     # Tauri 桌面壳（不重写界面，只包一层窗口 + 拉起后端）
├── .env.example                 # 环境变量样例，安装脚本会复制为 .env
├── README.md / CHANGELOG.md
├── docs/
│   ├── review-overview.md       # 运行时结构、13 项验收矩阵、风险登记
│   ├── delivery_review.md       # 七条需求与 14 条验收的交付审查
│   ├── stage6_effect_registry.md # 效果注册表与相关测试记录
│   ├── stage8_content_policy.md  # 内容策略与本地回退
│   ├── stage9_world_crossover.md # 作品穿越
│   ├── stage10_scenario_author.md# 一句话生成剧本
│   ├── stage11_quest_editor.md   # 任务编辑与手动推进
│   ├── stage13_narrative_debug_console.md # 叙事诊断与调试台
│   ├── stage14_world_language.md # 叙事语言跟随世界
│   ├── phase0_audit.md          # 阶段 0：真实链路审计与风险登记
│   ├── phase3_world_evolution.md# 断言 → 提案 → 采纳
│   ├── phase4_memory_provenance.md # 记忆晶体的来源与可信度
│   ├── phase5_game_clock.md     # 只在行动时推进的时间
│   ├── phase6_integration_review.md # 六个场景跑成测试
│   ├── phase7_completeness_review.md # 逐条核验「承诺 vs 证据」
│   ├── local_model_ninfer.md    # 本地模型接入
│   ├── postgres-migration.md    # SQLite → PostgreSQL 一次性导出
│   └── ...                      # 各阶段设计与审计记录
├── backend/
│   ├── requirements.txt / pytest.ini
│   ├── app/
│   │   ├── main.py              # FastAPI 入口、中间件、/api/settings
│   │   ├── config.py            # pydantic-settings，读取 .env
│   │   ├── api/                 # routes_engine / routes_knowledge / routes_game
│   │   │                        # routes_scenarios / routes_characters / routes_works
│   │   ├── db/                  # scenario_store / game_state_store / event_store / trace_store
│   │   ├── engines/             # 规则与引擎层
│   │   ├── models/              # game.py / knowledge.py / work.py（世界、状态、条件、作品档案）
│   │   ├── services/            # 编排与服务层（action_service 是唯一写入口）
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

**agent 层**（阶段 1–5）：`agent_router`（按角色路由模型，`AGENTS_<ROLE>_*` 决定用哪个，代码里没有一个模型名）、
`system_agent`（先确定性分类，只有长断言才问模型）、`npc_dossier`（NPC 是每回合按数据拼出来的档案）、
`player_context`（带 `fact` / `canon` / `belief` / `guess` / `history` 标签的分层上下文）、
`game_clock`（只有玩家行动才推进的时间）。

**services/**：`action_service`（唯一写入口）、`text_action_service`（自然语言 → `ActionRequest`）、
`knowledge_service`、`narrative_service`、`narrator_factory`、`intent_advisor`、`world_service`、
`world_editor`、`world_evolution`（断言 → 校验过的提案 → 可审核落盘）、
`authoring_session`（一句话变成编辑操作）、`scenario_author`、`starter_worlds`、`summary_service`、
`state_diagnostics`、`ai_generator`、`card_images`、`scenario_service`、`scenario_interpolation`、`game_session`。

---

## API 概览

全部接口挂在 `/api` 下，共 **146 个路由**（`/api/v1` 92 个、旧聊天链路 `/api/game` 27 个、其余 27 个）；设置 `API_TOKEN` 后除健康检查外都要求
`Authorization: Bearer <token>` 或 `X-API-Token`。完整清单与请求/响应结构见 <http://127.0.0.1:8000/docs>，
`/api/v1` 的每条路由都有类型化响应模型，并由 `backend/tests/api/test_routes_contracts.py` 守住。

### 规则链路 `/api/v1`（确定性游玩）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/action-types` | 内置行动目录（16 个内置 + 世界声明的 `custom`） |
| POST | `/campaigns/{id}/world` | 为战役绑定世界 |
| POST | `/campaigns/{id}/actions` | 提交结构化行动（唯一写入口） |
| POST | `/campaigns/{id}/actions/preview` | 干运行：只看判定，不写状态 |
| POST | `/campaigns/{id}/text-actions` | 自然语言行动，解析后走同一管线 |
| POST | `/campaigns/{id}/combat` | 战斗回合 |
| GET | `/campaigns/{id}/state` | 权威状态（含 `version`） |
| GET | `/campaigns/{id}/requests` | 该战役的请求记录（含 `applied/rejected/duplicate`） |
| GET | `/actions/{request_id}` | 按幂等键查单条请求与结果 |
| GET | `/campaigns/{id}/agent-calls` | 该战役的模型调用记录 |
| GET | `/campaigns/{id}/events` | 领域事件（可回放，支持 `after_version`） |
| GET | `/campaigns/{id}/event-executions` | 事件执行记录 |
| GET | `/campaigns/{id}/quest-progress` | 任务进度（读权威状态拼装） |
| GET | `/campaigns/{id}/lane` | 时间线/调试视图 |
| GET | `/campaigns/{id}/diagnostics` | 状态一致性诊断 |
| GET | `/campaigns/{id}/snapshots` | 快照列表；`/{version}` 取单份 |
| GET | `/campaigns/{id}/state-diff` | 状态差异（版本前后对比） |
| POST | `/campaigns/{id}/rollback` | 回滚到快照（受 `ENGINE_DEBUG_TOOLS` 限制） |
| POST | `/campaigns/{id}/repair` | 修复（同上） |
| POST | `/campaigns/{id}/quests/{q}/objectives/{o}/progress` | 调试用目标推进（同上，走真实提交路径） |
| GET/POST | `/campaigns/{id}/export`、`/import` | 存档导出/导入（覆盖需 `reset=true`） |
| GET/POST | `/worlds`、`/worlds/{id}` | 世界读取、新建与整份写入 |
| GET | `/world-presets` | 起步世界预设（奇幻/轨道站/雾镇/荒野） |
| POST | `/worlds/validate` | 校验引用与条件/效果 |
| POST | `/worlds/generate` | AI 生成世界 |
| POST | `/scenarios/author` | 一句话生成世界 + 知识 + 剧情线（`save=false` 为试运行） |
| GET/POST | `/worlds/{w}/quests` | 任务列表与新建 |
| GET/PATCH/PUT/DELETE | `/worlds/{w}/quests/{q}` | 单个任务的读取与整体替换 |
| PUT/DELETE | `/worlds/{w}/quests/{q}/objectives/{oid}` | 任务目标 |
| PUT/DELETE | `/worlds/{w}/quests/{q}/rewards/{kind}` | 任务奖励（路径 id 是效果种类） |
| POST | `/worlds/{w}/events/{e}/validate`、`/simulate` | 事件校验与只读模拟 |
| PUT/DELETE | `/worlds/{w}/{locations,items,doors,entities,events,quests}/{item_id}` | 资源级编辑 |
| PUT/DELETE | `/worlds/{w}/custom-actions/{action_id}` | 世界声明的自定义行动 |
| GET | `/worlds/{id}/map` | 地图数据 |
| GET | `/agents` | 每个角色（narrative/system/memory）由哪个模型回答、是否配置；不回显密钥 |
| GET | `/campaigns/{id}/agent-calls` | 该战役的模型调用记录（角色、任务、供应商、模型、耗时、是否降级） |
| POST | `/worlds/{w}/evolution/propose` | 断言 → 提案（`commit` 默认 false，只草案） |
| GET | `/worlds/{w}/evolution/proposals` | 提案审计轨迹（状态、置信度、驳回原因） |
| POST | `/worlds/{w}/evolution/proposals/{p}/approve` | 采纳一条提案（先重新校验整份世界文档） |
| POST | `/worlds/{w}/evolution/proposals/{p}/reject` | 驳回并记录原因 |
| GET | `/local-model` | 本地模型可用性 |

### 作品库 `/api/v1`（世界穿越）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/works` | 内置作品档案列表 |
| GET | `/works/{title}` | 单个作品（仅内置，未命中 404） |
| POST | `/works/resolve` | 解析作品；`allow_model=false` 时未命中直接 404 |
| POST | `/works/enter` | 生成世界骨架并记录角色选择 |

### 知识、预设与生成 `/api/v1`

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/knowledge/layers` | 知识分层 |
| GET/POST | `/scenarios/{id}/knowledge` | 知识条目列表与新建 |
| POST | `/scenarios/{id}/knowledge/validate` | 校验知识条目 |
| PATCH / DELETE | `/knowledge/{entry_id}` | 编辑/删除知识条目（含 `audience_npc_ids`） |
| GET | `/scenarios/{id}/context/preview` | 预览一次行动会注入的上下文 |
| GET/PUT | `/scenarios/{id}/play-config` | 游玩配置（模式、预算、预设、剧情线） |
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
| POST | `/{cid}/pause`、`/{cid}/resume` | 暂停/恢复：暂停的战役不推进游戏时间 |
| GET | `/{cid}/clock` | 游戏内时间与是否冻结 |

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

### 角色库 `/api/characters`

角色现在**独立于世界存在**：`GET /api/characters` 列全局库，`POST /api/characters` 直接在库外创建
（`scenario_id` 为空串），`POST /api/characters/{id}/library/import` 把某个世界里的角色复制一份收进库。
`GET /api/scenarios/{id}/characters` 仍然只列这个世界的角色，但**开局时可以从库里挑任何一个**——
`PUT /api/campaigns/{cid}/character` 不再因为角色属于别的世界而 409。前端首页新增「角色库」页
（`/characters`），SetupWizard 的角色步骤会同时列出本世界角色与库角色，并在卡片上标「· 库」。

全局设置：`GET/POST /api/settings`（供应商、模型、采样参数、`content_policy`、本地回退开关）、
`GET /api/health`、`GET /api/health/neo4j`。

### 一次行动请求的形态

```json
{"campaign_id": "camp-1", "request_id": "6f1c…", "action": "open",
 "target_id": "warehouse_door", "params": {}}
```

返回 `ActionResult`：`status`（`applied` / `rejected` / `duplicate` / `conflict`）、`ok`、`code`、
`message`、`state_version`、`effects_applied`、`events_fired`、`quests`、`story_nodes_entered`、
`narrative`（含 `diagnostics`）与提交后的 `state`；阶段 5 起还带 `minutes_spent` 与 `clock`（老客户端可忽略）。

`request_id` 是幂等键：同一个 id 重放返回 `duplicate` 与**同一份**结果，不会重复扣物品或发奖励。

---

## 测试与质量

```bash
# 后端全量（pytest，asyncio_mode = auto）
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' \
  backend/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider backend/tests

# 只跑验收走查（1–14 条，不需要任何密钥）
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' \
  backend/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  backend/tests/api/test_acceptance_story.py

# 前端构建（含 hook 顺序检查）
npm --prefix frontend run build
```

> `TEMP` / `TMP` 必须设置：pytest 默认临时目录在部分 Windows 环境下不可写；
> `-p no:cacheprovider` 同理，避免 `.pytest_cache` 写入失败。

- 最近一次全量：**1625 passed / 8 skipped**（跳过项为未启动 Neo4j 的图谱测试），约 130 秒。
- 验收走查集中在 `backend/tests/api/test_acceptance_story.py`（5 项），覆盖 m00001 第九节的 14 条：
  建世界/钥匙/拾取/无钥匙被拒/有钥匙通过/门状态与事件/任务进度/叙事/存档重载/幂等/叙事失败不回滚/
  非法事件配置/模拟只读/中文世界读中文。
- 测试按 `backend/tests/{api,db,engines,models,services,scripts}` 分层，
  包括效果注册表对齐、事件分支、NPC 知识可见性、并发 CAS、幂等重放、内容策略与本地回退、
  角色路由与降级、NPC 档案的知识隔离、演化提案的「未写入不叫已采纳」、游戏时钟暂停即冻结等。
- 六个架构场景跑成测试：`backend/tests/services/test_architecture_acceptance.py`（NPC 对话、玩家补世界、
  世界长剧情、知识隔离、离开游戏、模型失败），每个都打到真实入口与真实 SQLite。
- 前端两条静态检查进 CI 等效的位置：`npm --prefix frontend run check:hooks`（hook 顺序）与
  `check:agents`（调试台与演化面板的接线，防止「静默无数据」）。
- 前端构建会先执行 `frontend/scripts/check-hook-order.mjs`，防止 `useCallback` 未声明就进依赖数组；
  页面全部懒加载，发布包由 `vite build` 产出到 `frontend/dist`。
- 数据层：`scenarios.db` / `game_state.db` / `events.db` / `traces.db`，
  `SCHEMA_VERSION` 分别为 12 / 4 / 2 / 1（game_state.db 在阶段 3/5 各加一张表，纯增量迁移），打开旧库时按版本迁移；路径可用环境变量覆盖。

---

## 已知限制

取自 `docs/review-overview.md` 的 13 项验收矩阵中标记为"部分"的缺口、风险登记，以及
`docs/delivery_review.md` 里如实记录的未验证面：

| 系统 | 状态 | 主要缺口 |
|------|------|----------|
| 世界与地图 | 部分 | 无可走格子、碰撞与自由寻路；跨区域需逐段移动 |
| 实体与 NPC | 部分 | NPC 无自主行为；无关系网络界面 |
| 行动与交互 | 部分 | 无内置潜行、键盘方向移动、自由寻路 |
| 物品与属性 | 部分 | 无独立商店/交易面板；装备只加攻防 |
| LLM 调度 | 部分 | 判定由确定性规则承担，没有 LLM judge；超时/重试未按功能独立配置 |
| 记忆与历史 | 部分 | 规则链路只有确定性滚动摘要 + 可选 summary 预设；无统一 NPC 知识库视图 |
| 模型配置 | 部分 | 预设的角色覆盖仍以叙事为主 |
| 前端 | 部分 | `vite build` 已可运行（2999 modules → `frontend/dist`），构建产物由 `scripts/serve-dist.mjs` 托管并逐路由验证过 200；但没有浏览器自动化，交互仍是人工验证 |
| 旧聊天链路 | 待日落 | `LEGACY_APPLY_ITEM_TAGS` 默认 0，已不写权威状态，但代码与前端标记剥离逻辑仍在 |
| 叙事语言 | 部分 | 只支持中/英二选一；世界工坊未暴露 `language` 字段 |
| 作品库 | 部分 | 内置六部作品，未做联网检索；模型补写依赖已配置模型 |
| 世界演化 | 部分 | 提案与采纳已有界面，但只能新增，不能改字段或删除；故事线不在演化链路里 |
| 角色路由 | 部分 | 三个角色（narrative / system / memory），没有按 NPC 单独分配模型 |

风险登记里的三条 P1：服务默认无鉴权（本机使用，需要共享时设 `API_TOKEN`）、
SQLite 单写者（高并发回以版本冲突重试）、知识为词法相似度而非语义召回。
此外 `/api/game` 旧聊天链路仍保留独立的流式副作用路径，未绑定世界的冒险行为与绑定世界不同。

### 环境相关的两处坑

1. **`backend/requirements.txt` 在本机不可直接求解**：固定的 `litellm==1.43.0` 在 PyPI 上不存在，
   `graphiti-core>=0.5.0` 又要求 `pydantic>=2.8.2` 而 `requirements.txt` 钉的是 `2.8.0`。
   上游未动；本地开发时去掉这两个钉版本即可（安装脚本按原文件执行，遇到同样问题需手动处理）。
2. **pytest 必须设 `TEMP`/`TMP` 并关掉缓存提供者**，见上一节的命令。
3. **受限环境里 `vite dev` 可能白屏**（浏览器端依赖预打包被拦，裸导入 404）。这不是本项目
   的代码问题，改用 `--prod` 即可；桌面快捷方式已经默认 `--prod`。

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

改动前请确认不破坏下面四条不变量（`backend/tests/api/test_acceptance_story.py` 会检查）：

1. **模型不写状态**。任何状态变化都必须经 `ActionService` 提交，模型输出只进叙事。
2. **失败不回滚**。叙事、图谱镜像、摘要失败都只记录，已提交的回合保持有效。
3. **幂等**。同一个 `request_id` 重放只返回同一份结果。
4. **无脚本**。条件、效果、后续事件都是校验过的数据；不要引入 `eval` 或任意代码执行。

提交前跑一遍全量后端测试；改前端请至少跑 `npm --prefix frontend run build`。

---

## 许可证

MIT，见 [`LICENSE`](LICENSE)。