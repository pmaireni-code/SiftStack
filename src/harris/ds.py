"""Minimal DataSift client: mints a JWT from DATASIFT_EMAIL / DATASIFT_PASSWORD. Never prints credentials."""
import json, os, urllib.request

B = "https://apiv2.reisift.io"


class DS:
    def __init__(self):
        self.tok = None
        self.tok = self.call("/api/token/", "POST", {"email": os.environ["DATASIFT_EMAIL"],
                                                     "password": os.environ["DATASIFT_PASSWORD"]})[1]["access"]

    def call(self, path, method="GET", body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if self.tok:
            h["Authorization"] = "Bearer " + self.tok
        req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw[:300].decode("utf-8", "ignore")


def rows(res):
    return res if isinstance(res, list) else (res.get("results") or res.get("data") or []) if isinstance(res, dict) else []
