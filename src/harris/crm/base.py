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

    def update_lead(self, lead_id: str, values: dict, add_tags: list) -> list:
        """Write the given lead columns (an empty value clears the field) and add tags. A
        "Property Temperature" value sets the CRM's own hot/warm/cold marker. Returns problems."""
        raise NotImplementedError
