# 记忆与知识系统审计报告

- 审计日期：2026-11-15
- 仓库：https://github.com/LosSca/self-engine
- 分支 / HEAD：master / f03b548 "fix(settings): let a player actually configure a custom model endpoint"
- 审计方式：逐文件通读 + 全仓库 grep + 基线全量测试（未修改任何项目文件）
- 基线测试：2 failed, 1819 passed, 8 skipped（详见第 10 节）

## 0. 结论摘要

任务书（下称"任务书"）对现状的假设与代码实际状态有明显偏差，**既有能力比任务书假设的更强，缺失的部分比任务书假设的更集中**。三条最重要的结论：

1. **P0.1（多语言 Tokenizer）已经基本完成，只剩一处漏网。** `backend/app/services/retrieval/tokenizer.py` 是一套完整的中日英混排分词器（NFKC 归一化 + 中文二元组 + 停用词表），`memory_engine.py:13` 和 `knowledge_service.py:78-80` 都已经改用它。唯一仍在用拉丁字母正则的是 `game_session.py:535-554` 的 `_extract_context_keywords`，它服务于故事卡检索——**第三条检索路径**。

2. **P2 的混合检索栈已经存在，但只接在 Knowledge 上，没接在记忆上。** `backend/app/services/retrieval/` 已包含 BM25、向量通道、图通道、RRF 融合、重排、评测工具，全部可选依赖、缺省降级。记忆晶体（MemoryCrystal）的检索仍是有 6 个硬编码权重的关键词打分（`memory_engine.py:707-766`）。所以 P2 的工作不是"引入 BM25"，而是"把已有检索栈第二处复用"。

3. **P1（结构化事实 / 证据 / 状态机 / 写入管线）在代码中完全不存在。** `MemoryFact`、`evidence_type`、`valid_from`、`memory_facts` 等关键词在整个 backend 目录 0 命中。这是本次升级真正的核心缺口，也是唯一需要新增数据表的部分。

此外，**P3 的 NPC 权限隔离比任务书假设的更严格**：`witnessed_by` 的交叉求交在上下文构建之前完成，且失败时"失败关闭"（`_resolve_crystal_audiences`，`memory_engine.py:1021`），已有 8 个专门测试守护。任务书要求的"权限过滤不能只靠 Prompt"这一点，项目已经做到，缺的是**世界真值 / NPC 观察 / NPC 推断**三者的显式区分。

## 1. MemoryEngine 完整调用链

### 1.1 入口

| 入口 | 位置 | 用途 |
| --- | --- | --- |
| 进程级单例 | `backend/app/api/routes_game.py:108` | `_memory = MemoryEngine(event_store=_event_store, llm=_llm)` |
| 事件库 | `routes_game.py:59` | `EventStore(os.environ.get("EVENT_DB_PATH", <backend>/events.db))` |
| 叙事主链路 | `backend/app/services/game_session.py:1208-1224` | 每回合渲染 `=== PRMNT_MEM ===` 等分区 |
| NPC 视角窗口 | `memory_engine.py:1083` `build_npc_knowledge_window` | 供 `player_context.py` 与 NPC 回合使用 |

### 1.2 写入链路（结晶）

```
EventStore (events.db, 追加写, 不可变)
    │  _get_uncrystallized_events(campaign_id, limit)      memory_engine.py:1170
    │  以 SHORT 晶体的 source_end_created_at 作为游标
    ▼
crystallize_short()                                        memory_engine.py:413
    │  触发阈值 AUTO_CRYSTALLIZE_THRESHOLD = 4
    │  上限 MAX_CRYSTALLIZE_EVENTS = 200
    │  LLM 结构化提取 _compress_with_llm()                  memory_engine.py:648
    │  失败降级 json.dumps(源事件 ai_content) —— 不丢数据
    ▼
MemoryCrystal（SHORT）
    │  derive_memory_id = "mem_" + sha1(campaign|tier|start|end)[:16]
    │  stamp_provenance()                                    memory_engine.py:206
    ▼
cascade_consolidation()                                    memory_engine.py:549
    │  SHORT×4 → MEDIUM, MEDIUM×4 → LONG, LONG×4 → MEMORY
    │  _consolidate_tier()                                   memory_engine.py:458
    ▼
_persist_crystal() → EventStore.append(MEMORY_CRYSTAL)      memory_engine.py:1130
    │  payload 内含完整 provenance（source/observer/confidence/
    │  importance/visibility/related_entities/witnessed_by）
    ▼
进程内缓存 self._crystals[campaign_id]
```

