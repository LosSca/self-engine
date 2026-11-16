# docs/archive/ — 已完成使命的一次性脚本与实验记录

这里的东西**不再维护，也不保证还能跑**。保留它们只为一件事：让后来的人能看到
某个结论是怎么得出的，而不是只能看到结论。

## 内容

| 文件 | 是什么 | 为什么归档 |
|------|--------|-----------|
| `fase3a_ab.md` / `fase3b_ab.md` | 两轮 narrator A/B 实验的设计、语料、指标与结论 | 实验已结束，结论已进入代码与 CHANGELOG |
| `ab_auditor.py` / `aggregate_ab.py` / `aggregate_3b.py` | 上述实验的评审与汇总脚本 | 需要本地模型与订阅代理，现在跑不出当时的数 |
| `prep_judge.py` / `prep_judge_3b.py` / `revalidate_3b.py` | 预生成 judge 输入、复验 3B 模型结论 | 同上，一次性 |
| `validate_fase2_cache.py` / `direct_cache_probe.py` | 验证 prompt cache 命中 | 一次性 |
| `litellm_wire_capture.py` | 抓 litellm 实际发出的报文 | 一次性排障工具 |
| `validate_fase3b_auditor.py` | 用固定语料验证审计引擎 | 已被 `backend/tests/engines/test_auditor_engine.py` 取代 |

## 边界

**这些不是运维脚本**。真会用到、因此留在 `backend/scripts/` 的是：
`sqlite_to_postgres.py`、`migrate_memory_facts.py`、`check_local_model.py`、
`e2e_local_narration.py`、`dump_traces.py`、`tic_harness.py`（被归档脚本共享的场景上下文加载器）。
