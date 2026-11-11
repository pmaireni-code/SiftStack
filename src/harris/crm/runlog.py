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
    problems = [(r.get("status"), r.get("step") or r.get("task")) for r in rows
                if r.get("status") in ("blocked", "error") and r.get("step") != "(whole run)"]
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


def subject_tag(problems, log_received=True):
    """Prefix for the email subject when a step was blocked or failed ("[BLOCKED: bid updater] "), or when
    no run log arrived for the day ("[NO RUN LOG RECEIVED TODAY] ")."""
    blocked = [n for s, n in problems if s == "blocked"]
    failed = [n for s, n in problems if s == "error"]
    out = "" if log_received else "[NO RUN LOG RECEIVED TODAY] "
    if blocked:
        out += f"[BLOCKED: {', '.join(blocked)}] "
    if failed:
        out += f"[FAILED: {', '.join(failed)}] "
    return out


# ---- the run-logs branch: how a scheduled run's lines outlive its container -------------------------
BRANCH = "run-logs"
PUBLIC_FIELDS = ("ts_start", "ts_end", "run_id", "step", "task", "profile", "mode", "status", "error_code",
                 "planned", "leads_read", "leads_written", "api_calls", "rejected_by_verification",
                 "destructive_leads", "batched", "batches", "skip_trace_credits", "trestle_queries", "ai_usage")


def houston_day(ts):
    """ISO UTC timestamp -> its Houston calendar date (None when unreadable)."""
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromisoformat(ts).astimezone(ZoneInfo("America/Chicago")).date()
    except (TypeError, ValueError):
        return None


def public(rec):
    """The repo is PUBLIC: a published line carries counts and statuses only. Error text can name a lead's
    address (a blocked Starting Bid clear does), so it becomes a category: the guard's code, an exception
    type, or an exit code. The full text stays in the run's own output and in the alert email."""
    out = {k: rec.get(k) for k in PUBLIC_FIELDS}
    err = rec.get("error") or ""
    if rec.get("error_code"):
        err = f"blocked by the safety brakes ({rec['error_code']})" if rec.get("status") == "blocked" else rec["error_code"]
    elif err and not err.startswith(("step exited", "skipped:", "exit code")):
        err = err.split(":", 1)[0][:60]  # "RuntimeError: <anything>" -> "RuntimeError"
    out["error"] = err
    out["would_block"] = bool(rec.get("would_block"))
    return out


def _git(*args, cwd=None, check=True):
    """git in the repo (SIFT_RUNLOG_GIT_DIR overrides it: the tests point it at a scratch clone)."""
    import subprocess
    p = subprocess.run(["git", *args], cwd=cwd or os.environ.get("SIFT_RUNLOG_GIT_DIR") or ROOT, capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(p.stderr or p.stdout).strip()[:200]}")
    return p


def day_file(day):
    return f"logs/run_log_{day:%Y-%m-%d}.jsonl"


def publish(rows, remote="origin", sleep=None):
    """Append the run's lines (public form) to logs/run_log_<Houston date>.jsonl on the run-logs branch and
    push. Retries a rejected push 4 times (2, 4, 8, 16 s), rebasing on what another run pushed meanwhile.
    -> (ok, message). Never raises: a failed push must not fail the business steps."""
    import shutil, tempfile, time
    sleep = sleep or time.sleep
    if not rows:
        return True, "nothing to publish"
    wt = tempfile.mkdtemp(prefix="runlogs-")
    shutil.rmtree(wt)
    try:
        have = _git("fetch", remote, BRANCH, check=False).returncode == 0
        _git("worktree", "add", "--detach", wt, f"{remote}/{BRANCH}" if have else "HEAD")
        if not have:  # first run ever: an empty branch holding nothing but the logs
            _git("checkout", "--orphan", BRANCH + "-new", cwd=wt)
            _git("rm", "-rqf", ".", cwd=wt)
            open(os.path.join(wt, "README.md"), "w").write(
                "# run-logs\n\nOne JSON line per Harris automation step, written by src/harris/run_*.sh "
                "(crm/runlog.py). Counts and statuses only: no names, no addresses.\n")
        by_day = {}
        for r in rows:
            by_day.setdefault(houston_day(r.get("ts_start")) or datetime.now(timezone.utc).date(), []).append(public(r))
        for day, recs in by_day.items():
            f = os.path.join(wt, day_file(day))
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "a") as fh:
                fh.writelines(json.dumps(x) + "\n" for x in recs)
        _git("add", "-A", cwd=wt)
        ident = ["-c", "user.name=Harris run log", "-c", "user.email=harris-runlog@users.noreply.github.com"]
        _git(*ident, "commit", "-qm", f"run log: {rows[0].get('run_id') or 'run'}", cwd=wt)
        for attempt in range(5):
            p = _git("push", remote, f"HEAD:refs/heads/{BRANCH}", cwd=wt, check=False)
            if p.returncode == 0:
                return True, f"run log pushed to {BRANCH}: " + ", ".join(day_file(d) for d in by_day)
            if attempt == 4:
                return False, f"run log push to {BRANCH} failed: {(p.stderr or p.stdout).strip()[:200]}"
            sleep(2 ** (attempt + 1))
            if _git("fetch", remote, BRANCH, cwd=wt, check=False).returncode == 0:
                _git(*ident, "rebase", "-q", f"{remote}/{BRANCH}", cwd=wt, check=False)
    except Exception as e:  # noqa: BLE001 (reported, never fatal)
        return False, f"run log push to {BRANCH} failed: {str(e)[:200]}"
    finally:
        _git("worktree", "remove", "--force", wt, check=False)
    return False, "run log push failed"