### 1.3 读取链路

```text
game_session._handle_narrative (game_session.py:1208-1224)
    │  rag_query = 玩家输入 + 场景文本
    ▼
MemoryEngine.build_context_window_async(...)               memory_engine.py:915
    ├─ rag_on = _rag_crystals_enabled() and bool(query|npcs|location)
    ├─ MEMORY 层：全量无条件注入 === PRMNT_MEM ===           memory_engine.py:938-941
    ├─ LONG/MEDIUM/SHORT：_select_ranked_crystals()          memory_engine.py:785
    │      └─ _score_crystal()                               memory_engine.py:707
    ├─ Graphiti 事实通道（可选）=== WORLD FACTS ===          memory_engine.py:974-1007
    └─ 裸事件尾巴 === DELTA ===                              memory_engine.py:1009-1015
```

`build_context_window`（同步版，`memory_engine.py:835`）与异步版逻辑重复，仅少 Graphiti 通道——**这份重复是两个函数的近似复制，后续改动必须同步两处**，属于本次改造要消除的维护风险点。

### 1.4 关键常量

| 常量 | 值 | 位置 |
| --- | --- | --- |
| `AUTO_CRYSTALLIZE_THRESHOLD` | 4 | `memory_engine.py:336+` |
| `MAX_CRYSTALLIZE_EVENTS` | 200 | 同上 |
| `NPC_RAW_EVENT_LIMIT` | 20 | 同上 |
| `CONSOLIDATION_COUNT` | 4 | 同上 |
| `AUDIENCE_POLICY` | `"source-intersection-v1"` | 同上 |
| `TIER_CONFIDENCE` | SHORT 1.0 / MEDIUM 0.9 / LONG 0.8 / MEMORY 0.7 | `memory_engine.py:~110` |
| `_CRYSTALS_CONTEXT_FRACTION` | 0.10 | `memory_engine.py:772` |
| `_CRYSTALS_MIN/MAX_BUDGET_TOKENS` | 4000 / 100000 | `memory_engine.py:773-774` |
| `_AI_CHAR_LIMITS` | SHORT 2000 / MEDIUM 5000 / LONG 10000 / MEMORY 20000 | `memory_engine.py:~120` |

## 2. KnowledgeService 完整调用链

```text
routes_knowledge.py (预览端点) / action_service (玩法链路)
    ▼
load_play_context(scenario_store, scenario_id)              knowledge_service.py:342
    ├─ scenario_store.get_scenario()
    ├─ scenario_store.get_play_config()
    └─ scenario_store.list_knowledge()
    ▼
build_context(entries, mode, max_depth=9, story_node, storyline,
              scenario_id, story_nodes, armed_packs, focus,
              budget_chars, query, vector_retriever)        knowledge_service.py:90
    ├─ 1) 确定性过滤（每条都记录 ExcludedEntry 原因）        :147-182
    │      enabled / audience_npc_ids / mode_scope / depth 上限
    ├─ 2) build_scene_query(player_input, focus)             :194
    ├─ 3) vector_retriever(documents, ids, metadata)         :201-210（可为 None）
    ├─ 4) RetrievalService().retrieve(...)                   :211
    │      BM25 + 可选向量 → RRF → rerank(build_signals)
    ├─ 5) 未命中的条目按作者顺序追加到尾部                  :229-250
    ├─ 6) budget_chars 截断（always_on 豁免）                :257-274
    └─ 7) 返回 KnowledgeContext(stats=ContextStats(...))     :280-305
```

**降级语义已经实现且测试过**：无 `ENGINE_EMBEDDING_PROVIDER` 且无 `OPENAI_API_KEY`/`CUSTOM_LLM_API_KEY` 时 `default_vector_retriever_factory` 返回 `None`（`knowledge_service.py:68-72`），并在 `ContextStats.retrieval_degraded` 里写字面量 `"vector:not_configured"`（`:296-299`）。任务书"没有 Embedding 配置时系统仍能完整运行"的要求，在 Knowledge 侧**已满足**，在记忆侧待接入。

## 3. 数据库与数据模型

