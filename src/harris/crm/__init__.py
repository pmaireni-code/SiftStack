"""CRM connectors for the Harris pipeline. HARRIS_CRM picks one (default: datasift)."""
import os

from .base import CRM, DAILY_FIELDS, DAILY_TAGS  # noqa: F401

CONNECTORS = {"datasift": ("datasift", "DataSiftCRM")}


def get_crm(name=None) -> CRM:
    name = (name or os.environ.get("HARRIS_CRM") or "datasift").lower()
    if name not in CONNECTORS:
        raise SystemExit(f"unknown CRM {name!r}; known: {', '.join(CONNECTORS)} (see crm/TEMPLATE.py)")
    module, cls = CONNECTORS[name]
    return getattr(__import__(f"{__name__}.{module}", fromlist=[cls]), cls)()
