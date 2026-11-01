"""Template for a new CRM connector. Copy to crm/<name>.py, fill in the three methods, then:

    1. add  "<name>": ("<name>", "<ClassName>")  to CONNECTORS in crm/__init__.py
    2. set  HARRIS_CRM=<name>  (plus the CRM's own credentials) in the cloud environment
    3. dry run:  python3 src/harris/harris_upload.py work/<batch>/leads.csv          (writes nothing)
       one lead: python3 src/harris/harris_upload.py work/<batch>/leads.csv --limit 1 --commit
       daily:    bash src/harris/run_daily.sh --dry-run

Before writing code, create in the CRM: a list/pipeline/tag to hold the leads ("Harris - Foreclosure")
and custom fields for what it lacks natively: Auction Date, Contract Deadline, Foreclosure Status
(Active, Urgent, Deadline Passed, Postponed, Auction Passed), Starting Bid, Original Loan Amount,
Loan Date, Lender, Trustee Firm. Read crm/base.py for the lead columns.

Example shapes, to be verified against the CRM's own docs before relying on them:
- Odoo: external API over XML-RPC (/xmlrpc/2/common to authenticate, /xmlrpc/2/object execute_kw
  for search_read / create / write) on models such as res.partner (owner) and crm.lead (the deal);
  custom fields are x_ fields added with Studio or a module.
- GoHighLevel-style white labels: REST API with contacts, custom fields, tags and opportunities,
  authenticated with a location API key.
Upsert by property address (search first, then create or write) so re-sends never duplicate.
"""
from .base import CRM


class NewCRM(CRM):
    name = "new"

    def __init__(self):
        # read credentials from the environment, never from code
        raise NotImplementedError("fill in the connection")

    def upsert_lead(self, row, commit):
        raise NotImplementedError

    def read_leads(self, list_name):
        raise NotImplementedError

    def update_lead(self, lead_id, values, add_tags):
        raise NotImplementedError
