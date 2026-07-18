# P2 Research Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export and independently reproduce checksum-protected, public, signed research evidence for a sealed strategy artifact.

**Architecture:** `ResearchBundle` creates a deterministic reference directory from the existing research DuckDB repositories.  The bundle holds canonical public evidence and checksums, while `reproduce_experiment.py` reads the named original DuckDB and explicit vendor input, rebuilds the recorded evidence in a temporary directory, and requires exact digest equality.

**Tech Stack:** Python 3.12, DuckDB repositories, canonical JSON/SHA-256 helpers, existing backtester trace signatures, pytest.

## Global Constraints

- Do not embed or write private keys, PEMs, raw private bytes, environment dumps, or secret-bearing configuration.
- A bundle is a reference bundle: the research DuckDB and vendor data are external, explicit reproduction inputs.
- Exported evidence must be canonical, deterministic, checksum-protected, and read-only.
- Reject absolute paths, traversal, symlinks, non-regular files, missing/extra entries, malformed payloads, and checksum mismatches.
- Reproduction operates in a fresh temporary directory and must not mutate the bundle or research database.
- A verified/reproduced software bundle does not grant trading authority; only a valid Task 7 attestation can do that.

---

### Task 1: Define the public bundle format and deterministic export

**Files:**
- Create: `trader/research/bundle.py`
- Create: `tests/research/test_research_bundle.py`

**Interfaces:**
- Produces `BundleError`, `BundleDigest`, `VerifiedResearchBundle`, and `ResearchBundle`.
- `ResearchBundle(db).export(artifact_id: str, path: Path) -> BundleDigest` reads `ExperimentRegistry`, `EligibilityDecisionRepository`, `OperatorReviewRepository`, and `AttestationRepository`.
- `ResearchBundle.verify(path: Path) -> VerifiedResearchBundle` accepts only the format written by `export`.

- [ ] **Step 1: Write the failing export determinism test**

```python
def test_export_is_byte_identical_and_read_only(populated_db, tmp_path):
    bundle = ResearchBundle(populated_db)
    first = bundle.export(ARTIFACT_ID, tmp_path / "first")
    second = bundle.export(ARTIFACT_ID, tmp_path / "second")

    assert first.manifest_digest == second.manifest_digest
    assert tree_bytes(tmp_path / "first") == tree_bytes(tmp_path / "second")
    assert all(not (p.stat().st_mode & 0o222) for p in (tmp_path / "first").rglob("*"))
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py::test_export_is_byte_identical_and_read_only -q`

Expected: FAIL because `trader.research.bundle` does not exist.

- [ ] **Step 3: Implement the minimal public format and export path**

```python
@dataclass(frozen=True)
class BundleDigest:
    manifest_digest: str
    path: Path

class ResearchBundle:
    def __init__(self, db: Any):
        self._db = db

    def export(self, artifact_id: str, path: Path) -> BundleDigest:
        evidence = self._load_public_evidence(artifact_id)
        files = self._canonical_files(evidence)
        return self._write_staged(path, files)
```

Use fixed names `artifact.json`, `family.json`, `trials.json`, `folds.json`,
`decision.json`, `review.json`, `attestation.json`, and `manifest.json`.
Serialize all JSON with `canonical_json_bytes`; the manifest contains format
version, artifact ID, attestation public-key ID, source/config/dataset/ruleset
digests, and a lexically sorted `{filename: sha256}` table.  Write beneath a
fresh staging sibling, `fsync` files, rename only after checksum validation,
then chmod directories `0o555` and regular files `0o444`.

- [ ] **Step 4: Run the focused test and confirm GREEN**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py::test_export_is_byte_identical_and_read_only -q`

Expected: PASS.

- [ ] **Step 5: Add the evidence-completeness tests**

```python
def test_export_binds_failed_trial_holdout_cost_stress_and_attestation(populated_db, tmp_path):
    result = ResearchBundle(populated_db).export(ARTIFACT_ID, tmp_path / "bundle")
    manifest = read_canonical_json(tmp_path / "bundle" / "manifest.json")
    trials = read_canonical_json(tmp_path / "bundle" / "trials.json")

    assert result.manifest_digest == manifest["manifest_digest"]
    assert any(trial["status"] == "FAILED" for trial in trials)
    assert manifest["attestation"]["public_key_id"].startswith("ed25519-")
    assert "private" not in canonical_text(tmp_path / "bundle")
