"""One JSON line per process that opened the CRM, success or failure: work/run_log.jsonl (gitignored).

    python3 -m crm.runlog [--run-id ID] [--path P]     # print lines (all, or one run's) as a table

Written at exit (atexit), so a blocked step, a dry run, an exception and a normal end all leave their line.
Only a process killed outright (SIGKILL, container reclaimed mid-step) leaves none; run_*.sh records a
fallback line for a step that died without one.
"""
import atexit, json, os, sys
from datetime import datetime, timezone

from . import guard

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def path():
    return os.environ.get("SIFT_RUN_LOG") or os.path.join(ROOT, "work", "run_log.jsonl")


def write(rec, p=None):
    p = p or path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(rec) + "\n")


def read(p=None, run_id=None, since=None):
    p = p or path()
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if run_id and r.get("run_id") != run_id:
            continue
        if since and (r.get("ts_start") or "") < since:
            continue
        out.append(r)
    return out


_state = {"started": None, "exit": None, "exc": None, "done": False}


def start(run):
    """Hook the process so its line is written however it ends."""
    if _state["started"]:
        return
    _state["started"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    real_exit, real_hook = sys.exit, sys.excepthook

    def _exit(code=0):
        _state["exit"] = code
        real_exit(code)

    def _hook(t, v, tb):
        if isinstance(v, guard.GuardStop):  # the guard already said why; no traceback
            _state["exit"] = v.code
            return
        _state["exc"] = f"{t.__name__}: {str(v)[:300]}"
        real_hook(t, v, tb)

    sys.exit, sys.excepthook = _exit, _hook
    atexit.register(finish, run)


def finish(run):
    if _state["done"]:
        return
    _state["done"] = True
    rec = run.record()
    code = _state["exit"]
    if rec["status"] == "ok":
        if _state["exc"]:
            rec["status"], rec["error"] = "error", _state["exc"]
        elif code not in (None, 0) and not (isinstance(code, str) and not code):
            rec["status"], rec["error"] = "error", (code if isinstance(code, str) else f"exit code {code}")
    rec = {"ts_start": _state["started"], "ts_end": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "step": os.environ.get("SIFT_STEP") or "", **rec}
    try:
        write(rec)
    except OSError as e:
        print(f"[runlog] could not write {path()}: {e}", file=sys.stderr)


def table(rows):
    head = ("task", "status", "mode", "written", "read", "api", "rejected", "batched", "error")
    lines = ["  ".join(f"{h:<18}" if h == "task" else h for h in head)]
    for r in rows:
        lines.append(f"{r.get('task', ''):<18}  {r.get('status', '')}  {r.get('mode', '')}  {r.get('leads_written')}  "
                     f"{r.get('leads_read')}  {r.get('api_calls')}  {r.get('rejected_by_verification')}  "
                     f"{'yes' if r.get('batched') else 'no'}  {r.get('error') or ''}")
    return "\n".join(lines)


def digest(rows):
    """The email's "Automation runs" section: one line per step (task, status, leads written, batched),
    blocked and failed steps first. -> (html, text, problems) where problems = [(status, step)]."""
    from html import escape
    rows = sorted(rows, key=lambda r: ({"blocked": 0, "error": 1}.get(r.get("status"), 2), r.get("ts_start") or ""))
    problems = [(r.get("status"), r.get("step") or r.get("task")) for r in rows if r.get("status") in ("blocked", "error")]
    if not rows:
        return ("<h3>Automation runs</h3><p>No automation run log was available for this day (not a confirmation that "
                "the runs succeeded).</p>", "Automation runs: no run log available for this day (not a confirmation "
                "that the runs succeeded).", problems)
    html_rows, text = [], ["Automation runs (blocked and failed first):"]
    for r in rows:
        st = (r.get("status") or "ok").upper()
        name = r.get("step") or r.get("task")
        where = f"{name} ({r.get('task')})" if r.get("step") and r.get("step") != r.get("task") else name
        batched = "yes" if r.get("batched") else "no"
        written = "-" if r.get("leads_written") is None else r.get("leads_written")
        err = r.get("error") or ""
        style = ' style="color:#b00020;font-weight:bold"' if st in ("BLOCKED", "ERROR") else ""
        html_rows.append(f"<tr{style}><td>{escape(st)}</td><td>{escape(str(where))}</td><td>{escape(str(written))}</td>"
                         f"<td>{batched}</td><td>{escape(err)}</td></tr>")
        text.append(f"- {st} {where}: written {written}, batches {batched}" + (f" - {err}" if err else ""))
    head = ""
    if problems:
        head = ('<p style="color:#b00020;font-weight:bold">ATTENTION: ' + escape(", ".join(f"{n} {s}" for s, n in problems))
                + "</p>")
    html = ('<h3>Automation runs</h3>' + head + '<table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse">'
            '<tr><th align="left">Status</th><th align="left">Step</th><th align="left">Leads written</th>'
            '<th align="left">Batches</th><th align="left">Detail</th></tr>' + "".join(html_rows) + "</table>")
    return html, "\n".join(text), problems


def subject_tag(problems):
    """Prefix for the email subject when a step was blocked or failed: "[BLOCKED: bid updater] "."""
    blocked = [n for s, n in problems if s == "blocked"]
    failed = [n for s, n in problems if s == "error"]
    out = ""
    if blocked:
        out += f"[BLOCKED: {', '.join(blocked)}] "
    if failed:
        out += f"[FAILED: {', '.join(failed)}] "
    return out


def step_end(step, code, run_id=None):
    """run_*.sh, after each step: a step that died without a line, or exited non-zero while its lines say ok
    (a raised SystemExit, a kill), gets an error line so the run's record is never silently green."""
    run_id = run_id or os.environ.get("SIFT_RUN_ID") or ""
    code = int(code)
    mine = [r for r in read(run_id=run_id) if r.get("step") == step]
    if code == 0 and mine:
        return
    if code != 0 and any(r.get("status") in ("blocked", "error") for r in mine):
        return
    if code == 0 and not mine:
        return  # a step that never opened the CRM (download, OCR): nothing to say
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write({"ts_start": now, "ts_end": now, "step": step, "run_id": run_id, "task": step,
           "profile": os.environ.get("SIFT_PROFILE") or "manual", "mode": "", "status": "error",
           "error": ("skipped: its input was not produced (an earlier step failed)" if code == 4 and not mine
                     else f"step exited with code {code}" + ("" if mine else " and left no log line")), "planned": None,
           "leads_read": None, "leads_written": None, "api_calls": None, "rejected_by_verification": None,
           "destructive_leads": None, "batched": False, "batches": 0,
           "skip_trace_credits": None, "trestle_queries": None, "ai_usage": None})


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--step-end"]:
        step_end(a[1], a[2])
        sys.exit(0)
    rows = read(a[a.index("--path") + 1] if "--path" in a else None, a[a.index("--run-id") + 1] if "--run-id" in a else None)
    for r in rows:
        print(json.dumps(r))
