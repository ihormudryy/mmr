# P2 Research Bundle Design

## Purpose

Task 8 creates a deterministic, read-only research evidence bundle for a sealed
strategy artifact.  The bundle lets an offline operator verify its public
evidence and reproduce the recorded experiment against the original research
DuckDB and vendor data.  It is not a source of trading authority: authority
continues to come only from a valid, unexpired, non-revoked signed eligibility
attestation.

## Scope and constraints

- The bundle is a reference bundle.  It does not embed a DuckDB snapshot or
  licensed vendor bars; reproduction receives those external inputs explicitly.
- Every bundled object is public evidence.  No private key, PEM, raw private
  bytes, environment dump, or secret-bearing configuration is permitted.
- The exported directory is deterministic, canonical, checksum-protected, and
  made read-only after a successful export.
- Verification and reproduction fail closed.  They must reject malformed,
  missing, extra, corrupted, symlinked, or traversal-style entries.
- Reproduction creates all transient material in a fresh temporary directory;
  it does not mutate the evidence database or the exported bundle.

## Architecture

`trader.research.bundle` owns bundle construction and validation.  Its
`ResearchBundle.export(path)` obtains the artifact family and associated public
evidence from the research repositories, writes a fixed set of canonical JSON
files, records a SHA-256 checksum for each file in a canonical manifest, and
changes the completed directory tree to read-only.  `ResearchBundle.verify(path)`
performs structural and checksum validation before deserializing the public
evidence and returning a `VerifiedResearchBundle` projection.

The bundle manifest binds the strategy artifact and experiment family, source
and configuration digests, dataset-manifest digest, ruleset and decision,
qualitative-review reference, signed attestation, attestation public-key ID,
and the file-checksum table.  Fixed file names and canonical serialization make
repeated exports of the same inputs byte-identical.

`scripts/reproduce_experiment.py` is an offline command.  It accepts the bundle
directory, a research DuckDB path, and an explicit vendor-data adapter/input. It
first calls `ResearchBundle.verify`, then loads the referenced records, rebuilds
the recorded folds and signal/order traces into a new temporary directory, and
compares the resulting canonical digests with the bundle’s expected evidence.
It emits a public JSON report containing the artifact identity, checked inputs,
trace digests, and PASS/FAIL result.

## Failure handling

Export refuses a non-empty destination, unsafe relative file name, unavailable
required evidence, or a value that cannot be canonicalized.  It writes to a
staging sibling and atomically publishes only after all checksums succeed.

Verification rejects absolute paths, `..` components, symlinks, non-regular
files, unexpected tree entries, duplicate names, checksum mismatches, and
private-key-shaped fields or file names.  It never treats a partial result as
verified.

Reproduction rejects a bundle whose verified dataset, source, configuration,
family, artifact, or recorded trace digest differs from the external source.
Vendor absence or data mismatch is a failed reproduction, not a best-effort
report.

## Testing and operator documentation

`tests/research/test_research_bundle.py` will use a compact deterministic
fixture and vendor adapter.  It will cover repeatable export, read-only output,
structural/path safety, checksums and corruption, private-material exclusion,
and two identical reproductions.  The fixture includes a failed trial, stressed
cost result, one holdout access, and deterministic replay trace.

The research runbook documents export, verification, reproduction input
requirements, vendor-data limitations, key handling, and incident response for
a failed reproduction.  README links to the runbook and states that software
success remains `CANDIDATE` until qualified real data and a signed paper
attestation exist.
