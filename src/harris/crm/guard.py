"""Safety brakes for every write the Harris CRM connector makes. Policy only: no business rule lives here.

One Run per process (crm.get_crm() opens it). Every DataSift call goes through PatientApi.call, which asks
the Run first; every lead-level write in the connector announces itself with Run.before_write() so the
brakes see the lead, its address and what changes. A script that knows its whole batch up front declares
it with crm.plan(changes): the same checks run over the whole plan BEFORE the first write, so a blocked
step stops without writing anything.

Profiles (SIFT_PROFILE):
  manual     (default) live only when the plan touches <= DRY_THRESHOLD leads or SIFT_MODE=live; above
             that a dry run: nothing is written and a summary is printed. Over SIFT_MAX_RECORDS: stop.
  scheduled  set by the run_*.sh production scripts. Always live. Over SIFT_MAX_RECORDS: batches with a
             pause, logged; over SIFT_SCHEDULED_CEILING: stop without writing, logged as an error.
Destructive (both profiles): a DELETE always needs SIFT_ALLOW_DESTRUCTIVE=1 or --allow-destructive.
Clearing a field, removing a tag or unassigning on more than BULK_DESTRUCTIVE leads needs the same,
except a (task, field) listed in EXCEPTIONS, which carries its own reason test and cap.
"""
import os, re, sys, time
from collections import Counter, OrderedDict
from datetime import date

DRY_THRESHOLD = 10
BULK_DESTRUCTIVE = 10
TIER_TAGS = {"Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop"}
PROPERTY_UUID = re.compile(r"/api/internal/property/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")
UUID = re.compile(r"/(?:property|owner)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")


def _bid_clear_ok(values, today=None):
    """harris_bid_updater may clear Starting Bid only when the sale it belonged to is gone: the lead is
    Postponed or Auction Passed, or its auction date is already behind us."""
    from harris_status import parse_date
    if (values.get("Foreclosure Status") or "").strip() in ("Postponed", "Auction Passed"):
        return True
    d = parse_date(values.get("Auction Date"))
    return bool(d and d < (today or date.today()))


# (task, field) -> its own rule instead of the general >BULK_DESTRUCTIVE authorization.
# A clear that fails `ok` is blocked whatever the count and whatever the authorization.
EXCEPTIONS = {("harris_bid_updater", "Starting Bid"): {"max": 500, "ok": _bid_clear_ok,
                                                       "why": "auction date passed, or Postponed / Auction Passed"}}


class GuardStop(SystemExit):
    """Ends the step. A SystemExit, so the per-lead `except Exception` in the scripts cannot swallow it."""


class Blocked(GuardStop):
    def __init__(self, reason, status="blocked", code="blocked"):
        super().__init__(3)
        self.reason, self.status, self.code = reason, status, code


class DryRunDone(GuardStop):
    def __init__(self):
        super().__init__(0)


def _env_flag(name, argv_flag=None):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "live") or bool(argv_flag and argv_flag in sys.argv)


