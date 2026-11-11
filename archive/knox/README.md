# Knox modules (archived 2026-11-11)

Eight modules written for the Knox DataSift account (Tennessee first-to-market, dispo, obituary). Archived by
the operator's decision: this repository writes only to the Harris account. Each one **refuses to run**
(prints REFUSED and exits 2 before reading arguments or calling anything), and src/account_lock.py has no
Knox fingerprint, so even imported code cannot write.

audit_tags_lists, dispo_flip_buyers, ftm_deep_prospect_push, obituary_crm_push, probate_audit_restore,
probate_pr_note, repair_pr_owners, siftmap_pull.

The Harris senior sweep used siftmap_pull's read-only SiftMap search; that part now lives in
src/harris/siftmap_read.py (search only).
