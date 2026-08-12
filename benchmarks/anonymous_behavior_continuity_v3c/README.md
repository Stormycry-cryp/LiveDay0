# Anonymous behavior continuity v3c

Governing contract:
`docs/benchmarks/v3c-anonymous-behavior-continuity-contract.md`.

Current state: `M1_NO_GO_PREFLIGHT`. This directory contains the two allowed
aggregate preflight outputs and no dataset, cases, manifest, RED result,
candidate result, synthetic layer, or heldout harness. Those artifacts remain
stage-gated and were not created.

v3c starts from v3b closeout commit
`0ee63e50e5896cfbdcc652a38854586db4fed607` but never reads v3b heldout
case-level content or invokes its consumed harness. It may read only v3b public
entities/observations to construct a conservative cross-version exclusion set
and cite the published aggregate decision.

The M1 preflight command was added with tests before execution. It accepted
only temporary official snapshots, verified fixed SHA/size/ZIP and the
pre-source official metadata/page DOI/license evidence, and
DOI/license identity before parsing, refused frozen artifacts, and emitted
aggregate `preflight_result.json` plus `preflight_report.md` with no source ID
or raw observation. It returned exit 2 because entity, source, component,
case-eligibility, and privacy gates did not all pass.

The machine-readable command, input identities, aggregate output fields,
forbidden output boundary, overwrite behavior, and exit codes are frozen in
`preflight_contract.json` before implementation or source inspection.
