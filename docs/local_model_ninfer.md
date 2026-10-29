# 本地模型接入（NInfer）

Project Lunar 支持把 **本机 NInfer** 当作一个 LLM provider 使用。NInfer 在本机
以 OpenAI 兼容协议提供服务，因此接入方式是在现有 `LLMRouter` 里增加一个
`local` provider，而不是另起一套调用链。

## 1. 架构位置

```
前端设置面板 / .env
        │  provider="local"
        ▼
LLMRouter (backend/app/engines/llm_router.py)
        │  LLMProvider.LOCAL
        ▼
litellm acompletion(model="openai/<alias>", api_base=http://127.0.0.1:8094/v1)
        ▼
NInfer (ninfer-serve-89.exe, RTX 4070 SUPER 12GB)
```

- 模型串固定为 `openai/<alias>`：litellm 会把前缀去掉，把裸 alias 发给
  `api_base`，也就是 NInfer 的 `--model-id`。
- 本地 provider 不发 `reasoning_effort`，并带 `drop_params=True`：NInfer 对
  OpenAI 参数校验严格，未知参数会让整单失败。
- 本地 provider 不走 CLIProxyAPI 的非流式 workaround，保留原生 SSE 流式输出。

## 2. 配置项

`backend/app/config.py`（也可用同名环境变量或仓库根 `.env` 覆盖）：

| 配置 | 环境变量 | 默认值 |
| --- | --- | --- |
| `local_llm_base_url` | `LOCAL_LLM_BASE_URL` | `http://127.0.0.1:8094/v1` |
| `local_llm_model` | `LOCAL_LLM_MODEL` | `qwen3.8-27b` |
| `local_llm_api_key` | `LOCAL_LLM_API_KEY` | 空（启动时由 `api-key.txt` 填入 `.env`） |
| `local_llm_context_window` | `LOCAL_LLM_CONTEXT_WINDOW` | `49152`（KV window 32768 + retrieval 16384） |

仓库根 `.env` 已写入本机值（gitignored）；`.env.example` 只有占位符。

## 3. 启动 NInfer

```powershell
E:\ninfer\start-local.ps1      # 后台启动，日志在 E:\ninfer\logs
Invoke-RestMethod http://127.0.0.1:8094/health
```

服务参数：`--host 127.0.0.1 --port 8094 --model-id qwen3.8-27b`，API key 取
`E:\ninfer\api-key.txt` 首行。显卡 12GB，只支持文本；不要开
`--structured-output`（当前版本会崩溃）。

## 4. 使用方式

### 默认行为（不需要任何开关）

`ENGINE_NARRATOR` 默认 `auto`：只要 `LOCAL_LLM_BASE_URL` 与
`LOCAL_LLM_API_KEY` 都有值，叙事就走本机模型；两者缺一才退回模板叙事。
决策集中在 `backend/app/services/narrator_factory.py`，两条链路
（`/api/v1/.../actions` 与 `/api/game/action`）共用同一个判断，
不再各写一套。

| 变量 | 作用 |
| --- | --- |
| `ENGINE_NARRATOR` | `auto`（默认）/ `llm` / `template` |
| `ENGINE_LLM_PROVIDER` | 留空＝优先本机；`local` / `deepseek` / `openai` / `anthropic` 强制 |
| `ENGINE_UNIFIED_NARRATOR` | 只覆盖聊天链路，取值同上 |

### 显存被占用时

如果另一程序已经占着显存（游戏、渲染器），标准启动脚本会报
`requested Engine runtime reservation requires ... but only ... are available`。
改用低显存启动器，模型相同，只是设备预算更小：

```powershell
E:\ninfer\start-local-lowvram.bat   # kv-capacity 24576 / 窗口 24576 + 检索 8192
```

有效上下文从 49152 降到 32768——叙事提示词只有几百 token，实际不受影响。


### 前端

设置面板 → Provider 选「本地 NInfer」→ 模型 `qwen3.8-27b`。选择会随请求
body 的 `provider`/`model` 传给后端。

### 引擎叙事

```powershell
$env:ENGINE_NARRATOR = "llm"
$env:ENGINE_LLM_PROVIDER = "local"
```

`ENGINE_NARRATOR=llm` 时，行动处理服务在提交状态前用 LLM 生成叙事；模型失败
或超时会记录 `narrative.failed` 域事件，**已提交的游戏状态不受影响**，接口
返回 `narrative.status="failed"`。

### 诊断端点

`GET /api/v1/local-model` 返回 `configured / base_url / model / api_key_set /
reachable / available_models / detail`。它只读 `/v1/models`，不会触发推理。

## 5. 验证

```powershell
cd E:\shujia\backend
.\.venv\Scripts\python.exe scripts\check_local_model.py
.\.venv\Scripts\python.exe -m pytest tests/engines/test_llm_router_local.py tests/api/test_local_model_status.py -q
```

`scripts/e2e_local_narration.py` 会跑完整一局：建世界 → 绑定战役 → 观察 /
拾取钥匙 / 开门，逐回合检查叙事来自模型且状态已提交，最后核对门、背包、
任务与诊断结果；退出码 0 才算通过。

`check_local_model.py` 会依次验证：`/v1/models` 可达 → 非流式补全 → 流式补全，
全部走 `LLMRouter`，与游戏实际调用一致。

## 6. 故障与降级

| 现象 | 处理 |
| --- | --- |
| `LOCAL_LLM_API_KEY is not configured` | 把 `E:\ninfer\api-key.txt` 首行写进 `.env` 的 `LOCAL_LLM_API_KEY`，重启后端 |
| `reachable: false` + 连接失败 | 先 `start-local.ps1`，再 `GET /health`；模型加载需要时间 |
| 叙事超时 | `LLMNarrator` 默认 30s 超时，返回 `narrative.failed`，状态已提交；可改回 `ENGINE_NARRATOR=template` |
| 服务停止后游戏卡住 | 旧 `/api/game` 流式接口会失败；v1 行动接口在 `ENGINE_NARRATOR=template` 下完全不需要 LLM |

## 7. 已知限制

- 本地模型只用于叙事/对话生成，**不参与规则判定**：状态变化由
  `rules_engine` / `event_engine` 决定，LLM 文本不能修改权威状态。
- 模型为 27B 量化权重，12GB 显存下上下文 49152；超长上下文需在启动脚本调整
  `NINFER_KV_WINDOW` / `NINFER_KV_RETRIEVE`，并同步 `LOCAL_LLM_CONTEXT_WINDOW`。
- 未接入图像能力（NInfer 文本 only）。
