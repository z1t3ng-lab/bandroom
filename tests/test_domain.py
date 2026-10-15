import unittest
from datetime import date, datetime, time, timedelta, timezone

from domain import (
    DomainError,
    availability_suggestions,
    estimated_cost_cents,
    expand_availability,
    local_datetime,
    split_cost,
    utc_interval,
)


class AvailabilityTests(unittest.TestCase):
    def period(self, member_id, start, end, day="2026-06-17"):
        left = datetime.fromisoformat(f"{day}T{start}:00+00:00")
        right = datetime.fromisoformat(f"{day}T{end}:00+00:00")
        return {"member_id": member_id, "date": day, "kind": "available",
                "start_utc": left.isoformat(), "end_utc": right.isoformat()}

    def test_partial_overlaps_and_multiple_periods_return_real_intersections(self):
        expanded = [
            self.period(1, "18:00", "20:00"),
            self.period(1, "21:00", "22:00"),
            self.period(2, "19:00", "21:30"),
        ]
        slots = availability_suggestions(expanded, [1, 2, 3], date(2026, 6, 17), date(2026, 6, 17), 60, "UTC")
        both = [s for s in slots if s["available_count"] == 2]
        self.assertEqual([(s["start_time"], s["end_time"]) for s in both], [("19:00", "20:00")])
        self.assertEqual(both[0]["member_status"], {"1": "available", "2": "available", "3": "not_submitted"})
        one_member = {(s["start_time"], s["end_time"], tuple(s["available_member_ids"])) for s in slots if s["available_count"] == 1}
        self.assertIn(("18:00", "19:00", (1,)), one_member)
        self.assertIn(("19:00", "21:30", (2,)), one_member)

    def test_explicit_unavailable_is_not_treated_as_not_submitted(self):
        day = "2026-06-17"
        expanded = [self.period(1, "18:00", "21:00", day),
                    {"member_id": 2, "date": day, "kind": "unavailable",
                     "start_utc": f"{day}T00:00:00+00:00", "end_utc": "2026-06-18T00:00:00+00:00"}]
        slots = availability_suggestions(expanded, [1, 2, 3], date.fromisoformat(day), date.fromisoformat(day), 60, "UTC")
        self.assertEqual(slots[0]["member_status"], {"1": "available", "2": "unavailable", "3": "not_submitted"})

    def test_weekly_recurrence_and_date_exception(self):
        entry = {"member_id": 8, "kind": "available", "recurrence": "weekly",
                 "start_date": "2026-10-25", "until_date": "2026-11-08",
                 "start_time": "18:00", "end_time": "20:00", "exceptions": ["2026-11-01"]}
        dates = expand_availability([entry], date(2026, 10, 25), date(2026, 11, 8), "America/Toronto")
        self.assertEqual([item["date"] for item in dates], ["2026-10-25", "2026-11-08"])
        self.assertEqual([item["start_time"] for item in dates], ["18:00", "18:00"])
        # Wall-clock time stays at 6 pm through the fall-back transition.
        self.assertEqual(datetime.fromisoformat(dates[1]["start_utc"]).hour, 23)


class TimezoneTests(unittest.TestCase):
    def test_invalid_time_range_is_rejected(self):
        with self.assertRaises(DomainError):
            utc_interval(date(2026, 6, 1), time(20, 0), time(19, 0), "America/Toronto")

    def test_spring_forward_gap_is_rejected(self):
        with self.assertRaises(DomainError):
            local_datetime(date(2026, 3, 8), time(2, 30), "America/Toronto")
        self.assertEqual(local_datetime(date(2026, 3, 8), time(3, 30), "America/Toronto").hour, 3)

    def test_fall_back_repeated_time_supports_both_folds(self):
        first = local_datetime(date(2026, 11, 1), time(1, 30), "America/Toronto", fold=0)
        second = local_datetime(date(2026, 11, 1), time(1, 30), "America/Toronto", fold=1)
        self.assertEqual((second.astimezone(timezone.utc)-first.astimezone(timezone.utc)), timedelta(hours=1))


class CostTests(unittest.TestCase):
    def test_cost_rounding_adds_to_exact_total(self):
        shares = split_cost(1001, [4, 2, 9])
        self.assertEqual(shares, {4: 334, 2: 334, 9: 333})
        self.assertEqual(sum(shares.values()), 1001)

    def test_zero_members_and_hourly_estimate(self):
        self.assertEqual(split_cost(9500, []), {})
        self.assertEqual(estimated_cost_cents(90, 42.50, None), 6375)
        self.assertEqual(estimated_cost_cents(180, 42.50, 90), 9000)


if __name__ == "__main__":
    unittest.main()
