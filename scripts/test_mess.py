"""The mess menu — names, cleaning, CSV, who may write, and the chat answer.

Reading is everyone's; writing is the hostel warden's (auth.MENU_MANAGERS),
checked in app/mess.py before anything is read. Every refusal is asserted
twice, as everywhere else: the outcome, and that the stored menu did not
change. A mess with nothing saved is served from the sample and labelled
as one — in the API and in every chat reply.

The clock is pinned (Friday 23 October 2026), so "today" and "tomorrow"
are known days of the week.

No LLM, no network. Entities are pre-filled exactly as the classifier would.

    python scripts/test_mess.py
"""

from __future__ import annotations

from _harness import check, finish, sandbox, section

from fastapi.testclient import TestClient

from app import handlers, mess, users
from app.main import app
from app.state import new_state

WARDEN, ADVISOR, RAVI, PRIYA = "U101", "U102", "U001", "U002"


def ask(uid: str, **entities) -> dict:
    st = new_state("(pre-classified)", users.get(uid))
    st["entities"] = entities
    return handlers.handle_mess_menu(st)


def week_with(dish: str) -> dict:
    return {d: {m: [f"{dish} ({d[:3]} {m})"] for m in mess.MEALS} for d in mess.DAYS}


def main() -> int:
    section("names")
    for raw, want in [("IFC A", "IFC A"), ("ifc b", "IFC B"), ("IFC-C", "IFC C"), ("D mess", "IFC D"),
                      ("mess a", "IFC A"), ("b", "IFC B"), ("the IFC D", "IFC D"), ("ifcb", "IFC B"),
                      ("IFC E", None), ("hostel", None), ("", None), (None, None)]:
        check(f"normalize_mess({raw!r}) -> {want!r}", mess.normalize_mess(raw) == want, str(mess.normalize_mess(raw)))

    section("cleaning")
    w = mess.clean_week({"monday": {"lunch": "Rice, Dal ; Curd"}})
    check("a cell with a semicolon splits only on semicolons", w["monday"]["lunch"] == ["Rice, Dal", "Curd"], str(w["monday"]))
    w = mess.clean_week({"monday": {"lunch": "Rice, Dal, Curd"}})
    check("a cell without one splits on commas", w["monday"]["lunch"] == ["Rice", "Dal", "Curd"])
    check("every day and meal is present, empty where not given",
          set(w) == set(mess.DAYS) and all(set(v) == set(mess.MEALS) for v in w.values()) and w["sunday"]["dinner"] == [])
    for bad, why in [({"funday": {}}, "not a day"), ({"monday": {"brunch": "x"}}, "not one of"),
                     ({"monday": {"lunch": ["x"] * 31}}, "more than 30"), ({"monday": {"lunch": ["x" * 81]}}, "longer than"),
                     ("a list", "map days")]:
        try:
            mess.clean_week(bad)
            check(f"refused: {why}", False)
        except ValueError as e:
            check(f"refused, saying what is wrong: {why}", why in str(e), str(e))

    section("CSV")
    sheet = ("﻿Day,Breakfast,Lunch,Evening Snacks,Dinner\n"
             "Mon,Idli; Sambar,\"Rice, Dal\",Samosa,Chapati\n"
             "tuesday,Dosa,Rice,Tea,Biryani\n")
    w = mess.parse_csv(sheet)
    check("a 'Day, Breakfast…' sheet is read; short and lower-case day names too",
          w["monday"]["breakfast"] == ["Idli", "Sambar"] and w["tuesday"]["dinner"] == ["Biryani"], str(w["monday"]))
    check("  'Evening Snacks' is the snacks column, and a byte-order mark is ignored", w["monday"]["snacks"] == ["Samosa"])
    for bad, why in [("", "empty"), ("Day,Colour\nMon,red\n", "meal columns"),
                     ("Day,Lunch\nFunday,x\n", "not a day"), ("Day,Lunch\nMon,x\nMonday,y\n", "twice")]:
        try:
            mess.parse_csv(bad)
            check(f"CSV refused: {why}", False)
        except ValueError as e:
            check(f"CSV refused, saying what is wrong: {why}", why in str(e), str(e))
    sample = mess.get("IFC A")["data"]
    back = mess.parse_csv(mess.to_csv(sample))
    check("export then import gives the same menu ('Bread, butter & jam' stays one dish)",
          back == sample["week"], "round trip differs")

    store = sandbox("json")
    section("the sample, until a menu is saved")
    menus = mess.menus()["data"]
    check("four messes, in order, every one the labelled sample",
          [m["mess"] for m in menus] == list(mess.MESSES) and not any(m["official"] for m in menus))
    check("the sample messes differ from each other",
          len({str(m["week"]["monday"]) for m in menus}) == 4)

    section("who may write a menu")
    before = store.list_menus()["data"]
    for uid, who in [(RAVI, "a student"), (ADVISOR, "the faculty advisor"), (None, "nobody signed in")]:
        res = mess.save(users.get(uid) if uid else None, "IFC A", week_with("X"))
        check(f"{who}: refused, and nothing stored",
              res["outcome"] == "not_authorized" and store.list_menus()["data"] == before, str(res.get("outcome")))
    res = mess.save(users.get(WARDEN), "IFC Z", week_with("X"))
    check("an unknown mess: not_found", res["outcome"] == "not_found", res["error"])
    res = mess.save(users.get(WARDEN), "IFC A", {d: {} for d in mess.DAYS})
    check("an empty menu: refused", res["outcome"] == "invalid" and "no dishes" in res["error"])
    res = mess.save(users.get(WARDEN), "IFC A", {"monday": {"brunch": "x"}})
    check("a bad meal: refused, naming it", res["outcome"] == "invalid" and "brunch" in res["error"])
    check("  none of those stored anything", store.list_menus()["data"] == before)
    res = mess.save(users.get(WARDEN), "ifc a", week_with("Pongal"), {"lunch": "12:30 – 2:00"})
    check("the warden saves IFC A (named loosely)", res["success"] and res["data"]["mess"] == "IFC A", str(res.get("error")))
    menus = {m["mess"]: m for m in mess.menus()["data"]}
    check("  IFC A is now the saved menu, marked official, stamped with who",
          menus["IFC A"]["official"] and menus["IFC A"]["updated_by"] == WARDEN
          and menus["IFC A"]["week"]["friday"]["lunch"] == ["Pongal (fri lunch)"])
    check("  and the other three are still the sample", not any(menus[m]["official"] for m in ("IFC B", "IFC C", "IFC D")))

    section("asking in chat (Friday 23 Oct)")
    sandbox("json")
    out = ask(RAVI, meal="dinner")
    check("Ravi: 'what's for dinner?' -> his mess, today, dinner",
          out["debug"]["mess"]["resolved"] == {"mess": "IFC A (home mess)", "day": "2026-10-23 (Friday)", "meal": "dinner"},
          str(out["debug"].get("mess")))
    sample_friday = mess.get("IFC A")["data"]["week"]["friday"]["dinner"]
    check("  the reply lists that dinner, with its timing", all(d in out["reply"] for d in sample_friday)
          and "7:30" in out["reply"] and "(today)" in out["reply"], out["reply"])
    check("  and says it is a sample, in italics the chat can render (*…*, not _…_)",
          "*This is a sample menu" in out["reply"] and "_This" not in out["reply"])
    out = ask(RAVI, mess="B mess", meal="lunch", start_date="2026-10-24")
    check("'lunch at B mess tomorrow' -> IFC B, Saturday",
          out["debug"]["mess"]["resolved"]["mess"] == "IFC B" and "Saturday" in out["debug"]["mess"]["resolved"]["day"]
          and "(tomorrow)" in out["reply"] and "your mess" not in out["reply"], out["reply"])
    out = ask(RAVI)
    check("no meal named: the whole day, all four meals",
          all(m.title() in out["reply"] for m in mess.MEALS), out["reply"])
    out = ask(PRIYA, meal="lunch")
    check("a day scholar with no home mess is asked which mess",
          out["debug"]["outcome"] == "needs_mess" and "IFC A" in out["reply"], out["reply"])
    out = ask(PRIYA, mess="IFC C", meal="lunch")
    check("  and answered once she names one", out["debug"]["outcome"] == "answered"
          and out["debug"]["mess"]["resolved"]["mess"] == "IFC C")
    out = ask(RAVI, mess="IFC Z")
    check("a mess that doesn't exist is said so, never guessed",
          out["debug"]["outcome"] == "unknown_mess" and "IFC Z" in out["reply"], out["reply"])
    mess.save(users.get(WARDEN), "IFC A", week_with("Pongal"))
    out = ask(RAVI, meal="lunch")
    check("once the warden saves IFC A, the answer is from it — and is no longer called a sample",
          "Pongal (fri lunch)" in out["reply"] and "sample" not in out["reply"]
          and out["debug"]["mess"]["source"] == "saved", out["reply"])

    section("over HTTP")
    sandbox("json")
    c = TestClient(app)
    check("signed out: 401", c.get("/api/mess").status_code == 401)

    def login(username: str, password: str) -> None:
        c.cookies.clear()
        assert c.post("/api/login", json={"username": username, "password": password}).status_code == 200

    login("ravi.kumar", "ravi-demo")
    got = c.get("/api/mess").json()
    check("a student reads every mess, sees his own, and may not edit",
          got["mine"] == "IFC A" and got["may_edit"] is False and len(got["menus"]) == 4)
    r = c.put("/api/mess/IFC A", json={"week": week_with("X")})
    check("a student PUT: 403, and the menu did not change",
          r.status_code == 403 and not c.get("/api/mess").json()["menus"][0]["official"])
    r = c.post("/api/mess/IFC A/import", files={"file": ("m.csv", b"Day,Lunch\nMon,Rice\n", "text/csv")})
    check("a student import: 403", r.status_code == 403)
    r = c.get("/api/mess/ifc-b.csv")
    check("anyone downloads a menu as CSV (the import template)",
          r.status_code == 200 and r.text.startswith("Day,Breakfast,Lunch,Snacks,Dinner")
          and "IFC_B_menu.csv" in r.headers["content-disposition"])
    check("  a mess that doesn't exist: 404", c.get("/api/mess/ifc-z.csv").status_code == 404)

    login("lakshmi.narayanan", "advisor-demo")
    check("the advisor: may not edit, and a PUT is 403",
          c.get("/api/mess").json()["may_edit"] is False
          and c.put("/api/mess/IFC A", json={"week": week_with("X")}).status_code == 403)

    login("meena.krishnan", "warden-demo")
    check("the warden may edit", c.get("/api/mess").json()["may_edit"] is True)
    r = c.post("/api/mess/IFC B/import", files={"file": ("m.csv", b"Day,Lunch\nMon,\"Rice; Dal\"\n", "text/csv")})
    check("an import is read back for checking", r.status_code == 200 and r.json()["week"]["monday"]["lunch"] == ["Rice", "Dal"])
    check("  and saves nothing", not c.get("/api/mess").json()["menus"][1]["official"])
    r = c.post("/api/mess/IFC B/import", files={"file": ("m.csv", b"Colour\nred\n", "text/csv")})
    check("a sheet without meal columns: 400, saying what's needed", r.status_code == 400 and "meal columns" in r.json()["detail"])
    r = c.post("/api/mess/IFC B/import", files={"file": ("m.csv", b"\x89PNG\r\n\x1a\n\x00\xff\xfe", "image/png")})
    check("a picture renamed .csv: refused", r.status_code in (400, 415), str(r.status_code))
    r = c.put("/api/mess/IFC B", json={"week": week_with("Dosa"), "timings": {"breakfast": "7 – 9"}})
    check("the warden's PUT saves", r.status_code == 200 and r.json()["menu"]["official"] is True)
    got = {m["mess"]: m for m in c.get("/api/mess").json()["menus"]}
    check("  every reader sees it at once, with its timing",
          got["IFC B"]["official"] and got["IFC B"]["timings"]["breakfast"] == "7 – 9")
    r = c.put("/api/mess/IFC B", json={"week": {"monday": {"lunch": ["x" * 81]}}})
    check("an invalid PUT: 400, and the saved menu is untouched",
          r.status_code == 400 and c.get("/api/mess").json()["menus"][1]["week"]["monday"]["lunch"] == ["Dosa (mon lunch)"])
    check("GET /api/me carries the home mess", (login("ravi.kumar", "ravi-demo"), c.get("/api/me").json()["user"]["mess"])[1] == "IFC A")

    return finish("mess menu")


if __name__ == "__main__":
    raise SystemExit(main())
