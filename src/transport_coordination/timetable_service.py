"""山区慢火车公共服务运行图管理服务。

在基础服务的组织、场所、操作者与审计边界之上，把站点服务承诺、
赶集与就医日历、客货混装能力、检修封锁、补贴协议和临时灾害限制
纳入同一版运行图计划；草案经地方确认后才能生效，临时变更必须保住
已承诺的最低频次和已接收货物，取消与恢复都以新事实落库而不删除历史。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date as date_type
from datetime import timedelta
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import IDENTIFIER
from .storage import Database
from .timetable_domain import (
    AFFECTED_GROUPS,
    CALENDAR_KINDS,
    CHANGE_TYPES,
    OPEN_END,
    blocked_stations,
    compute_coverage,
    compute_day,
    compute_round,
    parse_date,
    served_stops,
    commitment_required,
)

TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
MAX_COVERAGE_DAYS = 93


def _idempotent(connection, now: str, *, request_id: str, action: str,
                payload: dict[str, Any],
                create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
    """与基础服务一致的幂等写入：同一 request_id 重放返回首个回执。"""

    request_id = str(request_id).strip()
    if not IDENTIFIER.fullmatch(request_id):
        raise ValidationError("request_id 格式无效")
    payload_hash = digest(payload)
    row = connection.execute(
        "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    if row:
        if row["action"] != action or row["payload_hash"] != payload_hash:
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
    resource_type, resource_id, response = create()
    connection.execute(
        "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
        "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (request_id, action, payload_hash, resource_type, resource_id,
         canonical_json(response), now),
    )
    return WriteReceipt(request_id, resource_type, resource_id, False)


class TimetableService:
    """协调运行图版本、临时变更、承诺核对与考核证据。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ---------- 基础工具 ----------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _identifier(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _date(self, value: Any, field: str) -> str:
        return parse_date(value, field)

    def _time(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not TIME.fullmatch(value):
            raise ValidationError(f"{field} 必须是 HH:MM 时间")
        return value

    def _flag(self, value: Any, field: str) -> int:
        if value in (True, 1):
            return 1
        if value in (False, 0):
            return 0
        raise ValidationError(f"{field} 必须是布尔值")

    def _nonneg_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"{field} 必须是非负整数")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _check_scope(self, actor: Actor, site) -> None:
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")

    def _affected_groups(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError("affected_groups 必须是非空数组")
        groups = sorted({str(item) for item in value})
        unknown = [item for item in groups if item not in AFFECTED_GROUPS]
        if unknown:
            raise ValidationError(f"affected_groups 包含未知人群: {', '.join(unknown)}")
        return groups

    # ---------- 事实装载（冻结证据的输入） ----------

    def _load_inputs(self, connection, site_id: str, from_date: str, to_date: str) -> dict[str, Any]:
        site = self._site(connection, site_id)
        plans = []
        plan_rows = connection.execute(
            "SELECT * FROM plan_versions WHERE site_id=? AND state IN ('effective','superseded') "
            "AND valid_from<=? AND valid_to>=? ORDER BY activated_on, plan_id",
            (site_id, to_date, from_date),
        ).fetchall()
        for row in plan_rows:
            trains = []
            for train in connection.execute(
                "SELECT * FROM plan_trains WHERE plan_id=? ORDER BY train_no", (row["plan_id"],)
            ):
                stops = [
                    {"station_id": stop["station_id"], "arrive": stop["arrive"],
                     "depart": stop["depart"], "handles_freight": stop["handles_freight"]}
                    for stop in connection.execute(
                        "SELECT * FROM plan_stops WHERE plan_id=? AND train_no=? ORDER BY sequence",
                        (row["plan_id"], train["train_no"]),
                    )
                ]
                trains.append({"train_no": train["train_no"],
                               "run_weekdays": json.loads(train["run_weekdays"]),
                               "passenger_capacity": train["passenger_capacity"],
                               "freight_capacity": train["freight_capacity"], "stops": stops})
            plans.append({"plan_id": row["plan_id"], "valid_from": row["valid_from"],
                          "valid_to": row["valid_to"], "activated_on": row["activated_on"],
                          "superseded_on": row["superseded_on"], "trains": trains})
        changes = [
            {"change_id": row["change_id"], "plan_id": row["plan_id"],
             "change_type": row["change_type"], "service_date": row["service_date"],
             "train_no": row["train_no"], "station_id": row["station_id"],
             "arrive": row["arrive"], "depart": row["depart"],
             "handles_freight": row["handles_freight"],
             "affected_groups": json.loads(row["affected_groups"]),
             "replacement_mode": row["replacement_mode"],
             "replacement_capacity": row["replacement_capacity"],
             "replacement_carrier": row["replacement_carrier"],
             "restores_change_id": row["restores_change_id"],
             "forced_over_commitment": row["forced_over_commitment"],
             "reason": row["reason"], "created_by": row["created_by"],
             "created_at": row["created_at"]}
            for row in connection.execute(
                "SELECT * FROM temporary_changes WHERE site_id=? AND service_date BETWEEN ? AND ? "
                "ORDER BY created_at, change_id", (site_id, from_date, to_date),
            )
        ]
        commitments = [
            {"commitment_id": row["commitment_id"], "station_id": row["station_id"],
             "min_stops_per_day": row["min_stops_per_day"],
             "cargo_acceptance": row["cargo_acceptance"],
             "serve_market_days": row["serve_market_days"],
             "serve_medical_days": row["serve_medical_days"],
             "valid_from": row["valid_from"], "valid_to": row["valid_to"]}
            for row in connection.execute(
                "SELECT * FROM service_commitments WHERE site_id=? AND valid_from<=? "
                "AND (valid_to IS NULL OR valid_to>=?) ORDER BY commitment_id",
                (site_id, to_date, from_date),
            )
        ]
        calendar = [
            {"station_id": row["station_id"], "date": row["date"], "kind": row["kind"]}
            for row in connection.execute(
                "SELECT * FROM demand_calendar_entries WHERE site_id=? AND retracted=0 "
                "AND date BETWEEN ? AND ? ORDER BY date, station_id",
                (site_id, from_date, to_date),
            )
        ]
        blockades = [
            {"station_id": row["station_id"], "start_date": row["start_date"],
             "end_date": row["end_date"]}
            for row in connection.execute(
                "SELECT * FROM blockades WHERE site_id=? AND start_date<=? AND end_date>=? "
                "ORDER BY blockade_id", (site_id, to_date, from_date),
            )
        ]
        restrictions = [
            {"station_id": row["station_id"], "start_date": row["start_date"],
             "end_date": row["end_date"]}
            for row in connection.execute(
                "SELECT * FROM disaster_restrictions WHERE site_id=? AND start_date<=? AND end_date>=? "
                "ORDER BY restriction_id", (site_id, to_date, from_date),
            )
        ]
        agreements = [
            {"agreement_id": row["agreement_id"], "station_id": row["station_id"],
             "funder_name": row["funder_name"], "liable_organization": row["liable_organization"],
             "valid_from": row["valid_from"], "valid_to": row["valid_to"]}
            for row in connection.execute(
                "SELECT * FROM subsidy_agreements WHERE site_id=? AND valid_from<=? "
                "AND (valid_to IS NULL OR valid_to>=?) ORDER BY agreement_id",
                (site_id, to_date, from_date),
            )
        ]
        goods = [
            {"acceptance_id": row["acceptance_id"], "service_date": row["service_date"],
             "train_no": row["train_no"], "station_id": row["station_id"],
             "units": row["units"], "status": row["status"]}
            for row in connection.execute(
                "SELECT * FROM goods_acceptances WHERE site_id=? AND service_date BETWEEN ? AND ? "
                "ORDER BY acceptance_id", (site_id, from_date, to_date),
            )
        ]
        return {"site_id": site_id, "site_organization": site["organization_id"],
                "from_date": from_date, "to_date": to_date, "plans": plans,
                "changes": changes, "commitments": commitments, "calendar": calendar,
                "blockades": blockades, "restrictions": restrictions,
                "agreements": agreements, "goods": goods}

    # ---------- 服务承诺与需求日历 ----------

    def register_commitment(self, *, request_id: str, actor_id: str, site_id: str,
                            station_id: str, min_stops_per_day: int,
                            cargo_acceptance: bool = False, serve_market_days: bool = False,
                            serve_medical_days: bool = False, valid_from: str,
                            valid_to: str | None = None, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "station_id": station_id,
                   "min_stops_per_day": min_stops_per_day, "cargo_acceptance": cargo_acceptance,
                   "serve_market_days": serve_market_days, "serve_medical_days": serve_medical_days,
                   "valid_from": valid_from, "valid_to": valid_to, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            station_id = self._identifier(station_id, "station_id")
            if isinstance(min_stops_per_day, bool) or not isinstance(min_stops_per_day, int) \
                    or min_stops_per_day < 1:
                raise ValidationError("min_stops_per_day 必须是不小于 1 的整数")
            cargo_acceptance = self._flag(cargo_acceptance, "cargo_acceptance")
            serve_market_days = self._flag(serve_market_days, "serve_market_days")
            serve_medical_days = self._flag(serve_medical_days, "serve_medical_days")
            valid_from = self._date(valid_from, "valid_from")
            valid_to = self._date(valid_to, "valid_to") if valid_to else None
            if valid_to and valid_to < valid_from:
                raise ValidationError("valid_to 不能早于 valid_from")
            note = str(note).strip()

            def create() -> tuple[str, str, dict[str, Any]]:
                previous = connection.execute(
                    "SELECT * FROM service_commitments WHERE site_id=? AND station_id=? "
                    "AND superseded_by IS NULL ORDER BY valid_from DESC LIMIT 1",
                    (site_id, station_id),
                ).fetchone()
                commitment_id = uuid.uuid4().hex
                if previous is not None:
                    if valid_from <= previous["valid_from"]:
                        raise ConflictError("新承诺的生效日期必须晚于现行承诺")
                    # 现行承诺在新承诺生效前一日关闭，历史版本保留可复算。
                    close_to = (date_type.fromisoformat(valid_from) - timedelta(days=1)).isoformat()
                    if previous["valid_to"] and previous["valid_to"] < close_to:
                        close_to = previous["valid_to"]
                    connection.execute(
                        "UPDATE service_commitments SET valid_to=?, superseded_by=? "
                        "WHERE commitment_id=?",
                        (close_to, commitment_id, previous["commitment_id"]),
                    )
                connection.execute(
                    "INSERT INTO service_commitments(commitment_id,site_id,station_id,"
                    "min_stops_per_day,cargo_acceptance,serve_market_days,serve_medical_days,"
                    "valid_from,valid_to,superseded_by,note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
                    (commitment_id, site_id, station_id, min_stops_per_day, cargo_acceptance,
                     serve_market_days, serve_medical_days, valid_from, valid_to, note,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="timetable.commitment.registered",
                             resource_type="service_commitment", resource_id=commitment_id,
                             detail={"site_id": site_id, "station_id": station_id,
                                     "min_stops_per_day": min_stops_per_day,
                                     "valid_from": valid_from, "valid_to": valid_to,
                                     "supersedes": previous["commitment_id"] if previous else None},
                             occurred_at=self._now())
                return "service_commitment", commitment_id, {"commitment_id": commitment_id}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.commitment.register", payload=payload, create=create)

    def register_calendar_entry(self, *, request_id: str, actor_id: str, site_id: str,
                                station_id: str, date: str, kind: str,
                                note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "station_id": station_id,
                   "date": date, "kind": kind, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            station_id = self._identifier(station_id, "station_id")
            date = self._date(date, "date")
            if kind not in CALENDAR_KINDS:
                raise ValidationError("kind 必须是 market 或 medical")
            note = str(note).strip()

            def create() -> tuple[str, str, dict[str, Any]]:
                entry_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO demand_calendar_entries(entry_id,site_id,station_id,date,kind,"
                    "note,retracted,retracted_by,retracted_at,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,0,NULL,NULL,?,?)",
                    (entry_id, site_id, station_id, date, kind, note, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id,
                             action="timetable.calendar_entry.registered",
                             resource_type="demand_calendar_entry", resource_id=entry_id,
                             detail={"site_id": site_id, "station_id": station_id,
                                     "date": date, "kind": kind},
                             occurred_at=self._now())
                return "demand_calendar_entry", entry_id, {"entry_id": entry_id}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.calendar_entry.register", payload=payload,
                               create=create)

    def retract_calendar_entry(self, *, request_id: str, actor_id: str, entry_id: str,
                               reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "entry_id": entry_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM demand_calendar_entries WHERE entry_id=?", (entry_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("日历条目不存在")
                site = self._site(connection, row["site_id"])
                self._check_scope(actor, site)
                if row["retracted"]:
                    raise ConflictError("日历条目已撤回")
                connection.execute(
                    "UPDATE demand_calendar_entries SET retracted=1, retracted_by=?, retracted_at=? "
                    "WHERE entry_id=?",
                    (actor_id, self._now(), entry_id),
                )
                append_event(connection, actor_id=actor_id,
                             action="timetable.calendar_entry.retracted",
                             resource_type="demand_calendar_entry", resource_id=entry_id,
                             detail={"site_id": row["site_id"], "station_id": row["station_id"],
                                     "date": row["date"], "kind": row["kind"], "reason": reason},
                             occurred_at=self._now())
                return "demand_calendar_entry", entry_id, {"entry_id": entry_id}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.calendar_entry.retract", payload=payload,
                               create=create)

    # ---------- 运行图版本 ----------

    def create_plan(self, *, request_id: str, actor_id: str, site_id: str, label: str,
                    valid_from: str, valid_to: str,
                    trains: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "label": label,
                   "valid_from": valid_from, "valid_to": valid_to, "trains": trains}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            label = self._text(label, "label")
            valid_from = self._date(valid_from, "valid_from")
            valid_to = self._date(valid_to, "valid_to")
            if valid_to < valid_from:
                raise ValidationError("valid_to 不能早于 valid_from")
            normalized = self._normalize_trains(trains)

            def create() -> tuple[str, str, dict[str, Any]]:
                plan_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO plan_versions(plan_id,site_id,label,state,valid_from,valid_to,"
                    "confirmed_by,confirmed_at,activated_on,superseded_on,superseded_by,"
                    "created_by,created_at) VALUES(?,?,?,'draft',?,?,NULL,NULL,NULL,NULL,NULL,?,?)",
                    (plan_id, site_id, label, valid_from, valid_to, actor_id, self._now()),
                )
                for train in normalized:
                    connection.execute(
                        "INSERT INTO plan_trains(plan_id,train_no,run_weekdays,passenger_capacity,"
                        "freight_capacity) VALUES(?,?,?,?,?)",
                        (plan_id, train["train_no"], canonical_json(train["run_weekdays"]),
                         train["passenger_capacity"], train["freight_capacity"]),
                    )
                    for sequence, stop in enumerate(train["stops"], start=1):
                        connection.execute(
                            "INSERT INTO plan_stops(plan_id,train_no,station_id,sequence,arrive,"
                            "depart,handles_freight) VALUES(?,?,?,?,?,?,?)",
                            (plan_id, train["train_no"], stop["station_id"], sequence,
                             stop["arrive"], stop["depart"], stop["handles_freight"]),
                        )
                append_event(connection, actor_id=actor_id, action="timetable.plan.created",
                             resource_type="plan_version", resource_id=plan_id,
                             detail={"site_id": site_id, "label": label, "valid_from": valid_from,
                                     "valid_to": valid_to,
                                     "trains": [train["train_no"] for train in normalized]},
                             occurred_at=self._now())
                return "plan_version", plan_id, {"plan_id": plan_id, "state": "draft"}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.plan.create", payload=payload, create=create)

    def _normalize_trains(self, trains: Any) -> list[dict[str, Any]]:
        if not isinstance(trains, list) or not trains:
            raise ValidationError("trains 必须是非空数组")
        normalized = []
        seen_trains: set[str] = set()
        for index, train in enumerate(trains):
            if not isinstance(train, dict):
                raise ValidationError("trains 元素必须是对象")
            train_no = self._identifier(train.get("train_no", ""), f"trains[{index}].train_no")
            if train_no in seen_trains:
                raise ValidationError(f"车次 {train_no} 重复")
            seen_trains.add(train_no)
            run_weekdays = train.get("run_weekdays")
            if not isinstance(run_weekdays, list) or not run_weekdays \
                    or any(not isinstance(day, int) or isinstance(day, bool)
                           or day < 0 or day > 6 for day in run_weekdays):
                raise ValidationError("run_weekdays 必须是 0 到 6 的非空整数数组")
            run_weekdays = sorted(set(run_weekdays))
            passenger_capacity = self._nonneg_int(train.get("passenger_capacity"),
                                                  "passenger_capacity")
            freight_capacity = self._nonneg_int(train.get("freight_capacity"), "freight_capacity")
            stops = train.get("stops")
            if not isinstance(stops, list) or not stops:
                raise ValidationError(f"车次 {train_no} 必须至少有一个停靠站")
            normalized_stops = []
            seen_stations: set[str] = set()
            for position, stop in enumerate(stops):
                if not isinstance(stop, dict):
                    raise ValidationError("stops 元素必须是对象")
                station_id = self._identifier(stop.get("station_id", ""), "station_id")
                if station_id in seen_stations:
                    raise ValidationError(f"车次 {train_no} 在站点 {station_id} 重复停靠")
                seen_stations.add(station_id)
                normalized_stops.append({
                    "station_id": station_id,
                    "arrive": self._time(stop.get("arrive", ""), "arrive"),
                    "depart": self._time(stop.get("depart", ""), "depart"),
                    "handles_freight": self._flag(stop.get("handles_freight", False),
                                                  "handles_freight"),
                })
            normalized.append({"train_no": train_no, "run_weekdays": run_weekdays,
                               "passenger_capacity": passenger_capacity,
                               "freight_capacity": freight_capacity, "stops": normalized_stops})
        return normalized

    def confirm_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan = self._plan(connection, plan_id)
                site = self._site(connection, plan["site_id"])
                self._check_scope(actor, site)
                if plan["state"] != "draft":
                    raise ConflictError("只有草案可以提交地方确认")
                connection.execute(
                    "UPDATE plan_versions SET state='confirmed', confirmed_by=?, confirmed_at=? "
                    "WHERE plan_id=?",
                    (actor_id, self._now(), plan_id),
                )
                append_event(connection, actor_id=actor_id, action="timetable.plan.confirmed",
                             resource_type="plan_version", resource_id=plan_id,
                             detail={"site_id": plan["site_id"], "confirmed_by": actor_id},
                             occurred_at=self._now())
                return "plan_version", plan_id, {"plan_id": plan_id, "state": "confirmed"}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.plan.confirm", payload=payload, create=create)

    def activate_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan = self._plan(connection, plan_id)
                site = self._site(connection, plan["site_id"])
                self._check_scope(actor, site)
                if plan["state"] == "draft":
                    raise ConflictError("草案未经地方确认不能生效")
                if plan["state"] != "confirmed":
                    raise ConflictError("只有已确认的运行图可以生效")
                today = self._today()
                current = connection.execute(
                    "SELECT * FROM plan_versions WHERE site_id=? AND state='effective'",
                    (plan["site_id"],),
                ).fetchone()
                if current is not None:
                    connection.execute(
                        "UPDATE plan_versions SET state='superseded', superseded_on=?, "
                        "superseded_by=? WHERE plan_id=?",
                        (today, plan_id, current["plan_id"]),
                    )
                connection.execute(
                    "UPDATE plan_versions SET state='effective', activated_on=? WHERE plan_id=?",
                    (today, plan_id),
                )
                append_event(connection, actor_id=actor_id, action="timetable.plan.activated",
                             resource_type="plan_version", resource_id=plan_id,
                             detail={"site_id": plan["site_id"], "activated_on": today,
                                     "supersedes": current["plan_id"] if current else None},
                             occurred_at=self._now())
                return "plan_version", plan_id, {"plan_id": plan_id, "state": "effective"}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.plan.activate", payload=payload, create=create)

    def _plan(self, connection, plan_id: str):
        row = connection.execute(
            "SELECT * FROM plan_versions WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("运行图版本不存在")
        return row

    # ---------- 检修封锁、灾害限制与补贴协议 ----------

    def _register_window_fact(self, *, table: str, id_column: str, action: str,
                              request_id: str, actor_id: str, site_id: str, station_id: str,
                              start_date: str, end_date: str, reason: str,
                              resource_type: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "station_id": station_id,
                   "start_date": start_date, "end_date": end_date, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            station_id = self._identifier(station_id, "station_id")
            start_date = self._date(start_date, "start_date")
            end_date = self._date(end_date, "end_date")
            if end_date < start_date:
                raise ValidationError("end_date 不能早于 start_date")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                fact_id = uuid.uuid4().hex
                connection.execute(
                    f"INSERT INTO {table}({id_column},site_id,station_id,start_date,end_date,"
                    f"reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (fact_id, site_id, station_id, start_date, end_date, reason,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action=action,
                             resource_type=resource_type, resource_id=fact_id,
                             detail={"site_id": site_id, "station_id": station_id,
                                     "start_date": start_date, "end_date": end_date,
                                     "reason": reason},
                             occurred_at=self._now())
                return resource_type, fact_id, {id_column: fact_id}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action=action, payload=payload, create=create)

    def register_blockade(self, *, request_id: str, actor_id: str, site_id: str,
                          station_id: str, start_date: str, end_date: str,
                          reason: str) -> WriteReceipt:
        return self._register_window_fact(
            table="blockades", id_column="blockade_id", action="timetable.blockade.registered",
            request_id=request_id, actor_id=actor_id, site_id=site_id, station_id=station_id,
            start_date=start_date, end_date=end_date, reason=reason, resource_type="blockade")

    def register_restriction(self, *, request_id: str, actor_id: str, site_id: str,
                             station_id: str, start_date: str, end_date: str,
                             reason: str) -> WriteReceipt:
        return self._register_window_fact(
            table="disaster_restrictions", id_column="restriction_id",
            action="timetable.restriction.registered",
            request_id=request_id, actor_id=actor_id, site_id=site_id, station_id=station_id,
            start_date=start_date, end_date=end_date, reason=reason,
            resource_type="disaster_restriction")

    def register_subsidy_agreement(self, *, request_id: str, actor_id: str, site_id: str,
                                   station_id: str, funder_name: str,
                                   liable_organization: str, valid_from: str,
                                   valid_to: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "station_id": station_id,
                   "funder_name": funder_name, "liable_organization": liable_organization,
                   "valid_from": valid_from, "valid_to": valid_to}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            station_id = self._identifier(station_id, "station_id")
            funder_name = self._text(funder_name, "funder_name")
            liable_organization = self._text(liable_organization, "liable_organization")
            valid_from = self._date(valid_from, "valid_from")
            valid_to = self._date(valid_to, "valid_to") if valid_to else None
            if valid_to and valid_to < valid_from:
                raise ValidationError("valid_to 不能早于 valid_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                agreement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO subsidy_agreements(agreement_id,site_id,station_id,funder_name,"
                    "liable_organization,valid_from,valid_to,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (agreement_id, site_id, station_id, funder_name, liable_organization,
                     valid_from, valid_to, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id,
                             action="timetable.subsidy_agreement.registered",
                             resource_type="subsidy_agreement", resource_id=agreement_id,
                             detail={"site_id": site_id, "station_id": station_id,
                                     "funder_name": funder_name,
                                     "liable_organization": liable_organization,
                                     "valid_from": valid_from, "valid_to": valid_to},
                             occurred_at=self._now())
                return "subsidy_agreement", agreement_id, {"agreement_id": agreement_id}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.subsidy_agreement.register", payload=payload,
                               create=create)

    # ---------- 已接收货物 ----------

    def accept_goods(self, *, request_id: str, actor_id: str, site_id: str,
                     service_date: str, train_no: str, station_id: str, units: int,
                     description: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "service_date": service_date,
                   "train_no": train_no, "station_id": station_id, "units": units,
                   "description": description}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            service_date = self._date(service_date, "service_date")
            train_no = self._identifier(train_no, "train_no")
            station_id = self._identifier(station_id, "station_id")
            if isinstance(units, bool) or not isinstance(units, int) or units < 1:
                raise ValidationError("units 必须是不小于 1 的整数")
            description = self._text(description, "description")

            def create() -> tuple[str, str, dict[str, Any]]:
                inputs = self._load_inputs(connection, site_id, service_date, service_date)
                day = compute_day(inputs, service_date)
                train = next((item for item in day["trains"]
                              if item["train_no"] == train_no), None)
                if train is None or train["status"] != "scheduled":
                    raise ValidationError("该日期车次不在采用的运行图中")
                stop = next((item for item in train["stops"]
                             if item["station_id"] == station_id
                             and item["status"] in ("scheduled", "added")), None)
                if stop is None or not stop["handles_freight"]:
                    raise ValidationError("该日期车次在此站不办理小件货运")
                accepted = connection.execute(
                    "SELECT COALESCE(SUM(units),0) AS total FROM goods_acceptances "
                    "WHERE site_id=? AND service_date=? AND train_no=? AND status='accepted'",
                    (site_id, service_date, train_no),
                ).fetchone()["total"]
                if accepted + units > train["freight_capacity"]:
                    raise ValidationError("超出该车次的客货混装能力")
                acceptance_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO goods_acceptances(acceptance_id,site_id,service_date,train_no,"
                    "station_id,units,description,status,transferred_to,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'accepted',NULL,?,?)",
                    (acceptance_id, site_id, service_date, train_no, station_id, units,
                     description, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="timetable.goods.accepted",
                             resource_type="goods_acceptance", resource_id=acceptance_id,
                             detail={"site_id": site_id, "service_date": service_date,
                                     "train_no": train_no, "station_id": station_id,
                                     "units": units},
                             occurred_at=self._now())
                return "goods_acceptance", acceptance_id, {"acceptance_id": acceptance_id}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.goods.accept", payload=payload, create=create)

    # ---------- 临时变更（增停、越站、取消、替代运输、恢复） ----------

    def create_change(self, *, request_id: str, actor_id: str, plan_id: str,
                      change_type: str, service_date: str, train_no: str,
                      station_id: str | None = None,
                      affected_groups: list[str] | None = None,
                      reason: str, arrive: str | None = None,
                      depart: str | None = None, handles_freight: bool = False,
                      replacement: dict[str, Any] | None = None,
                      restores_change_id: str | None = None,
                      goods_transfer: dict[str, Any] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "change_type": change_type,
                   "service_date": service_date, "train_no": train_no, "station_id": station_id,
                   "affected_groups": affected_groups, "reason": reason, "arrive": arrive,
                   "depart": depart, "handles_freight": handles_freight,
                   "replacement": replacement, "restores_change_id": restores_change_id,
                   "goods_transfer": goods_transfer}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if change_type not in CHANGE_TYPES:
                raise ValidationError("change_type 不在允许范围内")
            groups = self._affected_groups(affected_groups)
            reason = self._text(reason, "reason")
            handles_freight = self._flag(handles_freight, "handles_freight")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan = self._plan(connection, plan_id)
                site = self._site(connection, plan["site_id"])
                self._check_scope(actor, site)
                if plan["state"] != "effective":
                    raise ConflictError("运行图尚未生效，不能登记临时变更")
                date = self._date(service_date, "service_date")
                if not (plan["valid_from"] <= date <= plan["valid_to"]):
                    raise ValidationError("service_date 不在运行图有效期内")
                train = connection.execute(
                    "SELECT * FROM plan_trains WHERE plan_id=? AND train_no=?",
                    (plan_id, train_no),
                ).fetchone()
                normalized_station = self._identifier(station_id, "station_id") \
                    if station_id else None
                normalized_arrive = self._time(arrive, "arrive") if arrive else None
                normalized_depart = self._time(depart, "depart") if depart else None
                replacement_mode = replacement_capacity = replacement_carrier = None
                restores = None
                forced = 0

                if change_type == "restore":
                    if not restores_change_id:
                        raise ValidationError("restore 必须指定 restores_change_id")
                    target = connection.execute(
                        "SELECT * FROM temporary_changes WHERE change_id=?",
                        (restores_change_id,),
                    ).fetchone()
                    if target is None or target["plan_id"] != plan_id:
                        raise NotFoundError("被恢复的临时变更不存在")
                    if target["change_type"] == "restore":
                        raise ValidationError("不能恢复一条恢复记录")
                    existing = connection.execute(
                        "SELECT 1 FROM temporary_changes WHERE restores_change_id=?",
                        (restores_change_id,),
                    ).fetchone()
                    if existing:
                        raise ConflictError("该临时变更已被恢复")
                    restores = restores_change_id
                    date = target["service_date"]
                    train_no_value = target["train_no"]
                    normalized_station = target["station_id"]
                else:
                    train_no_value = self._identifier(train_no, "train_no")
                    if train is None:
                        raise ValidationError("车次不在该运行图版本中")

                if change_type == "add_stop":
                    if not normalized_station or not normalized_arrive or not normalized_depart:
                        raise ValidationError("增停必须提供站点与到发时刻")
                if change_type == "skip_stop":
                    if not normalized_station:
                        raise ValidationError("越站必须提供站点")
                    stop = connection.execute(
                        "SELECT 1 FROM plan_stops WHERE plan_id=? AND train_no=? AND station_id=?",
                        (plan_id, train_no_value, normalized_station),
                    ).fetchone()
                    if stop is None:
                        raise ValidationError("该站在车次停靠序列中不存在")
                if change_type == "replacement":
                    if not normalized_station:
                        raise ValidationError("替代运输必须提供站点")
                    if not isinstance(replacement, dict):
                        raise ValidationError("replacement 必须是对象")
                    replacement_mode = self._text(replacement.get("mode", ""), "replacement.mode")
                    replacement_capacity = self._nonneg_int(replacement.get("capacity"),
                                                            "replacement.capacity")
                    replacement_carrier = self._text(replacement.get("carrier", ""),
                                                     "replacement.carrier")

                if change_type in ("skip_stop", "cancel_train"):
                    forced = self._guard_commitments(
                        connection, plan, date, train_no_value, normalized_station,
                        change_type, normalized_arrive, normalized_depart, handles_freight,
                    )
                    self._guard_goods(connection, plan, date, train_no_value,
                                      normalized_station, change_type, goods_transfer)

                change_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO temporary_changes(change_id,site_id,plan_id,change_type,"
                    "service_date,train_no,station_id,arrive,depart,handles_freight,"
                    "affected_groups,replacement_mode,replacement_capacity,replacement_carrier,"
                    "restores_change_id,forced_over_commitment,reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (change_id, plan["site_id"], plan_id, change_type, date, train_no_value,
                     normalized_station, normalized_arrive, normalized_depart,
                     handles_freight if change_type == "add_stop" else None,
                     canonical_json(groups), replacement_mode, replacement_capacity,
                     replacement_carrier, restores, forced, reason, actor_id, self._now()),
                )
                if change_type in ("skip_stop", "cancel_train") and goods_transfer:
                    self._transfer_goods(connection, plan["site_id"], date, train_no_value,
                                         normalized_station, change_type, change_id)
                append_event(connection, actor_id=actor_id,
                             action=f"timetable.change.{change_type}",
                             resource_type="temporary_change", resource_id=change_id,
                             detail={"site_id": plan["site_id"], "plan_id": plan_id,
                                     "service_date": date, "train_no": train_no_value,
                                     "station_id": normalized_station,
                                     "affected_groups": groups,
                                     "restores_change_id": restores,
                                     "forced_over_commitment": bool(forced),
                                     "reason": reason},
                             occurred_at=self._now())
                return "temporary_change", change_id, {"change_id": change_id}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action=f"timetable.change.{change_type}", payload=payload,
                               create=create)

    def _guard_commitments(self, connection, plan, date: str, train_no: str,
                           station_id: str | None, change_type: str,
                           arrive: str | None, depart: str | None,
                           handles_freight: int) -> int:
        """临时变更不得破坏已承诺的最低频次；不可抗力下放行并标记 forced。"""

        site_id = plan["site_id"]
        inputs = self._load_inputs(connection, site_id, date, date)
        hypothetical = {"change_id": "__pending__", "plan_id": plan["plan_id"],
                        "change_type": change_type, "service_date": date,
                        "train_no": train_no, "station_id": station_id,
                        "arrive": arrive, "depart": depart,
                        "handles_freight": handles_freight,
                        "replacement_mode": None, "replacement_capacity": None,
                        "replacement_carrier": None, "restores_change_id": None}
        # 守卫只衡量变更本身的影响：先剔除封锁与灾害限制再对比前后服务量。
        clear = {**inputs, "blockades": [], "restrictions": []}
        before = compute_day(clear, date)
        after = compute_day({**clear, "changes": clear["changes"] + [hypothetical]}, date)
        blocked = blocked_stations(inputs, date)
        violations = []
        for commitment in inputs["commitments"]:
            if not (commitment["valid_from"] <= date <= (commitment["valid_to"] or OPEN_END)):
                continue
            if not commitment_required(commitment, date, inputs["calendar"]):
                continue
            station = commitment["station_id"]
            before_served, _ = served_stops(before, station)
            after_served, _ = served_stops(after, station)
            if before_served >= commitment["min_stops_per_day"] > after_served:
                violations.append(station)
        if not violations:
            return 0
        if all(station in blocked for station in violations):
            return 1
        raise ValidationError(
            "临时变更会破坏站点 " + ", ".join(sorted(violations)) + " 已承诺的最低频次")

    def _pending_goods(self, connection, site_id: str, date: str, train_no: str,
                       station_id: str | None, change_type: str):
        if change_type == "skip_stop":
            return connection.execute(
                "SELECT * FROM goods_acceptances WHERE site_id=? AND service_date=? "
                "AND train_no=? AND station_id=? AND status='accepted'",
                (site_id, date, train_no, station_id),
            ).fetchall()
        return connection.execute(
            "SELECT * FROM goods_acceptances WHERE site_id=? AND service_date=? "
            "AND train_no=? AND status='accepted'",
            (site_id, date, train_no),
        ).fetchall()

    def _guard_goods(self, connection, plan, date: str, train_no: str,
                     station_id: str | None, change_type: str,
                     goods_transfer: dict[str, Any] | None) -> None:
        """临时变更必须安置已接收货物，否则拒绝落库。"""

        pending = self._pending_goods(connection, plan["site_id"], date, train_no,
                                      station_id, change_type)
        if not pending:
            return
        units = sum(row["units"] for row in pending)
        if not isinstance(goods_transfer, dict):
            raise ValidationError("临时变更必须安置已接收货物（goods_transfer 缺失）")
        capacity = goods_transfer.get("capacity")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < units:
            raise ValidationError("goods_transfer.capacity 不足以转运已接收货物")
        carrier = str(goods_transfer.get("carrier", "")).strip()
        if not carrier:
            raise ValidationError("goods_transfer.carrier 不能为空")

    def _transfer_goods(self, connection, site_id: str, date: str, train_no: str,
                        station_id: str | None, change_type: str, change_id: str) -> None:
        pending = self._pending_goods(connection, site_id, date, train_no,
                                      station_id, change_type)
        for row in pending:
            connection.execute(
                "UPDATE goods_acceptances SET status='transferred', transferred_to=? "
                "WHERE acceptance_id=?",
                (change_id, row["acceptance_id"]),
            )

    # ---------- 客流与评估轮次 ----------

    def record_ridership(self, *, request_id: str, actor_id: str, site_id: str,
                         service_date: str, train_no: str, station_id: str,
                         passengers: int, freight_units: int = 0) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "service_date": service_date,
                   "train_no": train_no, "station_id": station_id, "passengers": passengers,
                   "freight_units": freight_units}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            service_date = self._date(service_date, "service_date")
            train_no = self._identifier(train_no, "train_no")
            station_id = self._identifier(station_id, "station_id")
            passengers = self._nonneg_int(passengers, "passengers")
            freight_units = self._nonneg_int(freight_units, "freight_units")

            def create() -> tuple[str, str, dict[str, Any]]:
                observation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO ridership_observations(observation_id,site_id,service_date,"
                    "train_no,station_id,passengers,freight_units,submitted_at,round_id,"
                    "created_by) VALUES(?,?,?,?,?,?,?,?,NULL,?)",
                    (observation_id, site_id, service_date, train_no, station_id, passengers,
                     freight_units, self._now(), actor_id),
                )
                append_event(connection, actor_id=actor_id, action="timetable.ridership.recorded",
                             resource_type="ridership_observation", resource_id=observation_id,
                             detail={"site_id": site_id, "service_date": service_date,
                                     "train_no": train_no, "station_id": station_id,
                                     "passengers": passengers, "freight_units": freight_units},
                             occurred_at=self._now())
                return "ridership_observation", observation_id, {"observation_id": observation_id}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.ridership.record", payload=payload, create=create)

    def freeze_coverage(self, *, request_id: str, actor_id: str, site_id: str,
                        from_date: str, to_date: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id,
                   "from_date": from_date, "to_date": to_date}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            self._site(connection, site_id)
            from_date = self._date(from_date, "from_date")
            to_date = self._date(to_date, "to_date")
            if to_date < from_date:
                raise ValidationError("to_date 不能早于 from_date")
            span = (date_type.fromisoformat(to_date)
                    - date_type.fromisoformat(from_date)).days + 1
            if span > MAX_COVERAGE_DAYS:
                raise ValidationError(f"冻结区间不能超过 {MAX_COVERAGE_DAYS} 天")

            def create() -> tuple[str, str, dict[str, Any]]:
                inputs = self._load_inputs(connection, site_id, from_date, to_date)
                result = compute_coverage(inputs)
                evidence_hash = digest({"inputs": inputs, "result": result})
                snapshot_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO coverage_snapshots(snapshot_id,site_id,from_date,to_date,"
                    "inputs_json,result_json,evidence_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (snapshot_id, site_id, from_date, to_date, canonical_json(inputs),
                     canonical_json(result), evidence_hash, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="timetable.coverage.frozen",
                             resource_type="coverage_snapshot", resource_id=snapshot_id,
                             detail={"site_id": site_id, "from_date": from_date,
                                     "to_date": to_date, "evidence_hash": evidence_hash},
                             occurred_at=self._now())
                return "coverage_snapshot", snapshot_id, {"snapshot_id": snapshot_id,
                                                          "evidence_hash": evidence_hash}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.coverage.freeze", payload=payload, create=create)

    def create_evaluation_round(self, *, request_id: str, actor_id: str, site_id: str,
                                cutoff_at: str, snapshot_id: str,
                                low_utilization_threshold: float = 5.0) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "cutoff_at": cutoff_at,
                   "snapshot_id": snapshot_id,
                   "low_utilization_threshold": low_utilization_threshold}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            self._site(connection, site_id)
            cutoff_at = str(cutoff_at).strip()
            if not cutoff_at:
                raise ValidationError("cutoff_at 不能为空")
            if isinstance(low_utilization_threshold, bool) \
                    or not isinstance(low_utilization_threshold, (int, float)) \
                    or low_utilization_threshold < 0:
                raise ValidationError("low_utilization_threshold 必须是非负数值")

            def create() -> tuple[str, str, dict[str, Any]]:
                snapshot = connection.execute(
                    "SELECT * FROM coverage_snapshots WHERE snapshot_id=?", (snapshot_id,)
                ).fetchone()
                if snapshot is None or snapshot["site_id"] != site_id:
                    raise NotFoundError("覆盖证据快照不存在")
                round_id = uuid.uuid4().hex
                # 只接收截点之前提交的客流，迟到客流保留给下一轮评估。
                connection.execute(
                    "UPDATE ridership_observations SET round_id=? WHERE site_id=? "
                    "AND round_id IS NULL AND submitted_at<=?",
                    (round_id, site_id, cutoff_at),
                )
                observations = [
                    {"station_id": row["station_id"], "passengers": row["passengers"],
                     "freight_units": row["freight_units"]}
                    for row in connection.execute(
                        "SELECT * FROM ridership_observations WHERE round_id=? "
                        "ORDER BY observation_id", (round_id,),
                    )
                ]
                deferred = connection.execute(
                    "SELECT COUNT(*) AS count FROM ridership_observations WHERE site_id=? "
                    "AND round_id IS NULL", (site_id,),
                ).fetchone()["count"]
                result = compute_round(observations, json.loads(snapshot["result_json"]),
                                       low_utilization_threshold)
                connection.execute(
                    "INSERT INTO evaluation_rounds(round_id,site_id,cutoff_at,snapshot_id,"
                    "low_utilization_threshold,result_json,included_count,deferred_count,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (round_id, site_id, cutoff_at, snapshot_id, low_utilization_threshold,
                     canonical_json(result), len(observations), deferred, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id,
                             action="timetable.evaluation_round.created",
                             resource_type="evaluation_round", resource_id=round_id,
                             detail={"site_id": site_id, "cutoff_at": cutoff_at,
                                     "snapshot_id": snapshot_id,
                                     "included_count": len(observations),
                                     "deferred_count": deferred},
                             occurred_at=self._now())
                return "evaluation_round", round_id, {"round_id": round_id}

            return _idempotent(connection, self._now(), request_id=request_id,
                               action="timetable.evaluation_round.create", payload=payload,
                               create=create)

    # ---------- 查询：按日期还原、未满足承诺、冻结证据 ----------

    def timetable_on(self, site_id: str, date: str) -> dict[str, Any]:
        """按任意日期还原实际采用的运行图。"""

        date = self._date(date, "date")
        inputs = self._load_inputs(self.database.connection, site_id, date, date)
        return compute_day(inputs, date)

    def unmet_on(self, site_id: str, date: str) -> dict[str, Any]:
        """列出指定日期未满足的服务承诺与补救责任。"""

        date = self._date(date, "date")
        inputs = self._load_inputs(self.database.connection, site_id, date, date)
        coverage = compute_coverage(inputs)
        day = coverage["days"][0]
        items = [entry for entry in day["entries"] if not entry["met"]]
        return {"site_id": site_id, "date": date, "plan_id": day["plan_id"], "items": items}

    def list_changes(self, site_id: str, date: str | None = None) -> list[dict[str, Any]]:
        """列出临时变更事实，取消与恢复都保留在历史中。"""

        self._site(self.database.connection, site_id)
        if date:
            date = self._date(date, "date")
            rows = self.database.connection.execute(
                "SELECT * FROM temporary_changes WHERE site_id=? AND service_date=? "
                "ORDER BY created_at, change_id", (site_id, date),
            ).fetchall()
        else:
            rows = self.database.connection.execute(
                "SELECT * FROM temporary_changes WHERE site_id=? ORDER BY created_at, change_id",
                (site_id,),
            ).fetchall()
        return [self._change_dict(row) for row in rows]

    def _change_dict(self, row) -> dict[str, Any]:
        return {"change_id": row["change_id"], "site_id": row["site_id"],
                "plan_id": row["plan_id"], "change_type": row["change_type"],
                "service_date": row["service_date"], "train_no": row["train_no"],
                "station_id": row["station_id"], "arrive": row["arrive"],
                "depart": row["depart"], "handles_freight": row["handles_freight"],
                "affected_groups": json.loads(row["affected_groups"]),
                "replacement_mode": row["replacement_mode"],
                "replacement_capacity": row["replacement_capacity"],
                "replacement_carrier": row["replacement_carrier"],
                "restores_change_id": row["restores_change_id"],
                "forced_over_commitment": bool(row["forced_over_commitment"]),
                "reason": row["reason"], "created_by": row["created_by"],
                "created_at": row["created_at"]}

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM coverage_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("覆盖证据快照不存在")
        return {"snapshot_id": row["snapshot_id"], "site_id": row["site_id"],
                "from_date": row["from_date"], "to_date": row["to_date"],
                "inputs": json.loads(row["inputs_json"]),
                "result": json.loads(row["result_json"]),
                "evidence_hash": row["evidence_hash"],
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def recompute_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """用冻结输入复算服务覆盖并核对证据哈希。"""

        row = self.database.connection.execute(
            "SELECT * FROM coverage_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("覆盖证据快照不存在")
        inputs = json.loads(row["inputs_json"])
        result = compute_coverage(inputs)
        recomputed_hash = digest({"inputs": inputs, "result": result})
        return {"snapshot_id": snapshot_id, "evidence_hash": row["evidence_hash"],
                "recomputed_hash": recomputed_hash,
                "match": recomputed_hash == row["evidence_hash"]}

    def get_round(self, round_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM evaluation_rounds WHERE round_id=?", (round_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("评估轮次不存在")
        return {"round_id": row["round_id"], "site_id": row["site_id"],
                "cutoff_at": row["cutoff_at"], "snapshot_id": row["snapshot_id"],
                "low_utilization_threshold": row["low_utilization_threshold"],
                "result": json.loads(row["result_json"]),
                "included_count": row["included_count"],
                "deferred_count": row["deferred_count"],
                "created_by": row["created_by"], "created_at": row["created_at"]}
