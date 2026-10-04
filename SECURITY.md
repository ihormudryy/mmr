# Security Policy

MMR can place real orders in a brokerage account, so we take security reports seriously.

## Supported versions

Only the latest release and the `master` branch get security fixes.

| Version | Supported |
|---------|-----------|
| `master` / latest release | Yes |
| Older releases | No |

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security problem.**

Report it privately through GitHub: go to the [Security tab](https://github.com/ihormudryy/mmr/security) and choose **Report a vulnerability**. This opens a private advisory that only the maintainers can see.

Please include:

- What the problem is and what an attacker could do with it.
- Steps to reproduce, or a proof of concept.
- The version or commit you tested, and your deployment (split Docker or local `start_mmr.sh`).

What to expect:

- We acknowledge the report within **7 days**.
- We keep you updated while we work on a fix.
- We credit you in the advisory and the changelog, unless you prefer to stay anonymous.

## Scope

In scope, for example:

- Bypassing HMAC authentication on the typed RPC ports (42101–42105).
- Bypassing the dashboard session, CSRF protection or command authority.
- Placing, approving or cancelling orders without going through the proposal pipeline and risk gate.
- Leaking IB credentials, API keys or `service_hmac.key`.
- Code execution through config files, strategy loading or serialized data.

Out of scope:

- The legacy dill RPC (port 42001) when you enable it yourself with `unsafe_legacy_rpc: true` for offline simulation. It is unsafe by design and documented as such.
- Problems in IB Gateway, TWS or third-party data providers. Report those to the vendor.
- Losses from trading decisions or strategy behaviour. See the [Disclaimer](README.md#disclaimer).

## Keeping your deployment safe

- Never share `.env`, `~/.config/mmr/trader.yaml`, `~/.config/mmr/service_hmac.key` or logs that contain account numbers. Remove them before you attach anything to an issue.
- Keep the typed RPC ports on loopback (`127.0.0.1`) or the private Docker network, as the defaults do. Never expose them to the internet.
- Start with a paper account. Turn on live trading only after you have reviewed the risk limits.
