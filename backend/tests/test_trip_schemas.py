"""旅行请求边界、数据校验和兼容响应契约。"""

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    Location,
    PlanningResult,
    TripPlan,
    TripPlanResponse,
    TripRequest,
    WeatherInfo,
)


def make_request(**changes):
    values = {
        "city": "北京",
        "start_date": "2026-09-29",
        "end_date": "2026-10-01",
        "travel_days": 3,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
    }
    values.update(changes)
    return TripRequest(**values)


def make_plan():
    return TripPlan(
        city="北京",
        start_date="2026-09-29",
        end_date="2026-09-29",
        days=[],
        overall_suggestions="请提前预约。",
    )


def test_request_trims_city_and_preserves_date_strings():
    request = make_request(city="  北京 \t")
    assert request.city == "北京"
    assert request.model_dump()["start_date"] == "2026-09-29"
    assert request.model_dump()["end_date"] == "2026-10-01"


@pytest.mark.parametrize("city", ["", "  ", "\t\n"])
def test_request_rejects_blank_city(city):
    with pytest.raises(ValidationError, match="城市不能为空"):
        make_request(city=city)


@pytest.mark.parametrize("field", ["start_date", "end_date"])
@pytest.mark.parametrize(
    "value",
    ["2026-9-29", "20260929", "2026-09-29T00:00:00", " 2026-09-29", "2026-09-29 ", "2026/09/29"],
)
def test_request_requires_exact_date_format(field, value):
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        make_request(**{field: value})


@pytest.mark.parametrize("field", ["start_date", "end_date"])
@pytest.mark.parametrize("value", ["2026-02-29", "2026-04-31", "2026-13-01", "0000-01-01"])
def test_request_rejects_invalid_calendar_dates(field, value):
    with pytest.raises(ValidationError, match="有效的日历日期"):
        make_request(**{field: value})


@pytest.mark.parametrize(
    ("start", "end", "days"),
    [("2026-09-29", "2026-09-29", 1), ("2028-02-28", "2028-03-01", 3), ("2026-12-31", "2027-01-01", 2)],
)
def test_request_accepts_inclusive_date_ranges(start, end, days):
    assert make_request(start_date=start, end_date=end, travel_days=days).travel_days == days


def test_request_rejects_reversed_dates():
    with pytest.raises(ValidationError, match="结束日期不能早于开始日期"):
        make_request(end_date="2026-09-28", travel_days=1)


@pytest.mark.parametrize("days", [1, 2, 4])
def test_request_rejects_day_count_mismatch(days):
    with pytest.raises(ValidationError, match="旅行天数必须与起止日期"):
        make_request(travel_days=days)


@pytest.mark.parametrize("longitude,latitude", [(-180, -90), (180, 90), (116.4, 39.9)])
def test_location_accepts_normal_coordinates_and_boundaries(longitude, latitude):
    location = Location(longitude=longitude, latitude=latitude)
    assert location.longitude == longitude
    assert location.latitude == latitude


@pytest.mark.parametrize(
    "longitude,latitude",
    [(-180.001, 0), (180.001, 0), (0, -90.001), (0, 90.001), (float("nan"), 0), (0, float("inf"))],
)
def test_location_rejects_invalid_coordinates(longitude, latitude):
    with pytest.raises(ValidationError):
        Location(longitude=longitude, latitude=latitude)


@pytest.mark.parametrize("value,expected", [(0, 0), (-5, -5), ("0", 0), (" 25°C ", 25), ("-5℃", -5), ("12°", 12)])
def test_weather_preserves_temperature_parsing(value, expected):
    weather = WeatherInfo(date="2026-09-29", day_temp=value, night_temp=value)
    assert weather.day_temp == expected
    assert weather.night_temp == expected


@pytest.mark.parametrize("field", ["day_temp", "night_temp"])
@pytest.mark.parametrize("value", ["未知", "", "℃", "20到25°C", None])
def test_weather_rejects_invalid_temperature_instead_of_fabricating_zero(field, value):
    with pytest.raises(ValidationError):
        WeatherInfo(date="2026-09-29", **{field: value})


def test_response_keeps_existing_fields_and_compatible_defaults():
    plan = make_plan()
    response = TripPlanResponse(success=True, message="规划完成", data=plan)
    assert response.success is True
    assert response.message == "规划完成"
    assert response.data == plan
    assert response.status is None
    assert response.warnings == []
    assert TripPlanResponse(success=False).data is None


@pytest.mark.parametrize("status", ["succeeded", "degraded"])
def test_planning_result_and_response_accept_success_states(status):
    result = PlanningResult(plan=make_plan(), status=status, warnings=["天气暂不可用"])
    response = TripPlanResponse(success=True, data=result.plan, status=result.status, warnings=result.warnings)
    assert response.status == status
    assert response.warnings == ["天气暂不可用"]


def test_failed_response_does_not_require_a_plan():
    response = TripPlanResponse(success=False, status="failed")
    assert response.data is None
    with pytest.raises(ValidationError):
        PlanningResult(plan=make_plan(), status="failed")


def test_warning_defaults_are_independent():
    result = PlanningResult(plan=make_plan(), status="succeeded")
    other_result = PlanningResult(plan=make_plan(), status="succeeded")
    result.warnings.append("提示")
    assert other_result.warnings == []
    response = TripPlanResponse(success=True)
    other_response = TripPlanResponse(success=True)
    response.warnings.append("提示")
    assert other_response.warnings == []