三个独立 SQLite 存储，各自维护一张 `schema_version(version, applied_at)` 表，迁移写法统一为「版本号 → SQL 语句列表」的有序字典，**这是可以复用的既有范式**。

| 存储 | 类 | SCHEMA_VERSION | 表 | 路径 |
| --- | --- | --- | --- | --- |
| 事件 | `EventStore` `db/event_store.py:19` | 2 | `events` + `idx_campaign` | `EVENT_DB_PATH` 或 `backend/events.db` |
| 剧本/知识 | `ScenarioStore` `db/scenario_store.py:108` | 13 | `scenarios`、`story_cards`、`campaigns`、`scenario_characters`、`character_looks`、`scenario_knowledge`、`scenario_play_config`、`model_presets` | `routes_game._scenario_db_path()` |
| 游戏状态 | `GameStateStore` `db/game_state_store.py` | 5 | `worlds`、`campaign_state`、`action_requests`、`domain_events`、`campaign_snapshots`、`agent_calls`、`evolution_proposals`、`scheduled_events` | `routes_game._game_state_db_path()` |

### 3.1 events 表（记忆的唯一真相源）

```sql
CREATE TABLE events (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL,          -- JSON
  narrative_time_delta INTEGER NOT NULL DEFAULT 0,
  location TEXT NOT NULL DEFAULT '',
  entities TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  witnessed_by TEXT NOT NULL DEFAULT '[]'   -- 迁移 v2 追加
);
```

`Event` 是不可变 namedtuple（`event_store.py`），无 update 接口，只有 `update_witnessed_by` / `delete_npc_thoughts` / `delete_last_pair` 这几个有明确语义的例外。**"原始事件是历史证据，不能随意覆盖"这条原则在存储层已经成立**，本次改造不得引入任何改写历史事件的能力。

`EventType` 枚举：`PLAYER_ACTION`、`NARRATOR_RESPONSE`、`WORLD_TICK`、`COMBAT_ACTION`、`COMBAT_RESULT`、`PLOT_GENERATION`、`NPC_THOUGHT`、`JOURNAL_ENTRY`、`MEMORY_CRYSTAL`、`TIMESKIP`、`INVENTORY`、`INVENTORY_PROPOSAL`、`POWER_LEVEL_UPDATE`、`AI_OPENING_GENERATED`。

### 3.2 记忆的物理形态

记忆**没有独立表**。晶体以 `MEMORY_CRYSTAL` 事件的形式存放在 events 表，读取时反序列化并重算 provenance。这带来两个直接后果：

- 好处：天然满足"摘要不覆盖原始事件"与"摘要保存来源 ID"（payload 里有 `source_event_ids`）。
- 代价：无法按 `subject/predicate` 做结构化查询，也无法表达"事实随时间的有效期"。这正是 P1 必须新增表的原因。

## 4. 四层记忆的生成与合并逻辑

| 层 | 生成条件 | 输入 | 输出上限 |
| --- | --- | --- | --- |
| SHORT | 未结晶事件 ≥ 4 且事件类型 ∈ `CRYSTALLIZE_EVENT_TYPES` | 最多 200 条裸事件 | 2000 字符 |
| MEDIUM | SHORT 层未消费晶体 ≥ 4 | 4 条 SHORT | 5000 字符 |
| LONG | MEDIUM 层未消费晶体 ≥ 4 | 4 条 MEDIUM | 10000 字符 |
| MEMORY | LONG 层未消费晶体 ≥ 4 | 4 条 LONG | 20000 字符 |

**"未消费"的定义**：`_unconsumed_crystals`（`memory_engine.py:398`）取该层中 `source_end` 晚于上一上层晶体 `source_end` 的晶体。消费关系靠时间戳游标而非显式外键。

### 4.1 与任务书 P2 的两处正面冲突

1. **固定条数合并**。`_consolidate_tier` 取 `unconsumed[:CONSOLIDATION_COUNT]`（`CONSOLIDATION_COUNT = 4`），严格按时间序切片，不考虑主题、人物、地点、剧情线。这正是任务书 5.1 批评的做法。
2. **无主题聚类**。合并产物的 `related_entities` 是四个来源 `entities` 的并集（`_related_entities`，`memory_engine.py:194`），仅作元数据，不参与分组。

### 4.2 值得保留的部分

