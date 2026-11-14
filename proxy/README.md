# CLIProxyAPI Subscription Proxy

Routes Anthropic and OpenAI-compatible requests through authenticated subscriptions. The engine uses it for Claude models and for `gpt-5.6-sol`.

> This directory is a *shell* around the upstream binary: it ships `cliproxyapi/config.yaml` and `cliproxyapi/start-proxy.bat`, nothing else. The Python proxy that used
> to live here (`server.py` / `run.py` / `auth.py`) was removed in the import commit; the binary below
> is downloaded separately and is **not** part of this repository.

## CLIProxyAPI (recommended)

Uses [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), a Go binary that exposes Anthropic and OpenAI-compatible local APIs backed by OAuth credentials.

### Setup

1. Download the release for your platform from
   [router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) and put the
   executable in `proxy/cliproxyapi/` (the launch script expects `cli-proxy-api.exe`).
2. Copy `config.example.yaml` to `config.yaml` if you do not have one and set `port:` to
    `8318` — or keep your own port and match it in `.env` below. The shipped example uses
    `8317`, so the two files disagree on purpose: the port is yours to pick.
3. Authenticate providers that are not already configured, then start it:

```bash
cd proxy/cliproxyapi
./cli-proxy-api.exe -claude-login -config config.yaml
./cli-proxy-api.exe -codex-login -config config.yaml

# Start the proxy server (start-proxy.bat does the same on Windows)
./cli-proxy-api.exe -config config.yaml
```


### .env config

```
ANTHROPIC_PROXY_URL=http://127.0.0.1:8318
ANTHROPIC_PROXY_KEY=lunar-proxy-key
OPENAI_PROXY_URL=http://127.0.0.1:8318/v1
OPENAI_PROXY_KEY=lunar-proxy-key
```

### Verify

```bash
# List models
curl -s http://127.0.0.1:8318/v1/models -H "Authorization: Bearer lunar-proxy-key" | python -m json.tool

# Test Sonnet 4.6
curl -s -X POST http://127.0.0.1:8318/v1/messages \
  -H "Authorization: Bearer lunar-proxy-key" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":50,"messages":[{"role":"user","content":"Say hi"}]}'

# Test GPT-5.6 Sol
curl -s -X POST http://127.0.0.1:8318/v1/chat/completions \
  -H "Authorization: Bearer lunar-proxy-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.6-sol","max_tokens":50,"messages":[{"role":"user","content":"Say hi"}]}'
```

### Notes

- The backend's `LLMRouter` falls back to non-streaming when using the proxy (CLIProxyAPI streaming adds extra fields that confuse litellm's SSE parser). Text arrives as a single chunk instead of token-by-token.
- Auth credentials are saved in `~/.cli-proxy-api/`. Token auto-refreshes every 15 minutes.
- The API key (`lunar-proxy-key` in the examples) is whatever you put in `config.yaml`; it must match `ANTHROPIC_PROXY_KEY` / `OPENAI_PROXY_KEY` in `.env`.
- An existing CLIProxyAPI instance may use a different local port and key through the untracked `.env`.
