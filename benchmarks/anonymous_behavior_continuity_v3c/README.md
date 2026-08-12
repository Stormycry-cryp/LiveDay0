# Anonymous behavior continuity v3c

Governing contract:
`docs/benchmarks/v3c-anonymous-behavior-continuity-contract.md`.

Current state: `M0_CONTRACT_FROZEN / M1_SNAPSHOT_PENDING`. This directory intentionally contains no
dataset, cases, manifest, RED result, candidate result, or heldout harness yet.
Those artifacts are stage-gated and must receive new v3c identities.

v3c starts from v3b closeout commit
`0ee63e50e5896cfbdcc652a38854586db4fed607` but never reads v3b heldout
case-level content or invokes its consumed harness. It may read only v3b public
entities/observations to construct a conservative cross-version exclusion set
and cite the published aggregate decision.

The exact M1 preflight command will be added with tests before execution. It
must accept a temporary directory containing only the three official snapshots,
verify their fixed SHA/size before parsing, refuse to write frozen artifacts,
and emit aggregate `preflight_result.json` plus `preflight_report.md` with no
source ID or raw observation. Freeze remains fail-closed until the contract's
entity, source, component, case-eligibility, and privacy gates all pass.

The machine-readable command, input identities, aggregate output fields,
forbidden output boundary, overwrite behavior, and exit codes are frozen in
`preflight_contract.json` before implementation or source inspection.
