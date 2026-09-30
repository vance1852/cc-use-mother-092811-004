"""山区慢火车公共服务运行图的离线端到端验收。

在临时 SQLite 库中走完一条完整业务链：
站点服务承诺、赶集/就医日历、客货混装、检修封锁、灾害限制、补贴协议、
草案确认、已接收货物、临时取消/越站/恢复、替代运输、按日还原、冻结复算，
以及迟到客流只能进入下一轮评估。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError
from transport_coordination.public import PublicService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


def _bootstrap(database: Database, clock: FixedClock) -> tuple[DomainService, PublicService]:
    base = DomainService(database, clock)
    base.register_organization(request_id="org", actor_id="bootstrap",
                               organization_id="o1", name="山区铁路公共服务办公室")
    base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                        display_name="管理员", role="admin", organization_id="o1")
    base.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                        display_name="运行图经办人", role="operator", organization_id="o1")
    base.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="rv1",
                        display_name="地方联络员", role="reviewer", organization_id="o1")
    base.register_site(request_id="site", actor_id="op1", site_id="s1",
                       organization_id="o1", name="成昆南片区", timezone_name="Asia/Shanghai")
    return base, PublicService(database, clock)


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "public_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc))
        base, public = _bootstrap(database, clock)

        # 三个沿线站点，明确各自依靠慢火车的人群。
        public.register_station(request_id="st-a", actor_id="op1", station_id="st_qingshan",
                                site_id="s1", name="青山镇", township="青山镇",
                                populations=["赶集商贩", "就医群众"])
        public.register_station(request_id="st-b", actor_id="op1", station_id="st_yunling",
                                site_id="s1", name="云岭村", township="云岭乡",
                                populations=["赶集群众", "学生", "留守老人"])
        public.register_station(request_id="st-c", actor_id="op1", station_id="st_huaxi",
                                site_id="s1", name="花溪村", township="花溪乡",
                                populations=["果农", "赶集群众"])

        # 赶集（周六）、就医（每月 5 日）与青山镇固定集市日。
        public.register_calendar(request_id="cal-b-market", actor_id="op1", calendar_id="cal_yunling_market",
                                 station_id="st_yunling", kind="market", label="云岭周六赶集",
                                 rule={"type": "weekly", "weekdays": ["sat"]})
        public.register_calendar(request_id="cal-b-medical", actor_id="op1", calendar_id="cal_yunling_medical",
                                 station_id="st_yunling", kind="medical", label="每月五日乡卫生院就诊",
                                 rule={"type": "monthly_days", "days": [5]})
        public.register_calendar(request_id="cal-a-market", actor_id="op1", calendar_id="cal_qingshan_market",
                                 station_id="st_qingshan", kind="market", label="青山镇逢一集市",
                                 rule={"type": "dates", "dates": ["2026-10-01", "2026-10-15"]})
        public.register_calendar(request_id="cal-c-market", actor_id="op1", calendar_id="cal_huaxi_market",
                                 station_id="st_huaxi", kind="market", label="花溪周六赶集",
                                 rule={"type": "weekly", "weekdays": ["sat"]})

        # 补贴协议明确补救责任。
        public.register_agreement(request_id="agreement", actor_id="op1", agreement_id="agr_2026",
                                  title="山区慢火车基本出行补贴协议",
                                  station_ids=["st_qingshan", "st_yunling", "st_huaxi"],
                                  remedy_owner="县交通局", remedy_organization_id="o1",
                                  valid_from="2026-10-01", valid_to="2026-10-31",
                                  terms={"bus_backup_hours": 24})
        public.register_commitment(request_id="cm-a", actor_id="op1", commitment_id="cm_qingshan",
                                   station_id="st_qingshan", title="青山集市日至少一班",
                                   demand_kinds=["market"], min_stops_on_demand_day=1,
                                   agreement_id="agr_2026")
        public.register_commitment(request_id="cm-b", actor_id="op1", commitment_id="cm_yunling",
                                   station_id="st_yunling", title="云岭赶集就医保一班、每周不少于两班",
                                   demand_kinds=["market", "medical"], min_stops_on_demand_day=1,
                                   min_stops_per_week=2, agreement_id="agr_2026")
        public.register_commitment(request_id="cm-c", actor_id="op1", commitment_id="cm_huaxi",
                                   station_id="st_huaxi", title="花溪赶集保一班、每周不少于两班",
                                   demand_kinds=["market"], min_stops_on_demand_day=1,
                                   min_stops_per_week=2, agreement_id="agr_2026")

        # 草案版运行图：7265 次每日开行，三站均停，可混装小件农货 500kg。
        public.create_plan(request_id="plan", actor_id="op1", plan_id="plan_2026_10", site_id="s1",
                           title="2026 年 10 月山区慢火车运行图",
                           valid_from="2026-10-01", valid_to="2026-10-31")
        public.add_train(request_id="train-7265", actor_id="op1", plan_id="plan_2026_10",
                         train_code="7265", weekdays=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                         cargo_capacity_kg=500,
                         stops=[{"station_id": "st_qingshan", "depart": "07:10"},
                                {"station_id": "st_yunling", "arrive": "08:20", "depart": "08:23"},
                                {"station_id": "st_huaxi", "arrive": "09:05"}])

        # 检修封锁（花溪站 10 月 7 日）与灾害限制（云岭-花溪区段 10 月 5 日临时管控）。
        public.register_block(request_id="block-1007", actor_id="op1", block_id="blk_1007",
                              scope="station", station_id="st_huaxi",
                              start_date="2026-10-07", end_date="2026-10-07", label="花溪站道岔检修")
        public.register_disaster(request_id="disaster-1005", actor_id="op1", disaster_id="dst_1005",
                                 scope="section", effect="no_service",
                                 from_station_id="st_yunling", to_station_id="st_huaxi",
                                 start_date="2026-10-05", end_date="2026-10-05",
                                 label="持续降雨导致区间临时限速")

        # 草案经地方联络员（reviewer）确认后才生效。
        public.confirm_plan(request_id="confirm", actor_id="rv1", plan_id="plan_2026_10")
        evidence_hash = database.connection.execute(
            "SELECT evidence_hash FROM ps_plans WHERE plan_id='plan_2026_10'").fetchone()["evidence_hash"]
        assert evidence_hash

        # 赶集日前接收果农小件，10 月 3 日由 7265 次运出。
        public.accept_consignment(request_id="cargo-1", actor_id="op1", consignment_id="pkg_2026_1003",
                                  plan_id="plan_2026_10", train_code="7265",
                                  origin_station_id="st_yunling", destination_station_id="st_huaxi",
                                  send_date="2026-10-03", cargo_name="新鲜核桃", weight_kg=20)
        report_1003 = public.day_report("s1", "2026-10-03")
        assert report_1003["unmet_consignments"] == []
        assert report_1003["consignments"][0]["via"] == "rail:7265"
        public.mark_consignment_carried(request_id="cargo-1-done", actor_id="op1",
                                        consignment_id="pkg_2026_1003")

        # 10 月 1 日青山集市：列车正常停站，承诺满足。
        report_1001 = public.day_report("s1", "2026-10-01")
        assert not report_1001["unmet_commitments"]

        # 10 月 5 日就医日遇区段灾害：铁路压停云岭、花溪，安排公路替代运输兜底。
        report_1005_before = public.day_report("s1", "2026-10-05")
        assert report_1005_before["trains"][0]["suppressed"]["st_yunling"]
        assert report_1005_before["unmet_commitments"] == ["cm_yunling"]
        public.arrange_replacement(request_id="bus-1005", actor_id="op1", group_id="grp_1005",
                                   plan_id="plan_2026_10", event_date="2026-10-05",
                                   station_ids=["st_yunling", "st_huaxi"], mode="公路班车",
                                   capacity_seats=30, cargo_capacity_kg=100)
        report_1005 = public.day_report("s1", "2026-10-05")
        assert not report_1005["unmet_commitments"]
        cm_yunling = next(item for item in report_1005["commitments"] if item["commitment_id"] == "cm_yunling")
        assert cm_yunling["replacement_stops"] == ["grp_1005"]
        assert cm_yunling["remedy"]["covered"]

        # 10 月 7 日列车因水害抢修取消两日，同时已接收货物必须保住：先收货再取消。
        public.accept_consignment(request_id="cargo-2", actor_id="op1", consignment_id="pkg_2026_1007",
                                  plan_id="plan_2026_10", train_code="7265",
                                  origin_station_id="st_yunling", destination_station_id="st_huaxi",
                                  send_date="2026-10-07", cargo_name="山货样品", weight_kg=15)
        cancel = public.cancel_train(request_id="cancel-1007", actor_id="op1", plan_id="plan_2026_10",
                                     train_code="7265", event_date="2026-10-07", end_date="2026-10-08",
                                     reason="水害抢修，列车停运两日")
        canceled_amendment = cancel.resource_id
        # 取消事实明确影响了哪些人群。
        affected = json.loads(database.connection.execute(
            "SELECT affected_populations_json FROM ps_amendments WHERE amendment_id=?",
            (canceled_amendment,)).fetchone()["affected_populations_json"])
        assert "果农" in affected and "在途旅客" in affected

        # 未安排替代运输时，10 月 7 日承诺与货物双双未满足，责任落到补贴协议方。
        report_1007_gap = public.day_report("s1", "2026-10-07")
        assert report_1007_gap["trains"][0]["canceled"]
        assert "pkg_2026_1007" in report_1007_gap["unmet_consignments"]
        unmet_consignment = next(item for item in report_1007_gap["consignments"]
                                 if item["consignment_id"] == "pkg_2026_1007")
        assert unmet_consignment["remedy"]["agreements"][0]["remedy_owner"] == "县交通局"

        # 安排当日公路替代运输，挂接取消事实并明确承运这票货物。
        public.arrange_replacement(request_id="bus-1007", actor_id="op1", group_id="grp_1007",
                                   plan_id="plan_2026_10", event_date="2026-10-07",
                                   station_ids=["st_yunling", "st_huaxi"], mode="公路应急车",
                                   capacity_seats=20, cargo_capacity_kg=200,
                                   amendment_id=canceled_amendment,
                                   covers_consignment_ids=["pkg_2026_1007"])
        report_1007 = public.day_report("s1", "2026-10-07")
        assert not report_1007["unmet_commitments"]
        assert report_1007["consignments"][0]["via"] == "replacement:grp_1007"
        public.mark_consignment_carried(request_id="cargo-2-done", actor_id="op1",
                                        consignment_id="pkg_2026_1007")

        # 恢复是新事实：10 月 8 日起列车恢复，取消历史仍可查。
        public.restore_service(request_id="restore-1008", actor_id="op1", plan_id="plan_2026_10",
                               restores_amendment_id=canceled_amendment, event_date="2026-10-08",
                               reason="抢修完成，恢复开行")
        report_1008 = public.day_report("s1", "2026-10-08")
        assert not report_1008["trains"][0]["canceled"]
        amendments = public.list_amendments("plan_2026_10")
        kinds = {item["kind"] for item in amendments}
        assert {"cancel_train", "restore"} <= kinds

        # 10 月 10 日花溪赶集日越站且无替代：承诺未满足，补救责任可查。
        public.skip_stop(request_id="skip-1010", actor_id="op1", plan_id="plan_2026_10",
                         train_code="7265", station_id="st_huaxi", event_date="2026-10-10",
                         reason="临时施工慢行，单站通过")
        report_1010 = public.day_report("s1", "2026-10-10")
        assert "cm_huaxi" in report_1010["unmet_commitments"]
        cm_huaxi = next(item for item in report_1010["commitments"] if item["commitment_id"] == "cm_huaxi")
        assert cm_huaxi["unmet_reasons"] == ["demand_day_below_minimum"]
        assert cm_huaxi["remedy"]["agreements"][0]["remedy_owner"] == "县交通局"

        # 客流评估：第一轮收集观测后冻结，考核人员用冻结证据复算。
        public.open_round(request_id="round-1", actor_id="op1", plan_id="plan_2026_10")
        round_id = database.connection.execute(
            "SELECT round_id FROM ps_rounds WHERE plan_id='plan_2026_10' AND sequence_no=1").fetchone()[0]
        public.submit_ridership(request_id="ride-1", actor_id="op1", round_id=round_id,
                                station_id="st_yunling", train_code="7265", event_date="2026-10-03",
                                boarding=42, alighting=5)
        freeze = public.freeze_round(request_id="freeze-1", actor_id="op1", round_id=round_id,
                                     valid_from="2026-10-01", valid_to="2026-10-10")
        evidence = public.round_evidence(round_id)
        assert evidence["recomputed_match"]
        assert freeze.replayed is False

        # 冻结后迟到客流不能改账，只能进入下一轮评估。
        try:
            public.submit_ridership(request_id="ride-late", actor_id="op1", round_id=round_id,
                                    station_id="st_huaxi", train_code="7265", event_date="2026-10-03",
                                    boarding=3, alighting=30, late=1)
        except ConflictError:
            late_rejected = True
        else:
            late_rejected = False
        assert late_rejected
        public.open_round(request_id="round-2", actor_id="op1", plan_id="plan_2026_10")
        round_two = database.connection.execute(
            "SELECT round_id FROM ps_rounds WHERE plan_id='plan_2026_10' AND sequence_no=2").fetchone()[0]
        late_replay = public.submit_ridership(request_id="ride-late", actor_id="op1", round_id=round_two,
                                              station_id="st_huaxi", train_code="7265", event_date="2026-10-03",
                                              boarding=3, alighting=30, late=1)
        assert not late_replay.replayed

        valid, event_count = base.verify_audit()
        return {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "plan_evidence_hash": evidence_hash,
            "day_1003_unmet_consignments": report_1003["unmet_consignments"],
            "day_1005_replacements": [group["group_id"] for group in report_1005["replacements"]],
            "day_1007_train_canceled": report_1007["trains"][0]["canceled"],
            "day_1007_consignment_via": report_1007["consignments"][0]["via"],
            "day_1008_restored": not report_1008["trains"][0]["canceled"],
            "day_1010_unmet": report_1010["unmet_commitments"],
            "amendment_history": sorted(kinds),
            "freeze_recomputed_match": evidence["recomputed_match"],
            "freeze_unmet_commitment_days": evidence["summary"]["unmet_commitment_days"],
            "late_rejected_then_next_round": late_rejected and not late_replay.replayed,
        }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
