"""YouTube daily quota guard for S8 (UX: never fail half-way through a batch for quota).

Google counts the quota per project per day, the day being midnight-to-midnight Pacific time.
Before the first new upload of a run, S8 estimates the units the remaining new uploads of this
job will need (upload -> publish, `estimate_upload_units`) and compares them with what is left
today: `budget.youtube_quota_per_day` minus the real (model "youtube") youtube_quota records of
today in <vp_home>/costs.jsonl, all jobs together. Not enough -> WaitingQuota BEFORE any upload.
Only real YouTube runs are checked; a rehearsal (fake publisher) never waits.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..adapters.publisher import estimate_upload_units
from ..core.costs import CostJournal
from ..core.errors import WaitingQuota
from ..core.io import parse_iso

PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_day(at: datetime) -> Any:
    return at.astimezone(PACIFIC).date()


def used_today(costs: CostJournal, now: datetime | None = None) -> int:
    """Real YouTube units spent today (Pacific day), all jobs."""
    now = now or datetime.now(timezone.utc)
    today = pacific_day(now)
    total = 0
    for r in costs.records():
        if r.get("kind") != "youtube_quota" or r.get("model") != "youtube":
            continue
        try:
            if pacific_day(parse_iso(str(r.get("at")))) != today:
                continue
        except ValueError:
            continue
        total += int(r.get("units") or 0)
    return total


def hours_to_reset(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(PACIFIC)
    midnight = datetime(local.year, local.month, local.day, tzinfo=PACIFIC) + timedelta(days=1)
    return max((midnight - local).total_seconds() / 3600, 0.0)


def check(new_uploads: int, daily: int, costs: CostJournal,
          now: datetime | None = None) -> dict[str, int]:
    """Raise WaitingQuota if `new_uploads` do not fit into what is left today."""
    need = estimate_upload_units(new_uploads)
    used = used_today(costs, now)
    available = max(int(daily) - used, 0)
    info = {"need": need, "available": available, "used": used, "daily": int(daily),
            "new_uploads": new_uploads}
    if need > available:
        raise WaitingQuota(
            f"YouTube quota: {new_uploads} new upload(s) need ~{need} units, only {available} of "
            f"{daily} are left today (used {used}). Nothing was uploaded. The quota resets at "
            f"midnight Pacific time (in ~{hours_to_reset(now):.0f} h): run again after that, or "
            f"raise budget.youtube_quota_per_day if Google granted a bigger quota.")
    return info


def per_day(daily: int) -> int:
    """How many new uploads fit into one whole quota day (at least 1)."""
    n = 1
    while estimate_upload_units(n + 1) <= int(daily):
        n += 1
    return n


def check_one(total_new: int, daily: int, costs: CostJournal,
              now: datetime | None = None) -> dict[str, int]:
    """A job too big for one quota day goes over several days: before each new upload, does one
    more item fit into what is left today? Raise WaitingQuota otherwise (uploads done so far stay)."""
    need = estimate_upload_units(1)
    used = used_today(costs, now)
    available = max(int(daily) - used, 0)
    info = {"need": need, "available": available, "used": used, "daily": int(daily),
            "new_uploads": total_new, "split": 1, "per_day": per_day(daily)}
    if need > available:
        raise WaitingQuota(
            f"YouTube quota: this job's {total_new} new uploads need "
            f"~{estimate_upload_units(total_new)} units, more than one day's {daily}, so they go "
            f"over several days, about {info['per_day']} a day. Today's quota is used up "
            f"({available} left); what was uploaded stays. The quota resets at midnight Pacific "
            f"time (in ~{hours_to_reset(now):.0f} h): run again after that for the next part.")
    return info
