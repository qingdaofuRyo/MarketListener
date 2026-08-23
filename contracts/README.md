# Contracts

Version 2 uses JSON Schema Draft 2020-12. These files are the sole public
format definitions used by the desktop producer and Android consumer.

- `canonical-instrument.schema.json`: canonical identity and optional source symbols
- `bar.schema.json`: normalized OHLCV bar and source provenance
- `provider-run-result.schema.json`: a real Provider probe/run outcome with a
  source description, parameterized capability registration, and independent
  per-capability status/evidence/limits/error. It intentionally contains no
  source-wide availability status.
- `provider-run-result-v1.schema.json`: immutable historical v1 input schema;
  `migrate_v1_provider_run_result()` validates against it before migration and
  rejects every unknown root, error, or capability field.
- `quality-issue.schema.json`: quarantine and data-quality evidence
- `market-package-manifest.schema.json`: market package inventory
- `strategy-package-manifest.schema.json`: strategy package metadata
- `strategy-result.schema.json`: observational strategy output and risk tags
- `industry-graph-entity.schema.json`: graph entity (company/product/industry/region...)
- `industry-graph-relationship.schema.json`: typed, directed relationship with
  confidence and human-confirmation status
- `industry-graph-evidence.schema.json`: traceable evidence with source type,
  page/cell/DOM/line location, parser version and extraction time

Cross-field OHLC bounds and bar timestamp ordering are enforced by matching
desktop and Android validation code because JSON Schema cannot compare sibling
numeric or timestamp values directly. Shared legal and illegal examples live
under `tests/fixtures/contracts`.

Day 0 has stopped without final acceptance. Provider v1 probe reports are
migrated by the desktop contract code to this v2 shape while preserving legacy
run status in explicit migration metadata; unknown legacy capability names are
mapped to an explicit `other` operation rather than dropped. Other version 1
contracts remain historical baselines. Any future incompatible change requires
the ADR process described in `../docs/ADR.md`, not a rewrite of historical D0 evidence.

For v1 migration only, `source.legacy_name` preserves the provider name exactly
as received. Each migrated capability registration similarly carries
`legacy_name` and `legacy_occurrence`; both fields appear together, so identical
legacy names remain distinguishable without depending on array order. These
fields are migration evidence, not v2 capability routing inputs.

The fields are valid only as one coherent migration shape: an ordinary v2
report has neither migration metadata nor any `legacy_*` fields and cannot use
the reserved `legacy-` or `migration-root-error-` identifiers. A v1 migration
must carry the source legacy name; each migrated capability uses `legacy-*`
with its paired original name/occurrence, while an optional synthetic root
error uses the isolated `migration-root-error-*` form. This prevents normal
v2 reports from claiming an unauditable legacy identity.
