"""数据模型定义"""

import re
from typing import List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from datetime import date, datetime


# ============ 请求模型 ============

class TripRequest(BaseModel):
    """旅行规划请求"""
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "city": "北京",
            "start_date": "2025-06-01",
            "end_date": "2025-06-03",
            "travel_days": 3,
            "transportation": "公共交通",
            "accommodation": "经济型酒店",
            "preferences": ["历史文化", "美食"],
            "free_text_input": "希望多安排一些博物馆",
        }
    })

    city: str = Field(..., description="目的地城市")
    start_date: str = Field(..., description="开始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD")
    travel_days: int = Field(..., description="旅行天数", ge=1, le=30)
    transportation: str = Field(..., description="交通方式")
    accommodation: str = Field(..., description="住宿偏好")
    preferences: List[str] = Field(default_factory=list, description="旅行偏好标签")
    free_text_input: Optional[str] = Field(default="", description="额外要求")

    @field_validator('city')
    @classmethod
    def validate_city(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("目的地城市不能为空")
        return value

    @field_validator('start_date', 'end_date')
    @classmethod
    def validate_date(cls, value: str) -> str:
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise ValueError("日期必须使用 YYYY-MM-DD 格式")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("日期必须是有效的日历日期") from exc
        return value

    @model_validator(mode='after')
    def validate_travel_dates(self):
        start = date.fromisoformat(self.start_date)
        end = date.fromisoformat(self.end_date)
        if end < start:
            raise ValueError("结束日期不能早于开始日期")
        if (end - start).days + 1 != self.travel_days:
            raise ValueError("旅行天数必须与起止日期（含首尾两天）一致")
        return self
    
class POISearchRequest(BaseModel):
    """POI搜索请求"""
    keywords: str = Field(..., description="搜索关键词", json_schema_extra={"example": "故宫"})
    city: str = Field(..., description="城市", json_schema_extra={"example": "北京"})
    citylimit: bool = Field(default=True, description="是否限制在城市范围内")


class RouteRequest(BaseModel):
    """路线规划请求"""
    origin_address: str = Field(
        ..., description="起点地址", json_schema_extra={"example": "北京市朝阳区阜通东大街6号"},
    )
    destination_address: str = Field(
        ..., description="终点地址", json_schema_extra={"example": "北京市海淀区上地十街10号"},
    )
    origin_city: Optional[str] = Field(default=None, description="起点城市")
    destination_city: Optional[str] = Field(default=None, description="终点城市")
    route_type: Literal["walking", "driving", "transit"] = Field(default="walking", description="路线类型")

    @field_validator("origin_address", "destination_address", "origin_city", "destination_city")
    @classmethod
    def validate_route_text(cls, value):
        if value is not None and not value.strip():
            raise ValueError("地址和城市不能只包含空白")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def validate_transit_cities(self):
        if self.route_type == "transit" and not (self.origin_city and self.destination_city):
            raise ValueError("公交路线需要提供起点和终点城市")
        return self


class AuthCredentials(BaseModel):
    """注册和登录共用的 JSON 凭据；密码不会出现在模型 repr 中。"""
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=32)
    password: SecretStr = Field(min_length=8, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        if not isinstance(value, str):
            raise ValueError("用户名必须是字符串")
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]+", value):
            raise ValueError("用户名只能包含字母、数字和下划线")
        return value


class UserRegisterRequest(AuthCredentials):
    """公开注册不能传入管理员字段。"""


class UserLoginRequest(AuthCredentials):
    """JSON 登录请求。"""


# ============ 响应模型 ============

class Location(BaseModel):
    """地理位置"""
    longitude: float = Field(..., ge=-180, le=180, description="经度")
    latitude: float = Field(..., ge=-90, le=90, description="纬度")


class UserPublic(BaseModel):
    """可以返回给前端的用户字段，不包含密码哈希。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    is_admin: bool
    created_at: datetime


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(gt=0, description="访问令牌剩余有效秒数")
    user: UserPublic


class Attraction(BaseModel):
    """景点信息"""
    name: str = Field(..., description="景点名称")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    visit_duration: int = Field(..., description="建议游览时间(分钟)")
    description: str = Field(..., description="景点描述")
    category: Optional[str] = Field(default="景点", description="景点类别")
    rating: Optional[float] = Field(default=None, description="评分")
    photos: Optional[List[str]] = Field(default_factory=list, description="景点图片URL列表")
    poi_id: Optional[str] = Field(default="", description="POI ID")
    image_url: Optional[str] = Field(default=None, description="图片URL")
    ticket_price: int = Field(default=0, description="门票价格(元)")


class Meal(BaseModel):
    """餐饮信息"""
    type: str = Field(..., description="餐饮类型: breakfast/lunch/dinner/snack")
    name: str = Field(..., description="餐饮名称")
    address: Optional[str] = Field(default=None, description="地址")
    location: Optional[Location] = Field(default=None, description="经纬度坐标")
    description: Optional[str] = Field(default=None, description="描述")
    estimated_cost: int = Field(default=0, description="预估费用(元)")


class Hotel(BaseModel):
    """酒店信息"""
    name: str = Field(..., description="酒店名称")
    address: str = Field(default="", description="酒店地址")
    location: Optional[Location] = Field(default=None, description="酒店位置")
    price_range: str = Field(default="", description="价格范围")
    rating: str = Field(default="", description="评分")
    distance: str = Field(default="", description="距离景点距离")
    type: str = Field(default="", description="酒店类型")
    estimated_cost: int = Field(default=0, description="预估费用(元/晚)")


class DayPlan(BaseModel):
    """单日行程"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_index: int = Field(..., description="第几天(从0开始)")
    description: str = Field(..., description="当日行程描述")
    transportation: str = Field(..., description="交通方式")
    accommodation: str = Field(..., description="住宿")
    hotel: Optional[Hotel] = Field(default=None, description="推荐酒店")
    attractions: List[Attraction] = Field(default=[], description="景点列表")
    meals: List[Meal] = Field(default=[], description="餐饮列表")


