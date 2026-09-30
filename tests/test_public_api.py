import json
import unittest

from transport_coordination.api import route
from transport_coordination.public import PublicService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class PublicApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.base = DomainService(self.database)
        self.public = PublicService(self.database)
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

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1"):
        return route(self.base, method, path, body, {"X-Actor-Id": actor})

    def _ready_plan(self):
        for sid, name in (("st_a", "青山"), ("st_b", "云岭"), ("st_c", "花溪")):
            status, _ = self.call("POST", "/public/stations", {
                "request_id": f"req-{sid}", "station_id": sid, "site_id": "s1",
                "name": name, "township": name, "populations": ["赶集群众"]})
            self.assertEqual(201, status)
        status, payload = self.call("POST", "/public/calendars", {
            "request_id": "req-cal", "calendar_id": "cal_b", "station_id": "st_b",
            "kind": "market", "label": "周六赶集",
            "rule": {"type": "weekly", "weekdays": ["sat"]}})
        self.assertEqual(201, status)
        status, _ = self.call("POST", "/public/agreements", {
            "request_id": "req-agr", "agreement_id": "agr1", "title": "协议",
            "station_ids": ["st_a", "st_b", "st_c"], "remedy_owner": "县交通局",
            "valid_from": "2026-10-01", "valid_to": "2026-10-31"})
        self.assertEqual(201, status)
        status, _ = self.call("POST", "/public/commitments", {
            "request_id": "req-cm", "commitment_id": "cm_b", "station_id": "st_b",
            "title": "赶集保一班", "demand_kinds": ["market"], "min_stops_on_demand_day": 1,
            "agreement_id": "agr1"})
        self.assertEqual(201, status)
        status, _ = self.call("POST", "/public/plans", {
            "request_id": "req-plan", "plan_id": "p1", "site_id": "s1", "title": "10 月",
            "valid_from": "2026-10-01", "valid_to": "2026-10-31"})
        self.assertEqual(201, status)
        status, _ = self.call("POST", "/public/trains", {
            "request_id": "req-train", "plan_id": "p1", "train_code": "7265",
            "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "cargo_capacity_kg": 500,
            "stops": [{"station_id": "st_a"}, {"station_id": "st_b"}, {"station_id": "st_c"}]})
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/public/plans/confirm",
                                    {"request_id": "req-confirm", "plan_id": "p1"}, actor="rv1")
        self.assertEqual(201, status)

    def test_full_plan_flow_and_day_report(self):
        self._ready_plan()
        status, payload = self.call("GET", "/public/plan-for-date?site_id=s1&date=2026-10-03", actor="")
        self.assertEqual(200, status)
        self.assertEqual("p1", payload["plan"]["plan_id"])

        # 赶集日列车取消：承诺缺口与责任方可查。
        status, payload = self.call("POST", "/public/amendments/cancel-train", {
            "request_id": "req-cancel", "plan_id": "p1", "train_code": "7265",
            "event_date": "2026-10-03", "reason": "水害停运"})
        self.assertEqual(201, status)
        status, report = self.call("GET", "/public/day-report?site_id=s1&date=2026-10-03")
        self.assertEqual(200, status)
        self.assertEqual(["cm_b"], report["unmet_commitments"])
        owner = report["commitments"][0]["remedy"]["agreements"][0]["remedy_owner"]
        self.assertEqual("县交通局", owner)

        # 幂等重放返回同一资源。
        status, replay = self.call("POST", "/public/amendments/cancel-train", {
            "request_id": "req-cancel", "plan_id": "p1", "train_code": "7265",
            "event_date": "2026-10-03", "reason": "水害停运"})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        self.assertEqual(payload["resource_id"], replay["resource_id"])

    def test_reviewer_cannot_register_station(self):
        status, payload = self.call("POST", "/public/stations", {
            "request_id": "req-x", "station_id": "st_x", "site_id": "s1", "name": "x",
            "township": "x", "populations": ["学生"]}, actor="rv1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_round_evidence_endpoint(self):
        self._ready_plan()
        status, payload = self.call("POST", "/public/rounds", {"request_id": "req-r1", "plan_id": "p1"})
        self.assertEqual(201, status)
        round_id = self.database.connection.execute(
            "SELECT round_id FROM ps_rounds WHERE plan_id='p1'").fetchone()[0]
        status, _ = self.call("POST", "/public/ridership", {
            "request_id": "req-ride", "round_id": round_id, "station_id": "st_b",
            "train_code": "7265", "event_date": "2026-10-03", "boarding": 12, "alighting": 1})
        self.assertEqual(201, status)
        status, _ = self.call("POST", "/public/rounds/freeze",
                              {"request_id": "req-freeze", "round_id": round_id})
        self.assertEqual(201, status)
        status, evidence = self.call("GET", f"/public/round-evidence?round_id={round_id}", actor="rv1")
        self.assertEqual(200, status)
        self.assertTrue(evidence["recomputed_match"])

    def test_audit_chain_stays_valid(self):
        self._ready_plan()
        status, payload = route(self.base, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertTrue(payload["audit_valid"])


if __name__ == "__main__":
    unittest.main()
