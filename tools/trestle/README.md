# Trestle phone check (Windows, operator's machine)

Checks the phones of an Excel file against Trestle Phone Intel 3.0 from the operator's own computer and
writes a CSV with the original columns plus Trestle's answer. Used with the two Harris scripts:

1. `src/harris/trestle_export.py` (read only) writes the Excel: one row per phone, sheet `Telefonos`.
2. On Windows, double-click one of these (all files of this folder in the same folder on the PC):
   - `Validacion_tresle_normal.bat` - Phone Validation only ($0.015 per number).
   - `Validacion_tresle_con_litigante.bat` - adds the Litigator Check add-on ($0.005 more per number) and
     asks how many leads to process (Enter = 1, 0 = all). The CSV gets `trestle_is_litigator_risk`.
   - `cambiar_clave_trestle.bat` - forgets the saved key; the next run asks for the new one.
3. `src/harris/trestle_import.py RESULTS.csv` shows the plan; with the operator's OK,
   `SIFT_MODE=live ... --commit --limit 1`, then the rest, applies it to DataSift.

The key is never in these files: `trestle_excel.ps1` reads the Windows user variable `TRESTLE_APIKEY` and,
if it is missing, asks for it once (hidden) and saves it there. A run that ends without errors moves the
Excel, the CSV and the JSONL (full answers) to `completed/<date>_<name>/`; with errors they stay put and a
re-run does not query (or pay for) numbers that already answered. Results are gitignored here.
