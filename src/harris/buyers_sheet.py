"""The investor-buyer base in Google Sheets: tabs Compradores, Compras and Reportes. Used by buyer_affinity.py.

Access (operator's side, once):
  1. create the Google Sheet and copy its id (the long string in the URL between /d/ and /edit);
  2. share it, as Editor, with the service account's email (client_email in its JSON key);
  3. set two environment variables in the cloud environment, never in the repo:
       BUYERS_SHEET_ID              the sheet id
       GOOGLE_SERVICE_ACCOUNT_KEY   the service account JSON key (raw JSON, base64 of it, or a file path)

Rules (operator, 2026-11-20):
  - no duplicates: a row is found again by its key (a buyer also by any of its company keys) and updated in place;
  - rows are NEVER deleted and nothing is cleared;
  - the program writes ONLY the columns it owns. Every other column (Status, Last contact, Notes, or any column the
    operator adds) is never written; the run reads those cells before and after and stops if one changed;
  - values are written RAW (a name starting with "=" stays text, never a formula).
"""
import base64, json, os, time, urllib.error, urllib.parse, urllib.request

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
USER_COLUMNS = ["Status", "Last contact", "Notes"]  # added once to Compradores, then the operator's


def load_key(raw=None):
    """The service-account key from GOOGLE_SERVICE_ACCOUNT_KEY: raw JSON, base64 JSON or a path."""
    raw = (raw if raw is not None else os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY", "")).strip()
    if not raw:
        return None
    if raw.startswith("{"):
        return json.loads(raw)
    if os.path.isfile(raw):
        with open(raw) as f:
            return json.load(f)
    return json.loads(base64.b64decode(raw).decode())


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class SheetsClient:
    def __init__(self, sheet_id, key):
        self.sheet_id, self.key, self._tok, self._exp = sheet_id, key, None, 0

    def _token(self):
        if self._tok and time.time() < self._exp - 120:
            return self._tok
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        now = int(time.time())
        aud = self.key.get("token_uri") or "https://oauth2.googleapis.com/token"
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        body = _b64(json.dumps({"iss": self.key["client_email"], "scope": SCOPE, "aud": aud, "iat": now,
                                "exp": now + 3600}).encode())
        pk = serialization.load_pem_private_key(self.key["private_key"].encode(), password=None)
        sig = _b64(pk.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()))
        data = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                       "assertion": f"{head}.{body}.{sig}"}).encode()
        with urllib.request.urlopen(urllib.request.Request(aud, data=data), timeout=60) as r:
            tok = json.load(r)
        self._tok, self._exp = tok["access_token"], now + int(tok.get("expires_in", 3600))
        return self._tok

    def _call(self, path, body=None, method=None):
        for attempt in range(5):
            req = urllib.request.Request(API + "/" + self.sheet_id + path, method=method or ("POST" if body is not None else "GET"),
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": "Bearer " + self._token(), "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 4:
                    time.sleep(5 * (attempt + 1))
                    continue
                msg = e.read().decode()[:300]
                if e.code in (403, 404):
                    raise SystemExit(f"Google Sheets refused ({e.code}): share the sheet as Editor with "
                                     f"{self.key.get('client_email')} and check BUYERS_SHEET_ID. {msg}")
                raise RuntimeError(f"Sheets HTTP {e.code}: {msg}")
        raise RuntimeError("Sheets: gave up")

    def tabs(self):
        r = self._call("?fields=sheets.properties.title")
        return [s["properties"]["title"] for s in r.get("sheets") or []]

    def add_tab(self, title):
        self._call(":batchUpdate", {"requests": [{"addSheet": {"properties": {"title": title}}}]})

    def read(self, tab):
        r = self._call("/values/" + urllib.parse.quote(f"'{tab}'") + "?valueRenderOption=FORMATTED_VALUE")
        return r.get("values") or []

    def update(self, cells):
        """cells: [(tab, row1, col1, value)] -> one batch write, RAW."""
        if not cells:
            return
        data = [{"range": f"'{t}'!{col_letter(c)}{r}", "values": [[v]]} for t, r, c, v in cells]
        for i in range(0, len(data), 500):
            self._call("/values:batchUpdate", {"valueInputOption": "RAW", "data": data[i:i + 500]})

    def append(self, tab, rows):
        if rows:
            self._call("/values/" + urllib.parse.quote(f"'{tab}'!A1") + ":append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
                       {"values": rows})


def col_letter(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def cell(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def same(a, b):
    a, b = str(cell(a)).strip(), str(cell(b)).strip()
    if a == b:
        return True
    try:
        return abs(float(a.replace(",", "").replace("$", "")) - float(b.replace(",", "").replace("$", ""))) < 1e-6
    except ValueError:
        return False


def sync(client, tab, owned, rows, keys_of, user_columns=()):
    """Upsert `rows` (dicts over `owned` columns) into `tab`. keys_of(dict) -> set of match keys.
    -> {"added", "updated", "unchanged", "conflicts", "user_cells"}; raises if an operator cell would change."""
    if tab not in client.tabs():
        client.add_tab(tab)
    values = client.read(tab)
    header = list(values[0]) if values else []
    cells = []
    for name in list(owned) + list(user_columns):  # missing columns go at the right end, header cell only
        if name not in header:
            header.append(name)
            cells.append((tab, 1, len(header), name))
    col = {h: i for i, h in enumerate(header)}
    index, conflicts = {}, []
    for rn, row in enumerate(values[1:], 2):
        d = {h: (row[i] if i < len(row) else "") for h, i in col.items()}
        for k in keys_of(d):
            index.setdefault(k, rn)
    user = [h for h in header if h not in owned]
    before = {rn: tuple((values[rn - 1][col[h]] if col[h] < len(values[rn - 1]) else "") for h in user)
              for rn in range(2, len(values) + 1)}
    added, updated, unchanged, seen = [], 0, 0, set()
    for d in rows:
        hits = sorted({index[k] for k in keys_of(d) if k in index})
        if -1 in hits:  # the same row twice in this batch
            continue
        if len(hits) > 1:
            conflicts.append((d, hits))
        if hits:
            rn = hits[0]
            if rn in seen:
                continue
            seen.add(rn)
            row = values[rn - 1]
            diff = [(tab, rn, col[h] + 1, cell(d.get(h))) for h in owned
                    if not same(row[col[h]] if col[h] < len(row) else "", d.get(h))]
            cells += diff
            updated += bool(diff)
            unchanged += not diff
        else:
            added.append([cell(d.get(h)) if h in owned else "" for h in header])
            for k in keys_of(d):
                index[k] = -1
    client.update(cells)
    client.append(tab, added)
    after = client.read(tab)
    col2 = {h: i for i, h in enumerate(after[0])}
    for rn, vals in before.items():  # the operator's cells, read back
        now = tuple((after[rn - 1][col2[h]] if col2[h] < len(after[rn - 1]) else "") for h in user)
        if now != vals:
            raise SystemExit(f"STOP: an operator cell changed in {tab} row {rn}: {vals} -> {now}")
    if len(after) < len(values) + len(added):
        raise SystemExit(f"STOP: {tab} has {len(after)} rows, expected at least {len(values) + len(added)}")
    return {"added": len(added), "updated": updated, "unchanged": unchanged, "conflicts": conflicts,
            "user_cells": sum(len(v) for v in before.values())}
