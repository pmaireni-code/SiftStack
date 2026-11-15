# temperature_cleanup.py (archived 2026-11-15, does not run)

One-time cleanup of DataSift's own temperature on the Harris pipeline leads. Until 2026-11-11 the grader and
the probate / senior uploads set it (Hot 5, Warm 3) before any contact. On 2026-11-13 this script put 122
leads back to 0 (Foreclosure 63, Probate 59), each re-checked right before its write and read back; 165 leads
kept their temperature (contact, a status, a hand-set value, or no record of who set it). The operator
confirmed the result on 2026-11-15.

The exception in the safety brakes that let this script write the value 0 was removed the same day
(src/harris/crm/guard.py), together with CRM.reset_temperature. No code can write DataSift's temperature
now; the script is kept for the record only and fails at its first write if run.