class WeatherInfo(BaseModel):
    """天气信息"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_weather: str = Field(default="", description="白天天气")
    night_weather: str = Field(default="", description="夜间天气")
    day_temp: Union[int, str] = Field(default=0, description="白天温度")
    night_temp: Union[int, str] = Field(default=0, description="夜间温度")
    wind_direction: str = Field(default="", description="风向")
    wind_power: str = Field(default="", description="风力")

    @field_validator('day_temp', 'night_temp', mode='before')
    @classmethod
    def parse_temperature(cls, v):
        """解析温度,移除°C等单位"""
        if isinstance(v, str):
            # 移除°C, ℃等单位符号
            v = v.replace('°C', '').replace('℃', '').replace('°', '').strip()
            try:
                return int(v)
            except ValueError as exc:
                raise ValueError("温度必须是整数或带 °C、℃、° 单位的整数字符串") from exc
        return v


class Budget(BaseModel):
    """预算信息"""
    total_attractions: int = Field(default=0, description="景点门票总费用")
    total_hotels: int = Field(default=0, description="酒店总费用")
    total_meals: int = Field(default=0, description="餐饮总费用")
    total_transportation: int = Field(default=0, description="交通总费用")
    total: int = Field(default=0, description="总费用")


class TripPlan(BaseModel):
    """旅行计划"""
    city: str = Field(..., description="目的地城市")
    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    days: List[DayPlan] = Field(..., description="每日行程")
    weather_info: List[WeatherInfo] = Field(default=[], description="天气信息")
    overall_suggestions: str = Field(..., description="总体建议")
    budget: Optional[Budget] = Field(default=None, description="预算信息")


class PlanningResult(BaseModel):
    """规划器的结果及可供用户了解的降级提示"""
    plan: TripPlan
    status: Literal['succeeded', 'degraded']
    warnings: List[str] = Field(default_factory=list)


TaskStatus = Literal["queued", "running", "succeeded", "degraded", "failed"]
ToolCallStatus = Literal["running", "succeeded", "empty", "failed"]


class TaskSubmitResponse(BaseModel):
    """任务已入库，不代表行程已经生成成功。"""
    task_id: str
    status: TaskStatus


class TaskError(BaseModel):
    code: str
    message: str


class TaskToolCall(BaseModel):
    """一次真实 MCP 调用；运行中记录的结束时间和最终耗时为空。"""
    id: int
    agent_name: str
    tool_name: str
    status: ToolCallStatus
    arguments: dict = Field(default_factory=dict, description="脱敏后的调用参数摘要")
    result_summary: Optional[dict] = Field(default=None, description="有界的结果摘要")
    error: Optional[TaskError] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[float] = Field(default=None, ge=0)


class TaskListItem(BaseModel):
    task_id: str
    status: TaskStatus
    stage: str
    city: str
    start_date: str
    end_date: str
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    total_duration_ms: Optional[float] = Field(default=None, ge=0)


class TaskListResponse(BaseModel):
    items: List[TaskListItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class TaskDetailResponse(BaseModel):
    task_id: str
    status: TaskStatus
    stage: str
    request: TripRequest
    plan: Optional[TripPlan] = None
    warnings: List[str] = Field(default_factory=list)
    error: Optional[TaskError] = None
    tool_calls: List[TaskToolCall] = Field(default_factory=list, description="按调用记录 ID 排序")
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    updated_at: datetime
    total_duration_ms: Optional[float] = Field(default=None, ge=0)


class AdminTaskListItem(TaskListItem):
    """管理员列表额外返回任务所属用户。"""
    user_id: int
    username: str


class AdminTaskListResponse(BaseModel):
    items: List[AdminTaskListItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class AdminTaskDetailResponse(TaskDetailResponse):
    """管理员详情沿用普通详情结构，并明确所属用户。"""
    user_id: int
    username: str


class TripPlanResponse(BaseModel):
    """旅行计划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[TripPlan] = Field(default=None, description="旅行计划数据")
    status: Optional[Literal['succeeded', 'degraded', 'failed']] = Field(default=None, description="规划状态")
    warnings: List[str] = Field(default_factory=list, description="规划提示")


class POIInfo(BaseModel):
    """POI信息"""
    id: str = Field(..., description="POI ID")
    name: str = Field(..., description="名称")
    type: str = Field(..., description="类型")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    tel: Optional[str] = Field(default=None, description="电话")


class POISearchResponse(BaseModel):
    """POI搜索响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[POIInfo] = Field(default=[], description="POI列表")


class RouteInfo(BaseModel):
    """路线信息"""
    distance: float = Field(..., ge=0, allow_inf_nan=False, description="距离(米)")
    duration: int = Field(..., ge=0, description="时间(秒)")
    route_type: str = Field(..., description="路线类型")
    description: str = Field(..., description="路线描述")


class RouteResponse(BaseModel):
    """路线规划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[RouteInfo] = Field(default=None, description="路线信息")


class WeatherResponse(BaseModel):
    """天气查询响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[WeatherInfo] = Field(default=[], description="天气信息")


class GeocodeResponse(BaseModel):
    """地理编码响应；正常无匹配地址时 data 为 null。"""
    success: bool
    message: str
    data: Optional[Location] = None


# ============ 错误响应 ============

class ErrorResponse(BaseModel):
    """错误响应"""
    success: bool = Field(default=False, description="是否成功")
    message: str = Field(..., description="错误消息")
    error_code: Optional[str] = Field(default=None, description="错误代码")