- **LLM 失败降级不丢数据**：`_fallback_short` / `_fallback_player_summary`（`memory_engine.py:1215/1249`）在模型不可用时把源事件正文原样打包，保证"压缩不造成不可逆信息丢失"。
- **provenance 贯穿四层**：`stamp_provenance` 计算 source、observer、confidence、importance、visibility、related_entities、witnessed_by，并在跨层合并时用 `_common_witnesses`（`:152`）求交，而非并集——**权限只会收紧，不会因合并而放大**。这比任务书的要求更严格，必须保留。

## 5. 当前记忆检索算法

`_score_crystal`（`memory_engine.py:707-766`）：

```text
score = TIER_BASE[tier]                        # SHORT 4.0 / MEDIUM 3.0 / LONG 2.0
      + 5   × |query_keywords ∩ tokens(ai_content)|
      + 50  × [任一在场 NPC 名字出现在正文]
      + 30  × [任一地点关键词出现在正文]
      + 10  × (层内序号 / (层大小-1))             # 线性衰减，越新越高
```

预算与选择：`_select_ranked_crystals`（`:785`）按分数降序贪心填充 token 预算，再按时间序还原。每层预算 = `_compute_crystals_budget(context_window) // 3`，总预算 = `context_window × 0.10`，夹在 [4000, 100000]。

### 5.1 缺口（任务书 P0.3 / P2.3）

| 任务书要求 | 现状 | 判定 |
| --- | --- | --- |
| importance 参与打分 | 字段存在，评分未用 | ✗ |
| confidence 参与打分 | 字段存在，评分未用 | ✗ |
| related_entities 参与打分 | 字段存在，仅作元数据 | ✗ |
| observer_ids 参与打分 | 仅用于权限过滤，不参与排序 | ✗ |
| temporal_relevance | 只有层内 recency 线性奖励 | 部分 |
| task_relevance | 无任务/承诺概念 | ✗ |
| 混合检索（BM25/RRF/向量/图/时间） | 仅关键词重叠 | ✗ |
| 实体 / 别名索引 | 无 | ✗ |
| 时间有效期（valid_from/valid_until） | 无 | ✗ |

### 5.2 第三条检索路径（新发现的 P0 缺陷）

`game_session.py:535-554`：

```python
words = re.findall(r"[a-zA-ZÀ-ÿ]{3,}", text.lower())
```

外加一份硬编码的英/葡停用词表。它在 `game_session.py:715` 被 `context_keywords = self._extract_context_keywords(context_text)` 调用，用于给故事卡打分。**中文玩家输入时该函数返回空集合**，故事卡检索退化为纯作者顺序。这是任务书 P0.1 唯一尚未修复的位置。

## 6. NPC 记忆权限机制

### 6.1 数据流

```text
事件写入时 → witnessed_by（谁在场）      event_store.append(..., witnessed_by=[...])
    ▼
晶体化时 → _common_witnesses(sources)    memory_engine.py:152  取交集
    ▼
读取时   → _resolve_crystal_audiences()  memory_engine.py:1021 校验并经 AUDIENCE_POLICY
    ▼
build_npc_knowledge_window(npc_name)     memory_engine.py:1083 只返回命中的晶体
    ▼
player_context.PlayerContextBuilder._memory()  player_context.py:320 作为 npc_memory 层
```

### 6.2 已实现的三条硬性质

1. **失败关闭**：`_resolve_crystal_audiences` 先把 `witnessed_by` 清空，再尝试从事件库校验；校验失败则保持空集（`:1026-1027`）。宁可让 NPC 失忆，也不让它看到不该看的东西。
2. **合并不放权**：`AUDIENCE_POLICY = "source-intersection-v1"`，跨层合并取所有来源见证者的交集。测试 `test_consolidation_intersects_every_source_audience`、`test_unverified_legacy_summaries_cannot_gain_audience_through_consolidation` 守护这条性质。
3. **过滤发生在上下文构建之前**，不是靠 Prompt 请求模型保密。这正是任务书 P3 的核心要求。

### 6.3 缺口

