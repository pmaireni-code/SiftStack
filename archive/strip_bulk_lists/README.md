# strip_bulk_lists (archived 2026-11-11)

Removed DataFlik bulk lists from first-to-market records, in bulk, on another DataSift account (Knox).
Archived by the operator's decision: it **refuses to run** (`main()` prints REFUSED and returns 2 before
building a client or calling anything). Kept only for the API contract notes in its docstring
(`remove-lists` takes one list TITLE as a string; an array or a uuid returns 201 and does nothing).
