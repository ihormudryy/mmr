# Split-Compose Launcher Cutover Design

## Purpose

Make `docker.sh` operate the committed split Compose topology without silently
targeting the retired `mmr` monolith. Preserve the established operator entry
point while making the unavoidable image-based deployment model explicit.

## Decision

Use a service-aware `docker.sh` rather than retaining a dual monolith/split
mode or replacing the helper with raw Compose instructions.

- `./docker.sh -b` builds the one shared Compose image using `docker compose build`.
- `./docker.sh -e` selects `trader` by default. `./docker.sh -e dashboard`
  selects an allowlisted service: `trader`, `data`, `strategy`, `dashboard`,
  `scheduler`, or `ib-gateway`.
- An invalid `-e` service fails before invoking the container runtime.
- Legacy `mmr-mmr`/`mmr_mmr` names remain only in stale-container cleanup;
  no normal command executes, synchronizes, or backs up through them.
- `-s` and `-a` fail with a migration instruction. Source is baked into the
  shared image and every production service has a read-only filesystem, so
  rsyncing code would either fail or create a false deployment signal.
- `-B` uses the running `scheduler` service, which has both `mmr_db_data` and
  the host-backed backups mount. If the scheduler is absent, it takes a
  read-only sidecar copy from the new `mmr_db_data` volume.

## Bootstrap and security

`docker.sh -u` must prepare host configuration before Compose starts:

1. Create `~/.config/mmr` and copy bundled YAML defaults only when files are
   absent; never overwrite an operator configuration.
2. Generate `service_hmac.key` only when the configured key is absent, with
   `umask 177` and mode `0600`.
3. Store the configuration value as `~/.config/mmr/service_hmac.key`, not a
   host absolute path. The same relative-to-home location resolves correctly
   for host processes and for the container's `/home/trader` bind mount.
4. Preserve an existing configured key unchanged. A malformed or insecure
   existing key remains a trader-service fail-closed startup error.

`.dockerignore` already excludes `.env` and secrets, so this change does not
add a duplicate credential-leak mitigation.

## Verification

Launcher tests use a fake `docker` executable and a temporary home directory.
They verify the emitted runtime commands and configuration effects without
touching real containers, credentials, or host configuration. Compose topology
tests continue to establish the authoritative service names and volume name.

The existing full-suite epoch/timezone failures are outside this launcher
change and are not altered here.
