# 基于 MCP 的多智能体旅行规划系统

基于 LangChain、FastAPI 与 Vue 3 的全栈旅行规划项目，通过高德地图 MCP 查询景点、天气与酒店，根据用户偏好生成结构化多日行程。

本项目从 HelloAgents 教程项目逐步迁移。二次开发第 1–9 步已经完成：四个 Agent 与地图外围接口均已迁移，运行时不再依赖 HelloAgents；SQLite、用户认证、后台任务、本人历史、真实工具监控、前端完整流程及轻量管理员后台已经可用，并已通过独立环境复现和真实服务联调。

## 技术与请求流程

- 后端：Python 3.11、FastAPI、Pydantic、LangChain；`create_agent` 内部使用 LangGraph，业务层顺序编排四个角色。
- 工具：`langchain-mcp-adapters` 连接独立高德 MCP 会话，服务端固定为 `amap-mcp-server==0.1.11`；统一包装层记录每次真实调用的状态、耗时和安全摘要。
- 数据与认证：SQLite、SQLAlchemy 2.x、Argon2 密码哈希、两小时有效的 HS256 JWT；任务保存原始/编辑结果、失败原因及工具记录。
- 前端：Vue 3、TypeScript、Vue Router、Ant Design Vue、Axios；提供注册登录、路由守卫、任务轮询、工具过程、本人历史、管理员全局任务、编辑保存和导出。

兼容入口 `POST /api/trip/plan` 也要求登录：先创建可回看的任务，再同步等待同一任务执行链完成，最后按原有 `success/message/data/status/warnings` 结构返回。

前端主流程为：登录用户提交 `POST /api/trip/tasks` → 数据库保存 `queued` → 返回 `202 + task_id` → 跳转 `/result/:taskId` → 每秒查询任务阶段及工具记录 → 完成后展示数据库中的 `TripPlan`。每次真实 MCP 调用先写 `running`，结束后写 `succeeded / empty / failed`；刷新结果页或重新登录都能从数据库恢复。

前三个角色各使用有限的工具子集，每个查询角色最多调用 3 次；行程角色根据收集的数据生成计划，不直接调用地图工具。路线规划和地理编码属于单独的外围 API，不会在四阶段规划中自动调用。

无有效景点、模型失败或行程无效时返回失败；天气或酒店缺失时返回 `degraded`，并说明原因。输出最多修复一次。天气只采用真实预报，价格和预算仍为估算。

## 启动后端

本次验证使用 Windows、Python 3.11、Node.js 26.5.1。以下 PowerShell 命令从项目根目录执行；已有 `.venv` 可跳过创建，已有 `.env` 时保留原配置。

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -X utf8 -m pip install -r requirements-lock.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

`requirements.txt` 声明业务直接依赖，`requirements-dev.txt` 增加测试工具；`requirements-lock.txt` 是验证过的 Windows / Python 3.11 开发环境快照。学习时推荐按锁定文件安装。

从旧版环境迁移时，可在停止后端后精确同步，移除旧框架及不再需要的包：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m uv pip sync requirements-lock.txt --python .\.venv\Scripts\python.exe
```

编辑 `backend/.env`：

| 配置 | 用途 |
| --- | --- |
| `LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID` | 支持标准工具调用的 OpenAI-compatible 模型；为空时逐项回退到 `OPENAI_*` |
| `AMAP_API_KEY` | 高德 **Web 服务** Key |
| `UNSPLASH_ACCESS_KEY` | 可选图片服务；未配置时图片接口明确返回 503 |
| `AMAP_MCP_COMMAND` | 默认自动寻找当前环境的 `uvx`，也可填其完整路径 |
| `MCP_TIMEOUT_SECONDS` | 单次 MCP 请求超时，默认 30 秒 |
| `AMAP_POI_DETAIL_INTERVAL_SECONDS` | 同一次搜索补详情的间隔，默认 0.4 秒；不是全局限流 |
| `LLM_TIMEOUT_SECONDS / PLANNING_TIMEOUT_SECONDS` | 单次模型 / 整次规划超时，默认 90 / 600 秒 |
| `JWT_SECRET_KEY` | 至少 32 位的随机字符串；没有可用默认值，缺失时认证接口返回 503 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 访问令牌有效期，默认 120 分钟 |
| `DATABASE_URL` | 可选；默认固定为 `backend/data/trip_planner.db` |

配置固定读取本项目 `backend/.env`，不再读取相邻项目；进程变量优先于文件。[配置机制](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)

可以生成本机 JWT 密钥后手动填入 `backend/.env`，命令只打印随机值，不会修改配置文件：

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

对于 `api.deepseek.com`，模型工厂关闭思考模式以兼容强制结构化输出。示例中的每阶段工具调用上限、输出修复次数也可在限定范围内调整。

```powershell
# 防止当前终端已有 DEBUG=release 等非布尔变量干扰配置
$env:DEBUG = 'false'
.\.venv\Scripts\python.exe -X utf8 -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

