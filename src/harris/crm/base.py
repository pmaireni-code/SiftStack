"""The contract every CRM connector implements. Nothing outside src/harris/crm/ talks to a CRM.

A LEAD is one row of the neutral leads CSV (harris_to_datasift.py output). Column names:

  identity    Property Street Address, Property City, Property State, Property ZIP Code, Parcel ID
  owner       Owner First Name, Owner Last Name (company owners: last name only),
              Mailing Street Address, Mailing City, Mailing State, Mailing ZIP Code
  grouping    Lists (one list title), Tags (comma separated), Notes (free text, one per notice)
  property    Estimated Value, Year Built, Living SqFt, County, Notice Type, Owner Deceased, Date Added
  loan        Original Loan Amount, Loan Date, Lender, Trustee Firm, Auction Platform
  sale        Foreclosure Date, Auction Date, Contract Deadline, Foreclosure Status, Starting Bid

Dates are MM/DD/YYYY, money is plain digits, Foreclosure Status is one of
Active / Urgent / Deadline Passed / Postponed / Auction Passed.

To support a new CRM: copy TEMPLATE.py to <name>.py, implement the three methods, register it in
__init__.py, and set HARRIS_CRM=<name>. The pipeline, the matching and the scheduled runs do not change.
"""

# The fields the daily run recomputes and writes back (Starting Bid, dates, status)
DAILY_FIELDS = ("Starting Bid", "Auction Date", "Contract Deadline", "Foreclosure Status")
# Tags the daily run may add; tags are only ever added, never removed
DAILY_TAGS = ("Bid Posted", "Postponed")


class CRM:
    name = "base"

    # ---- safety brakes (crm/guard.py), shared by every connector
    def plan(self, changes: list, commit: bool = True):
        """Declare the step's writes before making them: [{"id", "address", "set": {col: value},
        "before": {col: value}, "add_tags", "assign", ...}]. Mode, caps and destructive checks run over the
        whole plan first, so a blocked step writes nothing; a manual run over 10 leads becomes a dry run."""
        from . import guard
        return guard.current().plan(changes, commit)

    def plan_rows(self, rows: list, commit: bool = True):
        """plan() for an upload: one change per row, keyed by its address."""
        return self.plan([self.row_change(r) for r in rows], commit)

    @staticmethod
    def row_change(row: dict) -> dict:
        key = " ".join(str(row.get(k) or "").strip() for k in ("Property Street Address", "Property ZIP Code")).strip()
        return {"address": key, "create": True,
                "set": {k: v for k, v in row.items() if (v or "").strip() and k not in ("Notes", "Tags", "Note Key")},
                "add_tags": [t.strip() for t in (row.get("Tags") or "").split(",") if t.strip()]}

    def upsert_lead(self, row: dict, commit: bool) -> dict:
        """Create the lead, or attach to the existing record at the same property address.

        Must be idempotent: re-sending the same row never duplicates the record, its list, its tags
        or its note. Should read the record back and report what did not land.
        Returns {"id": str, "created": bool, "problems": [str]}. With commit=False, write nothing and
        return {"id": "", "created": False, "problems": [], "preview": str}.
        """
        raise NotImplementedError

    def find_lead(self, row: dict) -> str:
        """Id of the record already at the row's property address, or "". Read-only. Optional: the
        probate dry run uses it to say which leads would land on an existing record."""
        return ""

    def read_leads(self, list_name: str, with_facts: bool = False) -> list:
        """Every lead on the list, as dicts carrying "id" plus at least: Property Street Address,
        Property ZIP Code, Foreclosure Date, Auction Date, Contract Deadline, Foreclosure Status,
        Starting Bid, Tags, Deal Grade, Deal Notes, ZIP Investor Activity. with_facts adds what the
        Deal Grade needs: Equity Percent, Estimated Value, Structure Type, Living SqFt, Bedrooms,
        Bathrooms, Year Built, Latitude, Longitude, Original Loan Amount, Loan Date, Property Temperature
        (the CRM's own hot/warm/cold marker, 0-5). Read-only."""
        raise NotImplementedError

    def list_assignments(self, list_name: str, fields: tuple = ()) -> list:
        """Every lead on the list as {"id", "Property Street Address", "Assigned To" (user's full name or ""),
        <each custom field in fields>}. Read-only. Optional: used by probate_assign.py."""
        raise NotImplementedError

    def assign_lead(self, lead_id: str, user_name: str) -> bool:
        """Assign the lead to the CRM user with this full name and read it back. Returns True when the
        record now reads as assigned to that user. Optional: used by probate_assign.py."""
        raise NotImplementedError

    def update_lead(self, lead_id: str, values: dict, add_tags: list) -> list:
        """Write the given lead columns (an empty value clears the field) and add tags. A
        "Property Temperature" value sets the CRM's own hot/warm/cold marker. Returns problems."""
        raise NotImplementedError
