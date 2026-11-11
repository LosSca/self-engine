# Agent 角色与模型路由

引擎把模型工作分成三个**角色**。角色是配置项，不是代码里的常量：换模型只改环境变量。

| 角色 | 职责 | 典型任务 |
| --- | --- | --- |
| `narrative` | 叙事与 NPC 对话 | `narrate` |
| `system` | 信息分类、实体识别、冲突检测、提案规划 | `classify`、`plan_ops` |
| `memory` | 记忆整理（预留） | — |

## 配置

```bash
AGENTS_NARRATIVE_PROVIDER=anthropic
AGENTS_NARRATIVE_MODEL=<模型 ID>
AGENTS_SYSTEM_PROVIDER=openai
AGENTS_SYSTEM_MODEL=<模型 ID>
AGENTS_SYSTEM_REASONING_EFFORT=high   # low | medium | high
AGENTS_SYSTEM_TIMEOUT=45
AGENTS_SYSTEM_ENABLED=1
```

- **未配置（缺少 provider 或 model）的角色 = 复用主模型**，零配置行为与升级前完全一致。
- `AGENTS_<ROLE>_ENABLED=0` 关闭角色；调用方按降级链处理。
- 降级链：`system → narrative`、`memory → narrative`、`narrative → 无`、
  仍失败则叙事回落 `TemplateNarrator`，规则结算完全不受影响。
- 模型名**只在环境里**，业务代码不出现任何具体模型标识。

## 调用审计

每次调用写一行 `agent_calls`（schema v3 新表）：role、task、provider、model、status、
degraded、input/output token、elapsed_ms、error。写入是独立事务且吞掉异常——
遥测永远不能让一个回合失败。

- `GET /api/v1/agents` — 每个角色由哪个模型回答（**不含任何密钥**）
- `GET /api/v1/campaigns/{id}/agent-calls?role=&limit=` — 本战役的调用记录
- 调试台「模型调用」页签展示这两者

## 按需调用

确定性优先。`SystemAgent.classify()` 先跑本地规则；只有「可能重要的未证实陈述」
才会去问模型。普通行动（移动、观察、拾取）不经过任何旗舰模型。
