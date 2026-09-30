import unittest
from datetime import date

from transport_coordination.coverage import (
    calendar_active,
    effective_trains,
    evaluate_day,
    recompute_summary,
)


def station(station_id):
    return {"station_id": station_id, "site_id": "s1", "name": station_id, "township": "乡",
            "populations_json": []}


def base_snapshot():
    return {
        "plan": {"plan_id": "p1", "valid_from": "2026-10-01", "valid_to": "2026-10-31"},
        "stations": [station("a"), station("b"), station("c")],
        "calendars": [
            {"calendar_id": "cal1", "station_id": "b", "kind": "market", "label": "周六赶集",
             "rule": {"type": "weekly", "weekdays": ["sat"]}},
            {"calendar_id": "cal2", "station_id": "b", "kind": "medical", "label": "逢五就医",
             "rule": {"type": "monthly_days", "days": [5]}},
            {"calendar_id": "cal3", "station_id": "a", "kind": "market", "label": "固定集市",
             "rule": {"type": "dates", "dates": ["2026-10-01"]}},
        ],
        "agreements": [
            {"agreement_id": "agr1", "title": "协议", "station_ids_json": ["a", "b", "c"],
             "remedy_owner": "县交通局", "valid_from": "2026-10-01", "valid_to": "2026-10-31"},
        ],
        "commitments": [
            {"commitment_id": "cm-a", "station_id": "a", "title": "A", "demand_kinds_json": ["market"],
             "min_stops_on_demand_day": 1, "min_stops_per_week": 0, "agreement_id": "agr1"},
            {"commitment_id": "cm-b", "station_id": "b", "title": "B", "demand_kinds_json": ["market", "medical"],
             "min_stops_on_demand_day": 1, "min_stops_per_week": 2, "agreement_id": "agr1"},
        ],
        "trains": [{
            "train_id": "t1", "train_code": "7265",
            "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "cargo_capacity_kg": 500,
            "stops": [{"station_id": "a"}, {"station_id": "b"}, {"station_id": "c"}],
        }],
        "blocks": [], "disasters": [], "amendments": [], "replacements": [], "consignments": [],
    }


class CalendarTest(unittest.TestCase):
    def test_weekly_rule(self):
        rule = {"type": "weekly", "weekdays": ["sat"]}
        self.assertTrue(calendar_active(rule, date(2026, 10, 3)))   # 周六
        self.assertFalse(calendar_active(rule, date(2026, 10, 5)))  # 周一

    def test_monthly_days_rule(self):
        rule = {"type": "monthly_days", "days": [5]}
        self.assertTrue(calendar_active(rule, date(2026, 10, 5)))
        self.assertFalse(calendar_active(rule, date(2026, 10, 6)))

    def test_explicit_dates_rule(self):
        rule = {"type": "dates", "dates": ["2026-10-01"]}
        self.assertTrue(calendar_active(rule, date(2026, 10, 1)))
        self.assertFalse(calendar_active(rule, date(2026, 10, 2)))

    def test_nth_weekday_rule(self):
        rule = {"type": "nth_weekday", "nth": 3, "weekday": "sun"}
        self.assertTrue(calendar_active(rule, date(2026, 10, 18)))
        self.assertFalse(calendar_active(rule, date(2026, 10, 11)))
        last = {"type": "nth_weekday", "nth": -1, "weekday": "sat"}
        self.assertTrue(calendar_active(last, date(2026, 10, 31)))


class EffectiveTrainsTest(unittest.TestCase):
    def test_weekday_filter(self):
        snapshot = base_snapshot()
        snapshot["trains"][0]["weekdays"] = ["mon", "wed", "fri"]
        self.assertEqual(["7265"], [t["train_code"] for t in effective_trains(snapshot, date(2026, 10, 5))])
        self.assertEqual([], effective_trains(snapshot, date(2026, 10, 6)))  # 周二

    def test_section_disaster_suppresses_stops_along_section(self):
        snapshot = base_snapshot()
        snapshot["disasters"] = [{
            "disaster_id": "d1", "scope": "section", "effect": "no_service",
            "from_station_id": "b", "to_station_id": "c",
            "start_date": "2026-10-05", "end_date": "2026-10-05"}]
        trains = effective_trains(snapshot, date(2026, 10, 5))
        self.assertEqual(["a"], [s["station_id"] for s in trains[0]["stops"]])
        self.assertIn("b", trains[0]["suppressed"])

    def test_skip_and_restore_are_both_facts(self):
        snapshot = base_snapshot()
        snapshot["amendments"] = [
            {"amendment_id": "m1", "kind": "skip_stop", "train_code": "7265", "station_id": "b",
             "event_date": "2026-10-03", "end_date": "2026-10-08"},
            {"amendment_id": "m2", "kind": "restore", "train_code": "7265", "station_id": "b",
             "event_date": "2026-10-06", "restores_amendment_id": "m1"},
        ]
        skipped = effective_trains(snapshot, date(2026, 10, 5))
        self.assertEqual(["a", "c"], [s["station_id"] for s in skipped[0]["stops"]])
        restored = effective_trains(snapshot, date(2026, 10, 6))
        self.assertEqual(["a", "b", "c"], [s["station_id"] for s in restored[0]["stops"]])

    def test_add_stop_brings_extra_station(self):
        snapshot = base_snapshot()
        snapshot["stations"].append(station("d"))
        snapshot["amendments"] = [
            {"amendment_id": "m1", "kind": "add_stop", "train_code": "7265", "station_id": "d",
             "event_date": "2026-10-03"}]
        trains = effective_trains(snapshot, date(2026, 10, 3))
        self.assertIn("d", [s["station_id"] for s in trains[0]["stops"]])