def fetch_day(day, remote="origin"):
    """The day's published lines from the run-logs branch, or None when that day's file was never pushed."""
    if _git("fetch", remote, BRANCH, check=False).returncode:
        return None
    p = _git("show", f"{remote}/{BRANCH}:{day_file(day)}", check=False)
    if p.returncode:
        return None
    return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]


def alert(rows):
    """The immediate notice for a run with a blocked or failed step -> (subject, text, html), or None."""
    from html import escape
    bad = [r for r in rows if r.get("status") in ("blocked", "error") and r.get("step") != "(whole run)"]
    if not bad:
        return None
    run_id = rows[0].get("run_id") or "run"
    kinds = sorted({r["status"].upper() for r in bad})
    subject = f"[{'/'.join(kinds)}] Harris automation {run_id}: " + ", ".join(r.get("step") or r.get("task") for r in bad)
    lines = [f"- {r['status'].upper()} {r.get('step') or r.get('task')} ({r.get('task')}): {r.get('error') or ''}" for r in bad]
    ok = [r.get("step") or r.get("task") for r in rows if r not in bad and r.get("step") != "(whole run)"]
    text = (f"The Harris automation run {run_id} had {len(bad)} step(s) blocked or failed:\n" + "\n".join(lines)
            + ("\n\nThe other steps ran: " + ", ".join(ok) if ok else "")
            + "\n\nBLOCKED means the safety brakes stopped that step on purpose and it wrote nothing; it is not a script "
              "failure. ERROR means the step failed. Details are in the run's output; the full day appears in the 7 pm report.")
    html = ("<p><b>The Harris automation run " + escape(run_id) + f" had {len(bad)} step(s) blocked or failed:</b></p><ul>"
            + "".join(f'<li style="color:#b00020">{escape(l[2:])}</li>' for l in lines) + "</ul>"
            + ("<p>The other steps ran: " + escape(", ".join(ok)) + "</p>" if ok else "")
            + "<p><b>BLOCKED</b> means the safety brakes stopped that step on purpose and it wrote nothing; it is not a script "
              "failure. <b>ERROR</b> means the step failed. Details are in the run's output; the full day appears in the 7 pm report.</p>")
    return subject, text, html


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
    if a[:1] == ["--run-end"]:  # --run-end RUN_NAME N_FAILED "steps": one line for the whole run, always
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        mine = read(run_id=os.environ.get("SIFT_RUN_ID") or "")
        write({"ts_start": min([r.get("ts_start") or now for r in mine] + [now]), "ts_end": now, "step": "(whole run)",
               "run_id": os.environ.get("SIFT_RUN_ID") or "", "task": a[1], "profile": os.environ.get("SIFT_PROFILE") or "manual",
               "mode": "", "status": "error" if int(a[2]) else "ok",
               "error": f"{a[2]} step(s) failed or blocked: {a[3]}" if int(a[2]) else "", "error_code": "",
               "would_block": "", "planned": None, "leads_read": None,
               "leads_written": sum(r.get("leads_written") or 0 for r in mine), "api_calls": sum(r.get("api_calls") or 0 for r in mine),
               "rejected_by_verification": sum(r.get("rejected_by_verification") or 0 for r in mine),
               "destructive_leads": sum(r.get("destructive_leads") or 0 for r in mine),
               "batched": any(r.get("batched") for r in mine), "batches": sum(r.get("batches") or 0 for r in mine),
               "skip_trace_credits": None, "trestle_queries": None, "ai_usage": None})
        sys.exit(0)
    if a[:1] == ["--publish"]:  # --publish RUN_ID: the run's lines to the run-logs branch
        ok, msg = publish(read(run_id=a[1]))
        print(("" if ok else "WARN: ") + msg)
        sys.exit(0)
    if a[:1] == ["--alert"]:  # --alert RUN_ID OUT: write OUT.txt/.html and print ALERT_SUBJECT when a step failed
        got = alert(read(run_id=a[1]))
        if got:
            subject, text, html = got
            os.makedirs(os.path.dirname(a[2]) or ".", exist_ok=True)
            open(a[2] + ".txt", "w").write(text)
            open(a[2] + ".html", "w").write(html)
            print(f"ALERT_SUBJECT: {subject}")
            print(f"ALERT_FILES: {a[2]}.txt {a[2]}.html")
        sys.exit(0)
    rows = read(a[a.index("--path") + 1] if "--path" in a else None, a[a.index("--run-id") + 1] if "--run-id" in a else None)
    for r in rows:
        print(json.dumps(r))
