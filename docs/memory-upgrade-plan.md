# 记忆与知识系统改造计划

配套文档：`docs/memory-upgrade-audit.md`（审计报告）
基线：master @ f03b548，2 failed / 1819 passed / 8 skipped

## 0. 设计原则（从现有代码继承，不可违反）

1. **events 表只追加，不修改。** 新增能力一律读事件、写新表。
2. **权限失败关闭。** 任何"不确定谁能看见"的情况，返回空集而不是全集。
3. **LLM 失败不丢数据。** 模型不可用时退化为确定性降级，保留源事件。
4. **旧存档必须能直接打开。** 不改动 `events`/`scenario_knowledge`/`campaign_state` 任何既有表。
5. **可选依赖必须可关。** 新增的 Embedding / Neo4j 通道一律"未配置即跳过"，不得成为启动前提。

## 1. 文件级改动清单

### 1.1 新建

| 文件 | 职责 | 规模估计 |
| --- | --- | --- |
| `backend/app/models/memory.py` | `EvidenceType`/`FactStatus`/`MemoryType` 枚举，`MemoryFact`/`MemoryEvidence`/`MemoryEntity` 模型，状态机合法迁移表 `ALLOWED_TRANSITIONS` 与 `can_transition()` | ~260 行 |
| `backend/app/db/memory_fact_store.py` | `memory.db` 的 SQLite 存储：facts / evidence / entities / relations / ingest_cursor + `schema_version`，幂等 upsert，时间范围查询，冲突查询 | ~420 行 |
| `backend/app/services/memory_ingestion.py` | 写入管线：Collector → Extractor → Validator → ConflictDetector → Commit，幂等、可重试、提取与提交分离 | ~380 行 |
| `backend/app/services/context_pack.py` | 统一上下文构建层：`ContextPack` 结构与 `render(budget)`，优先级排序、去重、来源标注 | ~300 行 |
| `backend/tests/models/test_memory_models.py` | 状态机与模型校验 | ~120 行 |
| `backend/tests/db/test_memory_fact_store.py` | 幂等、事务、时间查询、冲突 | ~200 行 |
| `backend/tests/services/test_memory_ingestion.py` | 管线端到端、幻觉拦截、重试幂等 | ~260 行 |
| `backend/tests/services/test_context_pack.py` | 优先级、预算、去重、来源 | ~180 行 |
| `backend/tests/engines/test_memory_scoring_baseline.py` | 新旧评分算法对比（固定数据集 + 指标） | ~220 行 |
| `backend/tests/memory_eval/cases.json` | 评测数据集（≥ 40 条，覆盖六类场景） | 数据 |
| `backend/tests/memory_eval/test_memory_eval.py` | Recall@5/@10、Precision@K、MRR、泄漏率、事实准确率 | ~240 行 |

### 1.2 修改

| 文件 | 改动 | 风险 |
| --- | --- | --- |
| `backend/app/engines/memory_engine.py` | ① 抽出私有 `_render_scene_memory(...)` 消除 `build_context_window`（:835）与 `build_context_window_async`（:915）的重复；② `_score_crystal` 支持可插入权重策略；③ MEMORY 层改为相关度选择；④ 晶体增加 `memory_type`/`evidence_type` 字段 | 中（现有 28 个测试守护） |
| `backend/app/services/game_session.py` | `_extract_context_keywords`（:535-554）改用 `tokenize_text` | 低 |
| `backend/app/engines/player_context.py` | 保持 API 不变，内部改为消费 `ContextPack`；保留 `ContextSection.kind` 语义 | 低 |
| `backend/app/api/routes_game.py` | 叙事链路可选消费 `ContextPack`（默认走旧路径，flag 控制） | 中 |
| `backend/app/main.py` | 注册 `routes_memory` | 低 |

### 1.3 新增接口

