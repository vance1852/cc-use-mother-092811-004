"""山区慢火车公共服务运行图的纯函数计算。

本模块不接触数据库：所有函数只依赖冻结的输入字典，
保证考核人员可以用同一份证据离线复算服务覆盖。
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import timedelta
from typing import Any

from .errors import ValidationError

CHANGE_TYPES = frozenset({"add_stop", "skip_stop", "cancel_train", "replacement", "restore"})
AFFECTED_GROUPS = frozenset({
    "market_goers",      # 赶集群众
    "medical_patients",  # 就医患者
    "farm_shippers",     # 小件农货托运人
    "commuters",         # 通勤通学
    "general",           # 一般旅客
})
CALENDAR_KINDS = frozenset({"market", "medical"})
PLAN_STATES = frozenset({"draft", "confirmed", "effective", "superseded"})
OPEN_END = "9999-12-31"


def parse_date(value: Any, field: str = "date") -> str:
    """把输入规范为 YYYY-MM-DD 文本。"""

    text = str(value).strip()
    try:
        return date_type.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def dates_between(start: str, end: str) -> list[str]:
    """生成闭区间内的日期序列。"""

    first = date_type.fromisoformat(start)
    last = date_type.fromisoformat(end)
    days = []
    current = first
    while current <= last:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def weekday_of(date_str: str) -> int:
    """返回日期对应的星期（周一为 0）。"""

    return date_type.fromisoformat(date_str).weekday()


def adopted_plan(plans: list[dict[str, Any]], date_str: str) -> dict[str, Any] | None:
    """还原指定日期实际采用的运行图版本。"""

    candidates = [
        plan for plan in plans
        if plan.get("activated_on")
        and plan["activated_on"] <= date_str
        and (not plan.get("superseded_on") or date_str < plan["superseded_on"])
        and plan["valid_from"] <= date_str <= plan["valid_to"]
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda plan: (plan["activated_on"], plan["plan_id"]))[-1]


def blocked_stations(inputs: dict[str, Any], date_str: str) -> dict[str, list[str]]:
    """汇总指定日期因检修封锁或灾害限制无法办理客运的站点。"""

    blocked: dict[str, list[str]] = {}
    for blockade in inputs.get("blockades", []):
        if blockade["start_date"] <= date_str <= blockade["end_date"]:
            blocked.setdefault(blockade["station_id"], []).append("blockade")
    for restriction in inputs.get("restrictions", []):
        if restriction["start_date"] <= date_str <= restriction["end_date"]:
            blocked.setdefault(restriction["station_id"], []).append("disaster")
    return blocked


def _active_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤掉已被恢复事实抵销的临时变更，恢复本身也是新事实。"""

    restored = {change["restores_change_id"] for change in changes if change["change_type"] == "restore"}
    return [change for change in changes
            if change["change_type"] != "restore" and change["change_id"] not in restored]


def compute_day(inputs: dict[str, Any], date_str: str) -> dict[str, Any]:
    """还原任意日期实际采用的运行图（含增停、越站、取消、替代运输与封锁）。"""

    blocked = blocked_stations(inputs, date_str)
    plan = adopted_plan(inputs.get("plans", []), date_str)
    if plan is None:
        return {"date": date_str, "plan_id": None, "trains": [], "replacements": [],
                "blocked_stations": sorted(blocked)}
    changes = [change for change in _active_changes(inputs.get("changes", []))
               if change["plan_id"] == plan["plan_id"] and change["service_date"] == date_str]
    cancelled = {change["train_no"] for change in changes if change["change_type"] == "cancel_train"}
    skipped = {(change["train_no"], change["station_id"]) for change in changes
               if change["change_type"] == "skip_stop"}
    added = [change for change in changes if change["change_type"] == "add_stop"]
    replacements = [change for change in changes if change["change_type"] == "replacement"]
    weekday = weekday_of(date_str)
    trains = []
    for train in plan["trains"]:
        if weekday not in train["run_weekdays"]:
            continue
        if train["train_no"] in cancelled:
            trains.append({"train_no": train["train_no"], "status": "cancelled", "stops": []})
            continue
        stops = []
        for stop in train["stops"]:
            station_id = stop["station_id"]
            if (train["train_no"], station_id) in skipped:
                status = "skipped"
            elif station_id in blocked:
                status = "blocked"
            else:
                status = "scheduled"
            stops.append({"station_id": station_id, "arrive": stop["arrive"],
                          "depart": stop["depart"], "handles_freight": stop["handles_freight"],
                          "status": status})
        for change in added:
            if change["train_no"] == train["train_no"]:
                stops.append({"station_id": change["station_id"], "arrive": change.get("arrive"),
                              "depart": change.get("depart"),
                              "handles_freight": bool(change.get("handles_freight")),
                              "status": "added"})
        trains.append({"train_no": train["train_no"], "status": "scheduled",
                       "passenger_capacity": train["passenger_capacity"],
                       "freight_capacity": train["freight_capacity"], "stops": stops})
    return {"date": date_str, "plan_id": plan["plan_id"], "trains": trains,
            "replacements": [{"change_id": change["change_id"], "station_id": change["station_id"],
                              "mode": change.get("replacement_mode"),
                              "capacity": change.get("replacement_capacity"),
                              "carrier": change.get("replacement_carrier")}
                             for change in replacements],
            "blocked_stations": sorted(blocked)}


