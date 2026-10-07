"""`mmr keys init|backup|restore`: RPC identity key management.

Kept free of service imports so the one-shot ``keygen`` container can run it
(``python -m trader.messaging.keys_cli``) without touching ``~/.config/mmr``
or the log directory. ``trader.mmr_cli`` delegates its ``keys`` command here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import Callable, Optional, TextIO

from trader.messaging.principals import SERVICE_PRINCIPAL, service_principals, service_rpc_files
from trader.messaging.rpc_keys import (
    RESTART_ON_ROTATE,
    RpcKeyError,
    backup_keys,
    init_keys,
    load_identity_material,
    restore_keys,
)
from trader.research.key_purpose import default_rpc_keys_dir

KEYGEN_CONTAINER_ENV = "MMR_KEYGEN_CONTAINER"
DEFAULT_BACKUP_DIR = Path("~/.local/share/mmr/backups/rpc_keys")


def running_in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def add_keys_parser(sub) -> argparse.ArgumentParser:
    keys_p = sub.add_parser(
        'keys', help='RPC identity keys (run on the host or via ./docker.sh -k)',
        epilog='Examples:\n'
               '  keys init\n'
               '  keys init --rotate dashboard\n'
               '  keys backup --recipient ~/.config/mmr/keys/rpc_backup_recipient.txt\n'
               '  op read <item> | keys restore FILE --identity-stdin',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    keys_sub = keys_p.add_subparsers(dest='keys_action')
    init_p = keys_sub.add_parser('init', help='Create missing RPC keypairs (never overwrites)')
    init_p.add_argument('--rotate', metavar='PRINCIPAL', help='Replace one principal\'s keypair')
    init_p.add_argument('--keys-dir', help='Override the RPC keys directory')
    backup_p = keys_sub.add_parser('backup', help='Encrypt the RPC keys with age')
    backup_p.add_argument('--recipient', required=True, help='age recipient (public key) file')
    backup_p.add_argument('--out', help='Output file (default ~/.local/share/mmr/backups/rpc_keys/)')
    backup_p.add_argument('--keys-dir', help='Override the RPC keys directory')
    restore_p = keys_sub.add_parser('restore', help='Restore RPC keys from an age backup')
    restore_p.add_argument('archive', help='The .tar.age backup file')
    source = restore_p.add_mutually_exclusive_group(required=True)
    source.add_argument('--identity-stdin', action='store_true', help='Read the age identity from stdin')
    source.add_argument('--identity-file', help='Read the age identity from this file')
    restore_p.add_argument('--keys-dir', help='Override the RPC keys directory')
    check_p = keys_sub.add_parser(
        'check-mount', help='Inside a service container: check it sees exactly its own keys '
                            '(run by ./docker.sh -K)')
    check_p.add_argument('service', help='Compose service name')
    check_p.add_argument('--keys-dir', help='Override the RPC keys directory')
    check_p.add_argument('--hmac-file', help='Override the retired HMAC key path')
    return keys_p


def _keys_dir(args) -> Path:
    return Path(args.keys_dir).expanduser() if getattr(args, 'keys_dir', None) else default_rpc_keys_dir()


def _print_init(rows, rotate: Optional[str], out: TextIO) -> None:
    out.write(f"{'principal':<15} {'status':<8} key id\n")
    for row in rows:
        out.write(f"{row.principal:<15} {row.status:<8} {row.key_id}\n")
    if rotate:
        services = ", ".join(RESTART_ON_ROTATE[rotate]) or "(no long-lived service)"
        out.write(
            f"\nRotated {rotate}. Restart these services together: {services}.\n"
            "There is no overlap window: in-flight requests fail with AUTHENTICATION_ERROR "
            "during the switch, and clients retry.\n")


def _read_identity(args, stdin) -> bytes:
    if args.identity_stdin:
        return stdin.buffer.read() if hasattr(stdin, 'buffer') else stdin.read().encode()
    return Path(args.identity_file).read_bytes()


def _identity_problem(principal: str | None, keys_dir: Path) -> list[str]:
    """Load the keys exactly as ``ServiceIdentity.load`` does at startup."""
    if principal is None:
        return []
    try:
        load_identity_material(principal, keys_dir)
    except RpcKeyError as exc:
        return [str(exc)]
    return []


def _mount_problems(service: str, keys_dir: Path, hmac_file: Path) -> list[str]:
    expected = service_rpc_files(service)
    seen = frozenset(os.listdir(keys_dir)) if keys_dir.is_dir() else frozenset()
    problems = [f"unexpected {name}" for name in sorted(seen - expected)]
    problems += [f"missing {name}" for name in sorted(expected - seen)]
    if not problems:
        for principal in service_principals(service):
            problems += _identity_problem(principal, keys_dir)
    if not hmac_file.exists():
        problems.append(f"{hmac_file} is not mounted (expected /dev/null)")
    elif hmac_file.read_bytes():
        problems.append(f"{hmac_file} is readable and not empty")
    return problems


def check_mount(args, out: TextIO) -> int:
    """Exit 0 only if this container sees exactly its own key pair, its peers' .pub and an empty HMAC file,
    and those keys load as they would at service startup (own pair matches)."""
    if args.service not in SERVICE_PRINCIPAL:
        out.write(f"Error: unknown service {args.service!r}; expected one of "
                  f"{', '.join(sorted(SERVICE_PRINCIPAL))}\n")
        return 2
    keys_dir = _keys_dir(args)
    hmac_file = (Path(args.hmac_file) if args.hmac_file
                 else keys_dir.parent.parent / "service_hmac.key")
    problems = _mount_problems(args.service, keys_dir, hmac_file)
    if problems:
        out.write(f"{args.service}: key mounts FAILED: {'; '.join(problems)}\n")
        return 1
    out.write(f"{args.service}: key mounts ok\n")
    return 0


def run_keys_command(args, *, in_container: bool, stdin=None, out: TextIO = None) -> int:
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    if getattr(args, 'keys_action', None) == 'check-mount':
        return check_mount(args, out)
    if in_container and os.environ.get(KEYGEN_CONTAINER_ENV) != "1":
        out.write(
            "Refusing to manage RPC keys inside a service container: keys/rpc is a tmpfs "
            "there and the keys would be lost. Use ./docker.sh -k on the host.\n")
        return 2
    action = getattr(args, 'keys_action', None)
    try:
        if action == 'init':
            rows = init_keys(_keys_dir(args), rotate=args.rotate)
            _print_init(rows, args.rotate, out)
        elif action == 'backup':
            stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            target = (Path(args.out).expanduser() if args.out
                      else DEFAULT_BACKUP_DIR.expanduser() / f"rpc_keys_{stamp}.tar.age")
            written = backup_keys(_keys_dir(args), target, Path(args.recipient).expanduser())
            out.write(f"Encrypted RPC key backup written to {written}\n")
        elif action == 'restore':
            restored = restore_keys(Path(args.archive), _keys_dir(args), _read_identity(args, stdin))
            out.write(f"Restored RPC keys for: {', '.join(restored)}\n")
        else:
            out.write("usage: mmr keys {init,backup,restore,check-mount} ...\n")
            return 2
    except RpcKeyError as exc:
        out.write(f"Error: {exc}\n")
        return 1
    return 0


def main(argv: Optional[list[str]] = None,
         in_container: Callable[[], bool] = running_in_container,
         out: Optional[TextIO] = None) -> int:
    parser = argparse.ArgumentParser(prog='mmr')
    add_keys_parser(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['keys', *(sys.argv[1:] if argv is None else argv)])
    return run_keys_command(args, in_container=in_container(), out=out)


if __name__ == '__main__':
    sys.exit(main())