访问 [API 文档](http://localhost:8000/docs)。健康检查只检查进程或本地配置，不代表外部服务已经连通。

当前任务执行使用 FastAPI 进程内 `BackgroundTasks`，请只启动一个后端 worker，执行规划时不要开启热重载。它适合本地学习和演示，不提供消息队列的断点续跑能力；服务重启时，遗留的 `queued/running` 任务会变为 `failed / SERVICE_RESTARTED`，已完成历史继续保留。[FastAPI BackgroundTasks 文档](https://fastapi.tiangolo.com/tutorial/background-tasks/)

仓库中的 `backend/run.py` 和 `python -m app.api.main` 均已固定关闭热重载；推荐仍使用上面的 Uvicorn 命令，便于明确看到监听地址和启动参数。

首次启动会创建 `users`、`trip_tasks`、`tool_calls` 三张表，并为 SQLite 开启外键和 WAL。规划任务显式携带自己的任务 ID，工具记录不会依赖全局“当前任务”；参数和结果只保存有长度限制的脱敏摘要，结构化输出工具不记作高德调用。独立地图接口不属于某个规划任务，第一版不写入任务后台。管理员不通过公开注册创建，运行下面的交互命令并输入两次密码：

```powershell
$env:DEBUG = 'false'
.\.venv\Scripts\python.exe -X utf8 -m scripts.create_admin your_admin_name
```

公开注册始终创建普通用户。用户名会去掉首尾空格并转为小写，只允许 3–32 位字母、数字和下划线；密码为 8–128 位且不会自动去空格。密码只保存 Argon2 哈希，JWT 密钥和数据库连接地址不会打印到启动日志。

首次 MCP 查询时，`uvx` 下载独立服务端环境，后续复用缓存。实际启动命令由 `mcp_service.py` 生成，通过 `scripts/run_amap_mcp.py` 屏蔽 0.1.11 公交工具写入 stdout 的调试输出；SDK 协议输出保持不变，并使用 UTF-8。后端自身无需安装 `fastmcp` 或 `amap-mcp-server`。[MCP stdio 协议要求](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)

## 启动前端

另开终端，从项目根目录执行：

```powershell
cd frontend
npm.cmd ci
if (!(Test-Path .env)) { Copy-Item .env.example .env }
npm.cmd run dev
```

编辑 `frontend/.env` 中的 `VITE_API_BASE_URL`、`VITE_AMAP_WEB_JS_KEY` 与 `VITE_AMAP_SECURITY_JS_CODE` 后重启前端。前端使用高德 **Web 端 JS API** Key，与后端 Key 分开配置。

打开 [首页](http://localhost:5173)。未登录会进入 `/login`；注册或登录后可新建规划，也可在 `/history` 按状态查看本人历史。结果页按任务 ID 恢复并展示真实阶段、工具状态、耗时和错误；保存编辑会写回后端。管理员登录后还会看到 `/admin/tasks` 入口，可按用户和状态查看全局任务及调用过程。所有业务请求共用 Axios 客户端并自动附带 Bearer Token，401 会清除本地会话并返回登录页。

## 当前 API

| 接口 | 用途 |
| --- | --- |
| `POST /api/auth/register` | JSON 注册普通用户，返回公开用户字段 |
| `POST /api/auth/login` | JSON 登录，返回 Bearer Token、有效秒数和用户信息 |
| `GET /api/auth/me` | 使用 `Authorization: Bearer <token>` 恢复当前用户 |
| `POST /api/trip/tasks` | 登录用户提交后台规划任务，返回 `202`、任务 ID 和 `queued` 状态 |
| `GET /api/trip/tasks` | 本人历史列表；支持 `page`、`page_size` 和 `status` 筛选 |
| `GET /api/trip/tasks/{task_id}` | 本人任务的请求、阶段、结果、错误、真实工具状态与耗时 |
| `PATCH /api/trip/tasks/{task_id}/result` | 保存成功/降级任务的编辑版 `TripPlan`，原始结果仍保留 |
| `GET /api/admin/tasks` | 管理员全局任务列表；支持分页、状态和用户名筛选 |
| `GET /api/admin/tasks/{task_id}` | 管理员查看任意任务的所属用户、状态、错误和真实工具过程 |
| `POST /api/trip/plan` | 登录后同步等待四阶段规划；复用任务执行链并保存历史，返回兼容结构 |
| `GET /api/map/poi` | 根据 `keywords/city/citylimit` 搜索，最多补齐前 5 个 POI 坐标 |
| `GET /api/map/weather` | 按 `city` 查询真实天气预报 |
| `POST /api/map/route` | 步行、驾车或公交路线，返回距离（米）、耗时（秒）和说明 |
| `GET /api/map/geocode` | 按 `address` 和可选 `city` 查询坐标 |
| `GET /api/poi/search` | 兼容原 POI 搜索入口 |
| `GET /api/poi/detail/{poi_id}` | 查询 POI 详情，不保证外部服务提供图片字段 |
| `GET /api/poi/photo` | 按 `name` 搜索图片，同步 HTTP 在线程池执行 |

普通空搜索返回 HTTP 200 和空列表；无路线/坐标/图片时返回空数据。外部故障返回 502、缺配置/工具返回 503、超时返回 504，并附安全 `error_code` 与说明。公交必须传起终点城市；公交距离使用服务端返回的起终点距离，并非该方案逐段行驶距离。

认证请求字段不合法返回 422，重复用户名返回 409，密码错误、令牌过期或签名无效返回 401，普通用户调用管理员接口返回 403。普通任务接口从当前令牌取得用户，不采用请求中的 `user_id`；访问他人任务统一返回 404。管理员权限由数据库中的 `is_admin` 校验，前端隐藏入口不能替代后端检查。旅行规划、地图和 POI 业务接口均要求登录，健康检查保持公开。第一版没有 refresh token 或注销黑名单；令牌保存在浏览器本地存储中并在两小时后到期，退出、切换账号或收到 401 时会清除令牌、用户信息及旧匿名结果缓存。

## 验证与阅读

在 `backend` 中运行离线测试，不消耗外部配额：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests -q
.\.venv\Scripts\python.exe -X utf8 -m pip check
```

真实联调会调用已配置的外部服务，单独运行：

```powershell
$env:DEBUG = 'false'
.\.venv\Scripts\python.exe -X utf8 -m scripts.check_integrations --mcp --llm
.\.venv\Scripts\python.exe -X utf8 -m scripts.check_trip_api --city 北京 --days 1
.\.venv\Scripts\python.exe -X utf8 -m scripts.check_map_api
```

`check_integrations` 分别验证 MCP 工具发现、景点/天气查询、模型标准工具调用和 `TripPlan` 结构化输出。`check_trip_api` 会注册唯一的一次性普通用户，经登录后的兼容接口执行完整四阶段，再从历史详情核对真实工具记录；联调账号和任务保留在本地数据库，便于管理员页面回看。以上命令会消耗真实模型或地图配额。

最终验收使用全新的 Python 3.11 虚拟环境从 `requirements-lock.txt` 安装，`pip check`、Python 编译、241 项离线测试及前端生产构建全部通过。另用 `deepseek-v4-pro` 和高德 MCP 完成北京 1 天游程：返回 `succeeded`，包含 2 个景点、1 天天气和 7 条真实工具记录。完整场景、命令与边界见 [最终验收记录](docs/最终验收记录.md)。

建议阅读顺序：

1. `backend/app/api/routes/admin.py`、`services/task_service.py`：管理员依赖、全局联表查询与详情复用。
2. `frontend/src/views/AdminTasks.vue`、`components/ToolCallTable.vue`：全局筛选、运行中详情轮询和工具耗时展示。
3. `frontend/src/main.ts`、`services/auth.ts`、`services/api.ts`：路由守卫、登录状态、令牌注入和 401 处理。
4. `frontend/src/views/Home.vue`、`views/Result.vue`、`views/History.vue`：提交任务、轮询过程、展示结果及本人历史恢复。
5. `backend/app/services/mcp_service.py`、`services/tool_monitor.py`：真实工具执行边界、结果分类和持久化监控。

具体文件、行号和逐步改造结果见 [二次开发变更记录](docs/二次开发变更记录.md)，完整计划见 [二次开发计划](二次开发计划.md)。[原教程](第十三章%20智能旅行助手.md) 保留历史示例，当前实现以代码与变更记录为准。

## 来源与协议

基于 [HelloAgents 教程](https://github.com/datawhalechina/Hello-Agents) 和原 [HelloAgents 框架](https://github.com/jjyaoao/HelloAgents) 项目继续开发，使用 [高德地图](https://lbs.amap.com/) 与 [amap-mcp-server](https://github.com/sugarforever/amap-mcp-server)。保留原项目 CC BY-NC-SA 4.0 协议。
