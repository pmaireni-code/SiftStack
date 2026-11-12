"""CRM connectors for the Harris pipeline. HARRIS_CRM picks one (default: datasift)."""
import os

from .base import CRM, DAILY_FIELDS, DAILY_TAGS  # noqa: F401

CONNECTORS = {"datasift": ("datasift", "DataSiftCRM")}


def get_crm(name=None) -> CRM:
    name = (name or os.environ.get("HARRIS_CRM") or "datasift").lower()
    if name not in CONNECTORS:
        raise SystemExit(f"unknown CRM {name!r}; known: {', '.join(CONNECTORS)} (see crm/TEMPLATE.py)")
    module, cls = CONNECTORS[name]
    from . import guard, runlog
    runlog.start(guard.current())  # every process that opens the CRM leaves one line in work/run_log.jsonl
    # src/account_lock.py: the Harris pipeline writes only to the Harris account. src/ may not be on the path
    # yet (a script run from src/harris imports crm before anything that adds it), so add it here
    import sys
    src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if src not in sys.path:
        sys.path.insert(0, src)
    import account_lock
    account_lock._on_refuse.append(lambda reason, code: guard.current().__dict__.update(
        status="blocked", error=reason, error_code=code))
    account_lock.declare("harris")
    return getattr(__import__(f"{__name__}.{module}", fromlist=[cls]), cls)()