- `visibility` 字段被持久化了，但**检索与过滤路径完全不读它**（形同注释）。
- 没有"世界真值 vs NPC 观察 vs NPC 推断"的三分：`MemoryCrystal.observer` 只有 `"events"` / `"player"` 两类来源标记，无法表达"某 NPC 以为 X 其实 Y"。
- 没有 NPC 私有信念存储，因此"NPC 可以有错误认识"这一条无法实现（只能靠 `npc_dossier` 的静态 `secrets`）。

## 7. 向量检索与 Graphiti 集成

两类可选依赖，都做到了"缺省不阻塞"：

| 组件 | 文件 | 触发条件 | 降级行为 |
| --- | --- | --- | --- |
| 向量通道 | `services/retrieval/vector_retriever.py`、`embedding_index.py`、`embedding_provider.py`、`services/retrieval_embedding.py` | `ENGINE_EMBEDDING_PROVIDER` 或存在 API Key | 返回 `None`，走纯 BM25，`stats.retrieval_degraded` 记 `vector:not_configured` |
| 图通道 | `engines/graphiti_engine.py`、`retrieval/graph_retriever.py`、`graph_query.py` | `memory_engine._graphiti` 非空 | 捕获 `(RuntimeError, OSError, ValueError, TypeError, KeyError, LookupError)` 并 `logger.warning`，不中断回合（`memory_engine.py:1002-1007`） |

`docs/phase4_memory_provenance.md` 明确记录了当时的取舍：

> What was deliberately not added: No vector store, no embedding index, no graph. Retrieval is still the existing keyword scoring plus the per-NPC witness filter. No candidate pipeline.

也就是说，**记忆侧的"无候选管线"是明确的设计选择**，不是遗漏。本次 P1 要补的正是这个候选管线。

## 8. 测试覆盖现状

基线：**1819 passed, 8 skipped, 2 failed**（两个失败与本任务无关，详见第 10 节）。

| 测试文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `tests/engines/test_memory_engine.py` | 28 | 结晶、增量、RAG 排序、MEMORY 层恒定注入、NPC 窗口过滤、权限求交、旧数据重建 |
| `tests/engines/test_memory_provenance.py` | 8 | provenance 落库与还原、删除见证者后的降级、memory_id 稳定性 |
| `tests/services/test_knowledge_service.py` | 24 | 深度链、模式隔离、预算截断、CJK 查询、排序复杂度 |
| `tests/services/test_retrieval_core.py` | — | BM25 / RRF / 重排 / 评测指标 |
| `tests/services/test_retrieval_channels.py` | — | 通道失败降级 |
| `tests/engines/test_event_engine.py` | — | 事件类型与见证者 |

### 8.1 覆盖缺口（对应任务书第九节）

- **无记忆评价数据集**：没有 Recall@K / Precision@K / MRR / 事实准确率 / 泄漏率的任何基准。
- **无新旧算法对比**：任务书 4.3 要求的"保留现有评分作为基准并比较"没有基础设施。
- **无 100 轮以上的长程召回测试**：现有测试最多构造十余条事件。
- **无时间推理测试**：因为没有有效期字段可测。
- **无并发写入 / 幂等写入测试**：事件库有 `threading.Lock`，但无测试。
- **无 Token Budget 上限断言**：预算逻辑存在，但没有"绝不超限"的测试。

## 9. 与任务书的差距矩阵

| 任务书条目 | 现状 | 差距 | 处置 |
| --- | --- | --- | --- |
| P0.1 统一 Tokenizer | memory_engine / knowledge_service 已完成 | `game_session._extract_context_keywords` 未统一 | **修复**（小改） |
| P0.2 事实来源区分 | 有 `ContextSection.kind`（fact/canon/belief/guess/history）与晶体 `source`/`observer` | 无 `evidence_type` 枚举；LLM 叙事与引擎结算无法区分 | **新增枚举 + 标注** |
| P0.3 记忆评分 | 6 权重关键词打分 | importance/confidence/entities/observer/temporal/task 全未用 | **扩展 + 基线对比测试** |
| P0.4 永久记忆注入 | `memory_engine.py:938-941` 全量注入 | 与场景无关的世界事实也进 Prompt | **改为相关度检索 + 保留常驻白名单** |
| P1 MemoryFact | 不存在 | 全部 | **新增** |
| P1 Memory Evidence | 晶体 payload 里有 `source_event_ids`，无独立证据表 | 无法多证据溯源、无 evidence_type | **新增** |
| P1 事实状态机 | 不存在 | 全部 | **新增** |
| P1 写入管线 | 无（设计上明确排除） | 全部 | **新增** |
| P2.1 主题驱动合并 | 固定 4 条时间序 | 全部 | **重构 `_consolidate_tier` 分组策略（保留旧路径可回退）** |
| P2.2 保留原始证据 | 已满足（事件不可变 + payload 带 source ids） | 无 | **保持，补测试** |
| P2.3 混合检索 | 检索栈存在但只服务 Knowledge | 记忆侧未接入 | **复用已有检索栈** |
| P2.4 实体别名检索 | 无 | 全部 | **新增实体表 + 别名归一** |
| P2.5 时间检索 | 无 | 全部 | **新增 valid_from/valid_until + 时间查询** |
| P3 NPC 独立认知 | witness 过滤 + 失败关闭已实现 | 无三分法、visibility 未使用、无私有信念 | **升级，保留既有机制** |
| P4 Context Pack | `player_context.py` 有雏形，未接入叙事主链路 | 无统一结构、无 sources、无 Token Budget 断言 | **新增组装层并接入** |
| 第九节 评测 | 无 | 全部 | **新增** |

