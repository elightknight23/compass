"""The mess menu: what each IFC serves, by weekday and meal.

A menu is a week — Monday to Sunday, breakfast, lunch, snacks and dinner —
per mess. It is structured data rather than a document, because the
question asked of it ("what's for dinner today?") is a lookup by weekday,
and which weekday "today" or "Friday" is belongs to Python (I5), not to a
model reading excerpts.

Who may change a menu is its own rule, like documents and certificates:
`auth.MENU_MANAGERS`, the hostel warden, because the mess is hostel
business. save() checks it first, before reading anything (I1, I3).

Until a mess's menu is saved, it is served from the sample file
(data/mess_menu.sample.json) and labelled as a sample everywhere it shows —
the same rule as the placeholder holidays: nothing passes a stand-in off as
the College's.
"""

from __future__ import annotations

import calendar as _calendar
import csv
import io
import json
import re
from datetime import date, datetime, timezone

from . import auth, backends, config

MESSES = ("IFC A", "IFC B", "IFC C", "IFC D")
MEALS = ("breakfast", "lunch", "snacks", "dinner")
DAYS = tuple(d.lower() for d in _calendar.day_name)  # monday .. sunday
MAX_ITEMS = 30
MAX_ITEM_CHARS = 80
MAX_TIMING_CHARS = 40

# Header words a menu's columns are recognised by, in a CSV or the editor.
MEAL_WORDS = {"breakfast": "breakfast", "tiffin": "breakfast", "lunch": "lunch",
              "snack": "snacks", "snacks": "snacks", "evening": "snacks",
              "dinner": "dinner", "supper": "dinner"}
_DAY_ALIASES = {**{d: d for d in DAYS}, **{d[:3]: d for d in DAYS},
                "tues": "tuesday", "wednes": "wednesday", "thur": "thursday", "thurs": "thursday"}