def served_stops(day: dict[str, Any], station_id: str) -> tuple[int, int]:
    """统计站点当日可用停靠次数，替代运输计入但单独标注。"""

    served = 0
    substituted = 0
    for train in day["trains"]:
        if train["status"] != "scheduled":
            continue
        for stop in train["stops"]:
            if stop["station_id"] == station_id and stop["status"] in ("scheduled", "added"):
                served += 1
    for replacement in day["replacements"]:
        if replacement["station_id"] == station_id:
            served += 1
            substituted += 1
    return served, substituted


def commitment_required(commitment: dict[str, Any], date_str: str,
                        calendar: list[dict[str, Any]]) -> bool:
    """判断承诺在指定日期是否生效：未挂日历旗帜则每日生效，否则只在赶集或就医日生效。"""

    kinds = set()
    if commitment.get("serve_market_days"):
        kinds.add("market")
    if commitment.get("serve_medical_days"):
        kinds.add("medical")
    if not kinds:
        return True
    return any(entry["station_id"] == commitment["station_id"] and entry["date"] == date_str
               and entry["kind"] in kinds for entry in calendar)


def liability_for(inputs: dict[str, Any], station_id: str, date_str: str) -> dict[str, Any]:
    """确定未满足承诺的补救责任方：优先补贴协议，否则回落到运营机构。"""

    for agreement in inputs.get("agreements", []):
        if (agreement["station_id"] == station_id and agreement["valid_from"] <= date_str
                <= (agreement["valid_to"] or OPEN_END)):
            return {"agreement_id": agreement["agreement_id"],
                    "liable_organization": agreement["liable_organization"],
                    "funder_name": agreement["funder_name"]}
    return {"agreement_id": None, "liable_organization": inputs.get("site_organization"),
            "funder_name": None}


def causes_for(day: dict[str, Any], station_id: str) -> list[str]:
    """给出站点服务缺口的可能原因，便于考核追溯。"""

    causes = []
    if station_id in day["blocked_stations"]:
        causes.append("blocked")
    if any(train["status"] == "cancelled" for train in day["trains"]):
        causes.append("train_cancelled")
    if not causes:
        causes.append("service_below_commitment")
    return causes


def compute_coverage(inputs: dict[str, Any]) -> dict[str, Any]:
    """按日核对服务承诺，输出未满足承诺与补救责任。"""

    days = []
    stations: dict[str, dict[str, Any]] = {}
    for date_str in dates_between(inputs["from_date"], inputs["to_date"]):
        day = compute_day(inputs, date_str)
        entries = []
        for commitment in inputs.get("commitments", []):
            if not (commitment["valid_from"] <= date_str <= (commitment["valid_to"] or OPEN_END)):
                continue
            if not commitment_required(commitment, date_str, inputs.get("calendar", [])):
                continue
            served, substituted = served_stops(day, commitment["station_id"])
            met = served >= commitment["min_stops_per_day"]
            entry = {"commitment_id": commitment["commitment_id"],
                     "station_id": commitment["station_id"],
                     "required": commitment["min_stops_per_day"],
                     "served": served, "substituted": substituted, "met": met}
            if not met:
                entry["liability"] = liability_for(inputs, commitment["station_id"], date_str)
                entry["causes"] = causes_for(day, commitment["station_id"])
            entries.append(entry)
            slot = stations.setdefault(commitment["station_id"],
                                       {"committed_days": 0, "met_days": 0,
                                        "unmet_days": 0, "served_days": 0})
            slot["committed_days"] += 1
            slot["met_days"] += 1 if met else 0
            slot["unmet_days"] += 0 if met else 1
            slot["served_days"] += 1 if served > 0 else 0
        days.append({"date": date_str, "plan_id": day["plan_id"], "entries": entries})
    for slot in stations.values():
        committed = slot["committed_days"]
        slot["coverage_ratio"] = round(slot["met_days"] / committed, 4) if committed else 1.0
    goods = inputs.get("goods", [])
    summary = {"stations": stations,
               "goods": {"accepted_units": sum(item["units"] for item in goods),
                         "transferred_units": sum(item["units"] for item in goods
                                                  if item["status"] == "transferred")}}
    return {"site_id": inputs["site_id"], "from_date": inputs["from_date"],
            "to_date": inputs["to_date"], "days": days, "summary": summary}


def compute_round(observations: list[dict[str, Any]], coverage: dict[str, Any],
                  threshold: float) -> dict[str, Any]:
    """用冻结证据与截至轮次截点的客流计算服务覆盖考核结果。"""

    totals: dict[str, dict[str, int]] = {}
    for observation in observations:
        slot = totals.setdefault(observation["station_id"],
                                 {"passengers": 0, "freight_units": 0, "observations": 0})
        slot["passengers"] += int(observation["passengers"])
        slot["freight_units"] += int(observation["freight_units"])
        slot["observations"] += 1
    coverage_stations = coverage.get("summary", {}).get("stations", {})
    stations = {}
    for station_id in sorted(set(totals) | set(coverage_stations)):
        sums = totals.get(station_id, {"passengers": 0, "freight_units": 0, "observations": 0})
        slot = coverage_stations.get(station_id, {})
        served_days = int(slot.get("served_days", 0))
        average = round(sums["passengers"] / served_days, 4) if served_days else 0.0
        stations[station_id] = {
            "passengers": sums["passengers"],
            "freight_units": sums["freight_units"],
            "observations": sums["observations"],
            "committed_days": int(slot.get("committed_days", 0)),
            "met_days": int(slot.get("met_days", 0)),
            "served_days": served_days,
            "coverage_ratio": slot.get("coverage_ratio"),
            "average_passengers_per_served_day": average,
            "low_utilization": bool(served_days and average < threshold),
        }
    return {"stations": stations}
