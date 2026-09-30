import json
import unittest
from datetime import datetime, timezone

from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from transport_coordination.public import PublicService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class PublicServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.service = PublicService(self.database, clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="公共服务办公室")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                 display_name="经办人", role="operator", organization_id="o1")
        self.base.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="rv1",
                                 display_name="地方联络员", role="reviewer", organization_id="o1")
        self.base.register_site(request_id="site", actor_id="op1", site_id="s1",
                                organization_id="o1", name="片区", timezone_name="Asia/Shanghai")
        for sid, name in (("st_a", "青山"), ("st_b", "云岭"), ("st_c", "花溪")):
            self.service.register_station(request_id=f"site-{sid}", actor_id="op1", station_id=sid,
                                          site_id="s1", name=name, township=name,
                                          populations=["赶集群众", "就医群众"])
        self.service.register_agreement(request_id="agr", actor_id="op1", agreement_id="agr1",
                                        title="补贴协议", station_ids=["st_a", "st_b", "st_c"],
                                        remedy_owner="县交通局", valid_from="2026-10-01",
                                        valid_to="2026-10-31")
        self.service.register_commitment(request_id="cm", actor_id="op1", commitment_id="cm_b",
                                         station_id="st_b", title="云岭赶集保一班",
                                         demand_kinds=["market"], min_stops_on_demand_day=1,
                                         agreement_id="agr1")
        self.service.register_calendar(request_id="cal", actor_id="op1", calendar_id="cal_b",
                                       station_id="st_b", kind="market", label="周六赶集",
                                       rule={"type": "weekly", "weekdays": ["sat"]})

    def tearDown(self):
        self.database.close()

    def _create_and_confirm_plan(self):
        self.service.create_plan(request_id="plan", actor_id="op1", plan_id="p1", site_id="s1",
                                 title="10 月运行图", valid_from="2026-10-01", valid_to="2026-10-31")
        self.service.add_train(request_id="train", actor_id="op1", plan_id="p1", train_code="7265",
                               weekdays=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                               cargo_capacity_kg=500,
                               stops=[{"station_id": "st_a"}, {"station_id": "st_b"},
                                      {"station_id": "st_c"}])
        self.service.confirm_plan(request_id="confirm", actor_id="rv1", plan_id="p1")

    # ---------------------------------------------------------- 权限与生命周期

    def test_operator_cannot_confirm_plan(self):
        self.service.create_plan(request_id="plan", actor_id="op1", plan_id="p1", site_id="s1",
                                 title="10 月", valid_from="2026-10-01", valid_to="2026-10-31")
        self.service.add_train(request_id="train", actor_id="op1", plan_id="p1", train_code="7265",
                               weekdays=["sat"], stops=[{"station_id": "st_a"}, {"station_id": "st_b"}])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_plan(request_id="confirm-op", actor_id="op1", plan_id="p1")

    def test_confirmed_plan_cannot_change_base_trains(self):
        self._create_and_confirm_plan()
        with self.assertRaises(ConflictError):
            self.service.add_train(request_id="train-2", actor_id="op1", plan_id="p1", train_code="7266",
                                   weekdays=["sun"], stops=[{"station_id": "st_a"}, {"station_id": "st_b"}])

    def test_amendments_require_confirmed_plan(self):
        self.service.create_plan(request_id="plan", actor_id="op1", plan_id="p1", site_id="s1",
                                 title="10 月", valid_from="2026-10-01", valid_to="2026-10-31")
        with self.assertRaises(ConflictError):
            self.service.skip_stop(request_id="skip", actor_id="op1", plan_id="p1", train_code="7265",
                                   station_id="st_b", event_date="2026-10-03", reason="草案期不允许变更")

    def test_commitment_requires_agreement_coverage(self):
        with self.assertRaises(NotFoundError):
            self.service.register_commitment(request_id="cm-x", actor_id="op1", commitment_id="cm_x",
                                             station_id="st_b", title="无协议承诺",
                                             demand_kinds=["market"], min_stops_on_demand_day=1,
                                             agreement_id="missing")

    # ---------------------------------------------------------- 变更事实链

    def test_cancel_restore_and_replacement_history(self):
        self._create_and_confirm_plan()
        cancel = self.service.cancel_train(request_id="cancel", actor_id="op1", plan_id="p1",
                                           train_code="7265", event_date="2026-10-03",
                                           reason="水害停运")
        gap = self.service.day_report("s1", "2026-10-03")
        self.assertTrue(gap["trains"][0]["canceled"])
        self.assertEqual(["cm_b"], gap["unmet_commitments"])

        self.service.arrange_replacement(request_id="bus", actor_id="op1", group_id="g1", plan_id="p1",
                                         event_date="2026-10-03", station_ids=["st_b"],
                                         mode="公路班车", capacity_seats=20,
                                         amendment_id=cancel.resource_id)
        covered = self.service.day_report("s1", "2026-10-03")
        self.assertEqual([], covered["unmet_commitments"])

        # 取消历史仍在，恢复是新事实。
        self.service.restore_service(request_id="restore", actor_id="op1", plan_id="p1",
                                     restores_amendment_id=cancel.resource_id,
                                     event_date="2026-10-04", reason="抢修完成")
        kinds = {row["kind"] for row in self.service.list_amendments("p1")}
        self.assertEqual({"cancel_train", "restore"}, kinds)
        restored = self.service.day_report("s1", "2026-10-04")
        self.assertFalse(restored["trains"][0]["canceled"])

    def test_restore_cannot_precede_original_fact(self):
        self._create_and_confirm_plan()
        cancel = self.service.cancel_train(request_id="cancel", actor_id="op1", plan_id="p1",
                                           train_code="7265", event_date="2026-10-03",
                                           reason="水害停运")
        with self.assertRaises(ValidationError):
            self.service.restore_service(request_id="restore-bad", actor_id="op1", plan_id="p1",
                                         restores_amendment_id=cancel.resource_id,
                                         event_date="2026-10-02", reason="提前恢复")

    def test_cancel_fact_names_affected_populations(self):
        self._create_and_confirm_plan()
        cancel = self.service.cancel_train(request_id="cancel", actor_id="op1", plan_id="p1",
                                           train_code="7265", event_date="2026-10-03",
                                           reason="水害停运")
        row = self.database.connection.execute(
            "SELECT affected_populations_json FROM ps_amendments WHERE amendment_id=?",
            (cancel.resource_id,)).fetchone()
        self.assertIn("赶集群众", json.loads(row["affected_populations_json"]))
        self.assertIn("在途旅客", json.loads(row["affected_populations_json"]))

    # ---------------------------------------------------------- 货物保障

    def test_accepted_consignment_is_protected_through_cancellation(self):
        self._create_and_confirm_plan()
        self.service.accept_consignment(request_id="pkg", actor_id="op1", consignment_id="pkg1",
                                        plan_id="p1", train_code="7265",
                                        origin_station_id="st_b", destination_station_id="st_c",
                                        send_date="2026-10-03", cargo_name="核桃", weight_kg=20)
        self.service.cancel_train(request_id="cancel", actor_id="op1", plan_id="p1", train_code="7265",
                                  event_date="2026-10-03", reason="水害停运")
        gap = self.service.day_report("s1", "2026-10-03")
        self.assertEqual(["pkg1"], gap["unmet_consignments"])
        self.service.arrange_replacement(request_id="bus", actor_id="op1", group_id="g1", plan_id="p1",
                                         event_date="2026-10-03", station_ids=["st_b", "st_c"],
                                         mode="公路应急车", cargo_capacity_kg=100,
                                         covers_consignment_ids=["pkg1"])
        fixed = self.service.day_report("s1", "2026-10-03")
        self.assertEqual("replacement:g1", fixed["consignments"][0]["via"])

    def test_consignment_validates_baseline_stops(self):
        self._create_and_confirm_plan()
        with self.assertRaises(ValidationError):
            self.service.accept_consignment(request_id="pkg-bad", actor_id="op1", consignment_id="pkgx",
                                            plan_id="p1", train_code="7265",
                                            origin_station_id="st_c", destination_station_id="st_b",
                                            send_date="2026-10-03", cargo_name="倒流", weight_kg=1)

    # ---------------------------------------------------------- 冻结证据与迟到客流

    def test_frozen_round_rejects_late_ridership_until_next_round(self):
        self._create_and_confirm_plan()
        self.service.open_round(request_id="r1", actor_id="op1", plan_id="p1")
        round_id = self.database.connection.execute(
            "SELECT round_id FROM ps_rounds WHERE plan_id='p1' AND sequence_no=1").fetchone()[0]
        self.service.submit_ridership(request_id="ride", actor_id="op1", round_id=round_id,
                                      station_id="st_b", train_code="7265", event_date="2026-10-03",
                                      boarding=10, alighting=2)
        self.service.freeze_round(request_id="freeze", actor_id="op1", round_id=round_id)
        evidence = self.service.round_evidence(round_id)
        self.assertTrue(evidence["recomputed_match"])
        with self.assertRaises(ConflictError):
            self.service.submit_ridership(request_id="late", actor_id="op1", round_id=round_id,
                                          station_id="st_b", train_code="7265", event_date="2026-10-03",
                                          boarding=1, alighting=1, late=1)
        self.service.open_round(request_id="r2", actor_id="op1", plan_id="p1")
        round_two = self.database.connection.execute(
            "SELECT round_id FROM ps_rounds WHERE plan_id='p1' AND sequence_no=2").fetchone()[0]
        receipt = self.service.submit_ridership(request_id="late", actor_id="op1", round_id=round_two,
                                                station_id="st_b", train_code="7265", event_date="2026-10-03",
                                                boarding=1, alighting=1, late=1)
        self.assertFalse(receipt.replayed)

    def test_idempotent_replay(self):
        first = self.service.register_station(request_id="dup", actor_id="op1", station_id="st_d",
                                              site_id="s1", name="新站", township="新乡",
                                              populations=["学生"])
        second = self.service.register_station(request_id="dup", actor_id="op1", station_id="st_d",
                                               site_id="s1", name="新站", township="新乡",
                                               populations=["学生"])
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)


if __name__ == "__main__":
    unittest.main()
