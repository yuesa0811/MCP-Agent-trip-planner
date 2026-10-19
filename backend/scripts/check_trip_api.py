"""真实规划联调：python -m scripts.check_trip_api --city 北京 --days 1。

使用已有 .env，注册一次性普通用户并调用真实模型与高德，通过兼容接口执行
完整四阶段，再从该用户的历史详情核对任务状态和真实工具记录。普通 pytest
不执行此脚本；联调用户与任务会保留在当前数据库，便于管理员页面回看。
"""

import argparse
import asyncio
import secrets
from datetime import date, timedelta
from time import perf_counter
from uuid import uuid4

import httpx


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="北京")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--start-date", default=date.today().isoformat())
    args = parser.parse_args()
    # 延迟导入，让 --help 不依赖本机 .env；真实执行仍按项目配置启动。
    from app.api.main import app
    from app.database import init_database
    from app.models.schemas import TripPlanResponse

    start = date.fromisoformat(args.start_date)
    request = {
        "city": args.city, "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=args.days - 1)).isoformat(),
        "travel_days": args.days, "transportation": "公共交通",
        "accommodation": "经济型酒店", "preferences": ["历史文化"],
    }
    username = f"integration_{uuid4().hex[:12]}"
    password = secrets.token_urlsafe(24)
    init_database()
    print("开始真实 POST /api/trip/plan 联调", flush=True)
    started = perf_counter()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        register = await client.post(
            "/api/auth/register", json={"username": username, "password": password},
        )
        if register.status_code != 201:
            print(f"注册联调用户失败，HTTP {register.status_code}")
            raise SystemExit(1)
        login = await client.post(
            "/api/auth/login", json={"username": username, "password": password},
        )
        if login.status_code != 200:
            print(f"联调用户登录失败，HTTP {login.status_code}")
            raise SystemExit(1)
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        response = await client.post("/api/trip/plan", json=request, headers=headers)
        print(f"HTTP {response.status_code}，耗时 {perf_counter() - started:.1f} 秒")
        if response.status_code != 200:
            # 路由中的受控业务错误已脱敏，不输出任何供应商原始异常。
            payload = response.json()
            print(payload.get("error_code", "REQUEST_ERROR"), payload.get("message", "请求未通过校验"))
            raise SystemExit(1)
        result = TripPlanResponse.model_validate(response.json())
        assert result.success and result.data is not None
        assert result.data.city == args.city and len(result.data.days) == args.days
        assert all(day.attractions and all(item.poi_id for item in day.attractions) for day in result.data.days)
        history = await client.get("/api/trip/tasks?page=1&page_size=1", headers=headers)
        history.raise_for_status()
        task_id = history.json()["items"][0]["task_id"]
        detail = await client.get(f"/api/trip/tasks/{task_id}", headers=headers)
        detail.raise_for_status()
        task = detail.json()
        assert task["status"] == result.status and task["plan"]["city"] == args.city
        assert task["tool_calls"], "成功行程应包含真实 MCP 工具记录"
        print(f"状态={result.status}；行程={len(result.data.days)} 天；真实天气={len(result.data.weather_info)} 天")
        print("景点：", "、".join(item.name for day in result.data.days for item in day.attractions))
        print("资料提示：", result.warnings)
        print(f"演示记录：用户={username}；任务={task_id}；工具调用={len(task['tool_calls'])} 次")


if __name__ == "__main__":
    asyncio.run(main())