```

- [ ] **Step 6: Implement complete repository projection**

Load the sealed artifact, family, all trials including archived trials, validation
folds, holdout row, decision keyed by the artifact, review keyed by the decision
digest, and its signed attestation.  Fail with `BundleError` if any required
record is absent or any ID/digest binding disagrees.  Project dataclasses to
explicit public dictionaries; never serialize `__dict__` or DB rows wholesale.

- [ ] **Step 7: Run the Task 1 file**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py -q`

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add trader/research/bundle.py tests/research/test_research_bundle.py
git commit -m "feat(research): export deterministic public evidence bundles"
```

### Task 2: Verify bundle integrity and path safety fail closed

**Files:**
- Modify: `trader/research/bundle.py`
- Modify: `tests/research/test_research_bundle.py`

**Interfaces:**
- Consumes the Task 1 fixed file set and manifest checksum table.
- Produces `ResearchBundle.verify(path) -> VerifiedResearchBundle`, whose projection exposes `manifest_digest`, `artifact_id`, `dataset_manifest_digest`, `attestation`, and `trace_digests`.

- [ ] **Step 1: Write the failing integrity/safety tests**

```python
@pytest.mark.parametrize("name", ["../artifact.json", "/tmp/artifact.json"])
def test_verify_rejects_manifest_path_traversal(populated_db, tmp_path, name):
    root = exported_bundle(populated_db, tmp_path)
    rewrite_manifest(root, file_name=name)
    with pytest.raises(BundleError, match="unsafe"):
        ResearchBundle(populated_db).verify(root)

def test_verify_rejects_tampered_or_extra_file(populated_db, tmp_path):
    root = exported_bundle(populated_db, tmp_path)
    (root / "artifact.json").chmod(0o644)  # simulate post-export tampering
    (root / "artifact.json").write_text("{}")
    with pytest.raises(BundleError, match="checksum"):
        ResearchBundle(populated_db).verify(root)
```

- [ ] **Step 2: Run the selected tests and confirm RED**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py -k 'traversal or tampered_or_extra' -q`

Expected: FAIL because `verify` is not implemented.

- [ ] **Step 3: Implement fail-closed verification**

```python
def verify(self, path: Path) -> VerifiedResearchBundle:
    root = _require_bundle_root(path)
    manifest = _load_canonical_json(root / "manifest.json")
    expected = _validated_checksum_table(manifest)
    _reject_unexpected_or_unsafe_entries(root, set(expected) | {"manifest.json"})
    for name, checksum in expected.items():
        _require_safe_name(name)
        if _sha256_file(root / name) != checksum:
            raise BundleError(f"checksum mismatch for {name}")
    return _verified_projection(manifest, root)
```

Require each entry to be a direct, regular, non-symlink child with a fixed
whitelisted filename. Reject writes in the published tree, malformed canonical
JSON, unexpected private-key field names (`private_key`, `pem`, `secret`), and
manifest IDs that do not match the embedded artifact/family/decision/review/
attestation bindings.

- [ ] **Step 4: Run the selected tests and confirm GREEN**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py -k 'traversal or tampered_or_extra' -q`

Expected: PASS.

- [ ] **Step 5: Add symlink, corruption, and no-private-material regression tests**

```python
def test_verify_rejects_symlink_and_private_material(populated_db, tmp_path):
    root = exported_bundle(populated_db, tmp_path)
    (root / "review.json").unlink()
    (root / "review.json").symlink_to(tmp_path / "outside.json")
    with pytest.raises(BundleError, match="symlink"):
        ResearchBundle(populated_db).verify(root)
```

- [ ] **Step 6: Complete verification projection and run the file**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add trader/research/bundle.py tests/research/test_research_bundle.py
git commit -m "feat(research): verify artifact bundle integrity"
```

### Task 3: Reproduce exact evidence from external inputs

**Files:**
- Create: `scripts/reproduce_experiment.py`
- Modify: `tests/research/test_research_bundle.py`

**Interfaces:**
- Consumes `ResearchBundle.verify`, `--bundle`, `--research-db`, and an explicit `--vendor-input` JSON fixture/adapter reference.
- Produces stdout canonical JSON `{status, artifact_id, manifest_digest, trace_digests}` and process status 0 only for exact reproduction.

- [ ] **Step 1: Write the failing double-reproduction test**

