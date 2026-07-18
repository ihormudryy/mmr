# P2 Research Evidence Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce reproducible, statistically honest, signed `PAPER_ELIGIBLE` artifacts from qualified point-in-time data while permanently retaining failed trials and reviews.

**Architecture:** A separate research DuckDB stores immutable dataset manifests, universe membership, experiment families, trials, validation folds, metrics, reviews, and attestations. Offline deterministic services create canonical digests and Ed25519 signatures; production receives only read-only artifact bundles and public verification keys.

**Tech Stack:** DuckDB, existing backtester, pandas/NumPy/SciPy, `exchange_calendars`, `cryptography` Ed25519, Hypothesis, pytest.

## Global Constraints

- Research never opens or writes `mmr_journal.duckdb`.
- A manifest is immutable after sealing. Corrections create a new manifest linked to the prior digest.
- Every attempted parameter trial, including exceptions and invalid outputs, belongs to the experiment family's multiple-testing denominator.
- Holdout access is write-once and auditable. A failed holdout retires that artifact version.
- Private signing keys are never stored in YAML, environment dumps, Docker images, trader mounts, logs, reports, or DuckDB.

---

### Task 1: Add isolated research configuration and dependencies

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `trader/config.py`
- Modify: `trader/container.py`
- Modify: `config_defaults/trader.yaml`
- Modify: `scripts/db_backup.sh`
- Create: `tests/test_research_config.py`

**Interfaces:** Produces `StorageConfig.research_duckdb_path: str` and the `RESEARCH_DUCKDB_PATH` override; consumed only by offline research repositories and backup tooling.

- [ ] Write tests for default/expanded `research_duckdb_path`, environment override `RESEARCH_DUCKDB_PATH`, and a guard rejecting equality with journal/history/operational paths.
- [ ] Add `cryptography>=45.0` to runtime dependencies and `hypothesis>=6.0` to test extras; run `uv lock` rather than editing the lock manually.
- [ ] Add `storage.research_duckdb_path: ~/.local/share/mmr/data/mmr_research.duckdb`, path expansion, Compose volume visibility for offline CLI only, and backup inclusion.
- [ ] Ensure `Trader` and `strategy_service` constructors do not receive/open the research path.
- [ ] Run `uv run --frozen --extra test pytest tests/test_research_config.py tests/test_config.py tests/test_db_backup.py -q` and commit `build(research): isolate evidence storage and crypto deps`.

### Task 2: Implement canonical dataset manifests and quality reports

**Files:**
- Create: `trader/research/__init__.py`
- Create: `trader/research/canonical.py`
- Create: `trader/research/dataset_manifest.py`
- Create: `trader/research/schema.py`
- Create: `tests/research/test_dataset_manifest.py`

**Interfaces:** Produces `canonical_json_bytes(value) -> bytes`, `dataset_manifest_digest(manifest) -> str`, and `DatasetManifestRepository.seal/get`; these exact digest bytes are consumed by every later artifact and attestation.

**Research migrations 1-2:** `dataset_manifests`, `dataset_files`, `dataset_quality_findings`, `dataset_corrections`; primary key is SHA-256 manifest digest and sealed rows are append-only.

- [x] Write golden canonicalization tests for UTC timestamps, decimals, tuple ordering, mapping-key sorting, Unicode, and rejection of NaN/Infinity/naive datetimes.
- [x] Write manifest tests covering vendor/retrieval, calendar/package version, timestamp convention, adjustments, boundaries, checksums, quality summary, spread source, and correction lineage.
- [x] Test seal idempotency and digest conflict; UPDATE/DELETE APIs must not exist for sealed records.
- [x] Implement `canonical_json_bytes(value)`, `sha256_digest(prefix, value)`, frozen `DatasetManifest`, `QualityFinding`, and `DatasetManifestRepository.seal/get`.
- [x] A required finding with `passed=False` makes `research_eligible=False`; no caller flag may override it.
- [x] Run `uv run --frozen --extra test pytest tests/research/test_dataset_manifest.py -q` and commit `feat(research): add immutable dataset manifests`.

### Task 3: Freeze point-in-time universes and qualify bars

**Files:**
- Create: `trader/research/universe_membership.py`
- Create: `trader/research/data_quality.py`
- Modify: `trader/data/universe.py`
- Create: `tests/research/test_point_in_time_universe.py`
- Create: `tests/research/test_data_quality.py`

