"""山区慢火车公共服务运行图的纯计算引擎。

本模块不接触数据库，只处理普通字典，便于对冻结快照做离线复算：
- 赶集/就医/月度固定日等日历激活判断；
- 在基准运行图上叠加检修封锁、灾害限制、临时增停/越站/取消/恢复事实；
- 按日还原实际停站、替代运输覆盖、承诺缺口与已接收货物去向；
- 对冻结轮次快照重算覆盖摘要，供考核人员核对。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_date(value: str) -> date:
    """解析并校验 ISO 日期。"""

    if not isinstance(value, str):
        raise ValueError("日期必须是 YYYY-MM-DD 字符串")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"日期格式无效: {value}") from exc
    return parsed


def daterange(start: date, end: date) -> Iterable[date]:
    """生成闭区间内的每一天。"""

    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def calendar_active(rule: dict[str, Any], day: date) -> bool:
    """判断一条日历规则在指定日期是否激活。

    支持的规则类型：
    - weekly: {"type": "weekly", "weekdays": ["mon", "sat"]}
    - dates:  {"type": "dates", "dates": ["2026-10-01"]}
    - monthly_days: {"type": "monthly_days", "days": [1, 15]}
    - nth_weekday: {"type": "nth_weekday", "nth": 3, "weekday": "sun"}（n=-1 表示最后一个）
    """

    kind = rule.get("type")
    if kind == "weekly":
        return WEEKDAY_NAMES[day.weekday()] in set(rule.get("weekdays", []))
    if kind == "dates":
        return day.isoformat() in set(rule.get("dates", []))
    if kind == "monthly_days":
        return day.day in set(rule.get("days", []))
    if kind == "nth_weekday":
        target = WEEKDAY_NAMES.index(rule["weekday"])
        nth = int(rule["nth"])
        if nth > 0:
            first = date(day.year, day.month, 1)
            first_target = 1 + (target - first.weekday()) % 7
            occurrence = first_target + 7 * (nth - 1)
            try:
                return day == date(day.year, day.month, occurrence)
            except ValueError:
                return False
        if nth == -1:
            next_month = date(day.year + (day.month // 12), day.month % 12 + 1, 1)
            last = next_month - timedelta(days=1)
            last_target = last.day - (last.weekday() - target) % 7
            return day == date(day.year, day.month, last_target)
    return False


def demand_kinds_active(calendars: list[dict[str, Any]], station_id: str,
                        wanted_kinds: Iterable[str], day: date) -> list[str]:
    """返回某站点在当天激活且被承诺关注的日历类别。"""

    wanted = set(wanted_kinds)
    active: list[str] = []
    for calendar in calendars:
        if calendar["station_id"] != station_id or calendar["kind"] not in wanted:
            continue
        if calendar_active(calendar["rule"], day):
            active.append(calendar["kind"])
    return active


def _fact_covers(fact: dict[str, Any], day: date) -> bool:
    start = parse_date(fact["event_date"] if "event_date" in fact else fact["start_date"])
    end_text = fact.get("end_date")
    end = parse_date(end_text) if end_text else start
    return start <= day <= end


def _restored(fact: dict[str, Any], day: date, amendments: list[dict[str, Any]]) -> bool:
    """取消/越站事实是否已被恢复事实解除（恢复从其事件日期起生效）。"""

    for amendment in amendments:
        if amendment.get("kind") != "restore":
            continue
        if amendment.get("restores_amendment_id") != fact["amendment_id"]:
            continue
        if parse_date(amendment["event_date"]) <= day:
            return True
    return False


def _section_stations(stop_ids: list[str], fact: dict[str, Any]) -> set[str]:
    """按本次列车的停站顺序，求区段封锁/灾害影响的停站集合。"""

    source = fact.get("from_station_id")
    target = fact.get("to_station_id")
    if source in stop_ids and target in stop_ids:
        left = stop_ids.index(source)
        right = stop_ids.index(target)
        left, right = sorted((left, right))
        return set(stop_ids[left:right + 1])
    return set()


def effective_trains(snapshot: dict[str, Any], day: date) -> list[dict[str, Any]]:
    """还原当天实际可乘的列车及其停站。

    返回每个列车的停站字典，附带 suppressed（被压停的站点及原因）。
    区段性事实按列车自身停站顺序界定影响范围。
    """

    amendments = snapshot.get("amendments", [])
    blocks = snapshot.get("blocks", [])
    disasters = snapshot.get("disasters", [])

    result: list[dict[str, Any]] = []
    for train in snapshot.get("trains", []):
        if train.get("weekdays") and WEEKDAY_NAMES[day.weekday()] not in set(train["weekdays"]):
            # 基准运行图当日不开行（临时加车由替代运输/单独事实表达）。
            continue
        stop_ids = [stop["station_id"] for stop in train["stops"]]
        suppressed: dict[str, list[str]] = {station_id: [] for station_id in stop_ids}

        canceled = False
        for amendment in amendments:
            if amendment.get("kind") != "cancel_train" or amendment.get("train_code") != train["train_code"]:
                continue
            if _fact_covers(amendment, day) and not _restored(amendment, day, amendments):
                canceled = True
                for station_id in stop_ids:
                    suppressed[station_id].append(f"amendment:{amendment['amendment_id']}:cancel_train")

        if not canceled:
            for block in blocks:
                if not _fact_covers(block, day):
                    continue
                affected = {block["station_id"]} if block.get("scope") == "station" else _section_stations(stop_ids, block)
                for station_id in affected & set(stop_ids):
                    suppressed[station_id].append(f"block:{block['block_id']}")
            for disaster in disasters:
                if not _fact_covers(disaster, day):
                    continue
                affected = {disaster["station_id"]} if disaster.get("scope") == "station" else _section_stations(stop_ids, disaster)
                for station_id in affected & set(stop_ids):
                    suppressed[station_id].append(
                        f"disaster:{disaster['disaster_id']}:{disaster.get('effect', 'no_stop')}"
                    )
            for amendment in amendments:
                if amendment.get("train_code") != train["train_code"]:
                    continue
                if amendment.get("kind") != "skip_stop" or not _fact_covers(amendment, day):
                    continue
                if _restored(amendment, day, amendments):
                    continue
                station_id = amendment.get("station_id")
                if station_id in suppressed:
                    suppressed[station_id].append(f"amendment:{amendment['amendment_id']}:skip_stop")

        active_stops = [
            {**stop, "suppressed_reasons": suppressed.get(stop["station_id"], [])}
            for stop in train["stops"]
            if not suppressed.get(stop["station_id"])
        ]
        added_stops: list[dict[str, Any]] = []
        for amendment in amendments:
            if amendment.get("kind") != "add_stop" or amendment.get("train_code") != train["train_code"]:
                continue
            if not _fact_covers(amendment, day):
                continue
            station_id = amendment.get("station_id")
            if station_id and station_id not in {stop["station_id"] for stop in active_stops}:
                added_stops.append({"station_id": station_id, "arrive": None, "depart": None,
                                    "stop_minutes": None, "added_by": amendment["amendment_id"],
                                    "suppressed_reasons": []})

        result.append({
            "train_id": train["train_id"],
            "train_code": train["train_code"],
            "cargo_capacity_kg": train.get("cargo_capacity_kg", 0),
            "canceled": canceled,
            "stops": active_stops + added_stops,
            "suppressed": {key: value for key, value in suppressed.items() if value},
        })
    return result


def replacement_groups(snapshot: dict[str, Any], day: date) -> list[dict[str, Any]]:
    """聚合当天开行的替代运输班次（同一 group_id 的多车记录合并）。"""

    groups: dict[str, dict[str, Any]] = {}
    for replacement in snapshot.get("replacements", []):
        if replacement["event_date"] != day.isoformat():
            continue
        group = groups.setdefault(replacement["group_id"], {
            "group_id": replacement["group_id"],
            "modes": set(),
            "station_ids": set(),
            "covers_consignments": set(),
            "affected_populations": set(),
            "cargo_capacity_kg": 0.0,
            "capacity_seats": 0,
            "responsible_actor_id": replacement.get("responsible_actor_id"),
            "source_type": replacement.get("source_type"),
            "source_id": replacement.get("source_id"),
            "replacement_ids": [],
        })
        group["modes"].add(replacement["mode"])
        group["station_ids"].update(replacement.get("station_ids_json", []))
        group["covers_consignments"].update(replacement.get("covers_consignments_json", []))
        group["affected_populations"].update(replacement.get("affected_populations", []))
        group["cargo_capacity_kg"] += float(replacement.get("cargo_capacity_kg", 0))
        group["capacity_seats"] += int(replacement.get("capacity_seats") or 0)
        group["replacement_ids"].append(replacement["replacement_id"])
    out: list[dict[str, Any]] = []
    for group in groups.values():
        out.append({
            "group_id": group["group_id"],
            "modes": sorted(group["modes"]),
            "station_ids": sorted(group["station_ids"]),
            "covers_consignments": sorted(group["covers_consignments"]),
            "affected_populations": sorted(group["affected_populations"]),
            "cargo_capacity_kg": group["cargo_capacity_kg"],
            "capacity_seats": group["capacity_seats"],
            "responsible_actor_id": group["responsible_actor_id"],
            "source_type": group["source_type"],
            "source_id": group["source_id"],
            "replacement_ids": sorted(group["replacement_ids"]),
        })
    return sorted(out, key=lambda item: item["group_id"])


def _agreements_active(snapshot: dict[str, Any], day: date) -> list[dict[str, Any]]:
    return [
        agreement for agreement in snapshot.get("agreements", [])
        if parse_date(agreement["valid_from"]) <= day <= parse_date(agreement["valid_to"])
    ]


def evaluate_day(snapshot: dict[str, Any], day: date) -> dict[str, Any]:
    """评估单日：需求日历、实际停站、承诺缺口、货物去向与补救责任。"""

    trains = effective_trains(snapshot, day)
    groups = replacement_groups(snapshot, day)
    stations = snapshot.get("stations", [])
    calendars = snapshot.get("calendars", [])
    commitments = snapshot.get("commitments", [])
    agreements = _agreements_active(snapshot, day)

    rail_at: dict[str, list[str]] = {station["station_id"]: [] for station in stations}
    rail_capacity: dict[str, float] = {}
    for train in trains:
        if train["canceled"]:
            continue
        rail_capacity[train["train_code"]] = float(train["cargo_capacity_kg"])
        for stop in train["stops"]:
            rail_at.setdefault(stop["station_id"], []).append(train["train_code"])
    replacement_at: dict[str, list[str]] = {station["station_id"]: [] for station in stations}
    for group in groups:
        for station_id in group["station_ids"]:
            replacement_at.setdefault(station_id, []).append(group["group_id"])

    # 承诺核算
    commitment_results: list[dict[str, Any]] = []
    for commitment in commitments:
        station_id = commitment["station_id"]
        active_kinds = demand_kinds_active(calendars, station_id, commitment["demand_kinds_json"], day)
        rail_count = len(rail_at.get(station_id, []))
        replacement_count = len(replacement_at.get(station_id, []))
        stop_count = rail_count + replacement_count
        met = True
        reasons: list[str] = []
        if active_kinds and stop_count < commitment["min_stops_on_demand_day"]:
            met = False
            reasons.append("demand_day_below_minimum")
        if commitment.get("min_stops_per_week", 0) > 0:
            weekly = _weekly_stop_count(snapshot, station_id, day)
            if weekly < commitment["min_stops_per_week"]:
                met = False
                reasons.append("weekly_below_minimum")
        commitment_results.append({
            "commitment_id": commitment["commitment_id"],
            "station_id": station_id,
            "title": commitment["title"],
            "demand_kinds": active_kinds,
            "rail_stops": sorted(rail_at.get(station_id, [])),
            "replacement_stops": sorted(replacement_at.get(station_id, [])),
            "stop_count": stop_count,
            "min_stops_on_demand_day": commitment["min_stops_on_demand_day"],
            "min_stops_per_week": commitment.get("min_stops_per_week", 0),
            "met": met,
            "unmet_reasons": reasons,
            "remedy": _remedy_for(agreements, station_id),
        })

    # 已接收小件农货核算（仅当天待运、状态 accepted）
    consignment_results = _evaluate_consignments(snapshot, day, trains, groups)

    return {
        "date": day.isoformat(),
        "trains": [
            {"train_code": train["train_code"], "canceled": train["canceled"],
             "stops": [stop["station_id"] for stop in train["stops"]],
             "suppressed": train["suppressed"]}
            for train in trains
        ],
        "replacements": groups,
        "stations": [
            {"station_id": station["station_id"],
             "demand_kinds": sorted({
                 kind for commitment in commitments
                 if commitment["station_id"] == station["station_id"]
                 for kind in demand_kinds_active(calendars, station["station_id"],
                                                 commitment["demand_kinds_json"], day)
             }),
             "rail_stops": sorted(rail_at.get(station["station_id"], [])),
             "replacement_stops": sorted(replacement_at.get(station["station_id"], []))}
            for station in stations
        ],
        "commitments": commitment_results,
        "consignments": consignment_results,
        "unmet_commitments": [item["commitment_id"] for item in commitment_results if not item["met"]],
        "unmet_consignments": [item["consignment_id"] for item in consignment_results
                               if item["status"] == "unmet"],
    }


def _weekly_stop_count(snapshot: dict[str, Any], station_id: str, day: date) -> int:
    """统计当日所在自然周（周一起）落在计划有效期内的累计服务次数。"""

    plan = snapshot.get("plan") or {}
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    if plan.get("valid_from"):
        monday = max(monday, parse_date(plan["valid_from"]))
    if plan.get("valid_to"):
        sunday = min(sunday, parse_date(plan["valid_to"]))
    total = 0
    for current in daterange(monday, sunday):
        for train in effective_trains(snapshot, current):
            if not train["canceled"] and any(stop["station_id"] == station_id for stop in train["stops"]):
                total += 1
        total += sum(
            1 for group in replacement_groups(snapshot, current)
            if station_id in group["station_ids"]
        )
    return total


def _remedy_for(agreements: list[dict[str, Any]], station_id: str) -> dict[str, Any]:
    owners: list[dict[str, Any]] = []
    for agreement in agreements:
        if station_id in agreement.get("station_ids_json", []):
            owners.append({"agreement_id": agreement["agreement_id"], "title": agreement["title"],
                           "remedy_owner": agreement["remedy_owner"],
                           "remedy_organization_id": agreement.get("remedy_organization_id")})
    if not owners:
        return {"covered": False, "agreements": []}
    return {"covered": True, "agreements": owners}


def _evaluate_consignments(snapshot: dict[str, Any], day: date,
                           trains: list[dict[str, Any]], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending = [
        item for item in snapshot.get("consignments", [])
        if item.get("status") == "accepted" and item["send_date"] == day.isoformat()
    ]
    train_lookup = {train["train_code"]: train for train in trains}
    used_capacity: dict[str, float] = {code: 0.0 for code in train_lookup}

    results: list[dict[str, Any]] = []
    for consignment in sorted(pending, key=lambda item: (item["accepted_at"], item["consignment_id"])):
        code = consignment["train_code"]
        weight = float(consignment.get("weight_kg", 0))
        train = train_lookup.get(code)
        via: str | None = None
        causes: list[str] = []

        covering_group = next(
            (group for group in groups if consignment["consignment_id"] in group["covers_consignments"]),
            None,
        )
        if covering_group is not None:
            if covering_group["cargo_capacity_kg"] <= 0:
                causes.append("replacement_without_cargo_capacity")
            elif consignment["origin_station_id"] not in covering_group["station_ids"]:
                causes.append("replacement_not_serving_origin")
            elif consignment["destination_station_id"] not in covering_group["station_ids"]:
                causes.append("replacement_not_serving_destination")
            else:
                via = f"replacement:{covering_group['group_id']}"

        if via is None and train is not None and not train["canceled"]:
            stop_ids = {stop["station_id"] for stop in train["stops"]}
            if consignment["origin_station_id"] not in stop_ids:
                causes.append("origin_not_served")
            if consignment["destination_station_id"] not in stop_ids:
                causes.append("destination_not_served")
            if not causes:
                if used_capacity[code] + weight <= float(train["cargo_capacity_kg"]):
                    used_capacity[code] += weight
                    via = f"rail:{code}"
                else:
                    causes.append("cargo_capacity_exceeded")
        elif via is None and not causes:
            if train is None:
                causes.append("train_not_in_plan")
            elif train["canceled"]:
                causes.append("train_canceled")

        if via:
            results.append({**consignment, "status": "covered", "via": via, "causes": []})
        else:
            results.append({
                **consignment,
                "status": "unmet",
                "via": None,
                "causes": causes,
                "remedy": _remedy_for(_agreements_active(snapshot, day), consignment["origin_station_id"]),
            })
    return results


def recompute_summary(snapshot: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    """对冻结快照按日期区间确定性地重算覆盖摘要。"""

    daily: list[dict[str, Any]] = []
    for current in daterange(start, end):
        result = evaluate_day(snapshot, current)
        daily.append({
            "date": result["date"],
            "unmet_commitments": result["unmet_commitments"],
            "unmet_consignments": result["unmet_consignments"],
            "station_stops": {
                station["station_id"]: len(station["rail_stops"]) + len(station["replacement_stops"])
                for station in result["stations"]
            },
        })
    return {
        "valid_from": start.isoformat(),
        "valid_to": end.isoformat(),
        "days": len(daily),
        "daily": daily,
        "unmet_commitment_days": sum(1 for item in daily if item["unmet_commitments"]),
        "unmet_consignment_count": sum(len(item["unmet_consignments"]) for item in daily),
    }