def _log(message: str) -> None:
    import sys
    print(f"[compass-mess] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def normalize_mess(text: str | None) -> str | None:
    """'ifc b', 'IFC-B', 'B mess', 'mess b', 'b' -> 'IFC B'. None if it
    names no mess we have — never a guess at the nearest one."""
    if not text:
        return None
    letters = re.sub(r"[^a-z]", "", str(text).lower())
    for word in ("ifc", "mess", "foodcourt", "food", "court", "the"):
        letters = letters.replace(word, "")
    name = f"IFC {letters.upper()}"
    return name if len(letters) == 1 and name in MESSES else None


def day_name(d: date) -> str:
    return DAYS[d.weekday()]


# ---------------------------------------------------------------------------
# Cleaning — the one shape every menu is stored in
# ---------------------------------------------------------------------------
def _items(value, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # Semicolons or line breaks, when a cell has them, are the
        # separator, so "Bread, butter & jam" survives an export and import;
        # a cell with neither is split on commas.
        value = re.split(r"[;\n]", value) if re.search(r"[;\n]", value) else value.split(",")
    if not isinstance(value, list):
        raise ValueError(f"{where}: expected a list of dishes")
    items = [re.sub(r"\s+", " ", str(v)).strip(" -•*") for v in value]
    items = [i for i in items if i]
    if len(items) > MAX_ITEMS:
        raise ValueError(f"{where}: more than {MAX_ITEMS} dishes")
    long = next((i for i in items if len(i) > MAX_ITEM_CHARS), None)
    if long:
        raise ValueError(f"{where}: '{long[:30]}…' is longer than {MAX_ITEM_CHARS} characters")
    return items


def clean_week(week) -> dict:
    """Every day and meal present, each a list of dish names. Raises
    ValueError naming the day and meal at fault."""
    if not isinstance(week, dict):
        raise ValueError("the menu must map days to meals")
    unknown = [k for k in week if str(k).lower() not in DAYS]
    if unknown:
        raise ValueError(f"'{unknown[0]}' is not a day of the week")
    out = {}
    for d in DAYS:
        meals = week.get(d) or week.get(d.title()) or {}
        if not isinstance(meals, dict):
            raise ValueError(f"{d.title()}: expected meals")
        bad = [k for k in meals if str(k).lower() not in MEALS]
        if bad:
            raise ValueError(f"{d.title()}: '{bad[0]}' is not one of {', '.join(MEALS)}")
        out[d] = {m: _items(meals.get(m), f"{d.title()} {m}") for m in MEALS}
    return out


def clean_timings(timings) -> dict:
    timings = timings or {}
    if not isinstance(timings, dict):
        raise ValueError("timings must map meals to times")
    out = {}
    for m in MEALS:
        t = re.sub(r"\s+", " ", str(timings.get(m) or "")).strip()
        if len(t) > MAX_TIMING_CHARS:
            raise ValueError(f"the {m} timing is longer than {MAX_TIMING_CHARS} characters")
        out[m] = t
    return out


def is_empty(week: dict) -> bool:
    return not any(items for meals in week.values() for items in meals.values())


# ---------------------------------------------------------------------------
# CSV — the import a warden can make from any spreadsheet
# ---------------------------------------------------------------------------
def parse_csv(text: str) -> dict:
    """A week from a 'Day, Breakfast, Lunch, Snacks, Dinner' sheet, dishes
    in each cell separated by commas, semicolons or line breaks. Raises
    ValueError with what to fix."""
    rows = [r for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("the file is empty")
    header = [c.strip().lower() for c in rows[0]]
    columns = {}
    for i, h in enumerate(header):
        meal = next((MEAL_WORDS[w] for w in MEAL_WORDS if w in h), None)
        if meal and meal not in columns.values():
            columns[i] = meal
    day_col = next((i for i, h in enumerate(header) if "day" in h), 0)
    if not columns:
        raise ValueError("the first row needs meal columns: Day, Breakfast, Lunch, Snacks, Dinner")
    week: dict = {}
    for n, row in enumerate(rows[1:], start=2):
        key = re.sub(r"[^a-z]", "", (row[day_col] if day_col < len(row) else "").lower())
        day = _DAY_ALIASES.get(key)
        if day is None:
            raise ValueError(f"row {n}: '{row[day_col] if day_col < len(row) else ''}' is not a day of the week")
        if day in week:
            raise ValueError(f"row {n}: {day.title()} appears twice")
        week[day] = {meal: row[i] if i < len(row) else "" for i, meal in columns.items()}
    if not week:
        raise ValueError("no day rows were found under the header")
    return clean_week(week)


def to_csv(menu: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Day", *[m.title() for m in MEALS]])
    for d in DAYS:
        w.writerow([d.title(), *["; ".join(menu["week"][d][m]) for m in MEALS]])
    return out.getvalue()


# ---------------------------------------------------------------------------
# Reading — saved menus, the sample for the rest
# ---------------------------------------------------------------------------
def _sample() -> dict:
    path = config.ROOT / "data" / "mess_menu.sample.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def menus() -> dict:
    """ok([menu per mess, in MESSES order]). A menu is {mess, official,
    week, timings, updated_by, updated_at}; official=False means the
    sample, shown because nothing has been saved for that mess yet."""
    res = backends.get_backend().list_menus()
    if not res["success"]:
        return res
    saved = {m["mess"]: m for m in res["data"]}
    sample = _sample()
    out = []
    for name in MESSES:
        if name in saved:
            out.append({**saved[name], "official": True})
            continue
        s = sample.get(name) or {}
        out.append({"mess": name, "official": False, "updated_by": None, "updated_at": None,
                    "week": clean_week(s.get("week") or {}), "timings": clean_timings(s.get("timings"))})
    return backends.ok(out)


def get(mess: str) -> dict:
    res = menus()
    if not res["success"]:
        return res
    return backends.ok(next((m for m in res["data"] if m["mess"] == mess), None))


# ---------------------------------------------------------------------------
# Writing — the warden's
# ---------------------------------------------------------------------------
def _refused(error: str, outcome: str) -> dict:
    return {"success": False, "data": None, "error": error, "kind": outcome, "outcome": outcome}


def save(user: dict | None, mess: str, week, timings=None) -> dict:
    """Make this the mess's menu. The caller is checked first (I1): a
    menu is read by every hosteller, so writing one is a staff decision."""
    if not auth.may_manage_menu(user):
        return _refused("Only the hostel warden can change a mess menu.", "not_authorized")
    name = normalize_mess(mess)
    if name is None:
        return _refused(f"There's no mess called {mess!r}. The messes are {', '.join(MESSES)}.", "not_found")
    try:
        week = clean_week(week)
        timings = clean_timings(timings)
    except ValueError as e:
        return _refused(f"{name}: {e}.", "invalid")
    if is_empty(week):
        return _refused(f"{name}: the menu has no dishes in it.", "invalid")
    stored = backends.get_backend().save_menu(name, {
        "week": week, "timings": timings, "updated_by": user["id"],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if not stored["success"]:
        return _refused(f"The menu couldn't be saved ({stored['error']}).", "unavailable")
    _log(f"{user['id']} saved the {name} menu")
    return {"success": True, "data": {**stored["data"], "official": True}, "error": None, "outcome": "saved"}


def import_csv(user: dict | None, mess: str, data: bytes) -> dict:
    """Read a CSV into a menu for the warden to check — nothing is saved."""
    if not auth.may_manage_menu(user):
        return _refused("Only the hostel warden can change a mess menu.", "not_authorized")
    if normalize_mess(mess) is None:
        return _refused(f"There's no mess called {mess!r}.", "not_found")
    if len(data) > 200_000:
        return _refused("That file is too large for a menu.", "too_large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError:
            return _refused("That file isn't text. Save the sheet as CSV and try again.", "unsupported_type")
    try:
        week = parse_csv(text)
    except ValueError as e:
        return _refused(f"{e}.", "invalid")
    return {"success": True, "data": {"week": week}, "error": None, "outcome": "parsed"}
