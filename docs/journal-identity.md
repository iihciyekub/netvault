# Journal identity and display names

Schema migration 9 retains the provider's original venue name in
`pdfs.journal_raw_title`, stores fetched and checksum-validated serial identifiers
in `journal_issns_json`, and stores the metadata work type in `work_type`.
`container_title` is the uppercase canonical display name; `journal_key` remains
the existing normalized filter key.

The registry uses four tables:

- `journal_canonical_names`: unique canonical names.
- `journal_name_aliases`: reviewed source-name aliases, including their evidence.
- `journal_issn_identifiers`: unique ISSNs associated with a reviewed identity.
- `journal_doi_overrides`: reviewed corrections limited to individual DOIs.

Resolution first uses validated ISSNs. If supplied identifiers resolve to
different identities, or none resolve to the registry, it retains the formatted
source name rather than applying a name alias. When no identifiers are supplied,
it uses a reviewed DOI override, then an exact reviewed alias, then formatting.
Formatting preserves punctuation, qualifiers, years and words; it does not use
edit distance or plural removal to infer equivalence.

Crossref/DOI metadata refreshes retain original names and serial identifiers.
The ORM applies the registry to inserts and updates. PostgreSQL also enforces it
with `trg_netvault_journal_identity` for direct SQL and older application writers.
Filtered statistics resolve user/default list aliases to canonical filter keys.

Historical titles with different ISSNs remain separate. Annual proceedings retain
their full edition titles, and are not inferred to be journals. A fallback metadata
error for a single article in Operations Research is corrected by DOI, without
aliasing the different journal Operational Research.

## Reviewed backfill

Back up the database and source first. Build a reviewed JSON plan with canonical
names, exact aliases, validated ISSNs, source evidence, DOI samples and any
individual DOI overrides. Then use:

```sh
python scripts/normalize-journals.py --plan /path/to/reviewed-plan.json
python scripts/normalize-journals.py --plan /path/to/reviewed-plan.json --apply
```

The first command is a dry run. The second requires PostgreSQL, checks for
conflicting registry mappings and ISSN checksums, runs in one transaction,
preserves PDF counts, and verifies uppercase formatting before committing.
Migration 9 releases its brief schema lock before the larger metadata backfill,
so ordinary readers can continue during most of the migration.

The production 2026-10-12 plan and original database/source backups are stored in
`/root/iiaide/backups/netvault-journal-normalization-20261012/`. The plan audits
633 representative DOIs rather than re-fetching every article. Registry ISSNs
belong to the venue identity; unsampled articles are not assigned sample ISSNs.
Names without serial evidence are only formatted or individually reviewed.