class EvaluateDayTest(unittest.TestCase):
    def test_normal_market_day_meets_commitment(self):
        report = evaluate_day(base_snapshot(), date(2026, 10, 3))  # 周六赶集
        self.assertEqual([], report["unmet_commitments"])
        cm_b = next(c for c in report["commitments"] if c["commitment_id"] == "cm-b")
        self.assertEqual(["market"], cm_b["demand_kinds"])

    def test_medical_day_without_service_is_unmet_and_names_owner(self):
        snapshot = base_snapshot()
        snapshot["amendments"] = [
            {"amendment_id": "m1", "kind": "cancel_train", "train_code": "7265",
             "event_date": "2026-10-05"}]
        report = evaluate_day(snapshot, date(2026, 10, 5))
        self.assertIn("cm-b", report["unmet_commitments"])
        cm_b = next(c for c in report["commitments"] if c["commitment_id"] == "cm-b")
        self.assertIn("demand_day_below_minimum", cm_b["unmet_reasons"])
        self.assertEqual("县交通局", cm_b["remedy"]["agreements"][0]["remedy_owner"])

    def test_replacement_covers_commitment_and_consignment(self):
        snapshot = base_snapshot()
        snapshot["amendments"] = [
            {"amendment_id": "m1", "kind": "cancel_train", "train_code": "7265",
             "event_date": "2026-10-05"}]
        snapshot["consignments"] = [{
            "consignment_id": "pkg1", "plan_id": "p1", "train_code": "7265",
            "origin_station_id": "b", "destination_station_id": "c", "send_date": "2026-10-05",
            "weight_kg": 30, "cargo_name": "核桃", "status": "accepted",
            "accepted_by": "op1", "accepted_at": "2026-10-04T08:00:00Z"}]
        snapshot["replacements"] = [{
            "replacement_id": "r1", "group_id": "g1", "event_date": "2026-10-05",
            "station_ids_json": ["b", "c"], "mode": "公路班车", "capacity_seats": 20,
            "cargo_capacity_kg": 100, "covers_consignments_json": ["pkg1"]}]
        report = evaluate_day(snapshot, date(2026, 10, 5))
        self.assertEqual([], report["unmet_commitments"])
        self.assertEqual("replacement:g1", report["consignments"][0]["via"])

    def test_consignment_over_capacity_is_unmet_with_owner(self):
        snapshot = base_snapshot()
        snapshot["consignments"] = [{
            "consignment_id": "pkg1", "plan_id": "p1", "train_code": "7265",
            "origin_station_id": "a", "destination_station_id": "b", "send_date": "2026-10-02",
            "weight_kg": 600, "cargo_name": "大件", "status": "accepted",
            "accepted_by": "op1", "accepted_at": "2026-10-01T08:00:00Z"}]
        report = evaluate_day(snapshot, date(2026, 10, 2))
        self.assertEqual(["pkg1"], report["unmet_consignments"])
        self.assertIn("cargo_capacity_exceeded", report["consignments"][0]["causes"])
        self.assertEqual("县交通局", report["consignments"][0]["remedy"]["agreements"][0]["remedy_owner"])

    def test_recompute_summary_is_deterministic(self):
        first = recompute_summary(base_snapshot(), date(2026, 10, 1), date(2026, 10, 7))
        second = recompute_summary(base_snapshot(), date(2026, 10, 1), date(2026, 10, 7))
        self.assertEqual(first, second)
        self.assertEqual(7, first["days"])


if __name__ == "__main__":
    unittest.main()
