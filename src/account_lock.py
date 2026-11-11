"""Account lock: a module may WRITE only to the DataSift account it was written for. Reads are never locked.

Every module that writes declares its account once (`account_lock.declare("harris")`; the Harris CRM
connector does it in crm.get_crm()). datasift_api_upload.Api.call checks, before the FIRST write of the
process, that the connected account is that one, and refuses otherwise without writing:
  * the connected account is read from the session token's `account` claim;
  * the repo is public, so only a FINGERPRINT of each account id is kept (sha256 with a fixed prefix);
  * a write from a process that declared no account is refused (datasift_api_upload.py run on its own too);
  * an account with no fingerprint here cannot be written to at all. Only Harris has one (operator,
    2026-11-11); the Knox modules are archived in archive/knox/.
"""
import base64, hashlib, json, sys

FINGERPRINTS = {
    # sha256("siftstack-account:" + account uuid) of the Harris account (pmaireni), taken 2026-11-11
    "harris": "39885a4150d26a4ac6a06304a9411867872a86035e4b20645479a475eed2bbc7",
}

_state = {"declared": None, "verified_token": None}
_on_refuse = []  # callbacks(reason, code): the Harris run log marks the step as blocked


class AccountRefused(SystemExit):
    """Ends the process (code 3). A SystemExit, so a per-record `except Exception` cannot swallow it and carry
    on writing record after record."""

    def __init__(self, reason, code):
        super().__init__(3)
        self.reason, self.kind = reason, code  # not .code: that is SystemExit's exit status


def fingerprint(account_id):
    return hashlib.sha256(("siftstack-account:" + str(account_id)).encode()).hexdigest()


def declare(name):
    """The account this process writes to. Unknown names are refused at once."""
    if name not in FINGERPRINTS:
        _refuse(f"no DataSift account named {name!r} is registered for writing (known: {', '.join(FINGERPRINTS)})",
                "unknown_account")
    _state["declared"] = name
    _state["verified_token"] = None


def declared():
    return _state["declared"]


def token_account(token):
    """The `account` claim of a DataSift session token (a uuid, or an object carrying one)."""
    try:
        p = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:  # noqa: BLE001
        return None
    a = claims.get("account")
    return a.get("uuid") if isinstance(a, dict) else a


def is_write(method, headers=None):
    m = (method or "GET").upper()
    return m in ("PATCH", "PUT", "DELETE") or (m == "POST" and (headers or {}).get("x-http-method-override", "").upper() != "GET")


def check_write(token, method, path):
    """Raise AccountRefused unless this process declared an account and the token belongs to it. Checked once
    per token (a re-minted token is checked again)."""
    if _state["verified_token"] == token:
        return
    name = _state["declared"]
    if not name:
        _refuse(f"{method} {path}: this program declared no DataSift account, so it may not write "
                "(account_lock.declare)", "undeclared_account")
    acct = token_account(token)
    if not acct or fingerprint(acct) != FINGERPRINTS[name]:
        _refuse(f"{method} {path}: the connected DataSift account is not the {name!r} account this program was "
                "written for; nothing was written", "wrong_account")
    _state["verified_token"] = token


def _refuse(reason, code):
    for cb in _on_refuse:
        try:
            cb(reason, code)
        except Exception:  # noqa: BLE001
            pass
    print(f"REFUSED (account lock): {reason}", file=sys.stderr)
    raise AccountRefused(reason, code)


def reset():
    """Tests."""
    _state.update(declared=None, verified_token=None)
