# Planned pipeline

1. PATH: report-level extraction -> validated target dates -> per-patient earliest eligible PATH date + report ID.
2. NOTE: note-level extraction -> validated target dates -> per-patient earliest eligible NOTE date + note ID.
3. ICD: patient-owned C56.*, C57.00–02 and C48.1/2 -> valid ICD dates -> per-patient earliest date + native record ID. Family History excluded. C79.60–63 and D39.10–12 auxiliary only.
4. Merge on MRN. Select earliest eligible date and preserve all source-specific dates/IDs. Recurrence does not reset initial target diagnosis. Unusable/conflicting evidence requires review.

Final target CSV order:

```text
MRN
onset_date
source
pathology_positive_date
progress_note_positive_date
icd10_positive_date
in_pathology
in_progress_note
in_icd10
pathology_source_id
progress_note_source_id
icd10_source_id
```

`in_*` means present in the corresponding source, not positive. Exclusions retain source-specific reasons and IDs. No identified target cancer does not automatically establish an eligible negative control.

NOTE top-level diagnosis_date follows its selected category, including Borderline, Benign, Other and dated unresolved lesions. Only a supported target Malignant date contributes to the cohort onset. PATH/ICD/merge implementation is pending; the current runnable code is NOTE model evaluation only.
