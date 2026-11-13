"""Leads called without an attempt increment, for the 7 pm email. READ-ONLY: it never writes to DataSift.

The call-attempt counter (predictivecall_attempts) is raised BY HAND by the caller in DataSift, once per round
on a lead, about 50 s after the last call (measured 2026-11-11); smrtPhone does not raise it. This section
lists leads with outbound calls in the window and no counter increase after their first call.

Window: from the previous report's cut (kept as a timestamp in the run log) to now; 24 hours when unknown.
Scope: leads on "Harris - Foreclosure" or "Harris - Probate" that can have calls (counter 1+, assigned to a
caller, or tagged Priority 1 / Blitz Active). The Senior Equity list is never read; a lead also on it is skipped.

Each lead's history is read TWICE and merged, because DataSift's log endpoint does not always return the same
events. Verdicts:
  (a) "probable olvido"  : outbound calls, no increase in EITHER read, and the counter shows no unseen increase
  (b) "inconsistent"     : the two reads disagree about the increase, or the counter is higher than the last
                           value the history shows (it went up but the event is missing). Informative only:
                           a lead whose reads disagree is NEVER put in (a).
  ok                     : an increase after the first call (one a day is enough, even for two rounds).
Calls = owner.call.* events with direction outbound (grouped by call id). Increases = the CALL counter's event
(property.predictivecall.attempts.increased, carrying predictivecall_attempts); the SMS counter never counts.
A time budget (default 15 min) caps the reading: past it, the section is marked partial with how many leads
were left unchecked, and the email still goes out on time.
"""
import time
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

HOU = ZoneInfo("America/Chicago")
LISTS = ("Harris - Foreclosure", "Harris - Probate")
SENIOR = "Harris - Senior Equity"
PIPELINE_TAGS = ("Priority 1", "Blitz Active")
INCREASE = "property.predictivecall.attempts.increased"
RECORD_URL = "https://app.reisift.io/records/properties/{uuid}/details"  # same as src/sms_agent/escalate.py
BUDGET_S = 15 * 60


