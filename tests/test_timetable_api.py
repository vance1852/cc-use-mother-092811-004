import unittest
from datetime import datetime, timezone

from transport_coordination.api import route
from transport_coordination.clock import FixedClock
from transport_coordination.service import DomainService
from transport_coordination.storage import Database
from transport_coordination.timetable_service import TimetableService


class TimetableApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, clock)
        self.timetable = TimetableService(self.database, clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="铁路公共服务办公室")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                   display_name="调度员", role="operator", organization_id="o1")
        self.domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="山区线路",
                                  timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="op1"):
        return route(self.domain, method, path, body, {"X-Actor-Id": actor},
                     timetable=self.timetable)

    def test_timetable_route_requires_service(self):
        status, payload = route(self.domain, "GET", "/timetable/adopted?site_id=s1&date=2026-10-05",
                                None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_commitment_write_and_adopted_query(self):
        status, payload = self._call("POST", "/timetable/commitments", {
            "request_id": "c1", "site_id": "s1", "station_id": "heping",
            "min_stops_per_day": 2, "cargo_acceptance": True, "valid_from": "2026-10-01"})
        self.assertEqual(201, status)
        self.assertEqual("service_commitment", payload["resource_type"])
        replay = self._call("POST", "/timetable/commitments", {
            "request_id": "c1", "site_id": "s1", "station_id": "heping",
            "min_stops_per_day": 2, "cargo_acceptance": True, "valid_from": "2026-10-01"})
        self.assertEqual(200, replay[0])
        self.assertTrue(replay[1]["replayed"])
        status, payload = self._call("GET", "/timetable/adopted?site_id=s1&date=2026-10-05",
                                     actor="")
        self.assertEqual(200, status)
        self.assertIsNone(payload["plan_id"])

    def test_unmet_query_returns_liability(self):
        self._call("POST", "/timetable/commitments", {
            "request_id": "c1", "site_id": "s1", "station_id": "heping",
            "min_stops_per_day": 1, "valid_from": "2026-10-01"})
        status, payload = self._call("GET", "/timetable/unmet?site_id=s1&date=2026-10-05")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("o1", payload["items"][0]["liability"]["liable_organization"])

    def test_missing_query_params_rejected(self):
        status, payload = self._call("GET", "/timetable/adopted")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])


if __name__ == "__main__":
    unittest.main()
