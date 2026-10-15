"""Pure scheduling and cost helpers for Bandroom."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo


class DomainError(ValueError):
    pass


def local_datetime(day: date, clock: time, tz_name: str = "America/Toronto", fold: int = 0) -> datetime:
    """Convert a local wall time to aware time, rejecting nonexistent DST times.

    Repeated fall-back times are valid. ``fold=0`` selects the earlier occurrence
    and ``fold=1`` the later occurrence.
    """
    zone = ZoneInfo(tz_name)
    candidate = datetime.combine(day, clock).replace(tzinfo=zone, fold=fold)
    round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
    if round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None) or round_trip.fold != fold:
        raise DomainError(f"{candidate.replace(tzinfo=None).isoformat(timespec='minutes')} is not a valid local time in {tz_name}")
    return candidate


def utc_interval(day: date, start: time, end: time, tz_name: str = "America/Toronto", start_fold: int = 0, end_fold: int = 0) -> tuple[datetime, datetime]:
    if end <= start:
        raise DomainError("End time must be later than start time")
    left = local_datetime(day, start, tz_name, start_fold).astimezone(timezone.utc)
    right = local_datetime(day, end, tz_name, end_fold).astimezone(timezone.utc)
    if right <= left:
        raise DomainError("The selected times do not form a positive interval")
    return left, right


def expand_availability(entries: list[dict], start_day: date, end_day: date, tz_name: str = "America/Toronto") -> list[dict]:
    """Expand one-time and weekly local availability entries into UTC intervals."""
    if end_day < start_day:
        raise DomainError("End date must be on or after start date")
    expanded: list[dict] = []
    for entry in entries:
        kind = entry.get("kind", "available")
        recurrence = entry.get("recurrence", "none")
        first = date.fromisoformat(entry["start_date"])
        last = date.fromisoformat(entry.get("until_date") or (end_day.isoformat() if recurrence == "weekly" else entry["start_date"]))
        exceptions = {date.fromisoformat(d) for d in entry.get("exceptions", [])}
        cursor = max(start_day, first)
        stop = min(end_day, last)
        while cursor <= stop:
            matches = cursor == first if recurrence == "none" else cursor.weekday() == first.weekday()
            if matches and cursor not in exceptions:
                if kind == "unavailable":
                    local_start = local_datetime(cursor, time(0, 0), tz_name)
                    local_end = local_datetime(cursor + timedelta(days=1), time(0, 0), tz_name)
                    left, right = local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)
                    start_label, end_label = "00:00", "24:00"
                else:
                    start_time = time.fromisoformat(entry["start_time"])
                    end_time = time.fromisoformat(entry["end_time"])
                    try:
                        left, right = utc_interval(cursor, start_time, end_time, tz_name,
                                                   int(entry.get("start_fold", 0)), int(entry.get("end_fold", 0)))
                    except DomainError:
                        # A weekly wall-time may land inside the spring-forward gap.
                        # That occurrence cannot exist; skip it while preserving the rule.
                        if recurrence == "weekly":
                            cursor += timedelta(days=1)
                            continue
                        raise
                    start_label, end_label = start_time.strftime("%H:%M"), end_time.strftime("%H:%M")
                expanded.append({**entry, "date": cursor.isoformat(), "start_utc": left.isoformat(),
                                 "end_utc": right.isoformat(), "start_time": start_label,
                                 "end_time": end_label, "kind": kind})
            cursor += timedelta(days=1)
    return expanded


def availability_suggestions(expanded: list[dict], member_ids: list[int], start_day: date, end_day: date,
                             min_minutes: int, tz_name: str = "America/Toronto") -> list[dict]:
    """Find contiguous real-time overlaps, retaining per-member response status.

    Only actual submitted available periods create candidate slots. Adjacent
    segments with the same available set are merged; results are ranked by the
    number of selected members who can attend, then by time and duration.
    """
    if min_minutes < 1:
        raise DomainError("Minimum duration must be at least one minute")
    selected = list(dict.fromkeys(int(m) for m in member_ids))
    by_day: dict[str, list[dict]] = {}
    submitted: dict[str, set[int]] = {}
    for item in expanded:
        day_key = item["date"]
        mid = int(item["member_id"])
        if mid not in selected:
            continue
        submitted.setdefault(day_key, set()).add(mid)
        if item.get("kind", "available") == "available":
            by_day.setdefault(day_key, []).append(item)

    candidates: list[dict] = []
    cursor_day = start_day
    while cursor_day <= end_day:
        day_key = cursor_day.isoformat()
        periods = by_day.get(day_key, [])
        boundaries = sorted({datetime.fromisoformat(p[edge]) for p in periods for edge in ("start_utc", "end_utc")})
        segments: list[tuple[datetime, datetime, tuple[int, ...]]] = []
        for left, right in zip(boundaries, boundaries[1:]):
            if right <= left:
                continue
            present = tuple(sorted({int(p["member_id"]) for p in periods
                                    if datetime.fromisoformat(p["start_utc"]) <= left
                                    and datetime.fromisoformat(p["end_utc"]) >= right}))
            if not present:
                continue
            if segments and segments[-1][1] == left and segments[-1][2] == present:
                segments[-1] = (segments[-1][0], right, present)
            else:
                segments.append((left, right, present))
        # Keep the complete submitted periods as candidates too. A member can be
        # free for 90 minutes while another becomes free halfway through; the
        # longer period is a useful alternative for the first member even when
        # the set of simultaneously-free members changes inside it.
        candidate_ranges = {(datetime.fromisoformat(p["start_utc"]), datetime.fromisoformat(p["end_utc"])) for p in periods}
        candidate_ranges.update((left, right) for left, right, _ in segments)
        for left, right in candidate_ranges:
            duration = int((right - left).total_seconds() // 60)
            if duration < min_minutes:
                continue
            present = tuple(sorted({int(p["member_id"]) for p in periods
                                    if datetime.fromisoformat(p["start_utc"]) <= left
                                    and datetime.fromisoformat(p["end_utc"]) >= right}))
            if not present:
                continue
            local_left = left.astimezone(ZoneInfo(tz_name))
            local_right = right.astimezone(ZoneInfo(tz_name))
            candidates.append({
                "date": day_key,
                "start_time": local_left.strftime("%H:%M"),
                "end_time": local_right.strftime("%H:%M"),
                "start_utc": left.isoformat(), "end_utc": right.isoformat(),
                "duration_minutes": duration,
                "available_member_ids": list(present),
                "member_status": {str(mid): ("available" if mid in present else
                                               "unavailable" if mid in submitted.get(day_key, set()) else "not_submitted")
                                  for mid in selected},
                "available_count": len(present),
            })
        cursor_day += timedelta(days=1)
    deduped = {}
    for slot in candidates:
        key = (slot["date"], slot["start_utc"], slot["end_utc"], tuple(slot["available_member_ids"]))
        deduped[key] = slot
    candidates = list(deduped.values())
    candidates.sort(key=lambda slot: (-slot["available_count"], slot["date"], slot["start_utc"], -slot["duration_minutes"]))
    return candidates


def split_cost(total_cents: int, member_ids: list[int]) -> dict[int, int]:
    """Split integer cents evenly; remainder cents go to the first IDs."""
    if total_cents < 0:
        raise DomainError("Cost cannot be negative")
    ids = list(dict.fromkeys(int(m) for m in member_ids))
    if not ids:
        return {}
    base, extra = divmod(total_cents, len(ids))
    return {member_id: base + (1 if index < extra else 0) for index, member_id in enumerate(ids)}


def estimated_cost_cents(duration_minutes: int, hourly_rate: float | None, flat_cost: float | None) -> int:
    if flat_cost is not None:
        cents = (Decimal(str(flat_cost)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return max(0, int(cents))
    if hourly_rate is None:
        return 0
    cents = (Decimal(str(hourly_rate)) * duration_minutes * 100 / 60).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(0, int(cents))
