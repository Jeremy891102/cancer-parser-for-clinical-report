# Reusable cancer workflow

The first reusable release keeps the reviewed ovarian prompts, schemas, dates, and output keys compatible with the existing commands. Shared code owns transport, request scheduling, durable checkpoints, token accounting, validation, storage and CSV export. A study profile supplies target sites, subtype references, unresolved-origin rules, prompts, schemas and ICD code groups.

1. Extract shared infrastructure and validators. Keep old imports and commands compatible.
2. Parameterize NOTE and PATH patient aggregation through a profile. Prove ovarian outputs match the pre-refactor snapshot.
3. Provide one entry point for NOTE/PATH inference, ICD processing, patient aggregation and cross-source cohort merge.
4. Exercise the reusable workflow offline with synthetic ovarian and breast examples, including recurrence dates, unknown origin, missing dates, ICD fallback, invalid rows and resume behavior.
5. Document the exact input/output contract and how a new cancer profile is supplied.

The current local/NYU NOTE jobs keep their already-loaded execution code. Refactoring does not submit model requests or redeploy those jobs. The historical ovarian clinical decisions remain source-specific. A breast example configures the software boundary and requires its own reviewed prompts and subtype definitions before model execution.

The new workflow writes to a caller-selected new run directory. Existing production results and manual adjudications are preserved. Only eligible NOTE/PATH dates determine onset when available. ICD supplies onset only when neither clinical source has an eligible date. Partial dates keep their precision, and unresolved ordering remains reviewable.

## Verified first release

The five implementation steps are complete. Shared infrastructure now lives in cancer_pipeline, with compatibility imports for existing commands. All 80 tests pass. The pre-refactor comparison is identical for 20 reviewed NOTE outputs, 56 saved NOTE predictions and 4,435 saved PATH outputs. See the reusable workflow guide and the profile guide.

The generic entry point has offline and simulated-transport verification. Breast remains a study-definition draft. Existing manual ovarian PATH adjudications are retained by the legacy workflow and require an explicit migration before adopting generic raw aggregation for that production cohort.
