"""Trestle phone scoring for the Harris call pipeline: the API call, the score -> dial tier rule, and the
per-lead record of what was scored (custom field "Trestle Phone Scores").

Pricing (Trestle self-service, checked 2026-11-12): Phone Validation $0.015 per query, Litigator Check
add-on +$0.005, so $0.020 per number with both. The API key is NOT in this code or in any variable: the
cloud environment's proxy injects it into requests to api.trestleiq.com, so the header carries a placeholder.

Operator's rules (2026-11-12):
  score 81-100 -> Dial First, 61-80 -> Dial Second, 41-60 -> Dial Third, 40 or less -> Drop;
  a litigator number -> Drop, and its lead gets "Litigator - Do Not Contact";
  a lead with no number scoring 61+ and none marked correct gets "Needs Re-Skip";
  every scored number is tagged "Trestle Scored", and its score and date are kept on the lead;
  a number not marked correct may be queried again after 180 days, a correct one never.
"""
import json, re, time, urllib.error, urllib.parse, urllib.request
from datetime import date, timedelta

ENDPOINT = "https://api.trestleiq.com/3.0/phone_intel"
COST_PHONE, COST_LITIGATOR = 0.015, 0.005
SCORED_TAG, LITIGATOR_PHONE_TAG = "Trestle Scored", "Litigator"
LITIGATOR_LEAD_TAG, RESKIP_TAG = "Litigator - Do Not Contact", "Needs Re-Skip"
FIELD = "Trestle Phone Scores"
RESCORE_DAYS = 180
CUTS = (("Dial First", 81), ("Dial Second", 61), ("Dial Third", 41))


def digits(number):
    d = re.sub(r"\D", "", str(number or ""))
    return d[-10:] if len(d) >= 10 else d


def tier_for(score):
    """Trestle activity score -> dial tier."""
    if score is None:
        return None
    for tier, low in CUTS:
        if score >= low:
            return tier
    return "Drop"


# ---- the per-lead record: "7135550123=85@2026-11-12; 7135550124=L72@2026-11-12; 7135550125=?@2026-11-12"
def parse_field(text):
    """-> {10-digit number: {"score": int|None, "litigator": bool, "date": date|None}}"""
    out = {}
    for part in (text or "").split(";"):
        m = re.match(r"\s*(\d{7,15})=(L?)(\d{1,3}|\?)@(\d{4}-\d{2}-\d{2})\s*$", part)
        if m:
            out[digits(m.group(1))] = {"score": None if m.group(3) == "?" else int(m.group(3)),
                                       "litigator": m.group(2) == "L", "date": date.fromisoformat(m.group(4))}
    return out


def format_field(scores):
    return "; ".join(f"{n}={'L' if s['litigator'] else ''}{'?' if s['score'] is None else s['score']}@{s['date'].isoformat()}"
                     for n, s in sorted(scores.items()))


def due(number_scores, correct, today=None):
    """Should this number be queried? Never scored -> yes; marked correct -> never again; else after 180 days."""
    if not number_scores:
        return True
    if correct:
        return False
    return (today or date.today()) - number_scores["date"] >= timedelta(days=RESCORE_DAYS)


# ---- the API
def query(number, opener=None, retries=3):
    """One Phone Validation query with the Litigator Check add-on. -> {"score", "litigator", "line_type",
    "is_valid", "error"}. The key is injected by the environment's proxy (header placeholder)."""
    url = ENDPOINT + "?" + urllib.parse.urlencode({"phone": digits(number), "add_ons": "litigator_checks"})
    req = urllib.request.Request(url, headers={"x-api-key": "proxy-injected", "Accept": "application/json"})
    opener = opener or urllib.request.urlopen
    for attempt in range(retries):
        try:
            with opener(req, timeout=20) as r:
                return parse_response(json.loads(r.read().decode()))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return {"score": None, "litigator": False, "error": f"HTTP {e.code}: {e.read().decode()[:160]}"}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return {"score": None, "litigator": False, "error": str(e)[:160]}
    return {"score": None, "litigator": False, "error": "gave up"}


def parse_response(data):
    lit = ((data.get("add_ons") or {}).get("litigator_checks") or {})
    flag = lit.get("phone.is_litigator_risk", lit.get("is_litigator_risk"))
    if isinstance(flag, dict):
        flag = flag.get("is_litigator_risk")
    score = data.get("activity_score")
    return {"score": int(score) if isinstance(score, (int, float)) else None, "litigator": bool(flag),
            "line_type": data.get("line_type"), "is_valid": data.get("is_valid"),
            "error": data.get("error") if isinstance(data.get("error"), str) else None}
