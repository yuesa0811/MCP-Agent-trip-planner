"""LLM服务模块"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from ..config import Settings, get_settings

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI


def create_langchain_llm(settings: Settings | None = None) -> ChatOpenAI:
    """创建供 LangChain 规划流程使用的标准工具调用客户端。

    每个规划任务独立创建并关闭客户端；关闭 SDK 自动重试，
    由业务层统一控制重试次数。
    """
    settings = settings if settings is not None else get_settings()
    model_config = settings.get_llm_config()
    if not model_config["api_key"].strip():
        raise ValueError("请配置 LLM_API_KEY 或 OPENAI_API_KEY")

    from langchain_openai import ChatOpenAI

    # DeepSeek 默认思考模式拒绝 ToolStrategy 所需的强制工具调用。
    # 本阶段仅对官方 DeepSeek 地址关闭思考模式，其他供应商不传此参数。
    extra_body = None
    if urlsplit(model_config["base_url"]).hostname == "api.deepseek.com":
        extra_body = {"thinking": {"type": "disabled"}}

    return ChatOpenAI(
        **model_config,
        temperature=0,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
        use_responses_api=False,
        extra_body=extra_body,
    )