```python
def test_reproduction_twice_has_identical_trace_digests(populated_db, tmp_path):
    root = exported_bundle(populated_db, tmp_path)
    args = [sys.executable, "scripts/reproduce_experiment.py", "--bundle", str(root),
            "--research-db", str(populated_db.path), "--vendor-input", str(VENDOR_FIXTURE)]
    one = subprocess.run(args, capture_output=True, text=True, check=True)
    two = subprocess.run(args, capture_output=True, text=True, check=True)
    assert json.loads(one.stdout) == json.loads(two.stdout)
```

- [ ] **Step 2: Run it and confirm RED**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py::test_reproduction_twice_has_identical_trace_digests -q`

Expected: FAIL because the script is absent.

- [ ] **Step 3: Implement the offline reproduction command**

```python
def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    verified = ResearchBundle(open_research_db(args.research_db)).verify(Path(args.bundle))
    with tempfile.TemporaryDirectory(prefix="mmr-reproduce-") as work:
        actual = rebuild_expected_traces(verified, Path(args.research_db), Path(args.vendor_input), Path(work))
    assert_exact_digests(verified.trace_digests, actual)
    print(canonical_json_bytes(report(verified, actual)).decode("utf-8"))
    return 0
```

Use only the project’s existing deterministic backtester/`trace_signature`
helpers. Validate external DB family/artifact/dataset digests before running.
Treat missing vendor input, mismatch, exception, or unexpected output as a
nonzero failure with a public-safe error message; do not write to DuckDB.

- [ ] **Step 4: Run the double-reproduction test and confirm GREEN**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py::test_reproduction_twice_has_identical_trace_digests -q`

Expected: PASS.

- [ ] **Step 5: Add an external-data mismatch test**

```python
def test_reproduction_fails_closed_on_vendor_digest_mismatch(populated_db, tmp_path):
    result = run_reproducer(exported_bundle(populated_db, tmp_path), altered_vendor_fixture)
    assert result.returncode != 0
    assert "digest mismatch" in result.stderr
```

- [ ] **Step 6: Run all bundle tests**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add scripts/reproduce_experiment.py tests/research/test_research_bundle.py
git commit -m "test(research): reproduce signed artifact bundles"
```

### Task 4: Document the offline procedure and run the P2 gate

**Files:**
- Create: `docs/superpowers/rollout/trading-income-research-runbook.md`
- Modify: `README.md`

**Interfaces:**
- Documents the exact `ResearchBundle.export`, `ResearchBundle.verify`, and `scripts/reproduce_experiment.py` operator workflow.

- [ ] **Step 1: Write documentation assertions**

```python
def test_runbook_states_external_input_and_no_authority():
    text = Path("docs/superpowers/rollout/trading-income-research-runbook.md").read_text()
    assert "research DuckDB" in text
    assert "vendor data" in text
    assert "does not authorize trading" in text
```

- [ ] **Step 2: Run the documentation assertion and confirm RED**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py::test_runbook_states_external_input_and_no_authority -q`

Expected: FAIL because the runbook does not exist.

- [ ] **Step 3: Add the runbook and README link**

Document preconditions, safe export destination, verification, reproduction with
`--research-db` and `--vendor-input`, expected PASS output, data-vendor
limitations, handling a mismatch, private-key hygiene, and the fact that a
software pass remains `CANDIDATE` without a qualified real dataset/family and
valid signed paper attestation. Link the runbook from a concise README research
evidence section.

- [ ] **Step 4: Run the documentation assertion and confirm GREEN**

Run: `uv run --frozen --extra test pytest tests/research/test_research_bundle.py::test_runbook_states_external_input_and_no_authority -q`

Expected: PASS.

- [ ] **Step 5: Run focused and canonical P2 verification**

Run: `uv run --frozen --extra test pytest tests/research/ -q`

Expected: PASS, including Task 8 bundle tests and Tasks 1–7 research tests.

Run: `uv run --frozen --extra test pytest -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add README.md docs/superpowers/rollout/trading-income-research-runbook.md tests/research/test_research_bundle.py
git commit -m "docs(research): publish artifact reproduction runbook"
```

## Plan self-review

- Spec coverage: Tasks 1–2 implement canonical public export, checksums,
  read-only publication, private-material exclusion, and fail-closed verification;
  Task 3 implements clean-directory reproduction against the external DuckDB and
  vendor input; Task 4 documents the workflow and runs the required gates.
- Placeholder scan: no deferred implementation placeholders remain.
- Type consistency: all tasks use the same `ResearchBundle.export/verify`,
  `BundleDigest`, and `VerifiedResearchBundle` interfaces.
