"""运行山区慢火车公共服务运行图管理的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ValidationError
from .service import DomainService
from .storage import Database
from .timetable_service import TimetableService

MARKET_DAY = "2026-10-05"
DISASTER_DAY = "2026-10-06"


def _bootstrap(domain: DomainService) -> None:
    domain.register_organization(request_id="acc-org", actor_id="bootstrap",
                                 organization_id="org-rail", name="铁路公共服务办公室")
    domain.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin",
                          display_name="系统管理员", role="admin", organization_id="org-rail")
    domain.register_actor(request_id="acc-operator", actor_id="admin", new_actor_id="operator",
                          display_name="运行图调度员", role="operator", organization_id="org-rail")
    domain.register_actor(request_id="acc-reviewer", actor_id="admin", new_actor_id="reviewer",
                          display_name="地方确认代表", role="reviewer", organization_id="org-rail")
    domain.register_actor(request_id="acc-auditor", actor_id="admin", new_actor_id="auditor",
                          display_name="考核人员", role="auditor", organization_id="org-rail")
    domain.register_site(request_id="acc-site", actor_id="operator", site_id="line-001",
                         organization_id="org-rail", name="山区慢火车线路",
                         timezone_name="Asia/Shanghai")


def _prepare_plan(service: TimetableService) -> str:
    service.register_commitment(request_id="acc-commit-heping", actor_id="operator",
                                site_id="line-001", station_id="heping",
                                min_stops_per_day=2, cargo_acceptance=True,
                                valid_from="2026-10-01", note="和平乡每日基本出行")
    service.register_commitment(request_id="acc-commit-malu", actor_id="operator",
                                site_id="line-001", station_id="malu",
                                min_stops_per_day=2, serve_market_days=True,
                                valid_from="2026-10-01", note="马路口赶集日保障")
    service.register_calendar_entry(request_id="acc-cal-market", actor_id="operator",
                                    site_id="line-001", station_id="malu",
                                    date=MARKET_DAY, kind="market", note="逢五赶集")
    service.register_subsidy_agreement(request_id="acc-subsidy", actor_id="operator",
                                       site_id="line-001", station_id="heping",
                                       funder_name="省交通厅公共服务补贴",
                                       liable_organization="org-rail",
                                       valid_from="2026-10-01")
    receipt = service.create_plan(
        request_id="acc-plan", actor_id="operator", site_id="line-001",
        label="2026 年四季度运行图", valid_from="2026-10-01", valid_to="2026-12-31",
        trains=[
            {"train_no": "5633", "run_weekdays": [0, 1, 2, 3, 4, 5, 6],
             "passenger_capacity": 320, "freight_capacity": 40,
             "stops": [
                 {"station_id": "heping", "arrive": "08:10", "depart": "08:14",
                  "handles_freight": True},
                 {"station_id": "malu", "arrive": "09:02", "depart": "09:05",
                  "handles_freight": True},
                 {"station_id": "qingshi", "arrive": "10:11", "depart": "10:13",
                  "handles_freight": False},
             ]},
            {"train_no": "5634", "run_weekdays": [0, 1, 2, 3, 4, 5, 6],
             "passenger_capacity": 320, "freight_capacity": 40,
             "stops": [
                 {"station_id": "qingshi", "arrive": "15:02", "depart": "15:04",
                  "handles_freight": False},
                 {"station_id": "malu", "arrive": "16:10", "depart": "16:13",
                  "handles_freight": True},
                 {"station_id": "heping", "arrive": "17:05", "depart": "17:09",
                  "handles_freight": True},
             ]},
        ])
    plan_id = receipt.resource_id
    service.confirm_plan(request_id="acc-confirm", actor_id="reviewer", plan_id=plan_id)
    service.activate_plan(request_id="acc-activate", actor_id="operator", plan_id=plan_id)
    return plan_id


def run() -> dict[str, object]:
    """执行一条完整业务链并返回可核对的结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "timetable_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        domain = DomainService(database, clock)
        service = TimetableService(database, clock)
        _bootstrap(domain)
        plan_id = _prepare_plan(service)

        # 已接收货物受保护：直接越站会被拒绝，先安排替代运输并转运货物才允许。
        service.accept_goods(request_id="acc-goods", actor_id="operator", site_id="line-001",
                             service_date=MARKET_DAY, train_no="5633", station_id="heping",
                             units=10, description="高山蔬菜 10 件")
        try:
            service.create_change(request_id="acc-skip-denied", actor_id="operator",
                                  plan_id=plan_id, change_type="skip_stop",
                                  service_date=MARKET_DAY, train_no="5633",
                                  station_id="heping",
                                  affected_groups=["market_goers", "farm_shippers"],
                                  reason="客流偏低拟越站")
            skip_denied = False
        except ValidationError:
            skip_denied = True
        service.create_change(request_id="acc-replacement", actor_id="operator",
                              plan_id=plan_id, change_type="replacement",
                              service_date=MARKET_DAY, train_no="5633", station_id="heping",
                              affected_groups=["market_goers", "farm_shippers"],
                              reason="赶集日公路接驳",
                              replacement={"mode": "bus", "capacity": 45,
                                           "carrier": "县运输公司"})
        service.create_change(request_id="acc-skip", actor_id="operator", plan_id=plan_id,
                              change_type="skip_stop", service_date=MARKET_DAY,
                              train_no="5633", station_id="heping",
                              affected_groups=["market_goers", "farm_shippers"],
                              reason="客流偏低越站，货物转接驳",
                              goods_transfer={"carrier": "县运输公司", "capacity": 10})
        market_day = service.timetable_on("line-001", MARKET_DAY)

        # 灾害限制下可以强制取消车次，但未满足承诺会带上补救责任。
        service.register_restriction(request_id="acc-disaster", actor_id="operator",
                                     site_id="line-001", station_id="heping",
                                     start_date=DISASTER_DAY, end_date="2026-10-07",
                                     reason="山体滑坡临时封锁")
        service.create_change(request_id="acc-cancel", actor_id="operator", plan_id=plan_id,
                              change_type="cancel_train", service_date=DISASTER_DAY,
                              train_no="5634", affected_groups=["medical_patients", "general"],
                              reason="滑坡断道，停运一日")
        unmet = service.unmet_on("line-001", DISASTER_DAY)
        service.create_change(request_id="acc-restore", actor_id="operator", plan_id=plan_id,
                              change_type="restore", service_date=DISASTER_DAY,
                              train_no="5634", affected_groups=["medical_patients", "general"],
                              reason="线路抢通恢复",
                              restores_change_id=_change_id(service, "acc-cancel"))
        restored_day = service.timetable_on("line-001", DISASTER_DAY)

        # 考核人员冻结证据并复算；迟到客流只进入下一轮评估。
        freeze = service.freeze_coverage(request_id="acc-freeze", actor_id="auditor",
                                         site_id="line-001", from_date="2026-10-01",
                                         to_date="2026-10-07")
        snapshot_id = freeze.resource_id
        recompute = service.recompute_snapshot(snapshot_id)
        service.record_ridership(request_id="acc-ride-1", actor_id="operator",
                                 site_id="line-001", service_date=MARKET_DAY,
                                 train_no="5634", station_id="heping", passengers=63,
                                 freight_units=10)
        service.record_ridership(request_id="acc-ride-2", actor_id="operator",
                                 site_id="line-001", service_date=MARKET_DAY,
                                 train_no="5634", station_id="malu", passengers=41)
        early = service.create_evaluation_round(request_id="acc-round-early",
                                                actor_id="auditor", site_id="line-001",
                                                cutoff_at="2026-09-30T07:00:00Z",
                                                snapshot_id=snapshot_id)
        late = service.create_evaluation_round(request_id="acc-round-late",
                                               actor_id="auditor", site_id="line-001",
                                               cutoff_at="2026-09-30T09:00:00Z",
                                               snapshot_id=snapshot_id)
        early_round = service.get_round(early.resource_id)
        late_round = service.get_round(late.resource_id)
        valid, event_count = domain.verify_audit()
        result = {
            "status": "ok",
            "plan_id": plan_id,
            "skip_denied_without_goods_plan": skip_denied,
            "market_day_plan": market_day["plan_id"] == plan_id,
            "market_day_replacements": len(market_day["replacements"]),
            "unmet_count": len(unmet["items"]),
            "unmet_liable": unmet["items"][0]["liability"]["liable_organization"]
            if unmet["items"] else None,
            "restored_train_running": any(
                train["train_no"] == "5634" and train["status"] == "scheduled"
                for train in restored_day["trains"]),
            "recompute_match": recompute["match"],
            "early_round_included": early_round["included_count"],
            "late_round_included": late_round["included_count"],
            "audit_events": event_count,
            "audit_valid": valid,
        }
        database.close()
        return result


def _change_id(service: TimetableService, request_id: str) -> str:
    row = service.database.connection.execute(
        "SELECT resource_id FROM request_receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    return row["resource_id"]


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = result["status"] == "ok" and result["audit_valid"] and result["recompute_match"] \
        and result["skip_denied_without_goods_plan"] and result["restored_train_running"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