## 10. 基线环境与测试可运行性

本次审计首先解决了一个阻塞性问题：**在所有项目测试都跑不起来的情况下，任何改动的验证都是空的。**

### 10.1 现象

首次全量测试：`8 skipped, 1818 errors in 22.20s`，全部卡在 pytest 的 `tmp_path` fixture，报 `PermissionError: [WinError 5] 拒绝访问`。

### 10.2 根因

Windows 上 `Path.mkdir(mode=0o700)` 会写出一个只授权"创建者主体"的显式 DACL。DSH 沙箱令牌不是该主体，于是该目录内后续任何 `mkdir`/`scandir` 都返回 `WinError 5`。pytest 在 `_pytest/tmpdir.py:198,208,212,258` 与 `_pytest/pathlib.py:268` 硬编码了 `mode=0o700`。

最小复现：

```python
from pathlib import Path
a = Path(r"<scratch>/z1")
a.mkdir(mode=0o700, exist_ok=True)   # ok
(a / "z2").mkdir()                    # PermissionError [WinError 5] <scratch>/z1/z2
list(a.iterdir())                     # PermissionError [WinError 5] <scratch>/z1
```

### 10.3 解决方案（仓库外，不进版本库）

新建 `<scratch>/_dsh_shim/dsh_pytest_shim.py`，通过 `PYTHONPATH` + `-p dsh_pytest_shim` 加载，做三件事：

1. 把 `os.mkdir` 与 `pathlib` 各 `Path.mkdir` 的 mode 强制成 `0o777`；
2. 在 pytest 之前预建 `<tempdir>/pytest-of-<user>`；
3. 替换 `_pytest.pathlib` / `_pytest.tmpdir` 中无法容忍不可列举目录的几个函数。

它只影响本机测试进程，不改项目配置、不进仓库。

### 10.4 基线

```text
2 failed, 1819 passed, 8 skipped, 19691 warnings in 43.63s
```

两个失败均为既有问题、与本任务无关：

- `tests/api/test_town_simulation.py::test_the_panel_shows_what_the_town_is_doing`（单独运行时通过，顺序相关）
- `tests/services/test_rejection_hint.py::test_the_text_lane_returns_the_hint_as_the_message`（断言 `[CONTINUE]` 出现在 message 中失败，实际 message 为 "I could not match that to anything in here..."）

记忆相关测试单独运行：`test_memory_provenance.py` + `test_memory_engine.py` = **75 passed**。

## 11. 审计结论

1. 任务书描述的架构目标与现有代码**方向一致**，但现有代码比任务书假设的更完整（检索栈、上下文分层、权限求交都已存在）。
2. 真正缺失的是一条**结构化事实链路**（P1），以及把已有能力**复用/接通**的三处（P0.4 的记忆注入、P2.3 的记忆检索、P4 的 Context Pack 接入）。
3. 现有设计有三条必须继承的硬约束：事件不可变、权限求交失败关闭、LLM 失败降级不丢数据。任何新组件都必须与之一致。
4. 数据库改动应尽量"新增表"而非"改老表"，以保证旧存档可直接打开。

详细改造计划见 `docs/memory-upgrade-plan.md`。
