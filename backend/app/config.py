# backend/app/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    anthropic_proxy_url: str = ""
    anthropic_proxy_key: str = "proxy"
    openai_proxy_url: str = ""
    openai_proxy_key: str = "proxy"
    # Local OpenAI-compatible inference server (NInfer and friends). Selecting
    # provider "local" sends chat completions to this base URL instead of a cloud
    # provider; the NInfer defaults below match the local RTX 4070 SUPER launch
    # (E:\ninfer\start-local.ps1: --host 127.0.0.1 --port 8094 --model-id qwen3.8-27b).
    local_llm_base_url: str = "http://127.0.0.1:8094/v1"
    local_llm_model: str = "qwen3.8-27b"
    local_llm_api_key: str = ""
    # NInfer on this box is launched with --kv-capacity 34304 and NINFER_KV_WINDOW
    # 32768, so the model can attend over window + retrieval = 49152 tokens.
    local_llm_context_window: int = 49152
    # OpenAI-compatible endpoint + credentials for one *custom* provider. The
    # router builds "openai/<model>" against this base URL, so any vendor that
    # speaks the OpenAI chat-completions dialect (beichenapi, siliconflow, a
    # gateway, DeepSeek itself) works by filling these three in. Empty URL or
    # key means the custom provider is not offered at all.
    custom_llm_base_url: str = ""
    custom_llm_api_key: str = ""
    custom_llm_model: str = ""
    debug: bool = False
    # Root directory for scenario assets (character cards, art). Served at /media.
    media_dir: str = "../local/media"

    model_config = {"env_file": [".env", "../.env"]}


settings = Settings()