def ts_of(e):
    try:
        return datetime.strptime(str(e.get("timestamp") or e.get("created"))[:19].replace("T", " "),
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def call_time(c):
    try:
        return datetime.strptime(str(c.get("created"))[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def outbound_calls(events, start, end):
    """call id -> {"ts": first event time, "number", "user"} for outbound calls inside [start, end)."""
    calls = {}
    for e in events:
        if not (e.get("event_type") or "").startswith("owner.call."):
            continue
        c = (e.get("payload") or {}).get("call") or {}
        # the call's own creation time: an outcome event (no-answer, answered) can be logged hours later
        t = call_time(c) or ts_of(e)
        if c.get("direction") != "outbound" or not t or not start <= t < end:
            continue
        k = c.get("uuid") or (e.get("timestamp"), c.get("destination_number"))
        u = c.get("user")
        rec = calls.setdefault(k, {"ts": t, "number": c.get("destination_number"),
                                   "user": (u.get("uuid") if isinstance(u, dict) else u) or c.get("external_user")})
        rec["ts"] = min(rec["ts"], t)
    return calls


def increases(events):
    """(time, new value) of every call-counter increase (the SMS counter is ignored)."""
    out = []
    for e in events:
        if e.get("event_type") != INCREASE:
            continue
        v = ((e.get("payload") or {}).get("property") or {}).get("predictivecall_attempts")
        t = ts_of(e)
        if t and isinstance(v, list) and len(v) == 2:
            out.append((t, int(v[1] or 0)))
    return out


def verdict(read1, read2, counter, start, end):
    """-> (kind, detail). kind: None (no outbound call in the window), "ok", "forgot" (a), "inconsistent" (b)."""
    calls = {**outbound_calls(read1, start, end), **outbound_calls(read2, start, end)}
    if not calls:
        return None, {}
    first = min(c["ts"] for c in calls.values())
    up1 = [x for x in increases(read1) if x[0] >= first]
    up2 = [x for x in increases(read2) if x[0] >= first]
    detail = {"calls": sorted(calls.values(), key=lambda c: c["ts"]), "first": first}
    if up1 and up2:
        return "ok", detail
    if up1 or up2:
        return "inconsistent", dict(detail, reason="the two history reads disagree about the increase")
    seen = [v for _, v in increases(read1) + increases(read2)]
    if int(counter or 0) > (max(seen) if seen else 0):
        return "inconsistent", dict(detail, reason="the counter went up but the history shows no event for it")
    return "forgot", detail


def candidates(crm, users):
    """Lead ids on the two lists that can have calls. The Senior list is never queried."""
    api = crm.api
    L = lambda r: r if isinstance(r, list) else r.get("results") or []
    lists = {x["title"]: x["uuid"] for x in L(api.call("/api/internal/list/?limit=999"))}
    tags = {(t.get("name") or t.get("title")): t["uuid"] for t in L(api.call("/api/internal/tag/?limit=9999"))}
    extra = [{"predictivecall_attempts": [1, 9999]}] + [{"assigned_to": u} for u in users] \
        + ([{"any_tags": [tags[t] for t in PIPELINE_TAGS if t in tags]}] if any(t in tags for t in PIPELINE_TAGS) else [])
    ids = []
    for name in LISTS:
        if name not in lists:
            continue
        for must in extra:
            for pt in ("clean", "incomplete"):
                o = 0
                while True:
                    page = api.call("/api/internal/property/", "POST", {"limit": 100, "offset": o, "query": {"must": {
                        **must, "any_lists": [lists[name]], "property_type": pt}}},
                        headers={"x-http-method-override": "GET"}).get("results") or []
                    ids += [x["uuid"] for x in page]
                    o += len(page)
                    if len(page) < 100:
                        break
    return list(dict.fromkeys(ids))


def scan(crm, start, end, budget_s=BUDGET_S, clock=time.monotonic):
    """-> {"forgot": [...], "inconsistent": [...], "checked", "unchecked", "skipped_senior", "error"}."""
    users = crm._users()  # full name (lower) -> uuid
    names = {u: n.title() for n, u in users.items()}
    out = {"forgot": [], "inconsistent": [], "checked": 0, "unchecked": 0, "skipped_senior": 0, "error": ""}
    t0 = clock()
    try:
        ids = candidates(crm, list(users.values()))
    except Exception as e:  # noqa: BLE001 (the email must still go out)
        out["error"] = f"could not list the leads to check: {str(e)[:120]}"
        return out
    for i, lid in enumerate(ids):
        if clock() - t0 > budget_s:
            out["unchecked"] = len(ids) - i
            break
        try:
            d, read1 = crm.lead_activity(lid)
            on = {(x.get("title") if isinstance(x, dict) else x) for x in d.get("lists") or []}
            if SENIOR in on:
                out["skipped_senior"] += 1
                continue
            _, read2 = crm.lead_activity(lid)
        except Exception as e:  # noqa: BLE001
            out["inconsistent"].append({"id": lid, "address": "", "reason": f"history could not be read: {str(e)[:80]}"})
            continue
        out["checked"] += 1
        kind, det = verdict(read1, read2, d.get("predictivecall_attempts"), start, end)
        if kind in ("forgot", "inconsistent"):
            a = d.get("address") or {}
            out[kind].append({"id": lid, "address": ", ".join(x for x in (a.get("street"), str(a.get("postal_code") or "")[:5]) if x),
                              "times": [c["ts"].astimezone(HOU).strftime("%a %-I:%M %p") for c in det["calls"]],
                              "numbers": len({c["number"] for c in det["calls"]}),
                              "callers": sorted({names.get(c["user"], "unknown") for c in det["calls"]}),
                              "reason": det.get("reason", ""), "link": RECORD_URL.format(uuid=lid)})
    return out


def section(res, start, end, history=None):
    """-> (html, text, counts for the public run log: numbers only). history: [(Houston date, forgot count)]
    of earlier reports, for the 7-day total."""
    span = f"{start.astimezone(HOU):%a %b %-d %-I:%M %p} - {end.astimezone(HOU):%a %b %-d %-I:%M %p} (Houston)"
    counts = {"attempt_forgot": len(res["forgot"]), "attempt_inconsistent": len(res["inconsistent"]),
              "attempt_checked": res["checked"], "attempt_unchecked": res["unchecked"]}
    head = "<h3>Leads called without attempt increment</h3>"
    if res["error"]:
        msg = f"Could not read the call history today: {res['error']}. Nothing below is a confirmation."
        return head + f'<p style="color:#b00020;font-weight:bold">{escape(msg)}</p>', \
            "Leads called without attempt increment: " + msg, counts
    week = sum(n for _, n in (history or [])[-6:]) + len(res["forgot"])
    partial = (f'<p style="color:#b00020;font-weight:bold">PARTIAL: the time limit was reached; {res["unchecked"]} '
               "lead(s) were not checked.</p>") if res["unchecked"] else ""
    rows = "".join(f"<tr><td>{escape(x['address'])}</td><td>{escape(', '.join(x['times']))}</td><td>{x['numbers']}</td>"
                   f"<td>{escape(', '.join(x['callers']))}</td><td><a href=\"{x['link']}\">open</a></td></tr>" for x in res["forgot"])
    table = ('<table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse"><tr><th align="left">Lead</th>'
             '<th align="left">Calls</th><th align="left">Numbers dialed</th><th align="left">Caller</th>'
             '<th align="left">Record</th></tr>' + rows + "</table>") if res["forgot"] else "<p>None.</p>"
    inc = "".join(f"<li>{escape(x['address'] or x['id'][:8])}: {escape(x['reason'])}</li>" for x in res["inconsistent"])
    html = (head + f"<p>Window: {escape(span)}. {res['checked']} called-capable leads checked.</p>" + partial
            + "<p><b>Probable missed increment</b> (calls, counter not raised):</p>" + table
            + ("<p><b>Inconsistent history</b> (informative, not a caller error):</p><ul>" + inc + "</ul>" if inc else "")
            + f"<p>Total today: {len(res['forgot'])} | last 7 days: {week}</p>")
    text = ["Leads called without attempt increment", f"Window: {span}. {res['checked']} leads checked."]
    if res["unchecked"]:
        text.append(f"PARTIAL: time limit reached; {res['unchecked']} lead(s) not checked.")
    text.append("Probable missed increment:")
    text += [f"- {x['address']} | calls {', '.join(x['times'])} | {x['numbers']} number(s) | {', '.join(x['callers'])} | {x['link']}"
             for x in res["forgot"]] or ["- none"]
    if res["inconsistent"]:
        text.append("Inconsistent history (informative):")
        text += [f"- {x['address'] or x['id'][:8]}: {x['reason']}" for x in res["inconsistent"]]
    text.append(f"Total today: {len(res['forgot'])} | last 7 days: {week}")
    return html, "\n".join(text), counts
