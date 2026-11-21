"""Business-owned foreclosure leads: find the person behind the company (free, Texas Comptroller) and leave it in
the lead's notes. Dry run unless --commit.

    python3 entity_owner.py [--commit] [--limit N] [--allow-destructive]

Operator's rule (2026-11-18). DataSift's skip trace only traces people, so a lead owned by an LLC, a company, a
trust or a bank never gets a phone from it (it sits in the queue forever). For every lead on "Harris -
Foreclosure" that is owned by an entity, has no phone, and is in the skip-trace queue or qualifies for it
(skiptrace_queue's rule: Deal Grade Hot, <= $400k; operator 2026-11-21):
  - look the entity up in the Texas Comptroller's franchise-tax search (public, free): its managers / members /
    officers with title and address, and its registered agent;
  - add ONE note to the lead with what was found and where from (never twice: the note carries a marker);
  - tag "Entity Owner" when the name matches exactly one Texas entity AND one of its addresses shares the lead's
    mailing ZIP; tag "Entity Owner - Verify" when only the name matches, several entities match, nothing
    matches, or the owner is a bank / lender / trust (a person checks those by hand);
  - remove "Skip Trace Batch 1" from the lead (the skip trace would never process it).
No phone is looked up (operator: not for now). Nothing else on the lead changes; every write is read back.
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm import get_crm  # noqa: E402
import skiptrace_queue as Q  # noqa: E402

API = "https://comptroller.texas.gov/data-search/franchise-tax"
TAG_OK, TAG_VERIFY = "Entity Owner", "Entity Owner - Verify"
MARKER = "Entity lookup (Texas Comptroller"
LENDER = re.compile(r"\b(BANK|NATIONAL ASSOCIATION|MORTGAGE|FUNDING|SAVINGS|CREDIT UNION|LOAN)\b", re.I)
TRUST = re.compile(r"\b(TRUST|TRUSTEE|ESTATE OF)\b", re.I)
SUFFIX = re.compile(r"\b(L\s*L\s*C|INC|CORP|CORPORATION|CO|LTD|L\s*P|LLP|PLLC|COMPANY)\b\.?", re.I)
N = Q.N


owner_name, is_entity = Q.owner_name, Q.is_entity


def norm(name):
    """'Rei Trading Post Llc' and 'REI TRADING POST, LLC' -> 'REI TRADING POST'."""
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper().replace(".", ""))
    return " ".join(SUFFIX.sub(" ", s).split())


def kind_of(name):
    """The legal form in a name: LLC / INC / LP / LTD / ... ('' when none)."""
    s = re.sub(r"[^A-Z ]", " ", (name or "").upper().replace(".", ""))
    s = re.sub(r"\bL L C\b", "LLC", re.sub(r"\bL P\b", "LP", s))
    for k, words in (("LLC", ("LLC",)), ("INC", ("INC", "CORP", "CORPORATION")), ("LP", ("LP",)), ("LTD", ("LTD",)),
                     ("LLP", ("LLP",)), ("PLLC", ("PLLC",))):
        if any(re.search(rf"\b{w}\b", s) for w in words):
            return k
    return ""


def zip5(z):
    m = re.match(r"\d{5}", str(z or ""))
    return m.group(0) if m else ""


def _get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def lookup(name, get=_get, lead_zip=""):
    """-> (kind, record or None, candidates). kind: one | several | none. Several exact names are narrowed by the
    owner's legal form, then by the lead's mailing ZIP (only when exactly one candidate shares it)."""
    q = norm(name)
    if not q:
        return "none", None, []
    hits = (get(f"{API}?name={urllib.parse.quote(q)}") or {}).get("data") or []
    exact = [h for h in hits if norm(h.get("name")) == q]
    if len(exact) > 1 and kind_of(name):  # "X LLC" and "X INC." both exist: the owner's own suffix decides
        same = [h for h in exact if kind_of(h.get("name")) == kind_of(name)]
        exact = same or exact
    if len(exact) > 1 and lead_zip and len(exact) <= 6:  # same name: the one with an address in the lead's ZIP
        recs = []
        for h in exact:
            time.sleep(1)
            recs.append((h, (get(f"{API}/{h['taxpayerId']}") or {}).get("data") or {}))
        by_zip = [(h, r) for h, r in recs if lead_zip in entity_zips(r)]
        if len(by_zip) == 1:
            return "one", by_zip[0][1], [by_zip[0][0].get("name")]
    if len(exact) != 1:
        return ("several" if exact else "none"), None, [h.get("name") for h in (exact or hits)[:5]]
    time.sleep(1)
    return "one", (get(f"{API}/{exact[0]['taxpayerId']}") or {}).get("data") or {}, [exact[0].get("name")]


def entity_zips(rec):
    """Every ZIP the entity is known at: mailing, registered office, its managers / members / officers."""
    return ({zip5(rec.get("mailingAddressZip")), zip5(rec.get("registeredOfficeAddressZip"))}
            | {zip5(o.get("AD_ZP")) for o in rec.get("officerInfo") or []}) - {""}


def decide(d, kind, rec, cands, today=None):
    """-> (tag, note text)."""
    today = today or date.today()
    name = owner_name(d)
    head = f"{MARKER}, {today.isoformat()}): owner of record {name}."
    if LENDER.search(name):
        return TAG_VERIFY, head + " Bank or lender: not a seller to call (REO / the lender itself). Not looked up."
    if kind != "one":
        what = {"several": f"several Texas entities share the name: {', '.join(cands)}",
                "none": "no Texas entity with that exact name" + (f" (similar: {', '.join(cands)})" if cands else "")}[kind]
        extra = " Trust: find the trustee in the Harris County Clerk deed records." if TRUST.search(name) else ""
        return TAG_VERIFY, f"{head} {what}.{extra} Verify by hand."
    offs = rec.get("officerInfo") or []
    people = "; ".join(f"{(o.get('AGNT_NM') or '').title()} ({(o.get('AGNT_TITL_TX') or '').title()}), "
                       + ", ".join(x for x in (o.get("AD_STR_POB_TX"), o.get("CITY_NM"), o.get("ST_CD"), o.get("AD_ZP")) if x)
                       for o in offs) or "none listed"
    zips = entity_zips(rec)
    lead_zip = zip5(((d.get("owner") or {}).get("address") or {}).get("postal_code"))
    confirmed = bool(lead_zip) and lead_zip in zips
    note = (f"{head} Texas entity {rec.get('name')} ({rec.get('rightToTransactTX') or 'status unknown'}), "
            f"mailing {rec.get('mailingAddressStreet')}, {rec.get('mailingAddressCity')} {zip5(rec.get('mailingAddressZip'))}. "
            f"People: {people}. Registered agent: {rec.get('registeredAgentName') or '-'} (often a lawyer or agent service, "
            f"not the owner). Match: {'name and mailing ZIP' if confirmed else 'name only, the ZIP differs: verify'}.")
    return (TAG_OK if confirmed and offs else TAG_VERIFY), note[:1900]


def candidates(crm):
    """Entity-owned foreclosure leads with no phone that are in the skip-trace queue or qualify for it."""
    out = []
    for r in crm.list_assignments(Q.LIST, ("Foreclosure Status", "Deal Grade")):
        if Q.foreclosure_status(crm, r) in Q.DEAD_FORECLOSURE:  # dead for this sale: not worth the lookup
            continue
        d = crm.api.call(f"/api/internal/property/{r['id']}/")
        tags = {N(t) for t in d.get("tags") or []}
        if not is_entity(d) or tags & {TAG_OK, TAG_VERIFY}:
            continue
        if Q.TAG in tags and not (d.get("owner") or {}).get("phones"):
            out.append((r["id"], d))
            continue
        why = Q.why_not(d, entities_too=True, grade=Q.field_label(crm, r, "Deal Grade"))
        if not why:
            out.append((r["id"], d))
    return out


def ensure_tags(crm, commit):
    have = {N(t) for t in (lambda x: x if isinstance(x, list) else x.get("results") or [])(crm.api.call("/api/internal/tag/?limit=999"))}
    for t in (TAG_OK, TAG_VERIFY):
        if t not in have and commit:
            crm.api.call("/api/internal/tag/", "POST", {"title": t})


def main(*flags):
    commit = "--commit" in flags
    limit = int(flags[flags.index("--limit") + 1]) if "--limit" in flags else 0
    crm = get_crm()
    todo = candidates(crm)[: limit or None]
    plans = []
    for lid, d in todo:
        name = owner_name(d)
        try:
            lz = zip5(((d.get("owner") or {}).get("address") or {}).get("postal_code"))
            kind, rec, cands = ("none", None, []) if LENDER.search(name) else lookup(name, lead_zip=lz)
        except Exception as e:  # noqa: BLE001 (one failed lookup never stops the others)
            print(f"  WARN {d['address'].get('street')}: lookup failed ({str(e)[:80]}), left for the next run")
            continue
        tag, note = decide(d, kind, rec, cands)
        plans.append((lid, d, tag, note))
        print(f"  {d['address'].get('street')} | {name} -> {tag} | {note[len(MARKER):][:170]}")
    print(f"ENTITY OWNER: {len(plans)} leads ({sum(p[2] == TAG_OK for p in plans)} {TAG_OK}, "
          f"{sum(p[2] == TAG_VERIFY for p in plans)} {TAG_VERIFY})")
    crm.plan([{"id": lid, "address": d["address"].get("street"), "add_tags": [tag],
               "remove_tags": [Q.TAG] if Q.TAG in {N(t) for t in d.get("tags") or []} else []}
              for lid, d, tag, _ in plans], commit)
    if not commit:
        return 0
    ensure_tags(crm, commit)
    ok = bad = 0
    for lid, d, tag, note in plans:
        before = [N(t) for t in d.get("tags") or []]
        if MARKER not in crm.notes_text(lid):
            crm.add_note(lid, note)
        crm.update_lead(lid, {}, [tag])
        if Q.TAG in before:
            crm.api.call(f"/api/internal/property/{lid}/remove-tags/", "POST", {"tags": [Q.TAG]})
        back = crm.api.call(f"/api/internal/property/{lid}/")
        tags = [N(t) for t in back.get("tags") or []]
        want = (set(before) - {Q.TAG}) | {tag}
        if set(tags) == want and MARKER in crm.notes_text(lid):
            ok += 1
        else:
            bad += 1
            print(f"  WARN {d['address'].get('street')}: tags {tags}")
            if ok == 0:
                print("STOPPED after the first lead: it did not read back.")
                return 1
    print(f"ENTITY OWNER: {ok} written and read back, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
