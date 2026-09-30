"""山区慢火车公共服务运行图管理领域服务。

在基础协同服务（组织、角色、场所、幂等、审计链）之上，管理：
- 站点与赶集/就医日历；
- 补贴协议与站点最低服务承诺；
- 基准运行图（列车、停站、客货混装能力）与检修封锁、灾害限制；
- 已接收小件农货、临时增停/越站/取消/恢复事实与替代运输；
- 草案确认冻结、客流评估轮次，以及任意日期的运行图还原与责任查询。

所有变更只追加事实：取消与恢复都产生新的修订记录，历史不被删除或覆盖。
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Callable

from . import coverage
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .coverage import WEEKDAY_NAMES, parse_date
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import IDENTIFIER
from .storage import Database


class PublicService:
    """执行公共服务运行图的写入、确认、变更与查询规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _date(self, value: str, field: str) -> date:
        try:
            return parse_date(value)
        except ValueError as exc:
            raise ValidationError(str(exc).replace("日期", field)) from None

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site_scope(self, connection, actor: Actor, site_id: str) -> str:
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        if actor.role != "admin" and actor.organization_id != row["organization_id"]:
            raise PermissionDenied("不能操作其他组织的场所")
        return row["site_id"]

    def _station(self, connection, station_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM ps_stations WHERE station_id=?", (station_id,)).fetchone()
        if row is None:
            raise NotFoundError("站点不存在")
        return _station_row(row)

    def _require_site_station(self, connection, actor: Actor, station_id: str) -> dict[str, Any]:
        station = self._station(connection, station_id)
        self._site_scope(connection, actor, station["site_id"])
        return station

    def _plan_row(self, connection, plan_id: str):
        row = connection.execute("SELECT * FROM ps_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("运行图计划不存在")
        return row

    def _plan_scope(self, connection, actor: Actor, plan_id: str, draft: bool | None = None):
        row = self._plan_row(connection, plan_id)
        self._site_scope(connection, actor, row["site_id"])
        if draft is not None and (row["status"] == "draft") != draft:
            if draft:
                raise ConflictError("计划已经确认，不能再修改基准内容")
            raise ConflictError("只有已确认的计划才能记录运行事实")
        return row

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    # ------------------------------------------------------------------ 站点与日历

    def register_station(self, *, request_id: str, actor_id: str, station_id: str, site_id: str,
                         name: str, township: str, populations: list[str]) -> WriteReceipt:
        populations = _string_list(populations, "populations", unique=True)
        payload = {"actor_id": actor_id, "station_id": station_id, "site_id": site_id,
                   "name": name, "township": township, "populations": populations}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            station_id = self._identifier(station_id, "station_id")
            self._site_scope(connection, actor, site_id)
            name = self._text(name, "name")
            township = self._text(township, "township")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO ps_stations(station_id,site_id,name,township,populations_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (station_id, site_id, name, township, canonical_json(populations), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("站点编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="public.station.registered",
                            resource_type="station", resource_id=station_id,
                            detail={"site_id": site_id, "name": name, "township": township,
                                    "populations": populations})
                return "station", station_id, {"station_id": station_id}

            return self._idempotent(connection, request_id=request_id, action="register_station",
                                    payload=payload, create=create)

    def register_calendar(self, *, request_id: str, actor_id: str, calendar_id: str, station_id: str,
                          kind: str, label: str, rule: dict[str, Any]) -> WriteReceipt:
        kind = self._identifier(kind, "kind")
        _validate_calendar_rule(rule)
        payload = {"actor_id": actor_id, "calendar_id": calendar_id, "station_id": station_id,
                   "kind": kind, "label": label, "rule": rule}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            calendar_id = self._identifier(calendar_id, "calendar_id")
            self._require_site_station(connection, actor, station_id)
            label = self._text(label, "label")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO ps_calendars(calendar_id,station_id,kind,label,rule_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (calendar_id, station_id, kind, label, canonical_json(rule), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("日历编号已经存在或同站点同类日历重复") from exc
                self._audit(connection, actor_id=actor_id, action="public.calendar.registered",
                            resource_type="calendar", resource_id=calendar_id,
                            detail={"station_id": station_id, "kind": kind, "label": label, "rule": rule})
                return "calendar", calendar_id, {"calendar_id": calendar_id}

            return self._idempotent(connection, request_id=request_id, action="register_calendar",
                                    payload=payload, create=create)

    # ---------------------------------------------------------- 补贴协议与服务承诺

    def register_agreement(self, *, request_id: str, actor_id: str, agreement_id: str, title: str,
                           station_ids: list[str], remedy_owner: str, valid_from: str, valid_to: str,
                           remedy_organization_id: str | None = None, terms: dict[str, Any] | None = None) -> WriteReceipt:
        station_ids = _string_list(station_ids, "station_ids", identifier=True)
        start = self._date(valid_from, "valid_from")
        end = self._date(valid_to, "valid_to")
        if start > end:
            raise ValidationError("协议生效区间无效")
        terms = terms or {}
        if not isinstance(terms, dict):
            raise ValidationError("terms 必须是对象")
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "title": title,
                   "station_ids": station_ids, "remedy_owner": remedy_owner,
                   "remedy_organization_id": remedy_organization_id, "valid_from": valid_from,
                   "valid_to": valid_to, "terms": terms}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            agreement_id = self._identifier(agreement_id, "agreement_id")
            title = self._text(title, "title")
            remedy_owner = self._text(remedy_owner, "remedy_owner")
            for station_id in station_ids:
                self._station(connection, station_id)
            if remedy_organization_id and connection.execute(
                    "SELECT 1 FROM organizations WHERE organization_id=?", (remedy_organization_id,)).fetchone() is None:
                raise NotFoundError("补救责任组织不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO ps_subsidy_agreements(agreement_id,title,station_ids_json,remedy_owner,"
                        "remedy_organization_id,valid_from,valid_to,terms_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (agreement_id, title, canonical_json(station_ids), remedy_owner, remedy_organization_id,
                         start.isoformat(), end.isoformat(), canonical_json(terms), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("协议编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="public.agreement.registered",
                            resource_type="agreement", resource_id=agreement_id,
                            detail={"title": title, "station_ids": station_ids, "remedy_owner": remedy_owner,
                                    "valid_from": start.isoformat(), "valid_to": end.isoformat()})
                return "agreement", agreement_id, {"agreement_id": agreement_id}

            return self._idempotent(connection, request_id=request_id, action="register_agreement",
                                    payload=payload, create=create)

    def register_commitment(self, *, request_id: str, actor_id: str, commitment_id: str, station_id: str,
                            title: str, demand_kinds: list[str], min_stops_on_demand_day: int,
                            agreement_id: str, min_stops_per_week: int = 0) -> WriteReceipt:
        demand_kinds = _string_list(demand_kinds, "demand_kinds", identifier=True, unique=True)
        min_stops_on_demand_day = _positive_int(min_stops_on_demand_day, "min_stops_on_demand_day")
        min_stops_per_week = _nonnegative_int(min_stops_per_week, "min_stops_per_week")
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "station_id": station_id,
                   "title": title, "demand_kinds": demand_kinds,
                   "min_stops_on_demand_day": min_stops_on_demand_day,
                   "min_stops_per_week": min_stops_per_week, "agreement_id": agreement_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            commitment_id = self._identifier(commitment_id, "commitment_id")
            self._require_site_station(connection, actor, station_id)
            title = self._text(title, "title")
            agreement = connection.execute(
                "SELECT * FROM ps_subsidy_agreements WHERE agreement_id=?", (agreement_id,)
            ).fetchone()
            if agreement is None:
                raise NotFoundError("补贴协议不存在")
            if station_id not in json.loads(agreement["station_ids_json"]):
                raise ValidationError("协议未覆盖该站点，不能挂接承诺")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO ps_commitments(commitment_id,station_id,title,demand_kinds_json,"
                        "min_stops_on_demand_day,min_stops_per_week,agreement_id,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (commitment_id, station_id, title, canonical_json(demand_kinds),
                         min_stops_on_demand_day, min_stops_per_week, agreement_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("承诺编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="public.commitment.registered",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"station_id": station_id, "title": title, "demand_kinds": demand_kinds,
                                    "min_stops_on_demand_day": min_stops_on_demand_day,
                                    "min_stops_per_week": min_stops_per_week, "agreement_id": agreement_id})
                return "commitment", commitment_id, {"commitment_id": commitment_id}

            return self._idempotent(connection, request_id=request_id, action="register_commitment",
                                    payload=payload, create=create)

    # ---------------------------------------------------------- 计划与基准运行图

    def create_plan(self, *, request_id: str, actor_id: str, plan_id: str, site_id: str, title: str,
                    valid_from: str, valid_to: str, supersedes_plan_id: str | None = None) -> WriteReceipt:
        start = self._date(valid_from, "valid_from")
        end = self._date(valid_to, "valid_to")
        if start > end:
            raise ValidationError("计划生效区间无效")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "site_id": site_id, "title": title,
                   "valid_from": valid_from, "valid_to": valid_to,
                   "supersedes_plan_id": supersedes_plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan_id = self._identifier(plan_id, "plan_id")
            self._site_scope(connection, actor, site_id)
            title = self._text(title, "title")
            if connection.execute(
                    "SELECT 1 FROM ps_plans WHERE site_id=? AND status='draft'", (site_id,)).fetchone():
                raise ConflictError("该场所已经存在未确认的草案")
            if supersedes_plan_id:
                previous = self._plan_row(connection, supersedes_plan_id)
                if previous["site_id"] != site_id:
                    raise ValidationError("只能替代同一场所的计划")
                if previous["status"] != "confirmed":
                    raise ConflictError("只能替代已经确认的计划")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO ps_plans(plan_id,site_id,title,valid_from,valid_to,supersedes_plan_id,"
                        "status,created_by,created_at) VALUES(?,?,?,?,?,?, 'draft', ?,?)",
                        (plan_id, site_id, title, start.isoformat(), end.isoformat(),
                         supersedes_plan_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("计划编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="public.plan.created",
                            resource_type="plan", resource_id=plan_id,
                            detail={"site_id": site_id, "title": title, "valid_from": start.isoformat(),
                                    "valid_to": end.isoformat(), "supersedes_plan_id": supersedes_plan_id})
                return "plan", plan_id, {"plan_id": plan_id, "status": "draft"}

            return self._idempotent(connection, request_id=request_id, action="create_plan",
                                    payload=payload, create=create)

    def add_train(self, *, request_id: str, actor_id: str, plan_id: str, train_code: str,
                  weekdays: list[str], stops: list[dict[str, Any]], cargo_capacity_kg: float = 0) -> WriteReceipt:
        weekdays = _string_list(weekdays, "weekdays", identifier=True, unique=True)
        invalid_days = sorted(set(weekdays) - set(WEEKDAY_NAMES))
        if invalid_days:
            raise ValidationError(f"weekdays 含无效取值: {','.join(invalid_days)}")
        if not isinstance(stops, list) or len(stops) < 2:
            raise ValidationError("列车至少需要两个停站")
        normalized_stops: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, stop in enumerate(stops):
            if not isinstance(stop, dict) or "station_id" not in stop:
                raise ValidationError(f"第 {index + 1} 个停站缺少 station_id")
            station_id = self._identifier(stop["station_id"], f"stops[{index}].station_id")
            if station_id in seen:
                raise ValidationError("同一列车不能重复停同一站")
            seen.add(station_id)
            normalized_stops.append({
                "station_id": station_id,
                "arrive": stop.get("arrive"),
                "depart": stop.get("depart"),
                "stop_minutes": stop.get("stop_minutes"),
            })
        try:
            cargo_capacity_kg = float(cargo_capacity_kg)
        except (TypeError, ValueError):
            raise ValidationError("cargo_capacity_kg 必须是数字") from None
        if cargo_capacity_kg < 0:
            raise ValidationError("cargo_capacity_kg 不能为负")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "train_code": train_code,
                   "weekdays": weekdays, "stops": normalized_stops, "cargo_capacity_kg": cargo_capacity_kg}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan = self._plan_scope(connection, actor, plan_id, draft=True)
            train_code = self._identifier(train_code, "train_code")
            station_ids = {row["station_id"] for row in connection.execute(
                "SELECT station_id FROM ps_stations WHERE site_id=?", (plan["site_id"],))}
            for stop in normalized_stops:
                if stop["station_id"] not in station_ids:
                    raise ValidationError(f"停站 {stop['station_id']} 不属于该场所")

            def create() -> tuple[str, str, dict[str, Any]]:
                train_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO ps_plan_trains(train_id,plan_id,train_code,weekdays_json,cargo_capacity_kg,"
                        "stops_json,created_at) VALUES(?,?,?,?,?,?,?)",
                        (train_id, plan_id, train_code, canonical_json(weekdays), cargo_capacity_kg,
                         canonical_json(normalized_stops), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("计划下列车编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="public.train.added",
                            resource_type="train", resource_id=train_id,
                            detail={"plan_id": plan_id, "train_code": train_code, "weekdays": weekdays,
                                    "cargo_capacity_kg": cargo_capacity_kg,
                                    "stations": [stop["station_id"] for stop in normalized_stops]})
                return "train", train_id, {"train_id": train_id, "train_code": train_code}

            return self._idempotent(connection, request_id=request_id, action="add_train",
                                    payload=payload, create=create)

    def register_block(self, *, request_id: str, actor_id: str, block_id: str, scope: str,
                       start_date: str, end_date: str, label: str, station_id: str | None = None,
                       from_station_id: str | None = None, to_station_id: str | None = None) -> WriteReceipt:
        return self._register_restriction(connection_table="ps_blocks", id_field="block_id",
                                          action_name="public.block.registered", resource_type="block",
                                          request_id=request_id, actor_id=actor_id, restriction_id=block_id,
                                          scope=scope, start_date=start_date, end_date=end_date, label=label,
                                          station_id=station_id, from_station_id=from_station_id,
                                          to_station_id=to_station_id, effect=None)

    def register_disaster(self, *, request_id: str, actor_id: str, disaster_id: str, scope: str, effect: str,
                          start_date: str, end_date: str, label: str, station_id: str | None = None,
                          from_station_id: str | None = None, to_station_id: str | None = None) -> WriteReceipt:
        if effect not in {"no_stop", "no_service"}:
            raise ValidationError("effect 只能是 no_stop 或 no_service")
        return self._register_restriction(connection_table="ps_disasters", id_field="disaster_id",
                                          action_name="public.disaster.registered", resource_type="disaster",
                                          request_id=request_id, actor_id=actor_id, restriction_id=disaster_id,
                                          scope=scope, start_date=start_date, end_date=end_date, label=label,
                                          station_id=station_id, from_station_id=from_station_id,
                                          to_station_id=to_station_id, effect=effect)

    def _register_restriction(self, *, connection_table: str, id_field: str, action_name: str,
                              resource_type: str, request_id: str, actor_id: str, restriction_id: str,
                              scope: str, start_date: str, end_date: str, label: str,
                              station_id: str | None, from_station_id: str | None,
                              to_station_id: str | None, effect: str | None) -> WriteReceipt:
        if scope not in {"station", "section"}:
            raise ValidationError("scope 只能是 station 或 section")
        start = self._date(start_date, "start_date")
        end = self._date(end_date, "end_date")
        if start > end:
            raise ValidationError("限制区间无效")
        payload = {"actor_id": actor_id, "restriction_id": restriction_id, "scope": scope, "effect": effect,
                   "start_date": start_date, "end_date": end_date, "label": label, "station_id": station_id,
                   "from_station_id": from_station_id, "to_station_id": to_station_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            restriction_id = self._identifier(restriction_id, id_field)
            label = self._text(label, "label")
            if scope == "station":
                if not station_id:
                    raise ValidationError("station 级限制必须提供 station_id")
                self._require_site_station(connection, actor, station_id)
            else:
                if not from_station_id or not to_station_id:
                    raise ValidationError("section 级限制必须提供 from_station_id 与 to_station_id")
                origin = self._require_site_station(connection, actor, from_station_id)
                target = self._require_site_station(connection, actor, to_station_id)
                if origin["site_id"] != target["site_id"]:
                    raise ValidationError("区段两端必须属于同一场所")
                station_id = None

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection_table == "ps_blocks":
                    columns = ("block_id,scope,station_id,from_station_id,to_station_id,start_date,end_date,"
                               "label,created_by,created_at")
                    values = (restriction_id, scope, station_id, from_station_id, to_station_id,
                              start.isoformat(), end.isoformat(), label, actor_id, self._now())
                else:
                    columns = ("disaster_id,scope,effect,station_id,from_station_id,to_station_id,start_date,"
                               "end_date,label,created_by,created_at")
                    values = (restriction_id, scope, effect, station_id, from_station_id, to_station_id,
                              start.isoformat(), end.isoformat(), label, actor_id, self._now())
                try:
                    connection.execute(f"INSERT INTO {connection_table}({columns}) "
                                       f"VALUES({','.join('?' * len(values))})", values)
                except Exception as exc:
                    raise ConflictError("限制编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action=action_name,
                            resource_type=resource_type, resource_id=restriction_id,
                            detail={"scope": scope, "effect": effect, "station_id": station_id,
                                    "from_station_id": from_station_id, "to_station_id": to_station_id,
                                    "start_date": start.isoformat(), "end_date": end.isoformat(), "label": label})
                return resource_type, restriction_id, {id_field: restriction_id}

            return self._idempotent(connection, request_id=request_id, action=action_name,
                                    payload=payload, create=create)

    # ---------------------------------------------------------- 已接收小件农货

    def accept_consignment(self, *, request_id: str, actor_id: str, consignment_id: str, plan_id: str,
                           train_code: str, origin_station_id: str, destination_station_id: str,
                           send_date: str, cargo_name: str, weight_kg: float = 0) -> WriteReceipt:
        day = self._date(send_date, "send_date")
        try:
            weight_kg = float(weight_kg)
        except (TypeError, ValueError):
            raise ValidationError("weight_kg 必须是数字") from None
        if weight_kg < 0:
            raise ValidationError("weight_kg 不能为负")
        payload = {"actor_id": actor_id, "consignment_id": consignment_id, "plan_id": plan_id,
                   "train_code": train_code, "origin_station_id": origin_station_id,
                   "destination_station_id": destination_station_id, "send_date": send_date,
                   "cargo_name": cargo_name, "weight_kg": weight_kg}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            consignment_id = self._identifier(consignment_id, "consignment_id")
            plan = self._plan_scope(connection, actor, plan_id, draft=False)
            train = self._train_row(connection, plan_id, train_code)
            cargo_name = self._text(cargo_name, "cargo_name")
            self._station(connection, origin_station_id)
            self._station(connection, destination_station_id)
            if not (parse_date(plan["valid_from"]) <= day <= parse_date(plan["valid_to"])):
                raise ValidationError("承运日期不在计划有效期内")
            if WEEKDAY_NAMES[day.weekday()] not in json.loads(train["weekdays_json"]):
                raise ValidationError("该列车基准运行图当日不开行")
            stop_ids = [stop["station_id"] for stop in json.loads(train["stops_json"])]
            if origin_station_id not in stop_ids or destination_station_id not in stop_ids:
                raise ValidationError("收发站不在该列车的基准停站序列中")
            if stop_ids.index(origin_station_id) >= stop_ids.index(destination_station_id):
                raise ValidationError("发站在基准停站序列中必须先于到站")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO ps_consignments(consignment_id,plan_id,train_code,origin_station_id,"
                        "destination_station_id,send_date,weight_kg,cargo_name,status,accepted_by,accepted_at) "
                        "VALUES(?,?,?,?,?,?,?,?,'accepted',?,?)",
                        (consignment_id, plan_id, train_code, origin_station_id, destination_station_id,
                         day.isoformat(), weight_kg, cargo_name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("货运单编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="public.consignment.accepted",
                            resource_type="consignment", resource_id=consignment_id,
                            detail={"plan_id": plan_id, "train_code": train_code,
                                    "origin_station_id": origin_station_id,
                                    "destination_station_id": destination_station_id,
                                    "send_date": day.isoformat(), "weight_kg": weight_kg,
                                    "cargo_name": cargo_name})
                return "consignment", consignment_id, {"consignment_id": consignment_id, "status": "accepted"}

            return self._idempotent(connection, request_id=request_id, action="accept_consignment",
                                    payload=payload, create=create)

    def mark_consignment_carried(self, *, request_id: str, actor_id: str, consignment_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "consignment_id": consignment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            consignment_id = self._identifier(consignment_id, "consignment_id")
            row = connection.execute("SELECT * FROM ps_consignments WHERE consignment_id=?",
                                     (consignment_id,)).fetchone()
            if row is None:
                raise NotFoundError("货运单不存在")
            plan = self._plan_row(connection, row["plan_id"])
            self._site_scope(connection, actor, plan["site_id"])
            if row["status"] != "accepted":
                raise ConflictError("只有已接收待运的货物可以标记为已运")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("UPDATE ps_consignments SET status='carried' WHERE consignment_id=?",
                                   (consignment_id,))
                self._audit(connection, actor_id=actor_id, action="public.consignment.carried",
                            resource_type="consignment", resource_id=consignment_id,
                            detail={"plan_id": row["plan_id"], "train_code": row["train_code"],
                                    "send_date": row["send_date"]})
                return "consignment", consignment_id, {"consignment_id": consignment_id, "status": "carried"}

            return self._idempotent(connection, request_id=request_id, action="mark_consignment_carried",
                                    payload=payload, create=create)

    # ---------------------------------------------------------- 临时变更事实

    def add_stop(self, *, request_id: str, actor_id: str, plan_id: str, train_code: str, station_id: str,
                 event_date: str, reason: str, end_date: str | None = None) -> WriteReceipt:
        return self._amendment(request_id=request_id, actor_id=actor_id, plan_id=plan_id, kind="add_stop",
                               train_code=train_code, station_id=station_id, event_date=event_date,
                               end_date=end_date, reason=reason)

    def skip_stop(self, *, request_id: str, actor_id: str, plan_id: str, train_code: str, station_id: str,
                  event_date: str, reason: str, end_date: str | None = None) -> WriteReceipt:
        return self._amendment(request_id=request_id, actor_id=actor_id, plan_id=plan_id, kind="skip_stop",
                               train_code=train_code, station_id=station_id, event_date=event_date,
                               end_date=end_date, reason=reason)

    def cancel_train(self, *, request_id: str, actor_id: str, plan_id: str, train_code: str,
                     event_date: str, reason: str, end_date: str | None = None) -> WriteReceipt:
        return self._amendment(request_id=request_id, actor_id=actor_id, plan_id=plan_id, kind="cancel_train",
                               train_code=train_code, station_id=None, event_date=event_date,
                               end_date=end_date, reason=reason)

    def _amendment(self, *, request_id: str, actor_id: str, plan_id: str, kind: str, train_code: str,
                   station_id: str | None, event_date: str, end_date: str | None, reason: str) -> WriteReceipt:
        start = self._date(event_date, "event_date")
        finish = self._date(end_date, "end_date") if end_date else start
        if finish < start:
            raise ValidationError("结束日期不能早于开始日期")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "kind": kind, "train_code": train_code,
                   "station_id": station_id, "event_date": event_date, "end_date": end_date, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan = self._plan_scope(connection, actor, plan_id, draft=False)
            train = self._train_row(connection, plan_id, train_code)
            reason = self._text(reason, "reason", 400)
            if not (parse_date(plan["valid_from"]) <= start and finish <= parse_date(plan["valid_to"])):
                raise ValidationError("变更日期必须落在计划有效期内")
            station = None
            if station_id is not None:
                station = self._station(connection, station_id)
                if kind == "skip_stop":
                    stop_ids = [stop["station_id"] for stop in json.loads(train["stops_json"])]
                    if station_id not in stop_ids:
                        raise ValidationError("越站必须针对该列车的基准停站")
                if kind == "add_stop" and station["site_id"] != plan["site_id"]:
                    raise ValidationError("增停站点必须属于该场所")
            populations = self._affected_populations(connection, train, kind, station_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                amendment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO ps_amendments(amendment_id,plan_id,kind,train_code,station_id,event_date,"
                    "end_date,reason,affected_populations_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (amendment_id, plan_id, kind, train_code, station_id, start.isoformat(),
                     finish.isoformat() if end_date else None, reason, canonical_json(populations),
                     actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action=f"public.amendment.{kind}",
                            resource_type="amendment", resource_id=amendment_id,
                            detail={"plan_id": plan_id, "kind": kind, "train_code": train_code,
                                    "station_id": station_id, "event_date": start.isoformat(),
                                    "end_date": finish.isoformat() if end_date else None,
                                    "reason": reason, "affected_populations": populations})
                return "amendment", amendment_id, {"amendment_id": amendment_id, "kind": kind}

            return self._idempotent(connection, request_id=request_id, action=f"amendment_{kind}",
                                    payload=payload, create=create)

    def restore_service(self, *, request_id: str, actor_id: str, plan_id: str, restores_amendment_id: str,
                        event_date: str, reason: str) -> WriteReceipt:
        day = self._date(event_date, "event_date")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "restores_amendment_id": restores_amendment_id,
                   "event_date": event_date, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan = self._plan_scope(connection, actor, plan_id, draft=False)
            original = connection.execute("SELECT * FROM ps_amendments WHERE amendment_id=?",
                                          (restores_amendment_id,)).fetchone()
            if original is None:
                raise NotFoundError("被恢复的变更不存在")
            if original["plan_id"] != plan_id:
                raise ValidationError("恢复事实与原变更不属于同一计划")
            if original["kind"] not in {"skip_stop", "cancel_train"}:
                raise ValidationError("只能恢复越站或取消事实")
            if day < parse_date(original["event_date"]):
                raise ValidationError("恢复日期不能早于原变更开始日期")
            if not (parse_date(plan["valid_from"]) <= day <= parse_date(plan["valid_to"])):
                raise ValidationError("恢复日期必须落在计划有效期内")
            if connection.execute(
                    "SELECT 1 FROM ps_amendments WHERE kind='restore' AND restores_amendment_id=?",
                    (restores_amendment_id,)).fetchone():
                raise ConflictError("该变更已经存在恢复事实")
            reason = self._text(reason, "reason", 400)
            populations = json.loads(original["affected_populations_json"])

            def create() -> tuple[str, str, dict[str, Any]]:
                amendment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO ps_amendments(amendment_id,plan_id,kind,train_code,station_id,event_date,"
                    "end_date,reason,restores_amendment_id,affected_populations_json,created_by,created_at) "
                    "VALUES(?,?,'restore',?,?,?,NULL,?,?,?,?,?)",
                    (amendment_id, plan_id, original["train_code"], original["station_id"], day.isoformat(),
                     reason, restores_amendment_id, canonical_json(populations), actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="public.amendment.restore",
                            resource_type="amendment", resource_id=amendment_id,
                            detail={"plan_id": plan_id, "restores_amendment_id": restores_amendment_id,
                                    "train_code": original["train_code"], "station_id": original["station_id"],
                                    "event_date": day.isoformat(), "reason": reason,
                                    "affected_populations": populations})
                return "amendment", amendment_id, {"amendment_id": amendment_id, "kind": "restore"}

            return self._idempotent(connection, request_id=request_id, action="amendment_restore",
                                    payload=payload, create=create)

    def _affected_populations(self, connection, train_row, kind: str, station_id: str | None) -> list[str]:
        populations: set[str] = set()
        stop_ids = [stop["station_id"] for stop in json.loads(train_row["stops_json"])]
        targets = stop_ids if kind == "cancel_train" else [station_id]
        for target in targets:
            station = self._station(connection, target)
            populations.update(station["populations_json"])
        populations.add("在途旅客" if kind == "cancel_train" else "候车旅客")
        return sorted(populations)

    # ---------------------------------------------------------- 替代运输

    def arrange_replacement(self, *, request_id: str, actor_id: str, group_id: str, plan_id: str,
                            event_date: str, station_ids: list[str], mode: str,
                            covers_consignment_ids: list[str] | None = None, capacity_seats: int | None = None,
                            cargo_capacity_kg: float = 0, amendment_id: str | None = None,
                            responsible_actor_id: str | None = None) -> WriteReceipt:
        day = self._date(event_date, "event_date")
        station_ids = _string_list(station_ids, "station_ids")
        covers = _string_list(covers_consignment_ids or [], "covers_consignment_ids", allow_empty=True)
        mode = self._text(mode, "mode", 80)
        try:
            cargo_capacity_kg = float(cargo_capacity_kg)
        except (TypeError, ValueError):
            raise ValidationError("cargo_capacity_kg 必须是数字") from None
        if cargo_capacity_kg < 0:
            raise ValidationError("cargo_capacity_kg 不能为负")
        if capacity_seats is not None:
            capacity_seats = _nonnegative_int(capacity_seats, "capacity_seats")
        payload = {"actor_id": actor_id, "group_id": group_id, "plan_id": plan_id, "event_date": event_date,
                   "station_ids": station_ids, "mode": mode, "covers_consignment_ids": covers,
                   "capacity_seats": capacity_seats, "cargo_capacity_kg": cargo_capacity_kg,
                   "amendment_id": amendment_id, "responsible_actor_id": responsible_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            group_id = self._identifier(group_id, "group_id")
            plan = self._plan_scope(connection, actor, plan_id, draft=False)
            if not (parse_date(plan["valid_from"]) <= day <= parse_date(plan["valid_to"])):
                raise ValidationError("替代运输日期必须落在计划有效期内")
            site_stations = {row["station_id"] for row in connection.execute(
                "SELECT station_id FROM ps_stations WHERE site_id=?", (plan["site_id"],))}
            for station_id in station_ids:
                if station_id not in site_stations:
                    raise ValidationError(f"站点 {station_id} 不属于该场所")
            if amendment_id:
                amendment = connection.execute("SELECT * FROM ps_amendments WHERE amendment_id=?",
                                               (amendment_id,)).fetchone()
                if amendment is None:
                    raise NotFoundError("关联的临时变更不存在")
                if amendment["plan_id"] != plan_id:
                    raise ValidationError("替代运输与变更不属于同一计划")
            for consignment_id in covers:
                consignment = connection.execute(
                    "SELECT * FROM ps_consignments WHERE consignment_id=?", (consignment_id,)).fetchone()
                if consignment is None:
                    raise NotFoundError(f"货运单 {consignment_id} 不存在")
                if consignment["plan_id"] != plan_id or consignment["send_date"] != day.isoformat():
                    raise ValidationError(f"货运单 {consignment_id} 与本次替代运输不匹配")
                if consignment["status"] != "accepted":
                    raise ConflictError(f"货运单 {consignment_id} 已不在待运状态")
            responsible = None
            if responsible_actor_id:
                responsible = self._actor(connection, responsible_actor_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                replacement_id = uuid.uuid4().hex
                affected_populations = sorted({
                    population for sid in station_ids
                    for population in self._station(connection, sid)["populations_json"]
                } | {"换乘旅客"})
                connection.execute(
                    "INSERT INTO ps_replacements(replacement_id,group_id,amendment_id,event_date,"
                    "station_ids_json,mode,capacity_seats,cargo_capacity_kg,covers_consignments_json,"
                    "responsible_actor_id,affected_populations_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (replacement_id, group_id, amendment_id, day.isoformat(), canonical_json(station_ids), mode,
                     capacity_seats, cargo_capacity_kg, canonical_json(covers),
                     responsible.actor_id if responsible else None,
                     canonical_json(affected_populations), actor_id, self._now()),
                )
                if amendment_id:
                    connection.execute(
                        "UPDATE ps_amendments SET replacement_group_id=? WHERE amendment_id=?",
                        (group_id, amendment_id),
                    )
                self._audit(connection, actor_id=actor_id, action="public.replacement.arranged",
                            resource_type="replacement", resource_id=replacement_id,
                            detail={"group_id": group_id, "plan_id": plan_id, "event_date": day.isoformat(),
                                    "station_ids": station_ids, "mode": mode,
                                    "covers_consignments": covers, "capacity_seats": capacity_seats,
                                    "cargo_capacity_kg": cargo_capacity_kg, "amendment_id": amendment_id,
                                    "responsible_actor_id": responsible.actor_id if responsible else None,
                                    "affected_populations": affected_populations})
                return "replacement", replacement_id, {"replacement_id": replacement_id, "group_id": group_id}

            return self._idempotent(connection, request_id=request_id, action="arrange_replacement",
                                    payload=payload, create=create)

    # ---------------------------------------------------------- 草案确认

    def confirm_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            plan = self._plan_row(connection, plan_id)
            self._site_scope(connection, actor, plan["site_id"])
            if plan["status"] != "draft":
                raise ConflictError("只有草案可以确认")
            trains = connection.execute("SELECT COUNT(*) AS count FROM ps_plan_trains WHERE plan_id=?",
                                        (plan_id,)).fetchone()["count"]
            if not trains:
                raise ConflictError("草案至少需要一列车才能提交确认")
            snapshot = self._build_snapshot(connection, plan)
            evidence = digest(snapshot)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE ps_plans SET status='confirmed', snapshot_json=?, evidence_hash=?, "
                    "confirmed_by=?, confirmed_at=? WHERE plan_id=?",
                    (canonical_json(snapshot), evidence, actor_id, self._now(), plan_id),
                )
                if plan["supersedes_plan_id"]:
                    connection.execute(
                        "UPDATE ps_plans SET status='superseded' WHERE plan_id=?",
                        (plan["supersedes_plan_id"],),
                    )
                self._audit(connection, actor_id=actor_id, action="public.plan.confirmed",
                            resource_type="plan", resource_id=plan_id,
                            detail={"site_id": plan["site_id"], "evidence_hash": evidence,
                                    "trains": len(snapshot["trains"]), "stations": len(snapshot["stations"]),
                                    "supersedes_plan_id": plan["supersedes_plan_id"],
                                    "confirmed_by_name": actor.display_name})
                return "plan", plan_id, {"plan_id": plan_id, "status": "confirmed", "evidence_hash": evidence}

            return self._idempotent(connection, request_id=request_id, action="confirm_plan",
                                    payload=payload, create=create)

    # ---------------------------------------------------------- 客流评估轮次

    def open_round(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan = self._plan_scope(connection, actor, plan_id, draft=False)
            if connection.execute("SELECT 1 FROM ps_rounds WHERE plan_id=? AND status='open'",
                                  (plan_id,)).fetchone():
                raise ConflictError("该计划已经存在未冻结的评估轮次")

            def create() -> tuple[str, str, dict[str, Any]]:
                round_id = uuid.uuid4().hex
                sequence_no = connection.execute(
                    "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next FROM ps_rounds WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()["next"]
                connection.execute(
                    "INSERT INTO ps_rounds(round_id,plan_id,sequence_no,status,opened_at) VALUES(?,?,?, 'open', ?)",
                    (round_id, plan_id, sequence_no, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="public.round.opened",
                            resource_type="round", resource_id=round_id,
                            detail={"plan_id": plan_id, "sequence_no": sequence_no})
                return "round", round_id, {"round_id": round_id, "sequence_no": sequence_no}

            return self._idempotent(connection, request_id=request_id, action="open_round",
                                    payload=payload, create=create)

    def submit_ridership(self, *, request_id: str, actor_id: str, round_id: str, station_id: str,
                         train_code: str, event_date: str, boarding: int, alighting: int,
                         late: int = 0) -> WriteReceipt:
        day = self._date(event_date, "event_date")
        boarding = _nonnegative_int(boarding, "boarding")
        alighting = _nonnegative_int(alighting, "alighting")
        late = 1 if late else 0
        payload = {"actor_id": actor_id, "round_id": round_id, "station_id": station_id,
                   "train_code": train_code, "event_date": event_date, "boarding": boarding,
                   "alighting": alighting, "late": late}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            round_row = connection.execute("SELECT * FROM ps_rounds WHERE round_id=?", (round_id,)).fetchone()
            if round_row is None:
                raise NotFoundError("评估轮次不存在")
            if round_row["status"] != "open":
                raise ConflictError("轮次已经冻结，迟到客流请提交到下一轮评估")
            plan = self._plan_scope(connection, actor, round_row["plan_id"], draft=False)
            self._station(connection, station_id)
            self._train_row(connection, plan["plan_id"], train_code)
            if not (parse_date(plan["valid_from"]) <= day <= parse_date(plan["valid_to"])):
                raise ValidationError("观测日期不在计划有效期内")

            def create() -> tuple[str, str, dict[str, Any]]:
                observation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO ps_ridership(observation_id,plan_id,round_id,station_id,train_code,event_date,"
                    "boarding,alighting,late,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (observation_id, plan["plan_id"], round_id, station_id, train_code, day.isoformat(),
                     boarding, alighting, late, actor_id, self._now()),
                )
                return "ridership", observation_id, {"observation_id": observation_id, "late": late}

            return self._idempotent(connection, request_id=request_id, action="submit_ridership",
                                    payload=payload, create=create)

    def freeze_round(self, *, request_id: str, actor_id: str, round_id: str,
                     valid_from: str | None = None, valid_to: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "round_id": round_id, "valid_from": valid_from, "valid_to": valid_to}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            round_row = connection.execute("SELECT * FROM ps_rounds WHERE round_id=?", (round_id,)).fetchone()
            if round_row is None:
                raise NotFoundError("评估轮次不存在")
            if round_row["status"] != "open":
                raise ConflictError("轮次已经冻结")
            plan = self._plan_row(connection, round_row["plan_id"])
            self._site_scope(connection, actor, plan["site_id"])
            start = self._date(valid_from, "valid_from") if valid_from else parse_date(plan["valid_from"])
            end = self._date(valid_to, "valid_to") if valid_to else parse_date(plan["valid_to"])
            if start > end or start < parse_date(plan["valid_from"]) or end > parse_date(plan["valid_to"]):
                raise ValidationError("复算区间必须落在计划有效期内")
            snapshot = self._build_snapshot(connection, plan)
            snapshot["ridership"] = [_ridership_row(row) for row in connection.execute(
                "SELECT * FROM ps_ridership WHERE round_id=? ORDER BY event_date, observation_id",
                (round_id,))]
            summary = coverage.recompute_summary(snapshot, start, end)
            evidence = digest({"snapshot": snapshot, "summary": summary})
            frozen = {"snapshot": snapshot, "summary": summary, "evidence_hash": evidence,
                      "frozen_by": actor_id, "frozen_at": self._now()}

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE ps_rounds SET status='frozen', frozen_at=?, summary_json=? WHERE round_id=?",
                    (self._now(), canonical_json(frozen), round_id),
                )
                self._audit(connection, actor_id=actor_id, action="public.round.frozen",
                            resource_type="round", resource_id=round_id,
                            detail={"plan_id": plan["plan_id"], "sequence_no": round_row["sequence_no"],
                                    "evidence_hash": evidence,
                                    "unmet_commitment_days": summary["unmet_commitment_days"],
                                    "unmet_consignment_count": summary["unmet_consignment_count"]})
                return "round", round_id, {"round_id": round_id, "status": "frozen",
                                           "evidence_hash": evidence}

            return self._idempotent(connection, request_id=request_id, action="freeze_round",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 查询

    def _train_row(self, connection, plan_id: str, train_code: str):
        train = connection.execute("SELECT * FROM ps_plan_trains WHERE plan_id=? AND train_code=?",
                                   (plan_id, train_code)).fetchone()
        if train is None:
            raise NotFoundError(f"计划中不存在列车 {train_code}")
        return train

    def plan_for_date(self, site_id: str, day_text: str) -> dict[str, Any]:
        """返回某场所当日生效的已确认计划（不随查询时间变化）。"""

        day = parse_date(day_text)
        row = self.database.connection.execute(
            "SELECT * FROM ps_plans WHERE site_id=? AND status='confirmed' AND valid_from<=? AND valid_to>=? "
            "ORDER BY confirmed_at DESC, plan_id LIMIT 1",
            (site_id, day.isoformat(), day.isoformat()),
        ).fetchone()
        if row is None:
            return {"date": day.isoformat(), "plan": None}
        return {"date": day.isoformat(), "plan": _plan_meta(row)}

    def day_report(self, site_id: str, day_text: str, actor_id: str | None = None) -> dict[str, Any]:
        """按任意日期还原采用的运行图、未满足承诺与补救责任。"""

        day = parse_date(day_text)
        with self.database.transaction() as connection:
            if actor_id:
                actor = self._actor(connection, actor_id)
                self._site_scope(connection, actor, site_id)
            row = connection.execute(
                "SELECT * FROM ps_plans WHERE site_id=? AND status='confirmed' AND valid_from<=? AND valid_to>=? "
                "ORDER BY confirmed_at DESC, plan_id LIMIT 1",
                (site_id, day.isoformat(), day.isoformat()),
            ).fetchone()
            if row is None:
                return {"date": day.isoformat(), "site_id": site_id, "plan": None,
                        "trains": [], "replacements": [], "stations": [], "commitments": [],
                        "consignments": [], "unmet_commitments": [], "unmet_consignments": []}
            snapshot = self._build_snapshot(connection, row)
        report = coverage.evaluate_day(snapshot, day)
        report["site_id"] = site_id
        report["plan"] = _plan_meta(row)
        return report

    def plan_snapshot(self, plan_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            plan = self._plan_row(connection, plan_id)
            return self._build_snapshot(connection, plan)

    def round_evidence(self, round_id: str) -> dict[str, Any]:
        """读取冻结证据，并用同一快照确定性复算，核对考核数字。"""

        row = self.database.connection.execute("SELECT * FROM ps_rounds WHERE round_id=?",
                                               (round_id,)).fetchone()
        if row is None:
            raise NotFoundError("评估轮次不存在")
        if row["status"] != "frozen" or not row["summary_json"]:
            return {"round_id": round_id, "status": row["status"], "frozen": False}
        frozen = json.loads(row["summary_json"])
        start = parse_date(frozen["summary"]["valid_from"])
        end = parse_date(frozen["summary"]["valid_to"])
        recomputed = coverage.recompute_summary(frozen["snapshot"], start, end)
        evidence_match = digest({"snapshot": frozen["snapshot"], "summary": recomputed}) == frozen["evidence_hash"]
        return {"round_id": round_id, "status": "frozen", "frozen": True,
                "frozen_by": frozen["frozen_by"], "frozen_at": frozen["frozen_at"],
                "summary": frozen["summary"], "recomputed": recomputed,
                "evidence_hash": frozen["evidence_hash"],
                "recomputed_match": recomputed == frozen["summary"] and evidence_match}

    def list_amendments(self, plan_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM ps_amendments WHERE plan_id=? ORDER BY created_at, amendment_id", (plan_id,)
        ).fetchall()
        return [_amendment_row(row) for row in rows]

    # ---------------------------------------------------------- 快照装配

    def _build_snapshot(self, connection, plan_row) -> dict[str, Any]:
        site_id = plan_row["site_id"]
        stations = [_station_row(row) for row in connection.execute(
            "SELECT * FROM ps_stations WHERE site_id=? ORDER BY station_id", (site_id,))]
        station_ids = [station["station_id"] for station in stations]
        calendars: list[dict[str, Any]] = []
        commitments: list[dict[str, Any]] = []
        if station_ids:
            marks = ",".join("?" * len(station_ids))
            calendars = [{
                "calendar_id": row["calendar_id"], "station_id": row["station_id"], "kind": row["kind"],
                "label": row["label"], "rule": json.loads(row["rule_json"]),
            } for row in connection.execute(
                f"SELECT * FROM ps_calendars WHERE station_id IN ({marks}) ORDER BY calendar_id", station_ids)]
            commitments = [{
                "commitment_id": row["commitment_id"], "station_id": row["station_id"], "title": row["title"],
                "demand_kinds_json": json.loads(row["demand_kinds_json"]),
                "min_stops_on_demand_day": row["min_stops_on_demand_day"],
                "min_stops_per_week": row["min_stops_per_week"], "agreement_id": row["agreement_id"],
            } for row in connection.execute(
                f"SELECT * FROM ps_commitments WHERE station_id IN ({marks}) ORDER BY commitment_id",
                station_ids)]
        agreements = [{
            "agreement_id": row["agreement_id"], "title": row["title"],
            "station_ids_json": json.loads(row["station_ids_json"]), "remedy_owner": row["remedy_owner"],
            "remedy_organization_id": row["remedy_organization_id"],
            "valid_from": row["valid_from"], "valid_to": row["valid_to"],
            "terms": json.loads(row["terms_json"]),
        } for row in connection.execute(
            "SELECT * FROM ps_subsidy_agreements WHERE valid_to>=? AND valid_from<=? ORDER BY agreement_id",
            (plan_row["valid_from"], plan_row["valid_to"]))]
        trains = [{
            "train_id": row["train_id"], "train_code": row["train_code"],
            "weekdays": json.loads(row["weekdays_json"]),
            "cargo_capacity_kg": row["cargo_capacity_kg"], "stops": json.loads(row["stops_json"]),
        } for row in connection.execute(
            "SELECT * FROM ps_plan_trains WHERE plan_id=? ORDER BY train_code", (plan_row["plan_id"],))]
        blocks = [{
            "block_id": row["block_id"], "scope": row["scope"], "station_id": row["station_id"],
            "from_station_id": row["from_station_id"], "to_station_id": row["to_station_id"],
            "start_date": row["start_date"], "end_date": row["end_date"], "label": row["label"],
        } for row in connection.execute(
            "SELECT * FROM ps_blocks WHERE start_date<=? AND end_date>=? ORDER BY start_date, block_id",
            (plan_row["valid_to"], plan_row["valid_from"]))]
        disasters = [{
            "disaster_id": row["disaster_id"], "scope": row["scope"], "effect": row["effect"],
            "station_id": row["station_id"], "from_station_id": row["from_station_id"],
            "to_station_id": row["to_station_id"], "start_date": row["start_date"],
            "end_date": row["end_date"], "label": row["label"],
        } for row in connection.execute(
            "SELECT * FROM ps_disasters WHERE start_date<=? AND end_date>=? ORDER BY start_date, disaster_id",
            (plan_row["valid_to"], plan_row["valid_from"]))]
        amendments = [_amendment_row(row) for row in connection.execute(
            "SELECT * FROM ps_amendments WHERE plan_id=? ORDER BY event_date, amendment_id",
            (plan_row["plan_id"],))]
        replacements = [{
            "replacement_id": row["replacement_id"], "group_id": row["group_id"],
            "amendment_id": row["amendment_id"], "event_date": row["event_date"],
            "station_ids_json": json.loads(row["station_ids_json"]), "mode": row["mode"],
            "capacity_seats": row["capacity_seats"], "cargo_capacity_kg": row["cargo_capacity_kg"],
            "covers_consignments_json": json.loads(row["covers_consignments_json"]),
            "responsible_actor_id": row["responsible_actor_id"],
            "affected_populations": json.loads(row["affected_populations_json"]),
        } for row in connection.execute(
            "SELECT * FROM ps_replacements WHERE event_date>=? AND event_date<=? "
            "ORDER BY event_date, group_id, replacement_id",
            (plan_row["valid_from"], plan_row["valid_to"]))
            if row["amendment_id"] is None
            or connection.execute("SELECT 1 FROM ps_amendments WHERE amendment_id=? AND plan_id=?",
                                  (row["amendment_id"], plan_row["plan_id"])).fetchone() is not None]
        consignments = [{
            "consignment_id": row["consignment_id"], "plan_id": row["plan_id"],
            "train_code": row["train_code"], "origin_station_id": row["origin_station_id"],
            "destination_station_id": row["destination_station_id"], "send_date": row["send_date"],
            "weight_kg": row["weight_kg"], "cargo_name": row["cargo_name"], "status": row["status"],
            "accepted_by": row["accepted_by"], "accepted_at": row["accepted_at"],
        } for row in connection.execute(
            "SELECT * FROM ps_consignments WHERE plan_id=? ORDER BY send_date, consignment_id",
            (plan_row["plan_id"],))]
        return {
            "plan": _plan_meta(plan_row),
            "stations": stations,
            "calendars": calendars,
            "agreements": agreements,
            "commitments": commitments,
            "trains": trains,
            "blocks": blocks,
            "disasters": disasters,
            "amendments": amendments,
            "replacements": replacements,
            "consignments": consignments,
        }


# 与行转换相关的小工具

def _station_row(row) -> dict[str, Any]:
    return {"station_id": row["station_id"], "site_id": row["site_id"], "name": row["name"],
            "township": row["township"], "populations_json": json.loads(row["populations_json"])}


def _amendment_row(row) -> dict[str, Any]:
    return {"amendment_id": row["amendment_id"], "plan_id": row["plan_id"], "kind": row["kind"],
            "train_code": row["train_code"], "station_id": row["station_id"],
            "event_date": row["event_date"], "end_date": row["end_date"], "reason": row["reason"],
            "source_type": "amendment", "affected_populations": json.loads(row["affected_populations_json"]),
            "restores_amendment_id": row["restores_amendment_id"],
            "replacement_group_id": row["replacement_group_id"]}


def _ridership_row(row) -> dict[str, Any]:
    return {"observation_id": row["observation_id"], "station_id": row["station_id"],
            "train_code": row["train_code"], "event_date": row["event_date"],
            "boarding": row["boarding"], "alighting": row["alighting"], "late": row["late"]}


def _plan_meta(row) -> dict[str, Any]:
    return {"plan_id": row["plan_id"], "site_id": row["site_id"], "title": row["title"],
            "valid_from": row["valid_from"], "valid_to": row["valid_to"], "status": row["status"],
            "supersedes_plan_id": row["supersedes_plan_id"], "evidence_hash": row["evidence_hash"],
            "confirmed_by": row["confirmed_by"], "confirmed_at": row["confirmed_at"]}


def _string_list(value: Any, field: str, *, unique: bool = False, identifier: bool = False,
                 allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValidationError(f"{field} 必须是非空数组")
    result = [str(item).strip() for item in value]
    if any(not item for item in result):
        raise ValidationError(f"{field} 不能含空值")
    if identifier:
        for item in result:
            if not IDENTIFIER.fullmatch(item):
                raise ValidationError(f"{field} 含无效取值: {item}")
    if unique and len(set(result)) != len(result):
        raise ValidationError(f"{field} 不能重复")
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{field} 必须是不小于 1 的整数")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{field} 必须是非负整数")
    return value


def _validate_calendar_rule(rule: Any) -> None:
    if not isinstance(rule, dict) or "type" not in rule:
        raise ValidationError("日历规则必须包含 type")
    kind = rule["type"]
    if kind == "weekly":
        days = rule.get("weekdays")
        if not isinstance(days, list) or not days or any(day not in WEEKDAY_NAMES for day in days):
            raise ValidationError("weekly 规则的 weekdays 必须是有效星期数组")
    elif kind == "dates":
        dates = rule.get("dates")
        if not isinstance(dates, list) or not dates:
            raise ValidationError("dates 规则必须包含日期数组")
        for value in dates:
            parse_date(value)
    elif kind == "monthly_days":
        days = rule.get("days")
        if not isinstance(days, list) or any(not isinstance(day, int) or not 1 <= day <= 31 for day in days):
            raise ValidationError("monthly_days 规则的 days 必须是 1-31 的整数数组")
    elif kind == "nth_weekday":
        if rule.get("weekday") not in WEEKDAY_NAMES or not isinstance(rule.get("nth"), int):
            raise ValidationError("nth_weekday 规则需要 weekday 与整数 nth")
    else:
        raise ValidationError(f"不支持的日历规则类型: {kind}")
