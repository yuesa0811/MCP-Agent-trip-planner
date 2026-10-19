"""测试进程使用虚拟配置，不依赖开发者 .env 中的密钥。"""

import os

# 在导入 app.config 的全局 Settings 前设置；仅影响本次 pytest 进程。
os.environ.update({
    "DEBUG": "false",
    "LLM_API_KEY": "",
    "LLM_BASE_URL": "",
    "LLM_MODEL_ID": "",
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "https://example.invalid/v1",
    "OPENAI_MODEL": "test-model",
    "AMAP_API_KEY": "test-amap-key",
    "UNSPLASH_ACCESS_KEY": "",
    "UNSPLASH_SECRET_KEY": "",
    "JWT_SECRET_KEY": "test-only-jwt-secret-with-at-least-32-characters",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "120",
    "DATABASE_URL": "",
    "AMAP_MCP_COMMAND": "uvx",
    "MCP_TIMEOUT_SECONDS": "30",
    "LLM_TIMEOUT_SECONDS": "90",
    "LANGSMITH_TRACING": "false",
    "LANGCHAIN_TRACING_V2": "false",
})
