# 自定义 OpenAI 兼容供应商

设置面板里除了 DeepSeek / Anthropic / OpenAI / 本地 NInfer，还有一项「自定义兼容接口」。
它让任何说 OpenAI chat-completions 方言的服务（北辰、硅基流动、自建网关、别的中转站）
不必改代码就能接进来。

## 怎么配

`.env` 里填三项（`.env` 已在 gitignore 里，提交不会带上密钥）：

```
CUSTOM_LLM_BASE_URL=https://beichenapi.com/v1
CUSTOM_LLM_API_KEY=sk-...
CUSTOM_LLM_MODEL=deepseek-v4.1-flash
```

重启后端，打开设置面板 → 模型服务商 → 「自定义兼容接口」→ 在输入框里填模型名 → 应用设置。
只填了 URL 和 KEY 没填 MODEL 也能用：面板里的输入框会成为模型名的唯一来源。

## 它是怎么走的

- 面板选「自定义兼容接口」时，前端提交的是 `provider: "openai"`。
  后端 `apply_model_policy()` 发现 `custom_route_available()` 为真，就不去查内置的
  `_AUXILIARY_MODELS` 表（那张表只认得自家模型），而是让选定模型同时担任叙事与辅助两个角色——
  对别人的模型目录猜「辅助型号」只会猜错。
- `LLMRouter._get_api_base()` / `_get_api_key()` 在 `OPENAI` + 自定义端点已配置时
  返回 `CUSTOM_LLM_BASE_URL` / `CUSTOM_LLM_API_KEY`；`_build_model_string()` 这时返回
  `openai/<model>`，litellm 的 OpenAI 传输层会剥掉前缀、把裸模型名 POST 到 `api_base`。
- 若 `OPENAI_PROXY_URL` 也配了（CLIProxyAPI），**代理优先**：那是本机既有的人工路由，
  自定义端点只在没有代理时接手，不会悄悄顶掉它。

## 接口里能看到什么

`GET /api/settings` 会多两个字段：

| 字段 | 含义 |
|------|------|
| `custom_route_available` | `.env` 里 URL 与 KEY 都齐了才为 `true`；面板靠它决定显不显示这一项 |
| `custom_model` | `.env` 里的 `CUSTOM_LLM_MODEL`，没填则为 `null` |

**密钥与 base URL 都不会出现在响应里**（有测试守着：`tests/api/test_settings_custom_route.py`）。

## 测试

- `tests/engines/test_llm_router_custom_route.py`（7 项）：未配置时路由不存在、配置后 `api_base`/`api_key`
  正确、模型串带 `openai/` 前缀、没有自定义端点时 OpenAI 保持原样、选定模型兼任双角色、
  之后一次无关的 `apply_model_policy` 不会把选定模型清空。
- `tests/api/test_settings_custom_route.py`（3 项）：`/api/settings` 报告可用性但不泄露密钥与地址、
  端点缺失时路由关闭、`POST /api/settings` 能把路由切到新模型。

## 没做的事

- 面板里不能改 base URL 与密钥——它们只从 `.env` 读。把密钥经浏览器往返一次没有收益，
  只会让它多一个泄露面。
- 没有「测试连接」按钮；失败会照常走既有的降级路径（叙事退回确定性文本，错误进调试台）。