**Interfaces:** `PointInTimeMembership(conid, effective_from, effective_to, symbol, delisted_at, source)`; `DatasetQualifier.qualify(request) -> DatasetQualification`.

- [ ] Test membership as-of boundaries, ticker changes, delisted names, overlapping intervals, missing provenance, and frozen membership digest.
- [ ] Test XNYS regular-session completeness, early closes, DST, duplicates, gaps, timestamps outside session, corrupt/non-finite OHLCV, split discontinuities, and verified corrections.
- [ ] Keep genuine gaps/crashes/halts; only a correction with original value, replacement, source, reason, and reviewer may alter data.
- [ ] Add a read adapter from existing `UniverseAccessor`; do not mutate legacy current-universe behavior.
- [ ] Implement deterministic reports and fail the dataset rather than silently dropping a failed instrument/bar.
- [ ] Run focused tests and commit `feat(research): qualify point-in-time market datasets`.

### Task 4: Build the complete experiment registry

**Files:**
- Create: `trader/research/experiment_registry.py`
- Create: `trader/research/artifact.py`
- Modify: `trader/data/backtest_store.py`
- Modify: `trader/mmr_cli.py`
- Create: `tests/research/test_experiment_registry.py`

**Interfaces:** Produces `ExperimentRegistry.create_family`, `start_trial`, `finish_trial`, `open_holdout`, and `seal_artifact`; consumes sealed dataset digests and existing backtester result traces.

**Research migrations 3-6:** `experiment_families`, `experiment_trials`, `validation_folds`, `trial_metrics`, `strategy_artifacts`, `holdout_access_log`.

- [ ] Test family creation with commit, source-tree digest, dependency-lock digest, container digest, manifest digest, declared search space, cost model, and validation protocol.
- [ ] Test that `start_trial` inserts before execution and `finish_trial` records `SUCCEEDED`, `FAILED`, `INVALID`, or `TIMED_OUT` with traceback digest/safe summary.
- [ ] Test that no trial may be deleted and archived trials remain in `selection_trial_count`.
- [ ] Test a holdout token can be opened once per artifact version; any second open rejects and a failed holdout sets artifact state `RETIRED`.
- [ ] Implement `ExperimentRegistry` transactionally. Add an adapter that imports existing `BacktestStore` results as `LEGACY_UNQUALIFIED`; they cannot earn eligibility.
- [ ] Add CLI commands `mmr research family create`, `trial run`, `family show`, and `artifact show --json` with parameterized queries only.
- [ ] Run focused tests plus `tests/test_backtest_store.py`; commit `feat(research): record complete experiment families`.

### Task 5: Implement walk-forward, cost, robustness, and selection-bias analysis

**Files:**
- Create: `trader/research/validation.py`
- Create: `trader/research/statistics.py`
- Create: `trader/research/attribution.py`
- Modify: `trader/simulation/backtester.py`
- Create: `tests/research/test_validation_protocol.py`
- Create: `tests/research/test_selection_adjustment.py`

**Interfaces:** `ValidationPlan(training, folds, embargo, holdout)`; `ValidationResult` includes baseline/1.5x/2x costs, bootstrap interval, deflated/selection-adjusted Sharpe, fold/regime/month/instrument results, benchmark, capacity, and deterministic replay.

- [ ] Test chronological non-overlap and embargo with property-generated date ranges; reject random shuffles and fold leakage.
- [ ] Add deterministic fixtures for commissions/spread/slippage at 1x, 1.5x, 2x and prove higher costs cannot improve net P&L.
- [ ] Test parameter neighborhoods, all-trial selection denominator, bootstrap lower bound, deflated Sharpe inputs, profit factor, concentration, and remove-outlier diagnostics.
- [ ] Add exposure/volatility-matched SPY benchmark metrics: drawdown, return, downside deviation, recovery, time in market.
- [ ] Freeze regime definitions before trial results and require adequate sample counts; insufficient buckets are explicit, not assumed positive.
- [ ] Add a pure adapter around the existing backtester so identical inputs produce identical signal/order traces; do not fork execution math.
- [ ] Run focused tests and commit `feat(research): add leakage-safe validation protocol`.

### Task 6: Encode the quantitative eligibility ruleset

**Files:**
- Create: `trader/research/eligibility.py`
- Create: `trader/research/rulesets/paper_v1.py`
- Create: `tests/research/test_paper_eligibility.py`

**Interfaces:** `EligibilityDecision(state, ruleset_digest, passed, failures, evidence_refs)`; every rule returns stable code, observed value, threshold, and evidence reference.

