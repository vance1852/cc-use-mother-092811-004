import unittest
from datetime import datetime, timezone

from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, PermissionDenied, ValidationError
from transport_coordination.service import DomainService
from transport_coordination.storage import Database
from transport_coordination.timetable_service import TimetableService

DATE = "2026-10-05"  # 周一


class TimetableTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, clock)
        self.service = TimetableService(self.database, clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="铁路公共服务办公室")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                   display_name="调度员", role="operator", organization_id="o1")
        self.domain.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="rv1",
                                   display_name="地方代表", role="reviewer", organization_id="o1")
        self.domain.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                   display_name="考核员", role="auditor", organization_id="o1")
        self.domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="山区线路",
                                  timezone_name="Asia/Shanghai")
        self._commitment()
        self.plan_id = self._plan()

    def tearDown(self):
        self.database.close()

    def _commitment(self):
        self.service.register_commitment(request_id="commit", actor_id="op1", site_id="s1",
                                         station_id="heping", min_stops_per_day=2,
                                         cargo_acceptance=True, valid_from="2026-10-01")

    def _plan(self, request_id="plan", label="四季度运行图"):
        receipt = self.service.create_plan(
            request_id=request_id, actor_id="op1", site_id="s1", label=label,
            valid_from="2026-10-01", valid_to="2026-12-31",
            trains=[
                {"train_no": "5633", "run_weekdays": [0, 1, 2, 3, 4, 5, 6],
                 "passenger_capacity": 300, "freight_capacity": 40,
                 "stops": [
                     {"station_id": "heping", "arrive": "08:10", "depart": "08:14",
                      "handles_freight": True},
                     {"station_id": "malu", "arrive": "09:02", "depart": "09:05",
                      "handles_freight": True},
                 ]},
                {"train_no": "5634", "run_weekdays": [0, 1, 2, 3, 4, 5, 6],
                 "passenger_capacity": 300, "freight_capacity": 40,
                 "stops": [
                     {"station_id": "malu", "arrive": "16:10", "depart": "16:13",
                      "handles_freight": True},
                     {"station_id": "heping", "arrive": "17:05", "depart": "17:09",
                      "handles_freight": True},
                 ]},
            ])
        return receipt.resource_id

    def _activate(self, plan_id, suffix=""):
        self.service.confirm_plan(request_id=f"confirm{suffix}", actor_id="rv1", plan_id=plan_id)
        self.service.activate_plan(request_id=f"activate{suffix}", actor_id="op1", plan_id=plan_id)

    def _skip(self, request_id="skip", **overrides):
        params = dict(request_id=request_id, actor_id="op1", plan_id=self.plan_id,
                      change_type="skip_stop", service_date=DATE, train_no="5633",
                      station_id="heping", affected_groups=["market_goers"],
                      reason="客流偏低")
        params.update(overrides)
        return self.service.create_change(**params)

    # ---------- 版本生命周期 ----------

    def test_draft_cannot_be_activated_before_local_confirmation(self):
        with self.assertRaises(ConflictError):
            self.service.activate_plan(request_id="act-early", actor_id="op1",
                                       plan_id=self.plan_id)

    def test_operator_cannot_confirm_plan(self):
        with self.assertRaises(PermissionDenied):
            self.service.confirm_plan(request_id="confirm-op", actor_id="op1",
                                      plan_id=self.plan_id)

    def test_draft_plan_is_not_adopted_in_queries(self):
        day = self.service.timetable_on("s1", DATE)
        self.assertIsNone(day["plan_id"])
        self.assertEqual([], day["trains"])

    def test_activation_supersedes_previous_effective_plan(self):
        self._activate(self.plan_id)
        second = self._plan(request_id="plan-2", label="冬季调整图")
        self._activate(second, suffix="-2")
        before = self.service.timetable_on("s1", "2026-09-30")
        after = self.service.timetable_on("s1", DATE)
        self.assertIsNone(before["plan_id"])  # 生效日前无采用图
        self.assertEqual(second, after["plan_id"])
        changes = self.service.timetable_on("s1", DATE)
        self.assertEqual(2, len(changes["trains"]))

    # ---------- 临时变更规则 ----------

    def test_change_requires_affected_groups(self):
        self._activate(self.plan_id)
        with self.assertRaises(ValidationError):
            self._skip(affected_groups=[])
        with self.assertRaises(ValidationError):
            self._skip(affected_groups=["strangers"])

    def test_change_requires_effective_plan(self):
        with self.assertRaises(ConflictError):
            self._skip()

    def test_skip_breaking_minimum_frequency_is_rejected(self):
        self._activate(self.plan_id)
        with self.assertRaises(ValidationError):
            self._skip()

    def test_replacement_allows_skip_and_preserves_frequency(self):
        self._activate(self.plan_id)
        self.service.create_change(request_id="replace", actor_id="op1", plan_id=self.plan_id,
                                   change_type="replacement", service_date=DATE,
                                   train_no="5633", station_id="heping",
                                   affected_groups=["market_goers", "farm_shippers"],
                                   reason="赶集日公路接驳",
                                   replacement={"mode": "bus", "capacity": 45,
                                                "carrier": "县运输公司"})
        self._skip()
        day = self.service.timetable_on("s1", DATE)
        self.assertEqual(1, len(day["replacements"]))
        train = next(item for item in day["trains"] if item["train_no"] == "5633")
        stop = next(item for item in train["stops"] if item["station_id"] == "heping")
        self.assertEqual("skipped", stop["status"])
        self.assertEqual([], self.service.unmet_on("s1", DATE)["items"])

    def test_cancel_under_disaster_is_forced_and_reports_liability(self):
        self._activate(self.plan_id)
        self.service.register_subsidy_agreement(request_id="subsidy", actor_id="op1",
                                                site_id="s1", station_id="heping",
                                                funder_name="省交通厅",
                                                liable_organization="o1",
                                                valid_from="2026-10-01")
        self.service.register_restriction(request_id="disaster", actor_id="op1", site_id="s1",
                                          station_id="heping", start_date=DATE, end_date=DATE,
                                          reason="山体滑坡")
        receipt = self.service.create_change(request_id="cancel", actor_id="op1",
                                             plan_id=self.plan_id, change_type="cancel_train",
                                             service_date=DATE, train_no="5633",
                                             affected_groups=["medical_patients"],
                                             reason="滑坡断道")
        change = self.service.list_changes("s1", DATE)[0]
        self.assertTrue(change["forced_over_commitment"])
        self.assertEqual(receipt.resource_id, change["change_id"])
        unmet = self.service.unmet_on("s1", DATE)
        self.assertEqual(1, len(unmet["items"]))
        self.assertEqual("o1", unmet["items"][0]["liability"]["liable_organization"])
        self.assertIn("blocked", unmet["items"][0]["causes"])

    def test_unmet_liability_defaults_to_site_organization(self):
        self._activate(self.plan_id)
        self.service.register_restriction(request_id="disaster", actor_id="op1", site_id="s1",
                                          station_id="heping", start_date=DATE, end_date=DATE,
                                          reason="塌方")
        self.service.create_change(request_id="cancel", actor_id="op1", plan_id=self.plan_id,
                                   change_type="cancel_train", service_date=DATE,
                                   train_no="5633", affected_groups=["general"],
                                   reason="断道")
        unmet = self.service.unmet_on("s1", DATE)
        liability = unmet["items"][0]["liability"]
        self.assertIsNone(liability["agreement_id"])
        self.assertEqual("o1", liability["liable_organization"])

    def test_cancel_requires_goods_transfer(self):
        self._activate(self.plan_id)
        self.service.accept_goods(request_id="goods", actor_id="op1", site_id="s1",
                                  service_date=DATE, train_no="5633", station_id="heping",
                                  units=10, description="高山蔬菜")
        self.service.register_restriction(request_id="disaster", actor_id="op1", site_id="s1",
                                          station_id="heping", start_date=DATE, end_date=DATE,
                                          reason="塌方")
        with self.assertRaises(ValidationError):
            self.service.create_change(request_id="cancel-denied", actor_id="op1",
                                       plan_id=self.plan_id, change_type="cancel_train",
                                       service_date=DATE, train_no="5633",
                                       affected_groups=["farm_shippers"], reason="断道")
        with self.assertRaises(ValidationError):
            self.service.create_change(request_id="cancel-short", actor_id="op1",
                                       plan_id=self.plan_id, change_type="cancel_train",
                                       service_date=DATE, train_no="5633",
                                       affected_groups=["farm_shippers"], reason="断道",
                                       goods_transfer={"carrier": "县运输公司", "capacity": 5})
        self.service.create_change(request_id="cancel-ok", actor_id="op1", plan_id=self.plan_id,
                                   change_type="cancel_train", service_date=DATE,
                                   train_no="5633", affected_groups=["farm_shippers"],
                                   reason="断道",
                                   goods_transfer={"carrier": "县运输公司", "capacity": 10})
        row = self.database.connection.execute(
            "SELECT status, transferred_to FROM goods_acceptances").fetchone()
        self.assertEqual("transferred", row["status"])
        self.assertIsNotNone(row["transferred_to"])

    def test_restore_creates_new_fact_without_deleting_history(self):
        self._activate(self.plan_id)
        self.service.create_change(request_id="replace", actor_id="op1", plan_id=self.plan_id,
                                   change_type="replacement", service_date=DATE,
                                   train_no="5633", station_id="heping",
                                   affected_groups=["market_goers"], reason="接驳",
                                   replacement={"mode": "bus", "capacity": 45,
                                                "carrier": "县运输公司"})
        skip = self._skip()
        self.service.create_change(request_id="restore", actor_id="op1", plan_id=self.plan_id,
                                   change_type="restore", service_date=DATE, train_no="5633",
                                   affected_groups=["market_goers"], reason="客流回升恢复",
                                   restores_change_id=skip.resource_id)
        day = self.service.timetable_on("s1", DATE)
        train = next(item for item in day["trains"] if item["train_no"] == "5633")
        stop = next(item for item in train["stops"] if item["station_id"] == "heping")
        self.assertEqual("scheduled", stop["status"])
        history = self.service.list_changes("s1", DATE)
        self.assertEqual(3, len(history))
        self.assertEqual({"replacement", "skip_stop", "restore"},
                         {item["change_type"] for item in history})
        with self.assertRaises(ConflictError):
            self.service.create_change(request_id="restore-2", actor_id="op1",
                                       plan_id=self.plan_id, change_type="restore",
                                       service_date=DATE, train_no="5633",
                                       affected_groups=["market_goers"], reason="重复恢复",
                                       restores_change_id=skip.resource_id)

    def test_add_stop_and_blockade_appear_in_adopted_timetable(self):
        self._activate(self.plan_id)
        self.service.create_change(request_id="add", actor_id="op1", plan_id=self.plan_id,
                                   change_type="add_stop", service_date=DATE, train_no="5633",
                                   station_id="shiban", arrive="09:40", depart="09:42",
                                   handles_freight=False,
                                   affected_groups=["commuters"], reason="学生集中出行")
        self.service.register_blockade(request_id="blockade", actor_id="op1", site_id="s1",
                                       station_id="malu", start_date=DATE, end_date=DATE,
                                       reason="线路检修封锁")
        day = self.service.timetable_on("s1", DATE)
        train = next(item for item in day["trains"] if item["train_no"] == "5633")
        added = next(item for item in train["stops"] if item["station_id"] == "shiban")
        self.assertEqual("added", added["status"])
        blocked = next(item for item in train["stops"] if item["station_id"] == "malu")
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual(["malu"], day["blocked_stations"])

    def test_change_replay_is_idempotent(self):
        self._activate(self.plan_id)
        self.service.create_change(request_id="replace", actor_id="op1", plan_id=self.plan_id,
                                   change_type="replacement", service_date=DATE,
                                   train_no="5633", station_id="heping",
                                   affected_groups=["market_goers"], reason="接驳",
                                   replacement={"mode": "bus", "capacity": 45,
                                                "carrier": "县运输公司"})
        first = self._skip()
        second = self._skip()
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(2, len(self.service.list_changes("s1", DATE)))

    # ---------- 客货混装 ----------

    def test_goods_acceptance_respects_freight_capacity_and_stop(self):
        self._activate(self.plan_id)
        self.service.accept_goods(request_id="g1", actor_id="op1", site_id="s1",
                                  service_date=DATE, train_no="5633", station_id="heping",
                                  units=30, description="蜂蜜")
        with self.assertRaises(ValidationError):
            self.service.accept_goods(request_id="g2", actor_id="op1", site_id="s1",
                                      service_date=DATE, train_no="5633", station_id="heping",
                                      units=11, description="超载")
        with self.assertRaises(ValidationError):
            self.service.accept_goods(request_id="g3", actor_id="op1", site_id="s1",
                                      service_date=DATE, train_no="5633", station_id="nowhere",
                                      units=1, description="无此站")

    # ---------- 赶集日历承诺 ----------

    def test_market_commitment_only_applies_on_market_days(self):
        self.service.register_commitment(request_id="commit-market", actor_id="op1", site_id="s1",
                                         station_id="malu", min_stops_per_day=2,
                                         serve_market_days=True, valid_from="2026-10-01")
        self._activate(self.plan_id)
        plain_day = self.service.unmet_on("s1", "2026-10-06")
        self.assertEqual([], [e for e in plain_day["items"] if e["station_id"] == "malu"])
        self.service.register_calendar_entry(request_id="cal", actor_id="op1", site_id="s1",
                                             station_id="malu", date=DATE, kind="market")
        self.service.register_restriction(request_id="disaster-malu", actor_id="op1",
                                          site_id="s1", station_id="malu",
                                          start_date=DATE, end_date=DATE, reason="泥石流")
        market_day = self.service.unmet_on("s1", DATE)
        malu = [e for e in market_day["items"] if e["station_id"] == "malu"]
        self.assertEqual(1, len(malu))
        self.assertEqual(0, malu[0]["served"])

    # ---------- 冻结证据与评估轮次 ----------

    def _freeze(self, request_id="freeze"):
        receipt = self.service.freeze_coverage(request_id=request_id, actor_id="au1",
                                               site_id="s1", from_date="2026-10-01",
                                               to_date="2026-10-07")
        return receipt.resource_id

    def test_freeze_and_recompute_match(self):
        self._activate(self.plan_id)
        snapshot_id = self._freeze()
        result = self.service.recompute_snapshot(snapshot_id)
        self.assertTrue(result["match"])
        snapshot = self.service.get_snapshot(snapshot_id)
        heping = snapshot["result"]["summary"]["stations"]["heping"]
        self.assertEqual(7, heping["committed_days"])
        self.assertEqual(7, heping["met_days"])
        self.assertEqual(1.0, heping["coverage_ratio"])

    def test_operator_cannot_freeze_coverage(self):
        with self.assertRaises(PermissionDenied):
            self.service.freeze_coverage(request_id="freeze-op", actor_id="op1", site_id="s1",
                                         from_date="2026-10-01", to_date="2026-10-07")

    def test_late_ridership_only_enters_next_round(self):
        self._activate(self.plan_id)
        snapshot_id = self._freeze()
        self.service.record_ridership(request_id="ride", actor_id="op1", site_id="s1",
                                      service_date=DATE, train_no="5633", station_id="heping",
                                      passengers=12)
        early = self.service.create_evaluation_round(
            request_id="round-early", actor_id="au1", site_id="s1",
            cutoff_at="2026-09-30T07:00:00Z", snapshot_id=snapshot_id)
        early_round = self.service.get_round(early.resource_id)
        self.assertEqual(0, early_round["included_count"])
        self.assertEqual(1, early_round["deferred_count"])
        late = self.service.create_evaluation_round(
            request_id="round-late", actor_id="au1", site_id="s1",
            cutoff_at="2026-09-30T09:00:00Z", snapshot_id=snapshot_id)
        late_round = self.service.get_round(late.resource_id)
        self.assertEqual(1, late_round["included_count"])
        self.assertEqual(0, late_round["deferred_count"])
        heping = late_round["result"]["stations"]["heping"]
        self.assertEqual(12, heping["passengers"])
        self.assertTrue(heping["low_utilization"])

    def test_commitment_revision_keeps_history(self):
        first = self.database.connection.execute(
            "SELECT commitment_id FROM service_commitments").fetchone()["commitment_id"]
        self.service.register_commitment(request_id="commit-2", actor_id="op1", site_id="s1",
                                         station_id="heping", min_stops_per_day=3,
                                         cargo_acceptance=True, valid_from="2026-11-01")
        rows = self.database.connection.execute(
            "SELECT * FROM service_commitments ORDER BY valid_from").fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual("2026-10-31", rows[0]["valid_to"])
        self.assertEqual(first, rows[0]["commitment_id"])
        self.assertEqual(rows[1]["commitment_id"], rows[0]["superseded_by"])


if __name__ == "__main__":
    unittest.main()
