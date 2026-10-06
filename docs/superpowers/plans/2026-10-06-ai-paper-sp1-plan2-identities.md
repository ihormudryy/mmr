# AI Paper SP1 — Plan 2: Service Identities — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Run the tasks in number order; each task ends with the full suite green.

**Goal:** Replace the one shared HMAC key on the typed RPC boundary with one Ed25519 key per principal, a per-method allow-list, and signed responses that name the server and the request they answer. Hard cutover: no HMAC fallback.

**Architecture:** A new `trader/messaging/principals.py` holds the principal names, the trust matrix and the per-method allow-list (code, reviewed in git). A new `trader/messaging/rpc_keys.py` loads key files with strict file checks into an in-memory keyring at startup. `HmacServiceAuthenticator` in `trader/messaging/typed_rpc.py` is replaced by `ServiceIdentity` (own private key + keyring of peer public keys). The request envelope gains `principal`, `server`, `role` and `on_behalf_of`; the response gains `server` and `request_digest`. The server verifies, checks the destination, claims the nonce, resolves the method, checks the allow-list, and passes the authenticated caller to handlers that ask for it. Compose mounts each container's own private key and the public keys it needs, over a `tmpfs` that hides every other key.

**Tech Stack:** Python 3.12, `cryptography` (Ed25519, already used by `trader/research/signing.py`), pyzmq, pydantic, pytest, PyYAML (compose test). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`, section 5.3 "Service identities", the "Identities" bullet of section 6, and delivery step 2 of section 7. The spec is on the PR #46 branch (`/private/tmp/sp1-align2`), not on master; its section 5.3 is unchanged by PR #46's later commits.

## Global Constraints

- Ed25519 only. Reuse the primitives in `trader/research/signing.py` (`sign_bytes`, `verify_bytes`, `public_key_id`, `generate_private_key_pem`, `public_key_pem`). Never reuse a bundle key, `AttestationSigner` or `ArtifactVerifier` for RPC.
- "A bundle key is refused as an RPC key and the other way round" (spec 5.3). Enforced three ways: domain-separated signed bytes, the RPC loader refuses a key whose public bytes are in `keys/verify/*.pem`, and the bundle loaders refuse a key whose public bytes are in `keys/rpc/*.pub`.
- "A principal's public key comes only from the server's trusted keyring on disk. A request can name a principal but can never supply or point to a key." The keyring is loaded once at startup into a dict. Request handling never touches the filesystem.
- Key paths: private `~/.config/mmr/keys/rpc/<principal>.key` (mode exactly `0600`), public `~/.config/mmr/keys/rpc/<principal>.pub`. Created only by `mmr keys init`. The directory can be overridden with `MMR_RPC_KEYS_DIR` (tests, non-default homes). Nothing else selects a key.
- Fail closed everywhere: a missing, symlinked, wrong-mode, malformed or duplicate key stops the process at startup with a message naming the file and `mmr keys init`. No HMAC fallback, no "unauthenticated test mode".
- Hard cutover: after this plan, no code reads `service_hmac.key` or `MMR_SERVICE_HMAC_KEY_FILE`. A legacy HMAC envelope fails decode (`AUTHENTICATION_ERROR`) before any handler or nonce claim.
- Kept from today (`trader/messaging/typed_rpc.py`): strict JSON decode (`decode_request` :383, `decode_response` :399), 1 MiB limit, 30 s clock skew both ways, 60 s nonce TTL, nonce claimed last (after skew, signature and destination), one nonce cache per process shared by its servers (today: one `typed_authenticator` for three servers, `trading_runtime.py:471-472`), `(role, method)`-scoped registry (`TypedRpcRegistry.resolve` :727).
- `CommandReceipt` stays frozen. The coordinator's request hash (`canonical_request_hash`, `command_coordinator.py:493`) does not change.
- No container restart, broker order or deploy is authorized by this plan. Task 6 changes compose and `docker.sh`; the owner runs them.
- Test-first. Run single files with `.venv/bin/python -m pytest <path> -q --timeout=30`. Full suite: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`. Every task ends with the full suite green.
- Commit subjects: `feat:` / `fix:` / `test:` / `refactor:` / `docs:`, lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tasks 3 and 4 ship in the same PR. Between them any accepted principal can call any method; the branch is not deployable at that point.
- Line numbers cite master `8116f6a5`. An earlier task can shift them; the function name next to each number is the anchor.

## Rulings (spec silent or incomplete)

Each ruling is binding for this plan. Owner-visible ones are repeated under "Open questions".

1. **Principals in SP1.** `trader`, `strategy`, `cli`, `dashboard`, `ai_supervisor`, `ai_research` (six keypairs). `telegram_bridge` is SP2 and `scheduler` is retired by owner answer 3 (ruling 5): both are reserved names with no key and no allow-list entry. `ai_sandbox` never gets a name or key.
2. **Strategy rights follow the code, not the spec's summary.** Spec 5.3 lists `strategy` → trader as "intent, resolve, publish, feed". The strategy service also calls `create_proposal` (`trader/strategy/signal_proposer.py:138,208`), `get_trading_control` and `list_proposals` (SignalProposer reads, `strategy_runtime.py:620-627`) and `record_state_acknowledged` (outbox backstop, `strategy_runtime.py:611-615`). Removing them would break `auto_execute: propose`. The allow-list grants them. Everything else is refused.
3. **Trader → strategy rights** are the methods the trader sends today: `enable_strategy`, `disable_strategy`, `update_strategy_params`, `get_strategy_receipt` (coordinator forward, `production_api.py:2038-2058`), `list_strategies` (`command_stack.py:101`), `arm_paper_automation`, `disarm_paper_automation`, `get_paper_automation_arm` (`paper_hot_arm.py:306,321,339`) and `reload_strategies` (spec: "hot-arm and reload").
4. **AI principals get only methods that exist today.** `ai_supervisor`: account and market reads, `pause_trading`. `ai_research`: market-data reads. Plans 3–5 add `publish_ai_risk_policy`, `submit_ai_paper_decision`, `register_ai_deployment`, the scoreboard read and experiment methods to the allow-list in the tasks that create them.
5. **`scheduler` has no identity and no trading RPC rights** (owner answer 3). Its jobs run locally (`config_defaults/pycron.yaml:35-66`, container `docker-compose.yml:438`). Every scheduled command, checked against the ACL:

   | pycron job | command | trader RPC it can reach | ACL needed |
   |---|---|---|---|
   | `data_refresh_us` (:38) | `data refresh us_top20_daily us_top20_1min` | `_handle_data_refresh` (`mmr_cli.py:9430`) reuses `_handle_data_download` (:8742), whose trader probe `get_status` (:8840) and symbol fallback `rpc_mmr.resolve` (:8872) sit inside `try/except` and are soft: no trader means `rpc_mmr = None` and unresolved symbols are counted as failed. Refresh symbols come from universes already in the local DB (`accessor.resolve_symbol` first, :8860) | none |
   | `data_refresh_asx` (:50) | `data refresh asx_daily asx_1min` | same code path (IB history goes through the data service, not the typed trader ports) | none |
   | `db_backup` (:62) | `data backup --keep 7` | `_handle_data_backup` (:9033) reads `duckdb_path` and copies files; no SDK call | none |

   So the scheduler ACL is empty: `scheduler` is not in `KNOWN_PRINCIPALS`, has no key, and the container mounts only the `tmpfs` over `keys/rpc` (same as `data`). `MMR_RPC_PRINCIPAL=scheduler` is refused by the SDK. If a future pycron job really needs a trader read, the owner approves adding `scheduler` back with the exact method named; `tests/test_scheduler_acl.py` (Task 6) fails until the job list and the table below it agree. The cost of the soft fallback: a refresh job meeting a symbol that is not in the local DB no longer resolves it through the trader; it reports that symbol as failed (the CLAUDE.md "fail loudly" rule holds, nothing is guessed).
6. **Where `source` comes from** (spec 6: "source derived from the key, never from the body").
   - `CommandRequest` gains `principal: Optional[str] = None`, set by RPC handlers from the authenticated caller and never from the body. It is not part of `canonical_request_hash` (identity of the command, not of the caller). Internal commands (coordinator children, recovery) keep `None`.
   - Authorization reads `principal`, not `source`: `execute_automated_intent` requires `principal == "strategy"` (`automated_intent_command.py:175`); live approval requires `principal == "dashboard"` (replaces the `NON_HUMAN_APPROVE_SOURCES` deny-list at `command_coordinator.py:1275,1570` with an allow of one).
   - `execute_automated_intent` and `approve_proposal` set `source = principal` (spec 5.3 replaces the hard-coded `"strategy_service"` at `production_api.py:1124`). `ApproveProposalRequest.source` (`production_api.py:648`) is removed; a body that still sends it fails `VALIDATION_ERROR` (`extra="forbid"`).
   - `create_proposal` keeps the body `source` as an attribution label only (the bridge's `strategy:<name>` that `check_exits` filters on, `signal_proposer.py:77`). The label is checked against the principal: from `strategy` it must match `^strategy:[A-Za-z0-9_.-]+$`; from anyone else it must not start with `strategy:`; empty becomes the principal name. `CommandRequest.source` is the label, `principal` the caller.
   - Other handlers keep their constant labels (`"dashboard"`, `"operator"`). They never came from the body, and `REQUIRED_ACTIVATION_SOURCE = "operator"` (`trader/promotion/controller.py:97`) is a separate contract. Their authority now comes from the allow-list. Changing those labels is out of scope.
7. **Response binding.** `request_digest = sha256(raw request wire bytes)`. The server hashes the bytes it received; the client hashes the bytes it sent. A reply to a request the server could not decode carries `request_id = ""` and `request_digest = ""`, so a client never accepts it as its own (it times out, as today).
8. **Destination** covers server principal, socket role and method. A request for `strategy` replayed at `trader`, or for `query` replayed at `command`, fails signature-independent destination checks after the signature is verified and before the nonce is claimed.
9. **`on_behalf_of`** is accepted only from `trader` (the only forwarder). From any other principal it must be `null`, else `AUTHENTICATION_ERROR`. It is logged and never used for authorization.
10. **Unknown vs denied.** An unregistered method is `METHOD_NOT_ALLOWED` (as today). A registered method the caller may not call is `PERMISSION_DENIED`, logged at WARNING with principal, method and request id. `place_standalone_order` and `set_risk_limits` are not registered on the typed surface at all, so the AI table test expects `METHOD_NOT_ALLOWED` for them.
11. **Allow-list shape.** The table lives in `principals.py`. A production registry is built with that table; registering a method with no entry raises at startup (no silent dead method). A registry built without a table (tests) denies every method. An entry may be an empty set (explicitly nobody).
12. **Key file rules.** Private: regular file, not a symlink, mode exactly `0600`. Public: regular file, not a symlink, no group/world write bit. Directory mode is not checked (compose's `tmpfs` is root-owned `0755`). Two principals may not share a key. A server's own key may not equal a peer's.
13. **Rotation and recovery are a coordinated switch, not "generate and restart"** (owner answer 7). `mmr keys init` creates missing pairs and never overwrites. `mmr keys init --rotate <principal>` writes a new pair (temp file + `os.replace`). There is no overlap window: every server that trusts the principal (`peers_for` inverse) must load the new `.pub` at the same time, so every container holding the old private or public key is restarted together (a bind mount keeps the old inode), and requests in flight during the switch fail (`AUTHENTICATION_ERROR`; clients retry). The command prints the services and warns about in-flight requests. A lost private key is recovered the same way (rotate that principal), unless the encrypted backup (ruling 19) restores it. Procedure lives in `docs/OPERATIONAL_STATE.md` (Task 6); `tests/test_rpc_key_rotation.py` (Task 7) proves old-refused / new-accepted on every server that trusts the rotated principal.
14. **HMAC retirement** (owner answer 6). `HmacServiceAuthenticator`, `load_service_hmac_key`, `ServiceHmacKeyError`, `TypedRpcConfig.service_hmac_key_file` and every `MMR_SERVICE_HMAC_KEY_FILE` read are deleted: the new RPC boundary refuses an HMAC envelope (`AUTHENTICATION_ERROR`). The container mounts are removed in the cutover: the three `MMR_SERVICE_HMAC_KEY_FILE` env lines (`docker-compose.yml:219,310,396`), the yaml line (`docker.sh -u` strips it, keeps a `.bak`) and the `docker.sh` provisioning code. A leftover `service_hmac_key_file` in `trader.yaml` or a leftover env var is ignored with one WARNING naming it as retired. **No tool ever deletes `service_hmac.key`.** `docker.sh -u` prints a reminder while the file exists. The operator deletes it by hand only after (a) Task 7 passes, (b) a manual `mmr status` / `/cc` check on the running stack, and (c) the rollback decision is made (rollback means checking out the pre-cutover commit, which needs the file): `rm ~/.config/mmr/service_hmac.key`. Known limit: each container still mounts the whole `~/.config/mmr` directory, so the file stays readable inside containers until the operator deletes it; nothing reads it (Task 7 `test_no_code_path_opens_the_retired_hmac_key`).

15. **Host CLI plus a short-lived `cli` container** (owner answer 1). **Keygen runs in Docker** (owner answer 2): `./docker.sh -k [--rotate P]` runs `mmr keys init` in a one-shot `keygen` container (compose profile `tools`, `restart: "no"`, no network, no `tmpfs` overlay) that bind-mounts only the host `~/.config/mmr/keys/rpc` read-write and runs as the host uid:gid, so files land owned by the operator with modes `0600` / `0644`. `docker.sh` creates the host directory (mode `0700`) first so Docker never creates it as root, and checks the resulting modes afterwards. `mmr keys init|backup|restore` refuse to run in any other container (`/.dockerenv` or `/run/.containerenv` exists) unless `MMR_KEYGEN_CONTAINER=1`, which only the `keygen` service sets; inside every long-lived service `keys/rpc` is a `tmpfs` and keys would be lost. Running `mmr keys init` on the host stays possible but is not the documented path. The host CLI signs as `cli` (default) and reaches only the published loopback trader ports 42101/42102 (`mmr status`, `portfolio-snapshot`, `propose`, `approve`). Commands that need the private strategy ports (`strategies`, `strategies enable|disable|reload`) run in a new one-shot compose service `cli` (profile `tools`, never started by `-u`): `docker compose run --rm cli strategies`. It holds `cli.key` plus `trader.pub` and `strategy.pub` only. **The `cli` private key is never mounted in the `trader` container** (or any long-lived service), so `docker compose exec trader … mmr_cli` is refused and the runbook stops using it. `MMR_RPC_PRINCIPAL` may select `ai_supervisor` or `ai_research` for those clients; `trader`, `strategy`, `dashboard` and any reserved name are refused there. A principal without its private key file fails at the first typed call.
16. **Local hybrid (`start_mmr.sh`).** All services run as the same host user, so key isolation between them is not possible there; each process still loads only its own private key. `start_mmr.sh` runs `mmr keys init` if any key is missing (it already auto-provisioned the HMAC key, `start_mmr.sh:650-706`).

17. **Strategy control keeps both direct paths** (owner answer 4). `cli` and `dashboard` call the strategy service directly and are authenticated there as themselves (`enable_strategy_by_name`, `disable_strategy_by_name`, `reload_strategies`, `list_strategies`). Trader-originated hot-arm and the coordinator forward authenticate as `trader` (`on_behalf_of` is log-only, ruling 9). Nothing is routed through the trader that is not routed today; the Task 4 tables already say this and Task 7 pins both edges.
18. **Live activation is `cli` only** (owner answer 5). `activate_live_canary` and `activate_allocation` allow only `cli`; the signed attestation, preflight nonce and `CanaryActivationService` / allocation checks are unchanged and still run (the RPC allow-list is an extra gate, not a replacement). `dashboard` keeps read/status methods and the risk-reducing `deactivate_live_canary` and `suspend_allocation`, which never need a preflight nonce (`production_api.py:911`). `activate_paper_automation` is out of scope and keeps `HUMAN`.
19. **RPC key backup** (owner answer 7). RPC keys are backed up separately from DuckDB backups (`./docker.sh -B` and `data backup` never include `keys/rpc`) and separately from bundle-signing keys (`keys/verify`, the signer, are never in this archive). `mmr keys backup --recipient <file>` streams `tar` of `keys/rpc` (`*.key`, `*.pub`) into `age` (public-key encryption; the plaintext never touches disk) and writes `~/.local/share/mmr/backups/rpc_keys/rpc_keys_<UTC>.tar.age` with mode `0600` in a `0700` directory. Only the age identity (private) can decrypt it; the operator keeps that identity off the host (password manager or offline media). `mmr keys restore <file> --identity <file>` writes into an empty `keys/rpc` only and then re-runs the strict loaders. Tool choice (`age` vs `gpg` vs the macOS keychain) is an owner question.

## Review Focus

Inputs and failure modes the spec implies but its test list does not name. Each has a named test.

1. **A principal string that looks like a path** (`../verify/paper-automation`, `/etc/passwd`, `CLI`, `cli\x00`). Expect `AUTHENTICATION_ERROR` and no filesystem access. → Task 3 `test_path_like_principal_is_rejected_without_touching_the_filesystem`.
2. **A reply signed by a real server for a different request** (same server key, another request's digest) or by the other server. Expect `AuthenticationError` and a socket reset. → Task 3 `test_client_rejects_response_for_another_request_digest`, `test_client_rejects_response_signed_by_the_other_server`.
3. **Test runs reading the developer's real `~/.config/mmr/keys`.** The bundle loaders now read `keys/rpc`. Expect every test to see an empty temp dir unless it sets one. → Task 1 autouse fixture `isolated_rpc_keys_dir` + `test_default_rpc_keys_dir_is_isolated_in_tests`.
4. **A per-file bind mount of a missing key.** Docker creates a directory in its place and the service fails with a confusing error. Expect `docker.sh -u` to refuse first and name `mmr keys init`. → Task 6 `test_up_refuses_when_an_rpc_key_is_missing`.
5. **`on_behalf_of` used to escalate.** A dashboard request with `on_behalf_of="trader"`, or a trader forward of a dashboard command to a method only `dashboard` may call. Expect rejection and authorization as the signer. → Task 3 `test_on_behalf_of_from_non_trader_is_rejected`, Task 4 `test_forwarded_call_is_authorized_as_trader_not_on_behalf_of`.

---

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/messaging/principals.py` (new) | Principal names, trust matrix, peers per principal, allow-list tables | 1, 4 |
| `trader/research/key_purpose.py` (new) | Where RPC keys live; raw public bytes of RPC and bundle keys (no `trader` imports, so `signing.py` can use it) | 1 |
| `trader/messaging/rpc_keys.py` (new) | Strict key file loading, `RpcKeyring`, keypair generation, rotation | 1, 2 |
| `tests/rpc_identity_fixtures.py` (new) | In-memory identities for tests | 1, 3 |
| `trader/research/signing.py` | Bundle loaders refuse RPC keys | 2 |
| `trader/mmr_cli.py` | `mmr keys init [--rotate P]` | 2 |
| `trader/messaging/typed_rpc.py` | Envelope v2, `ServiceIdentity`, server/client checks, allow-list check, caller to handler | 3, 4 |
| `trader/trading/trading_runtime.py`, `trader/strategy/strategy_runtime.py`, `trader/trading/command_stack.py`, `trader/automation/paper_hot_arm.py`, `trader/sdk.py`, `web/trader_link.py`, `web/manage_client.py`, `web/command_center/gateway.py`, `trader/config.py` | Build identities instead of HMAC authenticators | 3 |
| `trader/messaging/production_api.py`, `trader/messaging/cli_surface.py`, `trader/messaging/manage_surface.py` | Allow-list wiring, caller-aware handlers, `on_behalf_of` forward | 4, 5 |
| `trader/trading/command_coordinator.py`, `trader/automation/automated_intent_command.py` | `CommandRequest.principal`; authz on principal | 5 |
| `docker-compose.yml`, `docker.sh`, `start_mmr.sh`, `trader/operations/health.py`, `CLAUDE.md`, `docs/OPERATIONAL_STATE.md` | Mounts, preflight, retirement, docs | 6 |
| `tests/test_rpc_trust_matrix.py` (new) | Split-service round trips over every edge + negatives | 7 |

---

### Task 1: Principals, key purpose and strict key files

**Files:**
- Create: `trader/messaging/principals.py`, `trader/research/key_purpose.py`, `trader/messaging/rpc_keys.py`, `tests/rpc_identity_fixtures.py`
- Modify: `tests/conftest.py` (autouse fixture)
- Test: `tests/test_rpc_keys.py`

**Interfaces:**
- Produces:
  - `principals.KNOWN_PRINCIPALS: frozenset[str]` = `{"trader","strategy","cli","dashboard","ai_supervisor","ai_research"}`; `SERVER_PRINCIPALS = {"trader","strategy"}`; `CLIENT_PRINCIPALS = {"cli","ai_supervisor","ai_research"}` (SDK-selectable); `RESERVED_PRINCIPALS = {"telegram_bridge","scheduler"}` (no key, no ACL entry; ruling 5).
  - `principals.SERVER_ACCEPTS: Mapping[str, frozenset[str]]` = `{"trader": {"cli","dashboard","strategy","ai_supervisor","ai_research"}, "strategy": {"cli","dashboard","trader"}}`.
  - `principals.CALLS: Mapping[str, frozenset[str]]` = `{"trader": {"strategy"}, "strategy": {"trader"}, "cli": {"trader","strategy"}, "dashboard": {"trader","strategy"}, "ai_supervisor": {"trader"}, "ai_research": {"trader"}}`.
  - `principals.peers_for(principal) -> frozenset[str]` = `SERVER_ACCEPTS.get(p, ∅) | CALLS[p]`; raises `ValueError` for an unknown name.
  - `principals.is_valid_principal_name(name: object) -> bool`: `isinstance(name, str)` and `re.fullmatch(r"[a-z][a-z_]{1,31}", name)` and `name in KNOWN_PRINCIPALS`.
  - `key_purpose.RPC_KEYS_DIR_ENV = "MMR_RPC_KEYS_DIR"`; `default_rpc_keys_dir() -> Path`; `bundle_verify_dir_for(rpc_dir: Path) -> Path` (= `rpc_dir.parent / "verify"`); `rpc_public_raw(rpc_dir) -> frozenset[bytes]` (raw 32-byte keys of `*.pub`; missing dir → empty); `bundle_public_raw(verify_dir) -> frozenset[bytes]` (`*.pem`; missing dir → empty); `KeyPurposeError(Exception)`.
  - `rpc_keys.RpcKeyError(Exception)`; `rpc_keys.load_rpc_private_key(keys_dir: Path, principal: str) -> Ed25519PrivateKey`; `rpc_keys.RpcKeyring` (immutable; `get(principal) -> Ed25519PublicKey` raises `RpcKeyError("unknown principal")`; `principals() -> frozenset[str]`; `from_public_keys(mapping)` for tests); `rpc_keys.load_identity_material(principal, keys_dir: Path | None = None) -> tuple[Ed25519PrivateKey, RpcKeyring]` (loads own key + `peers_for(principal)`).
  - `tests/rpc_identity_fixtures.py`: `write_keyset(dir: Path, principals=KNOWN_PRINCIPALS) -> dict[str, Ed25519PrivateKey]` (writes `<p>.key` 0600 and `<p>.pub` 0644).

- [ ] **Step 1: Write the failing tests** (`tests/test_rpc_keys.py`)

```python
import os, stat
import pytest
from trader.messaging import principals
from trader.messaging.rpc_keys import RpcKeyError, RpcKeyring, load_identity_material, load_rpc_private_key
from trader.research import key_purpose
from tests.rpc_identity_fixtures import write_keyset

def test_default_rpc_keys_dir_is_isolated_in_tests(tmp_path):
    assert str(key_purpose.default_rpc_keys_dir()).startswith(os.environ["MMR_RPC_KEYS_DIR"])
    assert ".config/mmr" not in str(key_purpose.default_rpc_keys_dir())

def test_peers_follow_the_trust_matrix():
    assert principals.peers_for("trader") == {"cli","dashboard","strategy","ai_supervisor","ai_research"}
    assert principals.peers_for("strategy") == {"cli","dashboard","trader"}
    assert principals.peers_for("ai_research") == {"trader"}
    for reserved in ("telegram_bridge", "scheduler"):
        with pytest.raises(ValueError):
            principals.peers_for(reserved)

@pytest.mark.parametrize("name", ["../verify/x", "/etc/passwd", "CLI", "cli\x00", "", "telegram_bridge", "scheduler", None, 7])
def test_invalid_principal_names(name):
    assert not principals.is_valid_principal_name(name)

def test_loads_own_key_and_peer_keyring(tmp_path):
    write_keyset(tmp_path)
    private, keyring = load_identity_material("strategy", tmp_path)
    assert keyring.principals() == {"cli","dashboard","trader"}
    with pytest.raises(RpcKeyError):
        keyring.get("ai_supervisor")

def test_missing_private_key_names_file_and_command(tmp_path):
    write_keyset(tmp_path); (tmp_path / "trader.key").unlink()
    with pytest.raises(RpcKeyError, match=r"trader\.key.*mmr keys init"):
        load_identity_material("trader", tmp_path)

def test_missing_peer_public_key_fails(tmp_path):
    write_keyset(tmp_path); (tmp_path / "ai_research.pub").unlink()
    with pytest.raises(RpcKeyError, match="ai_research.pub"):
        load_identity_material("trader", tmp_path)

@pytest.mark.parametrize("mode", [0o640, 0o644, 0o400, 0o700])
def test_private_key_mode_must_be_exactly_0600(tmp_path, mode):
    write_keyset(tmp_path); os.chmod(tmp_path / "cli.key", mode)
    with pytest.raises(RpcKeyError, match="0o600"):
        load_rpc_private_key(tmp_path, "cli")

def test_public_key_writable_by_group_is_refused(tmp_path):
    write_keyset(tmp_path); os.chmod(tmp_path / "trader.pub", 0o664)
    with pytest.raises(RpcKeyError, match="writable"):
        load_identity_material("cli", tmp_path)

def test_symlinked_key_files_are_refused(tmp_path):
    write_keyset(tmp_path)
    real = tmp_path / "real.key"; (tmp_path / "cli.key").rename(real)
    os.symlink(real, tmp_path / "cli.key")
    with pytest.raises(RpcKeyError, match="symlink"):
        load_rpc_private_key(tmp_path, "cli")

def test_principal_outside_known_set_never_builds_a_path(tmp_path):
    with pytest.raises(RpcKeyError, match="unknown principal"):
        load_rpc_private_key(tmp_path, "../verify/paper-automation")

def test_two_principals_sharing_a_key_is_refused(tmp_path):
    write_keyset(tmp_path)
    (tmp_path / "ai_research.pub").write_bytes((tmp_path / "cli.pub").read_bytes())
    with pytest.raises(RpcKeyError, match="same public key"):
        load_identity_material("trader", tmp_path)

def test_bundle_verify_key_is_refused_as_rpc_key(tmp_path):
    rpc = tmp_path / "rpc"; rpc.mkdir(); write_keyset(rpc)
    verify = tmp_path / "verify"; verify.mkdir()
    (verify / "paper-automation.pem").write_bytes((rpc / "dashboard.pub").read_bytes())
    with pytest.raises(RpcKeyError, match="bundle"):
        load_identity_material("trader", rpc)

def test_malformed_public_key_fails(tmp_path):
    write_keyset(tmp_path); (tmp_path / "trader.pub").write_bytes(b"garbage")
    with pytest.raises(RpcKeyError, match="trader.pub"):
        load_identity_material("cli", tmp_path)
```

- [ ] **Step 2: Run, expect FAIL** — `ModuleNotFoundError: trader.messaging.principals`.

- [ ] **Step 3: Implement.**
  - `tests/conftest.py`: autouse fixture `isolated_rpc_keys_dir(tmp_path_factory, monkeypatch)` sets `MMR_RPC_KEYS_DIR` to a fresh empty `tmp_path_factory.mktemp("rpc")`. (Task 2 makes the bundle loaders read this directory; without the fixture a test would read the developer's real keys.)
  - `key_purpose.py`: imports only stdlib + `cryptography`. `default_rpc_keys_dir()` returns `Path(os.environ[RPC_KEYS_DIR_ENV])` when set and non-empty, else `Path.home()/".config/mmr/keys/rpc"`. The raw-bytes readers use `serialization.load_pem_public_key` and `public_bytes(Raw, Raw)`; a non-Ed25519 or unparseable file raises `KeyPurposeError` naming the file.
  - `rpc_keys.py`, the file check (one function, used by both loaders):

```python
def _checked_path(keys_dir: Path, principal: str, suffix: str, *, private: bool) -> Path:
    if not is_valid_principal_name(principal):
        raise RpcKeyError(f"unknown principal {principal!r}")
    path = keys_dir / f"{principal}{suffix}"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise RpcKeyError(f"{path} is missing; run `mmr keys init` on the host") from None
    if stat.S_ISLNK(info.st_mode):
        raise RpcKeyError(f"{path} is a symlink; RPC keys must be regular files")
    if not stat.S_ISREG(info.st_mode):
        raise RpcKeyError(f"{path} is not a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if private and mode != 0o600:
        raise RpcKeyError(f"{path} has mode {oct(mode)}; must be exactly 0o600")
    if not private and mode & 0o022:
        raise RpcKeyError(f"{path} is group/world writable ({oct(mode)})")
    return path
```

  The private loader parses with `serialization.load_pem_private_key(data, password=None)` and requires `Ed25519PrivateKey`; the public loader uses `load_pem_public_key` and requires `Ed25519PublicKey`. They do **not** call `signing.load_signing_key`/`load_verify_key` (Task 2 adds the reverse refusal there). `load_identity_material` loads own key, then each peer's `.pub`; computes raw bytes of all of them; raises if two are equal ("same public key"), or if any is in `bundle_public_raw(bundle_verify_dir_for(keys_dir))` ("is a bundle key"). Messages never contain key bytes.

- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add rpc principals and strict key files`.

---

### Task 2: `mmr keys init` and bundle loaders refuse RPC keys

**Files:**
- Modify: `trader/messaging/rpc_keys.py` (generation), `trader/research/signing.py:97-136` (`load_signing_key`, `load_verify_key`), `trader/mmr_cli.py` (new `keys` sub-parser next to `research` at :1743, handler `_handle_keys_init`)
- Test: `tests/test_rpc_keys_init.py`, `tests/research/test_signing_key_purpose.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `rpc_keys.init_keys(keys_dir: Path, *, rotate: str | None = None) -> list[KeyInitRow]`; `rpc_keys.backup_keys(keys_dir, out_path, recipients_file, *, run=_run_age) -> Path`; `rpc_keys.restore_keys(archive, keys_dir, identity_file, *, run=_run_age) -> list[str]` where `KeyInitRow(principal: str, status: Literal["created","kept","rotated"], key_id: str)`; `rpc_keys.RESTART_ON_ROTATE: Mapping[str, tuple[str, ...]]` (compose services that mount the principal's private or public key, derived from `peers_for`). CLI: `mmr keys init [--rotate PRINCIPAL] [--keys-dir PATH]`, `mmr keys backup --recipient FILE [--out PATH]`, `mmr keys restore FILE --identity FILE [--keys-dir PATH]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rpc_keys_init.py
def test_init_creates_every_known_principal_with_strict_modes(tmp_path):
    rows = init_keys(tmp_path / "rpc")
    assert {r.principal for r in rows} == KNOWN_PRINCIPALS and {r.status for r in rows} == {"created"}
    for p in KNOWN_PRINCIPALS:
        assert stat.S_IMODE(os.lstat(tmp_path / "rpc" / f"{p}.key").st_mode) == 0o600
        assert stat.S_IMODE(os.lstat(tmp_path / "rpc" / f"{p}.pub").st_mode) == 0o644
    load_identity_material("trader", tmp_path / "rpc")  # round-trips

def test_init_is_idempotent_and_never_overwrites(tmp_path):
    init_keys(tmp_path); before = (tmp_path / "cli.key").read_bytes()
    assert {r.status for r in init_keys(tmp_path)} == {"kept"}
    assert (tmp_path / "cli.key").read_bytes() == before

def test_half_pair_is_an_error_not_a_repair(tmp_path):
    init_keys(tmp_path); (tmp_path / "cli.pub").unlink()
    with pytest.raises(RpcKeyError, match="cli"):
        init_keys(tmp_path)

def test_mismatched_pair_is_an_error(tmp_path):
    init_keys(tmp_path)
    (tmp_path / "cli.pub").write_bytes((tmp_path / "trader.pub").read_bytes())
    with pytest.raises(RpcKeyError, match="does not match"):
        init_keys(tmp_path)

def test_rotate_replaces_one_pair_only(tmp_path):
    init_keys(tmp_path); old = {p: (tmp_path / f"{p}.key").read_bytes() for p in KNOWN_PRINCIPALS}
    rows = init_keys(tmp_path, rotate="dashboard")
    assert [r.principal for r in rows if r.status == "rotated"] == ["dashboard"]
    assert (tmp_path / "dashboard.key").read_bytes() != old["dashboard"]
    assert all((tmp_path / f"{p}.key").read_bytes() == old[p] for p in KNOWN_PRINCIPALS - {"dashboard"})

def test_rotate_unknown_principal_is_refused(tmp_path):
    with pytest.raises(RpcKeyError):
        init_keys(tmp_path, rotate="telegram_bridge")

def test_cli_refuses_inside_an_ordinary_container(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("trader.mmr_cli._running_in_container", lambda: True)
    monkeypatch.delenv("MMR_KEYGEN_CONTAINER", raising=False)
    _run_cli(["keys", "init", "--keys-dir", str(tmp_path)])
    assert "docker.sh -k" in capsys.readouterr().out and not any(tmp_path.iterdir())

def test_cli_allows_keygen_in_the_one_shot_keygen_container(monkeypatch, tmp_path):
    monkeypatch.setattr("trader.mmr_cli._running_in_container", lambda: True)
    monkeypatch.setenv("MMR_KEYGEN_CONTAINER", "1")
    _run_cli(["keys", "init", "--keys-dir", str(tmp_path)])
    assert (tmp_path / "trader.key").exists()

def test_cli_prints_restart_list_on_rotate(tmp_path, capsys):
    _run_cli(["keys", "init", "--keys-dir", str(tmp_path)])
    _run_cli(["keys", "init", "--keys-dir", str(tmp_path), "--rotate", "strategy"])
    out = capsys.readouterr().out
    assert "strategy" in out and "trader" in out and "dashboard" in out  # services holding strategy.key/.pub
    assert "in-flight" in out                                            # requests fail during the switch
```

```python
# tests/test_rpc_keys_backup.py  (fake `run` replaces the age binary; one real-age test skipped when age is absent)
def test_backup_pipes_only_rpc_keys_into_the_encryptor_and_writes_no_plaintext(tmp_path):
    keys = tmp_path / "config/keys"; init_keys(keys / "rpc"); (keys / "verify").mkdir(); (keys / "verify/paper.pem").write_text("bundle")
    seen = {}
    def fake_run(argv, stdin_bytes): seen["argv"], seen["tar"] = argv, stdin_bytes; return b"CIPHERTEXT"
    out = backup_keys(keys / "rpc", tmp_path / "b/rpc_keys.tar.age", tmp_path / "recipient.txt", run=fake_run)
    names = set(tarfile.open(fileobj=io.BytesIO(seen["tar"])).getnames())
    assert names == {f"{p}.{e}" for p in KNOWN_PRINCIPALS for e in ("key", "pub")}   # no verify/ keys, no service_hmac.key
    assert out.read_bytes() == b"CIPHERTEXT" and stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    assert not list(tmp_path.rglob("*.tar"))                                          # plaintext never on disk

def test_backup_fails_loudly_without_the_encryptor(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(RpcKeyError, match="age"):
        backup_keys(tmp_path, tmp_path / "x.age", tmp_path / "r.txt")

def test_restore_refuses_a_non_empty_keys_dir_and_revalidates_modes(tmp_path):
    ...  # fake `run` returns a tar; restore into empty dir -> strict loaders pass, .key 0600;
         # into a dir that already has any *.key -> RpcKeyError, nothing overwritten

@pytest.mark.skipif(shutil.which("age") is None or shutil.which("age-keygen") is None, reason="age not installed")
def test_backup_restore_round_trip_with_real_age(tmp_path): ...

def test_docker_db_backup_never_includes_rpc_keys():
    assert "keys/rpc" not in Path("docker.sh").read_text().split("backup_databases")[1]   # -B path; see Task 6 for the real-file test
```

```python
# tests/research/test_signing_key_purpose.py
def test_bundle_signer_refuses_an_rpc_private_key(tmp_path, monkeypatch):
    rpc = tmp_path / "rpc"; init_keys(rpc); monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(rpc))
    with pytest.raises(InvalidKeyType, match="RPC"):
        load_signing_key(str(rpc / "ai_research.key"))

def test_bundle_verifier_refuses_an_rpc_public_key(tmp_path, monkeypatch):
    rpc = tmp_path / "rpc"; init_keys(rpc); monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(rpc))
    with pytest.raises(InvalidKeyType, match="RPC"):
        load_verify_key(str(rpc / "trader.pub"))

def test_bundle_keys_still_load_when_no_rpc_dir_exists(tmp_path):
    pem = tmp_path / "k.pem"; pem.write_bytes(generate_private_key_pem()); os.chmod(pem, 0o600)
    load_signing_key(str(pem))
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - `init_keys`: `keys_dir.mkdir(parents=True, mode=0o700, exist_ok=True)`. For each known principal: both files present → load both with Task 1 loaders and require the derived public key to equal the `.pub` ("does not match"), status `kept`; neither present → create; exactly one → `RpcKeyError` (no repair). Create writes the private PEM with `os.open(tmp, O_WRONLY|O_CREAT|O_EXCL, 0o600)`, `fsync`, then `os.replace` onto `<p>.key`; same for `.pub` with `0o644`. Rotate writes both temp files first, then replaces `.key` then `.pub`. Unknown `rotate` → `RpcKeyError`.
  - `RESTART_ON_ROTATE[p]` = compose services whose principal is `p` or has `p` in `peers_for` (`trader`→trader service; `strategy`→`strategy`; `dashboard`→`dashboard`; client principals map to no long-lived service: `cli` is the one-shot `cli` container, which re-reads its bind mounts on every run; the host CLI re-reads the file on every run).
  - `signing.load_signing_key` / `load_verify_key`: after the Ed25519 type check, compare the raw public bytes with `key_purpose.rpc_public_raw(key_purpose.default_rpc_keys_dir())` and raise `InvalidKeyType("key at … is an RPC identity key; bundle keys and RPC keys are separate")`. A `KeyPurposeError` from reading `keys/rpc` (a malformed `.pub`) is re-raised as `MalformedKey` naming that file, so the 17 call sites' existing `except (InsecureKeyFile, InvalidKeyType, MalformedKey)` still catch it (test: `test_malformed_rpc_pub_surfaces_as_malformed_key`). This covers all 17 bundle load sites (attest, allocation sign, `paper_activation._load_verify_keys` :456, `paper_materials.verify_qualified_paper_bundle` :37) without touching them.
  - `mmr_cli`: `_running_in_container()` returns `Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()`; the `keys` handlers refuse when it is true and `MMR_KEYGEN_CONTAINER != "1"`. The handler prints a table (principal, status, key id) and, on rotate, "restart these services: …". It needs no service.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add mmr keys init and refuse rpc keys as bundle keys`.

---

### Task 3: Signed envelope v2 and `ServiceIdentity` (the cutover)

This task replaces the authenticator and every place that builds one, and migrates the 13 test files that use HMAC, in one commit. A split would leave production code importing a deleted class.

**Files:**
- Modify: `trader/messaging/typed_rpc.py` (models :191-246, signing :253-293, authenticator :460-536, key loading :539-620 deleted, server :802-1004, client :1074-1267, module docstring)
- Modify (builders): `trader/trading/trading_runtime.py:93,149,456-472`; `trader/strategy/strategy_runtime.py:29-34,417,470,595-630`; `trader/trading/command_stack.py:108-116,152-185`; `trader/automation/paper_hot_arm.py:153-180`; `trader/sdk.py:221-224,287-325`; `web/trader_link.py:35-92`; `web/manage_client.py:31,57-61`; `web/command_center/gateway.py:197`; `trader/messaging/production_api.py:2061-2130` (type check of the identity); `trader/config.py:103-117,208`
- Modify (tests): the 13 files from `grep -rl "HmacServiceAuthenticator\|load_service_hmac_key\|service_hmac" tests` (`test_typed_rpc.py`, `test_typed_rpc_transport.py`, `test_typed_rpc_async_handlers.py`, `test_production_rpc_security.py`, `test_sdk.py`, `test_command_stack.py`, `test_command_coordinator.py`, `test_domain_feed.py`, `test_strategy_revisions.py`, `test_strategy_trader_gateway.py`, `test_config.py`, `test_service_health.py`, `test_docker_helper.py` — the last one only in Task 6)
- Test: `tests/test_typed_rpc_identity.py` (new)

**Interfaces:**
- Consumes: Task 1 `load_identity_material`, `peers_for`, `is_valid_principal_name`.
- Produces (in `typed_rpc.py`):
  - `TypedRpcRequest` fields: `method, request_id, timestamp, nonce, body, principal: str, server: str, role: str, on_behalf_of: Optional[str], signature: str` — all required, `on_behalf_of` may be `null`; `extra="forbid"`.
  - `TypedRpcResponse` fields: `request_id, ok, body=None, problem=None, server: str, request_digest: str, signature: Optional[str]=None`.
  - `@dataclass(frozen=True) class RpcCaller: principal: str; on_behalf_of: Optional[str]`.
  - `class ServiceIdentity` with `principal` (property), `sign_request(*, server, role, method, request_id, nonce, body, on_behalf_of=None) -> TypedRpcRequest`, `verify_request(request, *, role) -> RpcCaller`, `sign_response(response) -> TypedRpcResponse`, `verify_response(response, *, server, request_digest) -> None`. Constructor `ServiceIdentity(principal, private_key, keyring, *, now=time.time, clock_skew_seconds=30.0, nonce_cache=None)`. `ServiceIdentity.load(principal, keys_dir=None, *, now=time.time) -> ServiceIdentity`.
  - `TypedRpcServer(socket_role, registry, identity, address, port, max_in_flight)` — server principal is `identity.principal`, which must be in `SERVER_PRINCIPALS`.
  - `TypedRpcClient(socket_role, identity, *, server: str, address, port, timeout)`; `call(method, body, response_model, timeout=None, *, on_behalf_of=None)`.
  - `request_digest(raw: bytes) -> str` = `hashlib.sha256(raw).hexdigest()`.
  - `tests/rpc_identity_fixtures.py` gains `make_identities(now=time.time) -> dict[str, ServiceIdentity]` (one per known principal; keyrings per `peers_for`; one shared `ReplayNonceCache` per identity).

- [ ] **Step 1: Write the failing tests** (`tests/test_typed_rpc_identity.py`; unit level on `ServiceIdentity` plus a real-socket server/client pair like `tests/test_typed_rpc_transport.py:148-230`)

```python
NOW = 1_700_000_000.0

def _req(ids, caller="cli", server="trader", role="query", method="get_status", body=None, **kw):
    return ids[caller].sign_request(server=server, role=role, method=method,
                                    request_id="r1", nonce=uuid4().hex, body=body or {}, **kw)

def test_round_trip_returns_the_authenticated_caller():
    ids = make_identities(now=lambda: NOW)
    assert ids["trader"].verify_request(_req(ids), role="query") == RpcCaller("cli", None)

def test_unknown_principal_is_rejected_before_signature_work():
    ids = make_identities(now=lambda: NOW)
    req = _req(ids).model_copy(update={"principal": "telegram_bridge"})
    with pytest.raises(AuthenticationError, match="unknown principal"):
        ids["trader"].verify_request(req, role="query")

def test_path_like_principal_is_rejected_without_touching_the_filesystem(monkeypatch):
    ids = make_identities(now=lambda: NOW)
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("filesystem touched"))
    monkeypatch.setattr("os.lstat", lambda *a, **k: pytest.fail("filesystem touched"))
    for bad in ["../verify/paper-automation", "/etc/passwd", "CLI"]:
        with pytest.raises(AuthenticationError):
            ids["trader"].verify_request(_req(ids).model_copy(update={"principal": bad}), role="query")

def test_principal_not_accepted_by_this_server_is_rejected():
    ids = make_identities(now=lambda: NOW)   # strategy does not accept ai_supervisor
    req = ids["ai_supervisor"].sign_request(server="strategy", role="query", method="list_strategies",
                                            request_id="r", nonce="n", body={})
    with pytest.raises(AuthenticationError, match="unknown principal"):
        ids["strategy"].verify_request(req, role="query")

def test_signature_by_another_principals_key_is_rejected():
    ids = make_identities(now=lambda: NOW)
    req = _req(ids, caller="dashboard").model_copy(update={"principal": "cli"})
    with pytest.raises(AuthenticationError, match="signature"):
        ids["trader"].verify_request(req, role="query")

@pytest.mark.parametrize("field,value", [("body", {"x": 1}), ("method", "get_positions"),
                                         ("timestamp", NOW + 1), ("nonce", "other")])
def test_tampered_request_is_rejected(field, value):
    ids = make_identities(now=lambda: NOW)
    with pytest.raises(AuthenticationError):
        ids["trader"].verify_request(_req(ids).model_copy(update={field: value}), role="query")

@pytest.mark.parametrize("server,role", [("strategy", "query"), ("trader", "command")])
def test_wrong_destination_is_rejected_and_does_not_burn_the_nonce(server, role):
    ids = make_identities(now=lambda: NOW)
    sign = lambda srv, rl: ids["cli"].sign_request(server=srv, role=rl, method="get_status",
                                                   request_id="r", nonce="same-nonce", body={})
    with pytest.raises(AuthenticationError, match="destination"):
        ids["trader"].verify_request(sign(server, role), role="query")
    ids["trader"].verify_request(sign("trader", "query"), role="query")   # same nonce still free

def test_replayed_nonce_is_rejected():
    ids = make_identities(now=lambda: NOW); req = _req(ids)
    ids["trader"].verify_request(req, role="query")
    with pytest.raises(ReplayError):
        ids["trader"].verify_request(req, role="query")

@pytest.mark.parametrize("skew", [31.0, -31.0])
def test_stale_or_future_timestamp_is_rejected(skew):
    clock = {"t": NOW}; ids = make_identities(now=lambda: clock["t"]); req = _req(ids)
    clock["t"] = NOW + skew
    with pytest.raises(AuthenticationError, match="clock skew"):
        ids["trader"].verify_request(req, role="query")

def test_on_behalf_of_from_non_trader_is_rejected():
    ids = make_identities(now=lambda: NOW)
    with pytest.raises(AuthenticationError, match="on_behalf_of"):
        ids["trader"].verify_request(_req(ids, caller="dashboard", on_behalf_of="trader"), role="query")
    req = ids["trader"].sign_request(server="strategy", role="command", method="enable_strategy",
                                     request_id="r", nonce="n2", body={}, on_behalf_of="dashboard")
    assert ids["strategy"].verify_request(req, role="command") == RpcCaller("trader", "dashboard")

def test_bundle_signature_over_the_same_bytes_does_not_verify_as_rpc():
    ids = make_identities(now=lambda: NOW); req = _req(ids)
    raw_key = ids["cli"]._private_key_for_tests()          # test-only accessor, see Step 3
    bundle_style = sign_bytes(raw_key, rpc_signing_bytes(req)[len(RPC_REQUEST_CONTEXT):])
    with pytest.raises(AuthenticationError):
        ids["trader"].verify_request(req.model_copy(update={"signature": bundle_style}), role="query")

def test_legacy_hmac_envelope_is_refused_at_decode():
    legacy = canonical_json({"method": "get_status", "request_id": "r", "timestamp": NOW,
                             "nonce": "n", "body": {}, "signature": "00" * 32})
    with pytest.raises(AuthenticationError, match="malformed"):
        decode_request(legacy)

def test_client_rejects_response_signed_by_the_other_server():
    ids = make_identities(now=lambda: NOW)
    resp = ids["strategy"].sign_response(TypedRpcResponse(request_id="r", ok=True, body={},
                                         server="strategy", request_digest="d"))
    with pytest.raises(AuthenticationError):
        ids["cli"].verify_response(resp.model_copy(update={"server": "trader"}), server="trader", request_digest="d")
    with pytest.raises(AuthenticationError, match="server"):
        ids["cli"].verify_response(resp, server="trader", request_digest="d")

def test_client_rejects_response_for_another_request_digest():
    ids = make_identities(now=lambda: NOW)
    resp = ids["trader"].sign_response(TypedRpcResponse(request_id="r", ok=True, body={},
                                       server="trader", request_digest="other"))
    with pytest.raises(AuthenticationError, match="digest"):
        ids["cli"].verify_response(resp, server="trader", request_digest="mine")
```

  Transport-level tests in the same file (real ZMQ, pattern of `test_typed_rpc_transport.py`): `test_server_replies_authentication_error_and_runs_no_handler_for_legacy_envelope` (handler counter stays 0), `test_client_resets_socket_on_reply_from_wrong_server` (a fake ROUTER replying with a strategy-signed body; afterwards the client's socket identity changed), `test_server_identity_must_be_a_server_principal` (`TypedRpcServer(..., identity=ids["cli"])` raises `ValueError`), `test_startup_fails_without_keys` (`ServiceIdentity.load("trader", empty_dir)` raises `RpcKeyError`).

- [ ] **Step 2: Run, expect FAIL** (`ImportError: ServiceIdentity`).

- [ ] **Step 3: Implement `typed_rpc.py`.**

```python
RPC_REQUEST_CONTEXT = b"mmr.typed-rpc.request.v2\x00"
RPC_RESPONSE_CONTEXT = b"mmr.typed-rpc.response.v2\x00"

def rpc_signing_bytes(request: TypedRpcRequest) -> bytes:
    return RPC_REQUEST_CONTEXT + canonical_json({
        "principal": request.principal, "on_behalf_of": request.on_behalf_of,
        "server": request.server, "role": request.role, "method": request.method,
        "request_id": request.request_id, "timestamp": request.timestamp,
        "nonce": request.nonce, "body": request.body,
    })

def response_signing_bytes(response: TypedRpcResponse) -> bytes:
    return RPC_RESPONSE_CONTEXT + canonical_json({
        "server": response.server, "request_id": response.request_id,
        "request_digest": response.request_digest, "ok": response.ok, "body": response.body,
        "problem": response.problem.model_dump(mode="json") if response.problem else None,
    })
```

`ServiceIdentity.verify_request(request, *, role)` — the order is load-bearing:

```python
def verify_request(self, request: TypedRpcRequest, *, role: str) -> RpcCaller:
    now = self._now()
    if abs(now - request.timestamp) > self._clock_skew_seconds:
        raise AuthenticationError(f"timestamp outside the {self._clock_skew_seconds}s allowed clock skew")
    if not is_valid_principal_name(request.principal) \
            or request.principal not in SERVER_ACCEPTS[self.principal]:
        raise AuthenticationError("unknown principal")
    public_key = self._keyring.get(request.principal)          # dict lookup only
    try:
        verify_bytes(public_key, rpc_signing_bytes(request), request.signature)
    except BadSignature as exc:
        raise AuthenticationError("signature mismatch") from exc
    if request.server != self.principal or request.role != role:
        raise AuthenticationError("wrong destination")
    if request.on_behalf_of is not None and (
            request.principal != "trader" or not is_valid_principal_name(request.on_behalf_of)):
        raise AuthenticationError("on_behalf_of is only accepted from trader")
    self._nonce_cache.claim(request.nonce)
    return RpcCaller(request.principal, request.on_behalf_of)
```

`verify_response(response, *, server, request_digest)`: missing signature → error; `response.server != server` → "wrong server"; verify with `self._keyring.get(server)` (the key of the server the client dialled, never `response.server`'s choice); then `request_digest` must equal (`hmac.compare_digest`) → else "request digest mismatch". `_private_key_for_tests()` is defined only on the fixture subclass in `tests/rpc_identity_fixtures.py`; production `ServiceIdentity` keeps the key in a name-mangled slot and its `repr` shows only `principal` and key id.

Server `_handle_request`: compute `digest = request_digest(raw)` first; on decode failure reply with `request_digest=""`; call `caller = self.identity.verify_request(request, role=self.socket_role)` where `verify` was; every reply passes `server=self.identity.principal, request_digest=digest`. Log `principal`, `on_behalf_of`, `method`, `request_id` at DEBUG for every dispatch. Client `call`: `payload = canonical_json(request.model_dump(mode="json"))`, `digest = request_digest(payload)`, `self.identity.verify_response(reply, server=self.server, request_digest=digest)` in place of `verify_response(reply)`; the reset-on-failure path is unchanged.

Delete `HmacServiceAuthenticator`, `_digest`, `_response_digest`, `load_service_hmac_key`, `ServiceHmacKeyError`, `REQUIRED_KEY_FILE_MODE`, `MIN_KEY_BYTES`. Rewrite the module docstring's "Response signing" and security notes for Ed25519 (keep points 1, 2, 4–7; point 3 becomes "Ed25519 verify, no secret comparison").

- [ ] **Step 4: Rewire the builders** (each builds one identity per process and reuses it):

| Site | Today | After |
|---|---|---|
| `trading_runtime.py:471-472` | `HmacServiceAuthenticator(load_service_hmac_key(...))` | `self.rpc_identity = ServiceIdentity.load("trader", self.rpc_keys_dir or None)`; constructor param `service_hmac_key_file` (:93, :149) → `rpc_keys_dir: str = ''` |
| `production_api.py:2125` | `isinstance(authenticator, HmacServiceAuthenticator)` | `isinstance(identity, ServiceIdentity) and identity.principal == "trader"` |
| `command_stack.py:108-116, 152-185` | key-file probing; swallows load errors and returns `(None, None)` | `_strategy_control_credentials_ready` → `getattr(trader, "rpc_identity", None) is not None`; `_connect_strategy_control_port` uses `trader.rpc_identity`, `server="strategy"`; no `except Exception: return None, None` (fail closed) |
| `paper_hot_arm.py:153-180` | builds its own HMAC authenticator | uses `self._trader.rpc_identity`, `server="strategy"` |
| `strategy_runtime.py:595-630` | one HMAC authenticator for servers and trader clients | `ServiceIdentity.load("strategy", self.rpc_keys_dir or None)`; clients `server="trader"`; param `service_hmac_key_file` (:417, :470) → `rpc_keys_dir` |
| `sdk.py:221-224, 287-325` | `MMR_SERVICE_HMAC_KEY_FILE` | `self._rpc_principal = os.getenv("MMR_RPC_PRINCIPAL", "cli")`, must be in `CLIENT_PRINCIPALS` (else `ValueError` at construction); identity loaded lazily in `_ensure_typed_clients` (unchanged laziness: commands that need no service still need no key); trader clients `server="trader"`, strategy clients `server="strategy"` |
| `web/trader_link.py:46,60-92` | `build_authenticator(env)` | `build_identity(env) -> ServiceIdentity.load("dashboard", env.get("MMR_RPC_KEYS_DIR") or None)`; `connect_client(role, endpoint, *, server, identity=None, ...)`; delete `_DEFAULT_HMAC_KEY_PATH` |
| `web/manage_client.py:31,57-61` | default HMAC path | `build_identity(self._env)`; trader buckets `server="trader"`, strategy buckets `server="strategy"` |
| `web/command_center/gateway.py:197` | `build_authenticator(env)` | `build_identity(env)`, `server="trader"` |
| `config.py:103-117,208` | `service_hmac_key_file` field and flat key | removed; config load logs one WARNING when `service_hmac_key_file` is non-empty in YAML or `MMR_SERVICE_HMAC_KEY_FILE` is set: "retired by the Ed25519 cutover; ignored. Remove it and delete service_hmac.key." |

  `config_defaults/trader.yaml:79-99`: replace the `service_hmac_key_file` block with a comment pointing at `mmr keys init` and `~/.config/mmr/keys/rpc/`.

- [ ] **Step 5: Migrate the test files.** Replace each `HmacServiceAuthenticator(KEY, now=...)` with `make_identities(now=...)[principal]` (servers use `"trader"` / `"strategy"`, clients `"cli"` unless the test is about another caller) and add `server=` to every `TypedRpcClient(...)`. `test_production_rpc_security.py:174` becomes `test_build_production_registry_rejects_a_non_trader_identity`. `test_config.py` HMAC-field tests become `test_retired_service_hmac_key_file_is_ignored_with_a_warning`. Tests whose subject was the HMAC key file (`load_service_hmac_key` cases in `test_typed_rpc.py`) are deleted; Task 1 covers the new key files. Until Task 4, a registry built without an allow-list is not enforced yet, so these tests need no allow-list.

- [ ] **Step 6: Run** `tests/test_typed_rpc_identity.py`, the migrated files, then the full suite. Then `grep -rn "Hmac\|service_hmac\|SERVICE_HMAC\|MIN_KEY_BYTES\|REQUIRED_KEY_FILE_MODE\|ServiceHmacKeyError" trader web scripts config_defaults tests` must print only the config-retirement warning and its test.
- [ ] **Step 7: Commit** — `feat!: replace typed rpc hmac with ed25519 service identities`. Body: hard cutover, no dual mode (spec 5.3), the envelope fields, the HMAC config retirement.

---

### Task 4: Allow-list, `PERMISSION_DENIED` and the caller in handlers

**Files:**
- Modify: `trader/messaging/principals.py` (tables), `trader/messaging/typed_rpc.py` (`TypedRpcRegistration` :647, `TypedRpcRegistry` :658-743, server dispatch), `trader/messaging/production_api.py:2133` and `TypedStrategyControlPort.forward` :2038-2047, `trader/strategy/strategy_runtime.py:597-598`, `scripts/command_plane_drill.py`
- Test: `tests/test_rpc_acl.py` (new); update registry constructions in the 16 test files that call `TypedRpcRegistry(` and dispatch through a server

**Interfaces:**
- Consumes: Task 3 `RpcCaller`, `ServiceIdentity`.
- Produces:
  - `principals.TRADER_ACL`, `principals.STRATEGY_ACL: Mapping[tuple[str, str], frozenset[str]]` keyed by `(role, method)`.
  - `TypedRpcRegistry(*, acl: Mapping[tuple[str,str], frozenset[str]] | None = None, default_execution=...)`. `register(..., with_caller: bool = False)`. With an `acl`, registering a `(role, method)` missing from it raises `ValueError("… has no allow-list entry in principals …")`. Without an `acl`, every method's allowed set is empty.
  - `TypedRpcRegistration.allowed_principals: frozenset[str]`, `.with_caller: bool`. A `with_caller` handler is called `handler(parsed_body, caller)`.
  - Problem code `PERMISSION_DENIED`.

**The tables** (groups first, then entries). Every method below is registered today (enumerated with `grep -rn -A2 "\.register(" trader web`); a later plan adds its own methods.

```python
HUMAN = frozenset({"cli", "dashboard"})
ACCOUNT_READERS = HUMAN | {"ai_supervisor"}
MARKET_READERS = HUMAN | {"ai_supervisor", "ai_research"}

_TRADER_ACCOUNT_READS = ("get_status", "get_account_values", "get_portfolio_summary", "get_positions",
    "get_open_orders", "get_trades", "get_risk_limits", "get_ib_account", "get_fx_rates",
    "get_account_cash_by_currency", "get_command", "get_proposal", "get_paper_automation_status",
    "diagnose_portfolio_feed", "snapshot_with_cursor")
_TRADER_MARKET_READS = ("get_snapshot", "get_snapshots_batch", "get_market_depth",
    "get_published_contracts", "list_universes", "get_universe", "scanner_locations", "scan_ideas")

TRADER_ACL = {
    **{("query", m): ACCOUNT_READERS for m in _TRADER_ACCOUNT_READS},
    **{("query", m): MARKET_READERS for m in _TRADER_MARKET_READS},
    ("query", "resolve_instrument"): MARKET_READERS | {"strategy"},
    ("query", "discover_instrument"): MARKET_READERS | {"strategy"},
    ("query", "publish_instrument"): HUMAN | {"strategy"},
    ("query", "get_trading_control"): ACCOUNT_READERS | {"strategy"},
    ("query", "list_proposals"): ACCOUNT_READERS | {"strategy"},
    ("query", "reconcile_with_broker"): HUMAN,
    ("feed", "read_domain_events"): HUMAN | {"strategy"},
    ("command", "create_proposal"): HUMAN | {"strategy"},
    ("command", "execute_automated_intent"): frozenset({"strategy"}),
    ("command", "record_state_acknowledged"): frozenset({"strategy"}),
    ("command", "pause_trading"): HUMAN | {"ai_supervisor"},
    # Owner answer 5 (ruling 18): activation is cli only. dashboard keeps only the
    # risk-reducing deactivate_live_canary / suspend_allocation (in the HUMAN group below).
    ("command", "activate_live_canary"): frozenset({"cli"}),
    ("command", "activate_allocation"): frozenset({"cli"}),
    **{("command", m): HUMAN for m in (
        "approve_proposal", "reject_proposal", "cancel_order", "cancel_orders", "resume_trading",
        "preflight_command", "liquidate_account", "enable_strategy", "disable_strategy",
        "update_strategy_params", "deactivate_live_canary", "suspend_allocation",
        "activate_paper_automation",
        "deactivate_paper_automation", "create_universe", "delete_universe",
        "add_universe_symbols", "remove_universe_symbol", "import_universe_csv")},
}
STRATEGY_ACL = {
    **{("command", m): frozenset({"trader"}) for m in (
        "enable_strategy", "disable_strategy", "update_strategy_params",
        "arm_paper_automation", "disarm_paper_automation")},
    ("query", "get_strategy_receipt"): frozenset({"trader"}),
    ("query", "get_paper_automation_arm"): frozenset({"trader"}),
    ("query", "list_strategies"): HUMAN | {"trader"},
    ("command", "reload_strategies"): HUMAN | {"trader"},
    ("command", "enable_strategy_by_name"): HUMAN,
    ("command", "disable_strategy_by_name"): HUMAN,
}
```

  Step 3 below cross-checks each entry against its caller; Task 7 proves every edge over sockets. `scheduler` appears in no entry (ruling 5), `activate_live_canary` and `activate_allocation` list only `cli` (ruling 18); the trust-matrix edges for strategy control keep both direct callers and `trader` (ruling 17).

- [ ] **Step 1: Write the failing tests** (`tests/test_rpc_acl.py`)

```python
def test_registry_with_acl_refuses_a_method_without_an_entry():
    reg = TypedRpcRegistry(acl={("query", "a"): frozenset({"cli"})})
    with pytest.raises(ValueError, match="allow-list"):
        reg.register("query", "b", dict, dict, lambda b: {})

def test_registry_without_acl_denies_everyone(served):          # served: real trader server fixture
    served.registry.register("query", "ping", dict, dict, lambda b: {"ok": 1})
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("cli").call("ping", {}, dict)
    assert exc.value.code == "PERMISSION_DENIED"

def test_principal_outside_the_allow_list_is_denied_and_logged(served_with_acl, caplog):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served_with_acl.client("ai_research").call("get_positions", {}, dict)
    assert exc.value.code == "PERMISSION_DENIED"
    assert "ai_research" in caplog.text and "get_positions" in caplog.text

def test_unregistered_method_is_method_not_allowed_not_permission_denied(served_with_acl):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served_with_acl.client("cli").call("no_such_method", {}, dict)
    assert exc.value.code == "METHOD_NOT_ALLOWED"

def test_denied_request_still_burns_its_nonce(served_with_acl):
    req = served_with_acl.signed("ai_research", "get_positions")
    assert served_with_acl.send_raw(req).problem.code == "PERMISSION_DENIED"
    assert served_with_acl.send_raw(req).problem.code == "REPLAY_ERROR"

def test_with_caller_handler_receives_the_authenticated_caller(served_with_acl):
    seen = []
    served_with_acl.registry.register("query", "whoami", dict, dict,
                                      lambda b, caller: seen.append(caller) or {}, with_caller=True)
    served_with_acl.client("dashboard").call("whoami", {}, dict)
    assert seen == [RpcCaller("dashboard", None)]

def test_scheduler_is_not_a_principal_and_appears_in_no_acl():
    assert "scheduler" not in principals.KNOWN_PRINCIPALS and "scheduler" in principals.RESERVED_PRINCIPALS
    for table in (TRADER_ACL, STRATEGY_ACL):
        assert all("scheduler" not in allowed for allowed in table.values())

@pytest.mark.parametrize("method", ["activate_live_canary", "activate_allocation"])
def test_activation_is_cli_only(served_with_acl, method):
    assert TRADER_ACL[("command", method)] == frozenset({"cli"})
    for principal in ("dashboard", "strategy", "ai_supervisor", "ai_research"):
        with pytest.raises(TypedRpcRemoteError) as exc:
            served_with_acl.client(principal).call(method, ACTIVATION_BODY[method], dict)
        assert exc.value.code == "PERMISSION_DENIED", (principal, method)

@pytest.mark.parametrize("method", ["deactivate_live_canary", "suspend_allocation"])
def test_dashboard_may_still_deactivate_and_suspend(served_with_acl, method):
    served_with_acl.client("dashboard").call(method, DEACTIVATION_BODY[method], dict)   # reaches the handler

def test_dashboard_may_read_status(served_with_acl):
    served_with_acl.client("dashboard").call("get_paper_automation_status", {}, dict)

def test_every_production_trader_method_has_an_entry():
    reg = build_full_production_registry()        # helper: command_stack fixtures from tests/test_command_stack.py
    assert {(r.socket_role, r.method) for r in reg.registrations()} <= set(TRADER_ACL)

def test_every_strategy_method_has_an_entry():
    cmd, qry = TypedRpcRegistry(acl=STRATEGY_ACL), TypedRpcRegistry(acl=STRATEGY_ACL)
    register_strategy_control_authority(cmd, qry, _FakeRuntime())   # raises if one is missing

def test_forwarded_call_is_authorized_as_trader_not_on_behalf_of(strategy_served):
    # dashboard may not call enable_strategy on the strategy service directly ...
    with pytest.raises(TypedRpcRemoteError) as exc:
        strategy_served.client("dashboard").call("enable_strategy", ENABLE_BODY, dict)
    assert exc.value.code == "PERMISSION_DENIED"
    # ... the trader may, and on_behalf_of only reaches the log
    port = TypedStrategyControlPort(strategy_served.client("trader", "command"),
                                    strategy_served.client("trader", "query"))
    port.forward(strategy_control_cmd("enable_strategy", source="dashboard"))  # helper in tests/test_command_stack.py style
    assert strategy_served.last_caller == RpcCaller("trader", "dashboard")
```

  (`registrations()` is a new read-only iterator on `TypedRpcRegistry`. `CommandRequest(principal=...)` arrives in Task 5; in this task the forward uses `request.source` for `on_behalf_of` and the test builds the request without `principal`. Task 5 switches the forward to `request.principal` only and updates this test to build the request with `principal="dashboard"`.)

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - Registry: store `acl`; in `register`, `allowed = acl[(role, method)]` when `acl` is set (missing → `ValueError`), else `frozenset()`. Add `registrations()`.
  - Server dispatch, after `resolve` returns a registration and before body validation: `if caller.principal not in registration.allowed_principals: logging.warning("typed rpc PERMISSION_DENIED principal=%s method=%s request_id=%s", ...); raise _DispatchProblem("PERMISSION_DENIED", f"principal {caller.principal!r} may not call {method!r}")`. Pass `caller` as a second argument when `with_caller`.
  - `build_production_registry` (:2133): `TypedRpcRegistry(acl=TRADER_ACL, default_execution="thread")`. `strategy_runtime.py:597-598`: `acl=STRATEGY_ACL`. `scripts/command_plane_drill.py`: `acl=TRADER_ACL`.
  - `TypedStrategyControlPort.forward`: `self._command_client.call(request.action, body, dict, on_behalf_of=request.source)`.
  - Cross-check each table entry against its caller before committing: `grep -rn "call(\s*['\"]<method>" trader web` and the helper wrappers (`sdk.py`, `web/command_center/*.py`, `web/manage_client.py`, `trader/strategy/*.py`, `trader/automation/paper_hot_arm.py`). A caller that is not in the entry is a defect in the table, not in the caller; fix the table and say so in the commit body.
  - Tests that dispatch through a real server with an ad-hoc registry: build it with an explicit `acl={...}` naming the client principal they use.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: enforce per-method rpc allow-list and pass the caller to handlers`.

---

### Task 5: Authorization from the principal, never from the body

**Files:**
- Modify: `trader/trading/command_coordinator.py:437-470` (`CommandRequest.principal`), `:1275`, `:1565-1572` (live approve gate); `trader/automation/automated_intent_command.py:31-32,175`; `trader/messaging/production_api.py:513-520` (`CreateProposalRequest.source` doc), `:635-648` (`ApproveProposalRequest` drops `source`), `:1020-1035` (`_create_proposal_action`), `:1056-1069`, `:1089-1105`, `:1110-1128`, `:1170-1190` (`_strategy_control_rpc_handler`), `:1822-1830`, `:1861-1870` and `:1912` (register the create/approve/execute-intent and the three strategy-control handlers `with_caller=True`), `TypedStrategyControlPort.forward`; `trader/sdk.py:1716`; `web/command_center/routes_commands.py:582`
- Test: `tests/test_rpc_principal_authority.py` (new); adjust `tests/test_approval_command.py`, `tests/automation/test_automated_command_boundary.py`, `tests/test_propose_approve_integration.py` where they set `source=`

**Interfaces:**
- Consumes: Task 4 `with_caller`, `RpcCaller`.
- Produces: `CommandRequest.principal: Optional[str] = None` (not in `canonical_request_hash`); `automated_intent_command.STRATEGY_PRINCIPAL = "strategy"`; `command_coordinator.LIVE_APPROVE_PRINCIPAL = "dashboard"`; `production_api.attribution_label(principal: str, label: str) -> str` (raises `_DispatchProblem("PERMISSION_DENIED", …)`).

- [ ] **Step 1: Write the failing tests**

```python
def test_execute_automated_intent_source_and_principal_come_from_the_key(trader_served, intent_body):
    trader_served.client("strategy").call("execute_automated_intent", intent_body, dict)
    cmd = trader_served.coordinator.last_request
    assert (cmd.principal, cmd.source) == ("strategy", "strategy")

def test_automated_intent_service_refuses_a_non_strategy_principal():
    receipt = service.execute(dataclasses.replace(INTENT_CMD, principal="cli", source="strategy"))
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"

def test_approve_body_source_is_rejected(trader_served):
    with pytest.raises(TypedRpcRemoteError) as exc:
        trader_served.client("cli").call("approve_proposal", {**APPROVE_BODY, "source": "dashboard"}, dict)
    assert exc.value.code == "VALIDATION_ERROR"

@pytest.mark.parametrize("principal,allowed", [("dashboard", True), ("cli", False)])
def test_live_approval_requires_the_dashboard_principal(live_coordinator, principal, allowed):
    receipt = live_coordinator.execute(approve_cmd(principal=principal, source=principal))
    assert (receipt.error_code != "LLM_LIVE_APPROVE_FORBIDDEN") is allowed

def test_live_approval_with_no_principal_is_refused(live_coordinator):
    assert live_coordinator.execute(approve_cmd(principal=None, source="dashboard")).error_code == "LLM_LIVE_APPROVE_FORBIDDEN"

@pytest.mark.parametrize("principal,label,expected", [
    ("strategy", "strategy:momentum", "strategy:momentum"),
    ("cli", "", "cli"), ("cli", "llm", "llm"), ("dashboard", "", "dashboard"),
])
def test_create_proposal_attribution(trader_served, principal, label, expected):
    trader_served.client(principal).call("create_proposal", {**PROPOSAL_BODY, "source": label}, dict)
    cmd = trader_served.coordinator.last_request
    assert (cmd.principal, cmd.source) == (principal, expected)

@pytest.mark.parametrize("principal,label", [("cli", "strategy:momentum"), ("strategy", ""),
                                             ("strategy", "llm"), ("dashboard", "strategy:x")])
def test_create_proposal_label_cannot_impersonate(trader_served, principal, label):
    with pytest.raises(TypedRpcRemoteError) as exc:
        trader_served.client(principal).call("create_proposal", {**PROPOSAL_BODY, "source": label}, dict)
    assert exc.value.code == "PERMISSION_DENIED"

def test_cli_activation_still_requires_the_signed_attestation_and_preflight(trader_served):
    # Passing the allow-list is not enough: the existing checks run unchanged.
    receipt = trader_served.client("cli").call("activate_live_canary", UNSIGNED_ACTIVATION_BODY, dict)
    assert receipt["status"] != "EXECUTED" and receipt["error_code"]      # same rejection tests/promotion/test_canary_activation.py pins
    # a preflight-less activation is refused the same way as before this plan

AI_FORBIDDEN = ["approve_proposal", "execute_automated_intent", "liquidate_account", "resume_trading",
                "activate_live_canary", "activate_allocation", "activate_paper_automation",
                "create_proposal", "cancel_order"]
NOT_ON_TYPED_SURFACE = ["place_standalone_order", "set_risk_limits", "buy", "sell"]

@pytest.mark.parametrize("principal", ["ai_supervisor", "ai_research"])
def test_ai_principals_cannot_trade_or_set_limits(trader_served, principal):
    for method in AI_FORBIDDEN:
        assert _code(trader_served, principal, "command", method) == "PERMISSION_DENIED", method
    for method in NOT_ON_TYPED_SURFACE:
        assert _code(trader_served, principal, "command", method) == "METHOD_NOT_ALLOWED", method
```

  (`trader_served` builds the real `build_production_registry` with the command-stack fixtures from `tests/test_command_stack.py` and a recording coordinator; `_code` returns the `TypedRpcRemoteError.code`.)

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - `CommandRequest`: add `principal: Optional[str] = None` after `session_fingerprint`. Do not add it to `canonical_request_hash` or the ledger insert.
  - Live gate (:1568-1572): `if self._account_mode == "live" and cmd.principal != LIVE_APPROVE_PRINCIPAL: return reject("LLM_LIVE_APPROVE_FORBIDDEN", False)`. Delete `NON_HUMAN_APPROVE_SOURCES` and fix its docstring reference at :1301.
  - `automated_intent_command.py:175`: `if cmd.principal != STRATEGY_PRINCIPAL:`; `STRATEGY_PRINCIPAL = "strategy"`.
  - Handlers become `with_caller=True`:

```python
def attribution_label(principal: str, label: str) -> str:
    label = (label or "").strip()
    if principal == "strategy":
        if not re.fullmatch(r"strategy:[A-Za-z0-9_.-]+", label):
            raise _DispatchProblem("PERMISSION_DENIED", "strategy proposals must be labelled strategy:<name>")
        return label
    if label.startswith("strategy:"):
        raise _DispatchProblem("PERMISSION_DENIED", "only the strategy principal may use a strategy: label")
    return label or principal

def _create_proposal_rpc_handler(coordinator, account_id):
    def _handler(parsed: CreateProposalRequest, caller: RpcCaller) -> Dict[str, Any]:
        label = attribution_label(caller.principal, parsed.source)
        payload = {**parsed.model_dump(exclude={"command_id", "preflight_nonce"}), "source": label}
        request = CommandRequest(command_id=parsed.command_id, action="create_proposal",
            account_id=account_id, target_type="proposal", target_id="", expected_version=None,
            body=payload, source=label, principal=caller.principal,
            preflight_nonce=parsed.preflight_nonce)
        return _receipt_to_dict(coordinator.execute(request))
    return _handler
```

  `_create_proposal_action` keeps `source=command.source` (the label). Approve: `source=caller.principal, principal=caller.principal`. Execute-intent: `source=caller.principal, principal=caller.principal`. `_strategy_control_rpc_handler` (enable/disable/update_strategy_params on the trader): keeps `source="dashboard"` (a label) and sets `principal=caller.principal`, so the forward to the strategy service carries the real caller as `on_behalf_of`. Test: `test_strategy_control_forward_logs_the_real_caller` — `cli` calls trader `enable_strategy`; the strategy server sees `RpcCaller("trader", "cli")`.
  - `ApproveProposalRequest`: delete `source` and its doc lines (:635-636). `sdk.py:1716` and `web/command_center/routes_commands.py:582` stop sending it.
  - `TypedStrategyControlPort.forward`: `on_behalf_of = request.principal if is_valid_principal_name(request.principal) else None`. A label such as `operator` or `dashboard` from `source` is no longer forwarded; only the authenticated principal is.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: derive command authority from the rpc principal`.

---

### Task 6: Container mounts, `docker.sh`, `start_mmr.sh`, docs

**Files:**
- Modify: `docker-compose.yml` (services `data` :165, `trader` :192, `strategy` :272, `dashboard` :358, `scheduler` :438, new one-shot `cli` and `keygen`; drop `MMR_SERVICE_HMAC_KEY_FILE` at :219, :310, :396 and their comments), `docker.sh:494-548` and the option parser (new `-k [--rotate P]`; `_read_service_hmac_key_file`, `_write_portable_service_hmac_key_file`, `ensure_split_config`), `start_mmr.sh:650-706`, `trader/operations/health.py:45-55` (`_SECRET_MARKERS` += `"rpc_key"`, so a health payload never echoes an RPC key path), `CLAUDE.md` (Typed HMAC sections, ports table, `.env` paragraph), `docs/OPERATIONAL_STATE.md:40,168,283` (strategy commands move to the `cli` container; add the key rotation, recovery, backup and HMAC retirement runbook)
- Test: `tests/test_compose_rpc_keys.py` (new), `tests/test_scheduler_acl.py` (new), `tests/test_docker_helper.py:179,199` (replace), `tests/fullstack/test_rpc_key_mounts.py` (new, test profile only)

**Interfaces:**
- Consumes: `principals.peers_for`, `SERVER_PRINCIPALS`.
- Produces: `principals.SERVICE_PRINCIPAL: Mapping[str, str | None]` = `{"trader": "trader", "strategy": "strategy", "dashboard": "dashboard", "cli": "cli", "scheduler": None, "data": None}` (compose service → principal; `None` = no key, `tmpfs` only).

Mount pattern for one service (trader shown; the others follow `peers_for`):

```yaml
    volumes:
      - ${HOME}/.config/mmr:/home/trader/.config/mmr
      # Hide every RPC key, then mount only this principal's private key and the
      # public keys it needs (spec 5.3: no container mounts another principal's key).
      - type: tmpfs
        target: /home/trader/.config/mmr/keys/rpc
        tmpfs: {size: 65536, mode: 0755}
      - ${HOME}/.config/mmr/keys/rpc/trader.key:/home/trader/.config/mmr/keys/rpc/trader.key:ro
      - ${HOME}/.config/mmr/keys/rpc/strategy.pub:/home/trader/.config/mmr/keys/rpc/strategy.pub:ro
      - ${HOME}/.config/mmr/keys/rpc/cli.pub:/home/trader/.config/mmr/keys/rpc/cli.pub:ro
      - ${HOME}/.config/mmr/keys/rpc/dashboard.pub:/home/trader/.config/mmr/keys/rpc/dashboard.pub:ro
      - ${HOME}/.config/mmr/keys/rpc/ai_supervisor.pub:/home/trader/.config/mmr/keys/rpc/ai_supervisor.pub:ro
      - ${HOME}/.config/mmr/keys/rpc/ai_research.pub:/home/trader/.config/mmr/keys/rpc/ai_research.pub:ro
```

`data` and `scheduler` get only the `tmpfs` (no keys; ruling 5). The trader container's mounts do **not** include `cli.key` (only `cli.pub`).

New one-shot service (ruling 15), same `<<: *mmr-hardening` but `restart: "no"`, `profiles: ["tools"]`, `entrypoint: ["python", "-m", "trader.mmr_cli"]`, no `depends_on`, no published ports. Its `environment` copies the dashboard's typed-address variables for trader and strategy (`TRADER_TYPED_ADDRESS: tcp://trader` and the strategy equivalent; the implementer copies the exact names from the `dashboard` block). Mounts: `~/.config/mmr`, the `tmpfs` overlay, `cli.key`, `trader.pub`, `strategy.pub` (all `:ro`). Use: `docker compose run --rm cli strategies`. `docker compose up` never starts it (profile), so no long-lived container holds `cli.key`. New one-shot service `keygen` (ruling 15): `<<: *mmr-hardening`, `restart: "no"`, `profiles: ["tools"]`, `network_mode: none`, `entrypoint: ["python", "-m", "trader.mmr_cli", "keys"]`, `environment: {MMR_KEYGEN_CONTAINER: "1", MMR_RPC_KEYS_DIR: /keys}`, one volume `${HOME}/.config/mmr/keys/rpc:/keys` (read-write; no `~/.config/mmr` mount, no `tmpfs`). `docker.sh -k` runs `docker compose run --rm --no-deps --user "$(id -u):$(id -g)" keygen init [--rotate P]`. It is the only service that mounts the keys directory writable, and the only mount of the whole directory.

`ib-gateway` and `fullstack-tests` do not mount `~/.config/mmr` and need nothing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_compose_rpc_keys.py
def _visible_rpc_files(service: dict) -> set[str] | None:
    """Files under keys/rpc a container can read, or None if it sees the whole host dir."""
    ...  # parse short + long volume syntax; host ~/.config/mmr mount without a tmpfs over
         # keys/rpc → None; else the basenames of binds whose target is under keys/rpc

@pytest.mark.parametrize("name,principal", SERVICE_PRINCIPAL.items())
def test_each_service_sees_only_its_own_private_key_and_needed_public_keys(compose, name, principal):
    files = _visible_rpc_files(compose["services"][name])
    expected = set() if principal is None else {f"{principal}.key"} | {f"{p}.pub" for p in peers_for(principal)}
    assert files == expected

def test_no_service_mounts_another_principals_private_key(compose):
    for name, svc in compose["services"].items():
        files = _visible_rpc_files(svc)
        assert files is not None, f"{name} sees the whole keys/rpc directory"
        own = SERVICE_PRINCIPAL.get(name)
        assert {f for f in files if f.endswith(".key")} <= ({f"{own}.key"} if own else set()), name

def test_every_key_bind_is_read_only(compose): ...

def test_cli_private_key_is_mounted_only_in_the_short_lived_cli_service(compose):
    holders = [n for n, svc in compose["services"].items() if "cli.key" in (_visible_rpc_files(svc) or set())]
    assert holders == ["cli"]       # keygen mounts the directory itself, checked below

def test_keygen_service_is_one_shot_offline_and_the_only_writable_keys_mount(compose):
    keygen = compose["services"]["keygen"]
    assert keygen["profiles"] == ["tools"] and keygen["restart"] == "no" and keygen["network_mode"] == "none"
    assert keygen["environment"]["MMR_KEYGEN_CONTAINER"] == "1"
    writable = [n for n, svc in compose["services"].items()
                if any("keys/rpc" in str(v) and not str(v).endswith(":ro") and "tmpfs" not in str(v) for v in svc.get("volumes", []))]
    assert writable == ["keygen"]
    cli = compose["services"]["cli"]
    assert cli.get("profiles") == ["tools"] and cli.get("restart") == "no" and not cli.get("ports")

def test_trader_container_has_cli_pub_but_never_cli_key(compose):
    files = _visible_rpc_files(compose["services"]["trader"])
    assert "cli.pub" in files and "cli.key" not in files

def test_runbook_never_execs_the_cli_in_the_trader_container():
    text = Path("docs/OPERATIONAL_STATE.md").read_text()
    assert "exec trader python -m trader.mmr_cli" not in text and "docker compose run --rm cli" in text

def test_no_service_references_the_retired_hmac_key(compose_text):
    assert "service_hmac" not in compose_text and "MMR_SERVICE_HMAC_KEY_FILE" not in compose_text
```

```python
# tests/test_docker_helper.py (replaces :179 and :199)
def test_k_runs_keygen_in_the_one_shot_container_as_the_host_user(fake_docker):
    result = fake_docker.run("-k")
    call = fake_docker.compose_calls[-1]
    assert result.returncode == 0 and call[:3] == ["run", "--rm", "--no-deps"] and "keygen" in call and "init" in call
    assert f"{os.getuid()}:{os.getgid()}" in call
    assert stat.S_IMODE(os.lstat(fake_docker.home / ".config/mmr/keys/rpc").st_mode) == 0o700   # created by docker.sh, not by Docker

def test_k_rotate_passes_the_principal_and_prints_the_restart_list(fake_docker):
    write_keyset(fake_docker.home / ".config/mmr/keys/rpc")
    result = fake_docker.run("-k", "--rotate", "dashboard")
    assert fake_docker.compose_calls[-1][-2:] == ["--rotate", "dashboard"] and "restart" in result.stdout

def test_k_rejects_an_unknown_principal_before_any_docker_call(fake_docker):
    assert fake_docker.run("-k", "--rotate", "../x").returncode != 0 and not fake_docker.compose_calls

def test_up_refuses_when_an_rpc_key_is_missing(fake_docker):
    write_keyset(fake_docker.home / ".config/mmr/keys/rpc"); (fake_docker.home / ".config/mmr/keys/rpc/strategy.pub").unlink()
    result = fake_docker.run("-u")
    assert result.returncode != 0 and "mmr keys init" in result.stdout and not fake_docker.compose_calls

def test_up_never_deletes_an_existing_hmac_key_and_reminds_the_operator(fake_docker):
    write_keyset(...); key = fake_docker.home / ".config/mmr/service_hmac.key"; key.write_text("old")
    result = fake_docker.run("-u")
    assert result.returncode == 0 and key.read_text() == "old" and "delete it when the cutover is verified" in result.stdout
    assert "rm " not in Path("docker.sh").read_text().split("_require_rpc_keys")[1].split("}")[0]   # the helper itself never removes anything

def test_db_backup_helper_excludes_the_rpc_key_directory(fake_docker):
    write_keyset(...); fake_docker.run("-B", "t")
    assert not list(fake_docker.backup_dir.rglob("*.key")) and not list(fake_docker.backup_dir.rglob("*.pub"))

def test_up_no_longer_provisions_an_hmac_key_and_strips_the_yaml_line(fake_docker):
    write_keyset(...); fake_docker.trader_yaml.write_text("service_hmac_key_file: ~/.config/mmr/service_hmac.key\n")
    assert fake_docker.run("-u").returncode == 0
    assert not (fake_docker.home / ".config/mmr/service_hmac.key").exists()
    assert "service_hmac_key_file" not in fake_docker.trader_yaml.read_text()
    assert fake_docker.trader_yaml.with_suffix(".yaml.bak").exists()
```

```python
# tests/fullstack/test_rpc_key_mounts.py  (profile "test"; runs inside fullstack-tests with docker.sock)
@pytest.mark.parametrize("service,principal", [("trader","trader"), ("strategy","strategy"),
                                               ("dashboard","dashboard"), ("scheduler",None), ("data",None)])
def test_container_lists_only_its_keys(docker_client, service, principal):
    out = _exec(docker_client, service, ["ls", "/home/trader/.config/mmr/keys/rpc"])
    expected = set() if principal is None else {f"{principal}.key"} | {f"{p}.pub" for p in peers_for(principal)}
    assert set(out.split()) == expected
```

```python
# tests/test_scheduler_acl.py  (owner answer 3: the scheduler ACL may not grow beyond what the listed jobs need)
import yaml
from trader.messaging import principals
from trader.messaging.principals import TRADER_ACL, STRATEGY_ACL

# Every pycron job and the typed methods it needs. Adding a job without a row fails the first test.
SCHEDULED_JOB_RPC_NEEDS = {
    "data_refresh_us": frozenset(),    # data refresh: get_status probe + resolve fallback are soft (mmr_cli.py:8840,8872)
    "data_refresh_asx": frozenset(),
    "db_backup": frozenset(),          # data backup: local files only
}
SCHEDULER_ACL_ALLOWED = frozenset().union(*SCHEDULED_JOB_RPC_NEEDS.values())

def _pycron_jobs():
    cfg = yaml.safe_load(Path("config_defaults/pycron.yaml").read_text())
    return {j["name"]: j for j in cfg["jobs"]}      # implementer: use the real top-level key of that file

def test_every_scheduled_job_is_classified():
    assert set(_pycron_jobs()) == set(SCHEDULED_JOB_RPC_NEEDS)

def test_scheduler_acl_never_grows_beyond_the_listed_jobs():
    granted = {m for (_, m), who in {**TRADER_ACL, **STRATEGY_ACL}.items() if "scheduler" in who}
    assert granted <= SCHEDULER_ACL_ALLOWED
    assert "scheduler" not in principals.KNOWN_PRINCIPALS

def test_scheduler_container_has_no_rpc_keys(compose):
    assert _visible_rpc_files(compose["services"]["scheduler"]) == set()

def test_data_refresh_survives_with_no_trader_and_no_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("MMR_RPC_PRINCIPAL", raising=False)         # empty keys dir (conftest fixture)
    summary = run_data_download_for_known_local_symbol(tmp_path)    # helper in the test: symbol already in the local universe DB
    assert summary["failed"] == 0                                   # probe failed softly, local resolution sufficed
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - Compose: the mount block above per service, generated by hand from `peers_for` (the static test pins it). Remove the HMAC env lines and their comments; add one comment block near `x-mmr-hardening` explaining the overlay and the "missing file becomes a directory" trap.
  - `docker.sh`: delete `_read_service_hmac_key_file` / `_write_portable_service_hmac_key_file` and the provisioning branch of `ensure_split_config`. Add `_require_rpc_keys`: derive the required host files from `docker-compose.yml` itself (`grep -oE '\$\{HOME\}/\.config/mmr/keys/rpc/[a-z_]+\.(key|pub)'`, no second principal list; the `cli` service's files count too, so `-u` fails early if `cli.key` is missing), and refuse `-u` if any is missing or not a regular file, printing `Run ./docker.sh -k`. If `trader.yaml` has a `service_hmac_key_file:` line, remove it with `sed -i.bak`. If `~/.config/mmr/service_hmac.key` exists, print one line: "retired HMAC key left in place; delete it when the cutover is verified".
  - `start_mmr.sh:650-706`: replace `ensure_service_hmac_key` with `ensure_rpc_keys`, which runs `./docker.sh -k` (idempotent) when Docker is available, else `python3 -m trader.mmr_cli keys init` — ruling 16.
  - Docs: in `CLAUDE.md` replace "Typed HMAC RPC" wording with "Typed Ed25519 RPC (per-principal keys, allow-list in `trader/messaging/principals.py`)", the ports table protocol column, the `.env`/`service_hmac.key` sentence (now `~/.config/mmr/keys/rpc/`, `mmr keys init`), and add `./docker.sh -k [--rotate P]` and `mmr keys init|backup|restore` to the CLI command list (`-k` is the documented keygen path). In `docs/OPERATIONAL_STATE.md` replace `docker compose exec trader python -m trader.mmr_cli …` (lines 40 and 283) with `docker compose run --rm cli …` for strategy commands, keep the host `mmr …` form for trader commands over the published ports, and state that exec into `trader` is refused (no `cli` key there). Add a "RPC keys" section: (0) first setup: `./docker.sh -b`, `./docker.sh -k`; **cutover step, owner-run before the first cutover `-u`: `docker compose --profile test run fullstack-tests`** (proves the `tmpfs` + per-file bind overlay, the `cli` and `keygen` services on your Docker Desktop/Podman; abort the cutover on any failure); then `./docker.sh -u`; (1) rotation: `mmr keys backup`, `./docker.sh -k --rotate <p>`, restart every listed service together (in-flight requests fail), run the Task 7 matrix; (2) lost private key or lost host: restore from the encrypted backup, else rotate that principal (all servers that trust it restart together); (3) backup and restore commands and where the age identity must live; (4) HMAC retirement: the checklist and `rm ~/.config/mmr/service_hmac.key` (ruling 14); (5) RPC keys and bundle-signing keys stay separate.
- [ ] **Step 4: Run** the three test files (the fullstack one only with `docker compose --profile test run fullstack-tests`, which needs built images — owner step, not run in CI), then the full suite.
- [ ] **Step 5: Commit** — `feat: mount per-principal rpc keys and retire hmac provisioning`.

---

### Task 7: Split-service trust-matrix round trips (cutover gate)

Spec 5.3: "Before the cutover, a split-service test runs real round trips over every edge of the trust matrix … It also sends a wrong-principal, a tampered, a replayed, a wrong-destination and a legacy-HMAC request on each server, and all must be refused."

**Files:**
- Create: `tests/test_rpc_trust_matrix.py`

**Interfaces:**
- Consumes: everything above. Uses `init_keys` into `tmp_path`, then `ServiceIdentity.load(p, tmp_path)` for every principal (real files, real loaders), the real `build_production_registry` with the command-stack fixtures, and a real `StrategyRuntime`-style pair of registries via `register_strategy_control_authority` with a fake runtime. Servers bind on `127.0.0.1` free ports in a background asyncio loop (pattern: `tests/test_typed_rpc_transport.py:148-200`).

- [ ] **Step 1: Write the tests**

```python
EDGES = [  # (caller, server, role, method) — one real allowed call per trust-matrix edge
    ("cli", "trader", "query", "get_status"),
    ("cli", "strategy", "query", "list_strategies"),
    ("dashboard", "trader", "query", "get_positions"),
    ("dashboard", "strategy", "command", "enable_strategy_by_name"),
    ("cli", "strategy", "command", "enable_strategy_by_name"),            # ruling 17: direct, as themselves
    ("dashboard", "trader", "command", "deactivate_live_canary"),         # ruling 18
    ("strategy", "trader", "query", "resolve_instrument"),
    ("strategy", "trader", "command", "create_proposal"),   # valid_body uses source="strategy:matrix"
    ("trader", "strategy", "command", "enable_strategy"),
    ("trader", "strategy", "query", "get_paper_automation_arm"),
    ("ai_supervisor", "trader", "query", "get_account_values"),
    ("ai_research", "trader", "query", "get_snapshot"),
]

@pytest.mark.parametrize("caller,server,role,method", EDGES)
def test_every_trust_matrix_edge_round_trips(stack, caller, server, role, method):
    stack.client(caller, server, role).call(method, stack.valid_body(method), dict)

NON_EDGES = [("ai_supervisor", "strategy"), ("ai_research", "strategy"), ("strategy", "strategy"),
             ("trader", "trader")]

@pytest.mark.parametrize("caller,server", NON_EDGES)
def test_non_edges_are_refused(stack, caller, server):
    assert stack.raw_code(stack.signed(caller, server, "query", "get_status")) == "AUTHENTICATION_ERROR"

@pytest.mark.parametrize("server", ["trader", "strategy"])
class TestEachServerRefuses:
    def test_wrong_principal(self, stack, server):          # dashboard-signed, claims cli
        assert stack.raw_code(stack.signed("dashboard", server, claim="cli")) == "AUTHENTICATION_ERROR"
    def test_tampered(self, stack, server):
        assert stack.raw_code(stack.signed("cli", server, tamper_body=True)) == "AUTHENTICATION_ERROR"
    def test_replayed(self, stack, server):
        req = stack.signed("cli", server)
        stack.raw_code(req)
        assert stack.raw_code(req) == "REPLAY_ERROR"
    def test_wrong_destination(self, stack, server):
        other = "strategy" if server == "trader" else "trader"
        assert stack.raw_code(stack.signed("cli", other), to=server) == "AUTHENTICATION_ERROR"
    def test_legacy_hmac(self, stack, server):
        assert stack.raw_code(stack.legacy_hmac_envelope(), to=server) == "AUTHENTICATION_ERROR"

def test_scheduler_has_no_identity(stack):
    with pytest.raises(RpcKeyError):
        stack.identity_for("scheduler")                        # no key, not a principal

def test_dashboard_cannot_activate_but_cli_can_reach_the_handler(stack):
    assert stack.raw_code(stack.signed("dashboard", "trader", "command", "activate_live_canary")) == "PERMISSION_DENIED"
    assert stack.raw_code(stack.signed("cli", "trader", "command", "activate_live_canary")) != "PERMISSION_DENIED"

def test_no_code_path_opens_the_retired_hmac_key(tmp_path, monkeypatch):
    monkeypatch.setattr(builtins, "open", _fail_if_path_contains("service_hmac"))   # plus pathlib.Path.open / read_bytes
    ServiceIdentity.load("trader", tmp_path); stack_roundtrip(tmp_path)

def test_bundle_key_cannot_sign_rpc(stack, tmp_path):
    signer = AttestationSigner.generate()                    # a bundle key
    assert stack.raw_code(stack.signed_with_raw_key(signer, claim="cli")) == "AUTHENTICATION_ERROR"
```

  `stack.raw_code(...)` sends bytes on a raw DEALER and returns `problem.code` after verifying the reply with the server's real public key (so the refusal itself is authentic).

```python
# tests/test_rpc_key_rotation.py  (owner answer 7: rotation is a coordinated switch)
@pytest.mark.parametrize("rotated", sorted(KNOWN_PRINCIPALS))
def test_rotating_one_principal_flips_trust_on_every_server_that_trusts_it(tmp_path, rotated):
    keys = tmp_path / "rpc"; init_keys(keys)
    old_signer = ServiceIdentity.load(rotated, keys)           # keeps the old private key in memory
    init_keys(keys, rotate=rotated)
    trusting = [s for s in SERVER_PRINCIPALS if rotated in SERVER_ACCEPTS[s]]
    for server in SERVER_PRINCIPALS:
        stack = start_server(server, keys)                      # fresh process-equivalent: reloads keyring from disk
        if server in trusting:
            assert stack.raw_code(stack.signed_with(old_signer, to=server)) == "AUTHENTICATION_ERROR"   # old key refused
            assert stack.raw_code(stack.signed_with(ServiceIdentity.load(rotated, keys), to=server)) != "AUTHENTICATION_ERROR"
        else:
            assert rotated not in stack.trusted_principals()    # rotation changes nothing there
    if rotated in SERVER_PRINCIPALS:                            # a server's own key rotated: peers must reload too
        peer = start_server("cli_view", keys)                   # client verifying the server reply with the new .pub
        assert peer.client_accepts_reply_from(rotated)

def test_running_server_keeps_the_old_keyring_until_restarted(tmp_path):
    keys = tmp_path / "rpc"; init_keys(keys); server = start_server("trader", keys)
    init_keys(keys, rotate="cli")
    assert server.raw_code(server.signed_with(old_cli_identity, to="trader")) != "AUTHENTICATION_ERROR"   # why the restart list matters
```

- [ ] **Step 2: Run.** These tests pin behaviour built in Tasks 3–5 and should pass on the first run. Any failure is a defect in an earlier task: fix it there (same branch, a `fix:` commit naming the task), not by weakening this test.
- [ ] **Step 3: Full suite**, then `grep -rn "Hmac\|service_hmac" trader web scripts config_defaults docker-compose.yml docker.sh start_mmr.sh` (only the retirement warning, its test and the docker.sh yaml cleanup may match).
- [ ] **Step 4: Commit** — `test: add rpc trust matrix round trips`.

---

## Self-review against the spec

| Spec 5.3 / 6 requirement | Task |
|---|---|
| Ed25519, one private key per principal, only its own container mounts it | 1, 6 |
| Servers mount public keys of accepted principals; clients the servers' keys | 1 (`peers_for`), 6 |
| `principal` in the signed bytes; key chosen by principal; timestamp + nonce kept | 3 |
| Each server signs responses; client verifies with the server's key | 3 |
| Works for both servers; strategy verifies `cli`, `dashboard`, `trader` | 3, 7 |
| Bundle key refused as RPC key and the reverse | 1, 2, 3 (domain separation), 7 |
| Key only from the keyring on disk; a request cannot point to a key | 1, 3 (Review Focus 1) |
| Signed bytes cover principal, destination (server + method) and payload | 3 |
| Response names request id and request digest | 3 |
| Trust matrix | 4 (tables), 7 |
| Forwarding signs as `trader`; `on_behalf_of` for the log only | 3, 4 |
| Handlers receive the caller; `source="strategy_service"` replaced | 4, 5 |
| `allowed_principals` per registration; no entry = everyone refused; check after auth, before dispatch; `PERMISSION_DENIED`, logged; table in code | 4 |
| Keys at `~/.config/mmr/keys/rpc/<p>.key` (0600) / `.pub`, by `mmr keys init` | 1, 2 |
| No container mounts another principal's private key | 6 |
| Split-service round trips + five refusals on each server | 7 |
| Owner answers 1, 3-7 (rulings 5, 13-15, 17-19): `cli` container, empty scheduler ACL with a guard test, direct strategy control, `cli`-only activation, HMAC never auto-deleted, encrypted key backup and tested rotation | 1, 2, 4, 5, 6, 7 |
| Hard cutover, no dual-key mode, old key refused | 3, 6 |
| AI principals cannot call `approve_proposal`, `execute_automated_intent`, `place_standalone_order` or set limits | 5 |
| Source derived from the key, never the body | 5 |

Not in this plan (by design): `publish_ai_risk_policy`, `submit_ai_paper_decision`, `register_ai_deployment`, scoreboard and experiment methods (Plans 3–5 add their allow-list entries); "arming refused with only the old shared key" (Plan 4; after Task 3 no code path reads the old key, so Plan 4's check is that `ServiceIdentity` loaded); acceptance harness key use (Plan 6).

## Open questions for the owner

Answered (now rulings): CLI in containers and keygen in Docker (15), scheduler rights (5), strategy control (17), activation (18), old HMAC key (14), key backup (13, 19); the Docker overlay check is an explicit owner-run cutover step in Task 6 docs.

New, from the owner answers:

1. **Backup tool (ruling 19).** The plan proposes `age` with a public recipient file (small, no keyring, plaintext never on disk, offline private identity). Alternatives: `gpg`, or the macOS keychain / 1Password CLI. Which one? Where does the age identity live? `mmr keys backup|restore` run on the host (they need the `age` binary and the backups directory); do you want them in a one-shot container too?
2. **Retired HMAC file visibility (ruling 14).** Containers mount the whole `~/.config/mmr`, so `service_hmac.key` stays readable inside them until you delete it by hand (nothing reads it). Accept that, or hide it with a `/dev/null` bind per service (Docker would create an empty host file if it is already gone)?