- [ ] Add one test per approved gate: 200 round trips; eight instruments; expectancy at 1x/1.5x/2x; 95% selection-adjusted confidence; Sharpe lower bound; profit factor 1.20; 60% positive folds; 35% month/40% instrument concentration; scaled 3% drawdown; neighborhood robustness; liquidity/capacity; deterministic replay.
- [ ] Add benchmark/regime tests: benchmark-relative drawdown, accompanying exposure metrics, 70% eligible regimes, per-regime loss tolerance, transition stability.
- [ ] Add boundary and missing-evidence tests. All missing or non-finite critical observations fail closed.
- [ ] Implement immutable versioned ruleset `paper-v1`; compute digest from exact rule configuration and code/source identity.
- [ ] Persist the complete decision, including every passing and failing rule. No qualitative review may change a quantitative failure.
- [ ] Run focused tests and commit `feat(research): enforce paper eligibility ruleset v1`.

### Task 7: Add qualitative review and Ed25519 attestations

**Files:**
- Create: `trader/research/review.py`
- Create: `trader/research/attestation.py`
- Create: `trader/research/signing.py`
- Modify: `trader/mmr_cli.py`
- Create: `tests/research/test_attestation.py`
- Create: `tests/research/test_private_key_hygiene.py`

**Interfaces:** Produces offline `AttestationSigner.sign(payload) -> EligibilityAttestation` and production-safe `AttestationVerifier.verify(attestation, expected) -> VerifiedEligibility`; consumes quantitative decision and signed qualitative review.

**Research migrations 7-8:** append-only `operator_reviews`, `eligibility_attestations`, and `attestation_revocations`.

- [ ] Test the mandatory review fields: rationale, cost survival, failure regimes, data limitations, sensitivity, dependencies, capacity/decay, episode dominance, holdout-once confirmation.
- [ ] Test sign/verify, tampering of every authority field, wrong key, expiry, revoked attestation, changed artifact/allowlist/ruleset/mode/allocation, deterministic signature payload, and public-key rotation identifier.
- [ ] Test key permissions and hygiene: signer accepts a path/PKCS8 input only, refuses group/world-readable files, never prints key bytes, and generated bundles contain only public key IDs.
- [ ] Implement offline `AttestationSigner` and production-safe `AttestationVerifier`. Store signature as base64url; digest the unsigned canonical payload.
- [ ] Add `mmr research review submit`, `attest paper --key-file`, and `attest verify --public-key-file`. Require interactive confirmation unless `--review-id` and `--yes` are both present in a non-production offline context.
- [ ] Run focused tests and commit `feat(research): sign versioned eligibility attestations`.

### Task 8: Export a read-only artifact bundle and prove reproduction

**Files:**
- Create: `trader/research/bundle.py`
- Create: `scripts/reproduce_experiment.py`
- Create: `tests/research/test_research_bundle.py`
- Create: `docs/superpowers/rollout/trading-income-research-runbook.md`
- Modify: `README.md`

**Interfaces:** Produces `ResearchBundle.export(path) -> BundleDigest`, `ResearchBundle.verify(path) -> VerifiedResearchBundle`, and a CLI reproduction report with exact trace digests.

- [ ] Define a bundle manifest containing artifact, source/config digests, dataset manifest, ruleset, quantitative decision, qualitative review reference, attestation, public-key ID, and file checksums.
- [ ] Test deterministic export, read-only permissions, path traversal rejection, checksum verification, no private material, and corruption detection.
- [ ] Make `reproduce_experiment.py --bundle <path>` rebuild all folds/traces in a clean temporary directory and compare exact expected digests.
- [ ] Add a golden small dataset/family that includes at least one failed trial, cost stress, holdout access, and deterministic replay.
- [ ] Run the reproduction twice and assert identical output digests; run the canonical full suite.
- [ ] Document data-vendor limitations and that a software-pass remains `CANDIDATE` until a real qualified dataset/family earns the signed paper attestation.
- [ ] Commit `test(research): add reproducible signed artifact bundles`.

## P2 exit criteria

- Qualified data is point-in-time, provenance-complete, calendar-checked, and immutable.
- Failed candidates and holdout access cannot be erased.
- Statistical gates are deterministic, versioned, selection-aware, and fail closed.
- A signed artifact bundle reproduces from exact inputs and contains no signing secret.
- `trader_service` can verify the bundle with only a configured public key; it cannot mint eligibility.
