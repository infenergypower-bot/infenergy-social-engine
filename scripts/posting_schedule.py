"""Evidence-based platform posting windows in Infenergy's Central audience time."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CENTRAL_TIME = ZoneInfo("America/Chicago")

# Monday is 0. Minutes avoid the competition spike at exactly :00.
FACEBOOK_TIMES = ("21:05", "08:05", "08:05", "09:05", "08:05", "22:05", "10:05")
INSTAGRAM_TIMES = ("19:05", "19:05", "12:05", "09:05", "22:05", "21:05", "21:05")
LINKEDIN_TIMES = ("22:05", "16:05", "16:05", "17:05", "15:05", "09:05", "22:05")

PLATFORM_TIMES = {
    "facebook": FACEBOOK_TIMES,
    "instagram": INSTAGRAM_TIMES,
    "linkedin": LINKEDIN_TIMES,
}


def growth_schedule(content_date: str | date) -> dict[str, str]:
    day = date.fromisoformat(content_date) if isinstance(content_date, str) else content_date
    result: dict[str, str] = {}
    for platform, weekly_times in PLATFORM_TIMES.items():
        hour, minute = (int(part) for part in weekly_times[day.weekday()].split(":"))
        result[platform] = datetime.combine(day, time(hour, minute), CENTRAL_TIME).isoformat()
    return result


def first_scheduled_at(content_date: str | date) -> str:
    return min(growth_schedule(content_date).values(), key=datetime.fromisoformat)


def inventory_snapshot(data_dir: str, *, days: int = 14) -> list[dict]:
    """Show actual active inventory, including plans not yet prepared for delivery."""
    import json
    import sqlite3
    from inventory_db import get_db_path
    from content_operations import configured_platforms
    today = datetime.now(CENTRAL_TIME).date()
    with sqlite3.connect(get_db_path(data_dir), timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""SELECT outbox_id,content_date,scheduled_at,status,lifecycle_state,reason_code,
            last_error,package_json FROM content_outbox WHERE content_date>=? AND content_date<=?
            AND lifecycle_state!='CANCELLED' AND status NOT IN ('CANCELLED','HELD') ORDER BY datetime(scheduled_at)""",
            (today.isoformat(), (today+timedelta(days=days)).isoformat())).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        package = json.loads(item.pop('package_json'))
        from social.post_candidates import summary as candidate_summary
        result.append({**item, 'title': package.get('title') or package.get('thought_statement') or package.get('topic') or item['outbox_id'],
                       'candidate_recovery':candidate_summary(package),
                       'platforms': configured_platforms(package), 'platform_schedule': package.get('platform_schedule') or {}})
    return result
