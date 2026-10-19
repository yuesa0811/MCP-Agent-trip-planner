"""显式执行真实联调：在 backend 下运行 python -m scripts.check_integrations --mcp --llm。

会使用 .env 中的密钥产生真实 API 调用；普通 pytest 不执行本脚本。
只验证接入能力，不代表四个 Agent 已迁移，也不保存为用户行程。
"""

import argparse
import asyncio
import json

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import tool

from app.models.schemas import TripPlan
from app.services.llm_service import create_langchain_llm
from app.services.mcp_service import open_amap_tools


class IntegrationCheckError(RuntimeError):
    """仅包含本脚本生成的固定诊断信息，可以安全展示。"""


def report_error(exc):
    # MCP 的 AnyIO 上下文可能包装为异常组；兼容 Python 3.10，不直接引用 ExceptionGroup。
    if children := getattr(exc, "exceptions", None):
        for child in children:
            report_error(child)
    elif isinstance(exc, IntegrationCheckError):
        print(str(exc))
    else:
        print(f"{type(exc).__name__}; HTTP status={getattr(exc, 'status_code', None)}")


async def check_mcp():
    async with open_amap_tools() as tools:
        by_name = {item.name: item for item in tools}
        print("发现 MCP 工具:", ", ".join(by_name))
        checks = [
            ("maps_text_search", {"keywords": "故宫", "city": "北京"}, ("pois",)),
            ("maps_weather", {"city": "北京"}, ("forecasts", "lives")),
        ]
        for name, arguments, result_keys in checks:
            if name not in by_name:
                raise IntegrationCheckError(f"未发现预期工具 {name}，请核对 MCP 服务版本")
            current = by_name[name]
            print(name, "参数 schema:", json.dumps(current.args_schema, ensure_ascii=False))
            blocks = await current.ainvoke(arguments)
            # 当前高德 MCP 用 text block 返回 JSON；不打印完整查询结果。
            payloads = [json.loads(block["text"]) for block in blocks if block.get("type") == "text"]
            if any(isinstance(data, dict) and str(data.get("status")) == "0" for data in payloads):
                raise IntegrationCheckError(f"{name} 返回业务错误，请检查高德 Key 类型、权限或配额")
            if not any(isinstance(data, dict) and any(data.get(key) for key in result_keys) for data in payloads):
                raise IntegrationCheckError(f"{name} 未返回预期非空结果，请检查服务响应格式")
            print(name, "真实查询通过")


@tool
def probe_city(city: str) -> str:
    """接入验证工具：原样返回城市，不查询任何外部信息。"""
    return city


async def check_llm():
    model = create_langchain_llm()
    try:
        reply = await model.bind_tools([probe_city], tool_choice="probe_city").ainvoke(
            "请调用 probe_city，city 参数为北京。"
        )
        if not reply.tool_calls or reply.tool_calls[0]["name"] != "probe_city":
            raise IntegrationCheckError("模型未返回标准 tool_calls")
        if probe_city.invoke(reply.tool_calls[0]["args"]) != "北京":
            raise IntegrationCheckError("模型工具参数不符合预期")
        print("模型标准工具调用通过")

        # 固定样例仅检查模型协议和 Pydantic 解析，不检验行程推荐质量。
        sample = {
            "city": "北京", "start_date": "2026-09-29", "end_date": "2026-09-29",
            "days": [{
                "date": "2026-09-29", "day_index": 0, "description": "接入验证样例",
                "transportation": "步行", "accommodation": "无需住宿",
                "attractions": [], "meals": [],
            }],
            "weather_info": [], "overall_suggestions": "仅用于接入验证",
        }
        agent = create_agent(model, tools=[], response_format=ToolStrategy(TripPlan, handle_errors=False))
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "使用 TripPlan 输出工具原样提交以下样例，不补充任何信息：" + json.dumps(sample, ensure_ascii=False)}]},
            {"recursion_limit": 4},
        )
        plan = result["structured_response"]
        if not isinstance(plan, TripPlan) or plan.city != "北京" or len(plan.days) != 1:
            raise IntegrationCheckError("TripPlan 结构化输出不符合预期")
        print("TripPlan 结构化输出通过")
    finally:
        await model.root_async_client.close()
        model.root_client.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", action="store_true", help="发现工具并真实查询一次景点和天气")
    parser.add_argument("--llm", action="store_true", help="验证模型工具调用和 TripPlan 输出")
    args = parser.parse_args()
    if not (args.mcp or args.llm):
        parser.error("请指定 --mcp 或 --llm；外部接口可能计费")
    for enabled, name, check in ((args.mcp, "MCP", check_mcp), (args.llm, "LLM", check_llm)):
        if enabled:
            print(f"开始 {name} 真实联调", flush=True)
            try:
                await asyncio.wait_for(check(), timeout=180)
            except Exception as exc:
                # 外部异常可能包含请求地址或凭证，默认只输出类型及 HTTP 状态码。
                print(f"{name} 联调失败:")
                report_error(exc)
                raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(main())
