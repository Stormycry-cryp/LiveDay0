# v3c official-source preflight report

Decision: `NO_GO_PREFLIGHT`.
This report contains aggregate counts only; no source ID, raw observation, case, or heldout row is retained.

| Source | Base eligible | Cross-version excluded | v3c projected | Case eligible | Exact k eligible | Privacy capacity | Components |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| oulad | 2615 | 654 | 1961 | 1770 | 74 | 34 | 9 |
| online_retail_ii | 52 | 6 | 46 | 45 | 0 | 0 | 0 |
| electricity_load_diagrams | 370 | 307 | 63 | 53 | 0 | 0 | 0 |

Errors:
- real entity privacy capacity below 90
- real observation privacy capacity below 1800
- fewer than two source families have privacy capacity
- train: fewer than 30 entities
- test: fewer than 30 entities
- test: fewer than 6 components
- test: fewer than 2 sources
- heldout: fewer than 30 entities
- heldout: fewer than 6 components
- heldout: fewer than 2 sources
- MIA/re-identification feasibility gate failed

M1 writes no frozen entity, observation, case, label, split manifest, RED result, or heldout artifact.
Synthetic material contributes zero source/entity/observation/case capacity.
