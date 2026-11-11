"""SiftMap (map.reisift.io) property SEARCH for the Harris senior sweep. Read only.

The search client the senior sweep used to borrow from siftmap_pull.py, which is archived with the Knox
modules (archive/knox/). Only POST /properties/search/ is allowed here: anything else (add-properties-by-
query spends the record allowance) is refused before the network.
"""
import json, time, urllib.error, urllib.request

MAP = "https://map.reisift.io"
SEARCH = "/properties/search/"
# The whole-world viewport: searches intersect addresses with it, so a county search hands in a polygon that
# contains the county.
WORLD = [{"lon": -17.89461189115002, "lat": 72.08452694723852},
         {"lon": -17.89461189115002, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": 72.08452694723852}]
COUNTIES = {"48201": {"name": "Harris", "state": "TX"}}


def county_address(fips, rich=False):
    c = COUNTIES[fips]
    title = f"{c['name']} County, {c['state']}"
    base = {"state": c["state"], "title": title, "value": title, "county": c["name"], "searchType": "county",
            "counties": [{"fips": fips, "county_name": c["name"]}]}
    # the search endpoint also needs the thin pair, and `search` must be the BARE county name
    return base if rich else dict(base, search=c["name"], type="county")


class Map:
    def __init__(self, api):
        self.api = api

    def _post(self, path, body, timeout=240):
        if path != SEARCH:
            raise PermissionError(f"siftmap_read allows only {SEARCH} (read only); refused {path}")
        data = json.dumps(body).encode()
        for attempt in range(5):
            req = urllib.request.Request(MAP + path, data=data, method="POST", headers={
                "Authorization": "Bearer " + self.api.token, "Content-Type": "application/json",
                "accept": "application/json", "origin": "https://beta.reisift.io", "referer": "https://beta.reisift.io/"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    t = r.read().decode()
                    return json.loads(t) if t.strip().startswith(("{", "[")) else t
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt < 4:
                    self.api._mint()
                    continue
                if e.code in (429, 500, 502, 504) and attempt < 4:
                    time.sleep(4 * (attempt + 1))
                    continue
                raise RuntimeError(f"HTTP {e.code} on {path}: {e.read().decode()[:220]}")
        raise RuntimeError("gave up on " + path)
