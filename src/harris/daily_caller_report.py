"""A caller's day, read from DataSift's own activity logs, as an English email (HTML + text). READ-ONLY.

    python3 daily_caller_report.py [--user "charles cator"] [--date 2026-11-10] [--out work/report]
        -> <out>.html, <out>.txt, <out>.json  (and the subject on stdout)

Covers the leads assigned to the user plus any record with a call attempt (a caller can work a lead that is
not theirs), and every event of the day in Houston time (America/Chicago) that the user wrote: smrtPhone
calls from their number or user, notes, status changes, phone dispositions, tasks. Events are deduplicated
(a call shows on the owner and is read once per property that owner holds).

"To do" items are rules over what was logged, never guesses about the person:
  - a note that reads as interest (wants / looking to sell, interested, call back, offer) on a lead whose
    status is not Hot Lead or Warm Lead -> follow up and set the status
  - a note or disposition that reads as do-not-call on a lead whose status is not DNC -> set DNC
  - inbound calls or SMS on the day -> confirm they were answered
  - tasks the user created -> listed
  - called leads with no note -> listed
Conversations = leads whose note says the caller spoke with someone, or whose status changed.
Operator's schedule (2026-11-11): Monday to Friday, 7:00 p.m. Houston time.
"""
import collections, json, os, re, sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402

HOU = ZoneInfo("America/Chicago")
# Notes that only say nobody was reached, including cut-off words and joins ("Wrong numb", "No response.",
# "Wrong number / No respons", "wrong number and no response")
_NR = r"(no\s*(answer|resp\w*)|wrong\s*num\w*|dead\s*num\w*|voice\s*mail\w*|vm|left\s*(a\s*)?(vm|voicemail))"
NO_REPLY = re.compile(rf"^\s*{_NR}(\s*(/|,|-|and|&)?\s*{_NR})*[\s.!]*$", re.I)
INTEREST = re.compile(r"looking to sell|want(s)? to sell|interested in selling|is interested|call (me |them )?back|"
                      r"make (an |us an )?offer|send (an |us an )?offer|open to (an )?offer", re.I)
DNC_WORDS = re.compile(r"\bDNC\b|do not call|don'?t call|stop calling|remove (me|them|her|him)|not to be (contacted|called)|"
                       r"taken out (from|of) (the )?list|remove(d)? from (the )?list", re.I)