class Run:
    def __init__(self, task=None, out=print):
        self.task = task or os.environ.get("SIFT_TASK") or os.path.splitext(os.path.basename(sys.argv[0] or "python"))[0]
        self.profile = "scheduled" if os.environ.get("SIFT_PROFILE", "").lower() == "scheduled" else "manual"
        self.allow_destructive = _env_flag("SIFT_ALLOW_DESTRUCTIVE", "--allow-destructive")
        self.max_records = int(os.environ.get("SIFT_MAX_RECORDS") or 500)
        self.ceiling = int(os.environ.get("SIFT_SCHEDULED_CEILING") or 5000)
        self.pause = float(os.environ.get("SIFT_BATCH_PAUSE") or 30)
        want = os.environ.get("SIFT_MODE", "").lower()
        self.forced_mode = "live" if want == "live" or "--live" in sys.argv else "dry-run" if want in ("dry-run", "dry") else ""
        self.out = out
        self.mode = None          # decided by plan() or by the first write
        self.planned = None
        self.addresses, self.befores = {}, {}
        self.api_calls = 0
        self.read_ids, self.started, self.written = set(), [], set()
        self.rejected, self.destructive = set(), set()
        self.exc_counts = Counter()
        self.batched, self.batches = False, 0
        self.current = None
        self.intents = OrderedDict()   # lead id -> merged change, for the dry-run summary
        self.status, self.error, self.error_code = None, "", ""
        self.would_block = ""
        self.sleep = time.sleep

    # ---- classification
    def _destructive(self, ch):
        """[(kind, field)] the change destroys. A clear only counts when the field holds a value now (or
        when that is unknown): re-sending "" to an empty field changes nothing."""
        out = []
        before = ch.get("before")
        for f, v in (ch.get("set") or {}).items():
            if v in ("", None) and (before is None or f not in before or (before.get(f) or "") != ""):
                out.append(("clear", f))
        out += [("remove tag", t) for t in ch.get("remove_tags") or []]
        if ch.get("unassign"):
            out.append(("unassign", ""))
        old, new = ch.get("phone_tags_before"), ch.get("phone_tags_after")
        if new is not None:
            gone = set(old or []) - set(new)
            out += [("remove tag", t) for t in sorted(gone - TIER_TAGS)]
            if len(set(new) & TIER_TAGS) != 1 and set(old or []) & TIER_TAGS:
                out.append(("remove tag", "tier (phone left without exactly one tier)"))
        return out

    def _check(self, lead, ch, counts):
        """Raise Blocked if this change may not happen. counts: Counter carried across one pass."""
        for kind, field in self._destructive(ch):
            ex = EXCEPTIONS.get((self.task, field)) if kind == "clear" else None
            if ex:
                if not ex["ok"](ch.get("set") or {}):
                    raise Blocked(f"{field} clear on {self._addr(lead)} is not allowed: only when {ex['why']}",
                                  code=f"exception_reason:{field}")
                counts[("exc", field, lead)] = 1
                if len({k[2] for k in counts if k[:2] == ("exc", field)}) > ex["max"]:
                    raise Blocked(f"{self.task} may clear {field} on at most {ex['max']} leads per run", code=f"exception_cap:{field}")
                continue
            counts[("destructive", lead)] = 1
            if not self.allow_destructive and len([k for k in counts if k[0] == "destructive"]) > BULK_DESTRUCTIVE:
                raise Blocked(f"{kind} {field!r} on more than {BULK_DESTRUCTIVE} leads needs explicit authorization "
                              "(SIFT_ALLOW_DESTRUCTIVE=1 or --allow-destructive)", code=f"bulk_destructive:{kind}")

    def _addr(self, lead):
        return self.addresses.get(lead) or lead

    # ---- plan: the whole step, checked before any write
    def plan(self, changes, commit=True):
        """changes: [{"id", "address", "set": {field: value}, "before": {field: value}, "add_tags", "remove_tags",
        "phone_tags_before", "phone_tags_after"}] (only what applies). Decides the mode; raises Blocked
        (nothing written) or, in a dry run, prints the summary and raises DryRunDone."""
        if not commit:
            return self._preview(changes)
        leads = list(OrderedDict.fromkeys(c.get("id") or c.get("address") for c in changes))
        self.planned = len(leads)
        for c in changes:
            k = c.get("id") or c.get("address")
            if c.get("address"):
                self.addresses[k] = c["address"]
            if c.get("before") is not None:  # the write itself only knows the new values
                self.befores[k] = c["before"]
        try:
            if self.profile == "scheduled" and self.planned > self.ceiling:
                raise Blocked(f"plan of {self.planned:,} leads is over the scheduled ceiling of {self.ceiling:,}", "error", "ceiling")
            counts = Counter()
            for c in changes:
                self._check(c.get("id") or c.get("address"), c, counts)
            if self.profile == "manual":
                self.mode = self.forced_mode or ("live" if self.planned <= DRY_THRESHOLD else "dry-run")
                if self.mode == "live" and self.planned > self.max_records:
                    raise Blocked(f"plan of {self.planned:,} leads is over the per-run cap of {self.max_records:,} "
                                  "(raise it for this task only with SIFT_MAX_RECORDS)", code="cap")
            else:
                self.mode = "live"
                self.batched = self.planned > self.max_records
        except Blocked as b:
            self._block(b)
        self.out(f"[guard] {self.task}: {self.mode.upper()} ({self.profile}) - plans to touch {self.planned:,} leads"
                 + (f", in batches of {self.max_records}" if self.batched else ""))
        if self.mode == "dry-run":
            for c in changes:
                self._intend(c.get("id") or c.get("address"), c)
            self.out(self.summary())
            self.status = "dry-run"
            raise DryRunDone()
        return self

    def _preview(self, changes):
        """No --commit (run_*.sh --dry-run): nothing is written either way, but say what the brakes would
        decide for this plan in a real run with the same profile."""
        self.mode = self.mode or "dry-run (no --commit)"
        self.planned = len(set(c.get("id") or c.get("address") for c in changes))
        shadow = Run(task=self.task, out=lambda *_: None)
        shadow.forced_mode = shadow.forced_mode or "live"
        try:
            shadow.plan(changes, True)
            verdict = "would pass" + (f", in batches of {shadow.max_records}" if shadow.batched else "")
        except Blocked as b:
            self.would_block = b.reason
            verdict = f"WOULD BE BLOCKED: {b.reason}"
        except DryRunDone:
            verdict = "would pass"
        self.out(f"[guard] {self.task}: PREVIEW, nothing written ({self.profile}) - would touch {self.planned:,} leads | {verdict}")
        return self

    def _block(self, b):
        self.status, self.error, self.error_code = b.status, b.reason, b.code
        self.out(f"[guard] BLOCKED {self.task}: {b.reason}. Nothing written by this step.")
        raise b

    # ---- per write (connector)
    def before_write(self, lead, change, address=""):
        """Called by the connector before the calls that write one lead. Returns True when the write must be
        skipped (dry run). Backstop for scripts that did not plan: the same brakes, counted live."""
        if address:
            self.addresses.setdefault(lead, address)
        if "before" not in change and lead in self.befores:
            change = dict(change, before=self.befores[lead])
        if self.mode is None:  # no plan: the first DRY_THRESHOLD leads may go live in a manual run
            self.mode = "live" if self.profile == "scheduled" or self.forced_mode == "live" else \
                "dry-run" if self.forced_mode == "dry-run" else "live?"
        try:
            self._check(lead, change, self._live_counts())
        except Blocked as b:
            self._block(b)
        if lead not in self.started:
            self.started.append(lead)
            n = len(self.started)
            if self.mode == "live?" and n > DRY_THRESHOLD:
                self._block(Blocked(f"more than {DRY_THRESHOLD} leads without a plan: run it with SIFT_MODE=live "
                                    "(or dry-run first to see the summary)", code="no_plan"))
            if self.profile == "scheduled":
                if n > self.ceiling:
                    self._block(Blocked(f"over the scheduled ceiling of {self.ceiling:,} leads", "error", "ceiling"))
                if n > 1 and (n - 1) % self.max_records == 0:
                    self.batched = True
                    self.batches += 1
                    self.out(f"[guard] batch {self.batches} done ({n - 1:,} leads); pausing {self.pause:.0f}s")
                    self.sleep(self.pause)
            elif self.mode in ("live", "live?") and n > self.max_records:
                self._block(Blocked(f"over the per-run cap of {self.max_records:,} leads", code="cap"))
        if self._destructive(change):
            self.destructive.add(lead)
        self.current = lead
        if self.mode == "dry-run":
            self._intend(lead, change)
            return True
        return False

    def _live_counts(self):
        c = getattr(self, "_counts", None)
        if c is None:
            c = self._counts = Counter()
        return c

    def _intend(self, lead, ch):
        m = self.intents.setdefault(lead, {"set": {}, "add_tags": [], "remove_tags": [], "other": []})
        m["set"].update(ch.get("set") or {})
        m["add_tags"] += ch.get("add_tags") or []
        m["remove_tags"] += [t for k, t in self._destructive(ch) if k == "remove tag"]
        if ch.get("phone_tags_after") is not None:
            tier = sorted(set(ch["phone_tags_after"]) & TIER_TAGS)
            m["other"].append(("phone tier", ", ".join(tier) or "(no tier)"))
        if ch.get("assign"):
            m["other"].append(("assigned to", ch["assign"]))
        if ch.get("create"):
            m["other"].append(("record", "create or attach"))

    def summary(self):
        by_field = OrderedDict()
        for m in self.intents.values():
            for f, v in m["set"].items():
                by_field.setdefault(f, Counter())[v if v not in ("", None) else "(cleared)"] += 1
            for t in m["add_tags"]:
                by_field.setdefault("tag added", Counter())[t] += 1
            for t in m["remove_tags"]:
                by_field.setdefault("tag removed", Counter())[t] += 1
            for cat, v in m["other"]:
                by_field.setdefault(cat, Counter())[v] += 1
        lines = [f"DRY RUN - nothing written. {self.task} would change {len(self.intents):,} leads.",
                 "fields / tags: " + (", ".join(by_field) or "-")]
        for f, c in by_field.items():
            top = c.most_common(8)
            lines.append(f"  {f}: " + ", ".join(f"{v!s:.40} x{n}" for v, n in top) + (f" (+{len(c) - 8} more values)" if len(c) > 8 else ""))
        lines.append("examples:")
        for lead, m in list(self.intents.items())[:5]:
            what = "; ".join([f"{f}={v if v not in ('', None) else '(cleared)'}" for f, v in m["set"].items()]
                             + [f"+tag {t}" for t in m["add_tags"]] + [f"-tag {t}" for t in m["remove_tags"]]
                             + [f"{cat}={v}" for cat, v in m["other"]])
            lines.append(f"  {self._addr(lead)}: {what[:200]}")
        lines.append("To write it: SIFT_MODE=live (and --commit), after reviewing this summary.")
        return "\n".join(lines)

    # ---- every API call (PatientApi)
    def is_write(self, method, headers):
        m = method.upper()
        return m in ("PATCH", "PUT", "DELETE") or (m == "POST" and (headers or {}).get("x-http-method-override", "").upper() != "GET")

    def on_call(self, method, path, headers):
        """Before a call. Returns a synthetic response to skip a write, or None to send it."""
        self.api_calls += 1
        if not self.is_write(method, headers):
            return None
        if method.upper() == "DELETE" and not self.allow_destructive:
            self._block(Blocked(f"DELETE {path} needs explicit authorization (SIFT_ALLOW_DESTRUCTIVE=1 or --allow-destructive)",
                                code="delete"))
        if "/remove-tags/" in path or "/remove-lists/" in path:
            lead = (UUID.search(path) or [None, path])[1]
            try:
                self._check(lead, {"remove_tags": ["(via " + path.rsplit("/", 2)[-2] + ")"]}, self._live_counts())
            except Blocked as b:
                self._block(b)
            self.destructive.add(lead)
        if self.mode == "dry-run":
            return {"uuid": "DRY-RUN", "id": "DRY-RUN", "results": [], "dry_run": True}
        return None

    def after_call(self, method, path, headers, res):
        if self.is_write(method, headers):
            m = UUID.search(path)
            lead = self.current or (m.group(1) if m else None)
            if lead:
                self.written.add(lead)
            return
        # leads read = property records: a record detail, or the rows of a records search (custom-field
        # rows and owner records carry uuids too, and are not leads)
        m = PROPERTY_UUID.search(path)
        if m:
            self.read_ids.add(m.group(1))
        elif path.split("?")[0] == "/api/internal/property/" and isinstance(res, dict):
            for r in res.get("results") or res.get("data") or []:
                if isinstance(r, dict) and r.get("uuid"):
                    self.read_ids.add(r["uuid"])

    def reject(self, lead):
        self.rejected.add(lead)

    # ---- the log line
    def record(self):
        mode = self.mode or "read-only"
        if mode == "live?":
            mode = "live"
        return {"run_id": os.environ.get("SIFT_RUN_ID") or "", "task": self.task, "profile": self.profile, "mode": mode,
                "status": self.status or "ok", "error": self.error, "error_code": self.error_code,
                "would_block": self.would_block, "planned": self.planned,
                "leads_read": len(self.read_ids), "leads_written": len(self.written), "api_calls": self.api_calls,
                "rejected_by_verification": len(self.rejected), "destructive_leads": len(self.destructive),
                "batched": self.batched, "batches": self.batches + (1 if self.batched else 0),
                "skip_trace_credits": None, "trestle_queries": None, "ai_usage": None}


_RUN = None


def current():
    """The process's Run (created on first use)."""
    global _RUN
    if _RUN is None:
        _RUN = Run()
    return _RUN


def reset(run=None):
    """Tests: start a fresh Run."""
    global _RUN
    _RUN = run
    return _RUN