`backend/app/api/routes_memory.py`（只读为主，不改游戏状态）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/memory/facts` | 查询事实：`campaign_id`, `subject`, `status`, `at`（时间点） |
| GET | `/api/memory/facts/{fact_id}` | 单条事实 + 全部证据 |
| GET | `/api/memory/entities` | 实体与别名检索 |
| GET | `/api/memory/overview` | 各状态计数、最近写入、管线游标（面板用） |
| POST | `/api/memory/ingest` | 手动触发管线（`apply=false` 默认 dry-run，返回候选） |

## 2. 数据库设计

**新建独立文件 `memory.db`**，默认路径 = `EVENT_DB_PATH` 同目录（可用 `MEMORY_DB_PATH` 覆盖）。理由：

- 老库零改动 → 旧存档 100% 兼容，回滚只需删文件；
- 事实表与本机 events 库解耦，未来可换后端；
- 与现有三个 store 的 `schema_version` 迁移范式一致。

```sql
CREATE TABLE memory_facts (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object_value TEXT NOT NULL,
  memory_type TEXT NOT NULL DEFAULT 'semantic',
  evidence_type TEXT NOT NULL DEFAULT 'inferred_fact',
  confidence REAL NOT NULL DEFAULT 0.5,
  importance REAL NOT NULL DEFAULT 0.5,
  status TEXT NOT NULL DEFAULT 'candidate',
  valid_from TEXT,
  valid_until TEXT,
  source_event_ids TEXT NOT NULL DEFAULT '[]',
  observer_ids TEXT NOT NULL DEFAULT '[]',
  supersedes_fact_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_facts_campaign ON memory_facts(campaign_id, subject_id, predicate);
CREATE INDEX idx_facts_status ON memory_facts(campaign_id, status);

CREATE TABLE memory_evidence (
  id TEXT PRIMARY KEY,
  fact_id TEXT NOT NULL,
  source_event_id TEXT NOT NULL DEFAULT '',
  evidence_type TEXT NOT NULL,
  observer_id TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.5,
  recorded_at TEXT NOT NULL,
  evidence_content TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_evidence_fact ON memory_evidence(fact_id);

CREATE TABLE memory_entities (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  entity_type TEXT NOT NULL DEFAULT 'unknown',
  aliases TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_entities_campaign ON memory_entities(campaign_id);

CREATE TABLE memory_relations (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object_id TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.5,
  valid_from TEXT,
  valid_until TEXT,
  source_event_ids TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE memory_ingest_cursor (
  campaign_id TEXT PRIMARY KEY,
  last_event_id TEXT NOT NULL DEFAULT '',
  last_created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
```

### 2.1 与任务书建议表名的差异

任务书建议十张表。按"不要为了满足表名而重复创建现有功能"的约束，实际新增四张 + 一张游标：

| 任务书表 | 处置 | 理由 |
| --- | --- | --- |
| `memory_events` | 不建 | `events` 表已承担，且已是唯一真相源 |
| `memory_episodes` | 不建 | 晶体（`MEMORY_CRYSTAL` 事件）已承担 |
| `memory_facts` | **建** | 唯一真缺口 |
| `memory_entities` | **建** | 实体别名索引需要 |
| `memory_relations` | **建** | 多跳关系查询需要 |
| `memory_evidence` | **建** | 证据溯源需要 |
| `memory_versions` | 不建 | 版本即 `supersedes_fact_id` 链 + `status`，独立表冗余 |
| `memory_embeddings` | 不建 | 复用 `retrieval/embedding_index.py` 的索引文件 |
| `memory_clusters` | 不建（暂缓） | 主题聚类先以 `related_entities` + 时间窗实现，验证有效后再落表 |
| `memory_procedures` | 不建（暂缓） | procedural 类型先落在 `memory_facts.memory_type='procedural'` |

## 3. 分阶段实施与验收

### 阶段 P0（现有系统修复）

**P0.1 统一 Tokenizer**
- 改 `game_session.py:535-554` → 调用 `tokenize_text`，保留旧签名与降级（tokenizer 异常时返回空集）。
- 验收：中文输入 `_extract_context_keywords("我走进森林") ` 返回非空集合；现有 `test_game_session.py` 全绿。

**P0.2 事实来源区分**
- `models/memory.py` 定义 `EvidenceType`；`MemoryCrystal` 增加 `evidence_type` 字段（默认 `llm_narrative`，来源为引擎结算事件时由事件类型映射为 `committed_action`）。
- `player_context.ContextSection.kind` 与 `EvidenceType` 建立映射，不改现有取值。
- 验收：新增用例断言 `narrator.response` 产生的晶体不会得到 `committed_action`；`combat.result` / `inventory` 会。

**P0.3 记忆评分扩展**
- `_score_crystal` 增加权重来源：`confidence`、`importance`、`related_entities ∩ scene_entities`、`observer`（NPC 在场）、`temporal_relevance`（时间距离衰减）、`task_relevance`（当前任务关键词）。
- **保留旧权重作为基线**：`MemoryScoring` 类有两个实现 `legacy` / `hybrid`，通过 `MEMORY_SCORING` 环境变量或构造参数切换，默认 `hybrid`。
- 验收：`test_memory_scoring_baseline.py` 在固定 20 条记忆的语料上输出两套排序，断言 hybrid 的 Recall@5 / MRR 不低于 legacy；旧测试若断言具体排序，改为断言"相关记忆排在前 K"。

**P0.4 永久记忆注入策略**
- MEMORY 层不再全量注入：`_select_relevant_permanent()` 按相关度选，但**常驻白名单**（`always_include=True` 的晶体、作者 canon、importance ≥ 阈值的）无条件保留。
- 无 query（`rag_on=False`）时保持旧行为全量注入 → 现有测试 `test_memory_tier_always_included_canonically` 不动。
- 验收：有 query 时注入条数 < 全量；GameState 分区永远排在记忆分区之前（断言分区顺序）。

### 阶段 P1（结构化事实与证据）

- 新增 `models/memory.py`、`db/memory_fact_store.py`、`services/memory_ingestion.py`。
- 状态机：`ALLOWED_TRANSITIONS` 只允许 `extracted→candidate→verified→active→superseded`，以及任意态 → `rejected`/`retracted`；`llm_narrative` 与 `player_statement` 证据最多到 `candidate`。
- 管线幂等键：`(campaign_id, subject_id, predicate, object_value, source_event_id)`；重复消费同一事件不产生新行。
- 未知处理：抽取器遇到不认识的谓词 → `predicate="UNKNOWN"`，不猜测补全。
- 验收：
  - 同一批事件跑两遍，facts 数量不变；
  - `llm_narrative` 内容无法进入 `active`；
  - 跑管线前后 `events` 表行数与内容 hash 不变；
  - 事务：写入中途失败不留半条事实。

### 阶段 P2（巩固与检索）

- **主题合并**：`_consolidate_tier` 增加分组策略 `group_by="theme"`（按 `related_entities` + 地点 + 时间窗聚类），保留 `group_by="time"` 旧策略为默认回退。
- **混合检索**：新增 `services/memory_retrieval.py`，复用 `retrieval/` 的 `LexicalRetriever`、`vector_retriever`、`reciprocal_rank_fusion`、`reranker`，把晶体与 MemoryFact 都映射为 `RetrievedDoc`；`RetrievalService.retrieve` 原样复用。
- **实体别名**：`memory_entities.aliases` + `resolve_entities(query)`，支持中英日别名归一。
- **时间检索**：`valid_from`/`valid_until`/`recorded_at`，`list_facts(at=...)` 返回该时刻有效事实。
- 验收：中/英/日/混排查询各 ≥ 3 条用例；"第二章是敌人、第三章是朋友"场景在 `at=第二章` 与 `at=now` 返回不同结果；无 Embedding 时检索仍返回结果且 `degraded` 有记录。

### 阶段 P3（NPC 独立认知）

- 保留 `witnessed_by` 与 `AUDIENCE_POLICY`；新增 `npc_beliefs`（作为 `memory_facts` 的一个视图：`observer_ids=[npc]` 且 `evidence_type=npc_observation|npc_dialogue`）。
- `visibility` 字段接入过滤路径（当前存了不用）。
- 验收：泄漏率测试——构造"艾琳执行秘密任务"，断言玩家检索、无关 NPC 检索均 0 命中，艾琳的朋友只得到"离开城市"，城主得到"秘密任务"。

### 阶段 P4（Context Pack）

- `services/context_pack.py` 输出任务书规定的 JSON 结构，附带 `sources` 与 `render(budget)`。
- `player_context.py` 内部改为消费 pack（API 不变），`game_session` 通过 `MEMORY_CONTEXT_PACK=1` 启用。
- 验收：Token Budget 断言（`len(render) <= budget`）；事实与推积分区；每条关键事实带 source；过期事实不覆盖当前 GameState。

## 4. 评测数据集

`backend/tests/memory_eval/cases.json` 结构：

```json
{
  "id": "temporal-relationship-shift",
  "tag": ["temporal", "entity"],
  "language": "zh",
  "setup": {"events": [...], "facts": [...]},
  "query": "艾琳现在和我是什么关系？",
  "at": "now",
  "expected_ids": ["fact-...", "crystal-..."],
  "forbidden_ids": ["fact-enemy-..."],
  "observer": "player"
}
```

覆盖六类（数量为下限）：事实准确性 8、长期记忆 8、时间推理 8、权限隔离 8、检索能力 10、工程质量 6（用代码路径断言，不进 cases.json）。

指标由 `retrieval/evaluation.py` 已有的 `recall_at_k`/`precision_at_k`/`reciprocal_rank` 计算，新增：
- `fact_accuracy`（期望事实被召回且状态正确的比例）
- `false_fact_rate`（`llm_narrative` 进入 active 的比例，期望 0）
- `leak_rate`（`forbidden_ids` 被返回的比例，期望 0）
- `compression_retention`（压缩前后关键事实保留率）

## 5. 开关与回滚

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `MEMORY_DB_PATH` | 同 events.db 目录 `memory.db` | 事实库路径 |
| `MEMORY_FACTS_ENABLED` | `1` | 关闭则完全不写事实库，系统回到当前行为 |
| `MEMORY_SCORING` | `hybrid` | 设 `legacy` 回到原评分 |
| `MEMORY_CONTEXT_PACK` | `0` | 设 `1` 让叙事链路走新 Context Pack |
| `MEMORY_CONSOLIDATION` | `time` | 设 `theme` 启用主题合并 |

回滚策略：
- 代码回滚：`git revert`（无老表结构变更，不存在数据不兼容）；
- 数据回滚：删除 `memory.db` 即可，events 表从未被写入新字段；
- 行为回滚：把上述开关设为回退值，不重启会话也可恢复。

## 6. 实施顺序与时间盒

| 阶段 | 内容 | 预估 |
| --- | --- | --- |
| P0 | 4 项修复 + 测试 | 一轮 |
| P1 | 模型 + 存储 + 管线 + 测试 | 一轮 |
| P2 | 主题合并 + 混合检索 + 实体 + 时间 | 一轮 |
| P3 | NPC 三分法与泄漏测试 | 一轮 |
| P4 | Context Pack + 接入 | 一轮 |
| 评测 | 数据集 + 指标报告 | 一轮 |

每阶段结束跑全量测试，并在 `docs/memory-upgrade-progress.md` 记录：改动文件、测试结果、未完成项。

## 7. 明确不做的事

- 不重写 `MemoryEngine`：保留四层与 `crystallize/cascade` 结构，只替换内部策略。
- 不改 `events` 表结构，不加版本号。
- 不引入 Embedding 默认依赖，不引入 Neo4j 默认依赖。
- 不做 Cross-Encoder 精排（任务书标注"后续可选"）。
- 不做前端改动（除必要文档说明）。