HOT = {"hot lead", "warm lead"}
esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main(*flags):
    user = flags[flags.index("--user") + 1] if "--user" in flags else "charles cator"
    day = date.fromisoformat(flags[flags.index("--date") + 1]) if "--date" in flags else datetime.now(HOU).date()
    out = flags[flags.index("--out") + 1] if "--out" in flags else "work/report/daily_caller"
    crm = get_crm()
    api = crm.api
    users = crm._users()
    uid = users.get(user.lower())
    if not uid:
        raise SystemExit(f"no DataSift user named {user!r}")
    detail = {u["uuid"]: u for u in (lambda r: r if isinstance(r, list) else r.get("results") or [])(api.call("/api/internal/account/user/"))}
    email = (detail.get(uid) or {}).get("email", "")
    start = datetime(day.year, day.month, day.day, tzinfo=HOU).astimezone(timezone.utc).replace(tzinfo=None)
    end = start + timedelta(days=1)

    ids = []
    for must in ({"assigned_to": uid}, {"predictivecall_attempts": [1, 9999]}):
        for pt in ("clean", "incomplete"):
            o = 0
            while True:
                page = api.call("/api/internal/property/", "POST", {"limit": 100, "offset": o, "query": {"must": {**must, "property_type": pt}}},
                                headers={"x-http-method-override": "GET"}).get("results") or []
                ids += [x["uuid"] for x in page]
                o += len(page)
                if len(page) < 100:
                    break
    ids = list(dict.fromkeys(ids))

    calls, leads, phones, tasks, sms_in = {}, {}, {}, [], []
    for lid in ids:
        d, events = crm.lead_activity(lid)
        street = (d.get("address") or {}).get("street") or ""
        status = (d.get("status") or {}).get("title") if isinstance(d.get("status"), dict) else d.get("status")
        for e in events:
            try:
                ts = datetime.strptime(e["timestamp"][:19], "%Y-%m-%d %H:%M:%S")
            except (KeyError, ValueError):
                continue
            if not start <= ts < end:
                continue
            p = e.get("payload") or {}
            mine = e.get("author") == email or (e.get("author_extra_info") or {}).get("uuid") == uid \
                or str(((p.get("call") or {}).get("user") or {}).get("uuid") if isinstance((p.get("call") or {}).get("user"), dict) else "") == uid
            et = e.get("event_type") or ""
            if et.startswith("owner.call"):
                c = p.get("call") or {}
                if not mine and e.get("source") != "smrtphone":
                    continue
                k = c.get("uuid") or (e["timestamp"], et)
                calls.setdefault(k, {"dir": c.get("direction"), "events": set(), "dur": 0, "ts": ts, "street": street})
                calls[k]["events"].add(et)
                if et == "owner.call.answered":
                    calls[k]["dur"] = int(c.get("duration") or 0)
                continue
            if et == "owner.sms.received":
                sms_in.append((street, (p.get("sms") or {}).get("uuid")))
                continue
            if not mine:
                continue
            L = leads.setdefault(lid, {"street": street, "status": status, "notes": [], "status_changes": [], "attempts": 0})
            if et == "property.message.added":
                L["notes"].append(p.get("message") or "")
            elif et == "property.status.updated":
                L["status_changes"].append((ts, ((p.get("property") or {}).get("status") or [None, None])))
            elif et == "property.predictivecall.attempts.increased":
                L["attempts"] += 1
            elif et == "owner.phone.status.updated":
                o = p.get("owner") or {}
                phones[(o.get("phone"), ts)] = (o.get("status") or [None, None])[1]
            elif et == "task.created":
                t = p.get("task") or {}
                who = (t.get("assigned_to_user") or {}).get("email") or ""
                tasks.append((street, t.get("title") or "Task", (t.get("due_date") or "")[:10], who))
    # a call counts for this user only if their lead or their own call: keep calls on leads they touched
    touched = {L["street"] for L in leads.values()}
    calls = {k: v for k, v in calls.items() if v["street"] in touched or v["dir"] == "inbound"}
    sms_in = list(dict.fromkeys(sms_in))
    final_phone = collections.Counter()
    last = {}
    for (num, ts), st in sorted(phones.items(), key=lambda x: x[0][1]):
        last[num] = st
    final_phone.update(v for v in last.values() if v)

    out_calls = [c for c in calls.values() if c["dir"] == "outbound"]
    in_calls = [c for c in calls.values() if c["dir"] == "inbound"]
    answered = [c for c in calls.values() if "owner.call.answered" in c["events"]]
    worked = [L for L in leads.values() if L["attempts"]]
    no_note = [L["street"] for L in worked if not L["notes"]]
    convo = []
    todo = []
    for L in leads.values():
        real = [n for n in L["notes"] if n.strip() and not NO_REPLY.match(n.strip())]
        # the record's own status is the truth; the day's latest change only when the record carries none
        cur = L["status"] or (max(L["status_changes"])[1][1] if L["status_changes"] else "") or ""
        if real or L["status_changes"]:
            convo.append({"street": L["street"], "note": " / ".join(real)[:180] or "-", "status": cur or "no status"})
        text = " ".join(real)
        if INTEREST.search(text) and cur.lower() not in HOT:
            todo.append(f"Follow up on <b>{esc(L['street'])}</b> (interested in selling) and set its status to <b>Hot Lead</b>.")
        if DNC_WORDS.search(text) and cur.lower() != "dnc":
            todo.append(f"Set <b>{esc(L['street'])}</b> to <b>DNC</b> status (asked not to be contacted).")
    if in_calls or sms_in:
        where = sorted({s for s, _ in sms_in})
        todo.append(f"Confirm the {len(in_calls)} inbound call(s) and {len(sms_in)} SMS received"
                    + (f" (SMS from {esc(', '.join(where))})" if where else "") + " were answered.")
    for street, title, due, who in tasks:
        todo.append(f"Task <b>{esc(title)}</b> on {esc(street)}, due {esc(due)}" + (f", assigned to {esc(who)}" if who else "") + ".")
    if no_note:
        todo.append(f"{len(no_note)} called lead(s) have no note: {esc(', '.join(no_note[:8]))}.")

    first = min((c["ts"] for c in out_calls), default=None)
    lastc = max((c["ts"] for c in out_calls), default=None)
    hours = (f"{first.replace(tzinfo=timezone.utc).astimezone(HOU):%-I:%M %p} - {lastc.replace(tzinfo=timezone.utc).astimezone(HOU):%-I:%M %p}"
             if first else "no outbound calls")
    talk = round(sum(c["dur"] for c in answered) / 60)
    name = user.title()
    subject = f"Daily Report - {name} - {day:%B %-d, %Y}"
    summary = (f"{name} worked {len(worked)} leads with {len(out_calls)} outbound calls ({hours} Houston time) "
               f"and had {len(convo)} conversations with owners.")
    rows = [("Leads worked", len(worked)), ("Outbound calls", len(out_calls)),
            ("Inbound calls / SMS received", f"{len(in_calls)} / {len(sms_in)}"),
            ("Calls answered (incl. voicemail)", len(answered)), ("Total talk time", f"{talk} min"),
            ("Conversations", len(convo)), ("Notes on called leads", f"{len(worked) - len(no_note)} of {len(worked)}"),
            ("Phone numbers dispositioned", f"{sum(final_phone.values())}: " + ", ".join(f"{k.replace('_', ' ').title()} {v}" for k, v in final_phone.most_common())),
            ("Lead statuses changed", sum(1 for L in leads.values() if L["status_changes"])), ("Tasks created", len(tasks))]
    tbl = lambda head, body: ('<table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse"><tr>'
                              + "".join(f'<th align="left">{h}</th>' for h in head) + "</tr>"
                              + "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in body) + "</table>")
    html = (f"<p>Hi Maireni,</p><p><b>Summary:</b> {esc(summary)}</p><h3>Daily results</h3>" + tbl(["Metric", "Result"], rows)
            + "<h3>Conversations with owners</h3>"
            + (tbl(["Lead", "Note", "Status"], [(c["street"], c["note"], c["status"]) for c in convo]) if convo else "<p>None today.</p>")
            + "<h3>To do</h3>" + ("<ol>" + "".join(f"<li>{t}</li>" for t in todo) + "</ol>" if todo else "<p>Nothing pending from today's records.</p>")
            + f"<p><i>Source: DataSift activity logs (smrtPhone calls, notes, statuses, phone dispositions) for {day:%B %-d, %Y}, Houston time.</i></p>")
    strip = lambda s: re.sub(r"<[^>]+>", "", s)
    text = (f"Hi Maireni,\n\nSummary: {summary}\n\nResults: " + "; ".join(f"{k}: {v}" for k, v in rows)
            + "\n\nConversations:\n" + ("\n".join(f"- {c['street']}: {c['note']} ({c['status']})" for c in convo) or "None today.")
            + "\n\nTo do:\n" + ("\n".join(f"{i}. {strip(t)}" for i, t in enumerate(todo, 1)) or "Nothing pending.")
            + f"\n\nSource: DataSift activity logs for {day:%B %-d, %Y}, Houston time.")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out + ".html", "w").write(html)
    open(out + ".txt", "w").write(text)
    json.dump({"subject": subject, "worked": len(worked), "outbound": len(out_calls), "conversations": len(convo), "todo": len(todo)},
              open(out + ".json", "w"))
    print(f"SUBJECT: {subject}")
    print(f"REPORT: {len(worked)} leads worked, {len(out_calls)} outbound calls, {len(convo)} conversations, {len(todo)} to-do items "
          f"-> {out}.html / {out}.txt")


if __name__ == "__main__":
    main(*sys.argv[1:])
