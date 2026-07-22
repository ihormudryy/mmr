# Trading Income Research Runbook

## P2 release gate

Run the unified gate:

```bash
python3 scripts/p2_release_gate.py --synthetic-only --json --output p2-synthetic.json
# After exporting a bundle from a qualified experiment family:
python3 scripts/p2_release_gate.py --bundle ~/.local/share/mmr/artifacts/<artifact-id> \
  --manual-attestation-report p2-attestation.json
```

Manual attestation report JSON (non-fungible — real qualified data required):

```json
{
  "artifact_id": "artifact-<32hex>",
  "bundle_path": "~/.local/share/mmr/artifacts/<artifact-id>",
  "bundle_manifest_digest": "sha256:<hex>",
  "attestation_state": "PAPER_ELIGIBLE",
  "commit_digest": "<git rev-parse HEAD>",
  "config_digest": "sha256:<deployed trader.yaml>",
  "passed": true
}
```

## Reproducing an Artifact Bundle
When a new strategy is published, it is distributed as a read-only artifact bundle containing all the necessary evidence for evaluation and eligibility. To verify the bundle and replay its execution:

1. **Verify Bundle**: Ensure the bundle has not been tampered with and is signed by a trusted key:
```bash
mmr research attest verify --public-key-file <path_to_public_key> --bundle <path_to_bundle>
```

2. **Reproduce Experiment**: Rebuild the experiment traces deterministically:
```bash
python scripts/reproduce_experiment.py --bundle <path_to_bundle>
```
This script will rebuild all folds and traces in a clean temporary directory and compare the exact expected digests.

## Data Limitations
- Data vendors may introduce restatements, stock splits, or corrections over time. A software-pass experiment will remain `CANDIDATE` until a real qualified dataset and family earn a signed paper attestation.
