# Security policy

This project handles SSH private keys (`~/Library/Application Support/steamos-devkit/keys/`,
mode 0400) and speaks to SteamOS devices over the local network. Take care to
report, not publish, anything that could expose key material, device
credentials, or a user's network topology.

## Reporting

Please use GitHub's **private vulnerability reporting** (Security tab →
"Report a vulnerability") on this repository. Do not open a public issue for a
security problem. If the advisory flow is unavailable, redact details and ask
for a private channel in an issue.

## Scope notes

- Device pairing uses upstream's magic-phrase + HTTP flow on the LAN; by
  design it trusts the local network (same as Valve's upstream client).
- The host SSH policy is currently AutoAdd (upstream parity); TOFU recording
  is on the roadmap (D4).
- The packaged app embeds a Python runtime and rsync; bundled-dependency
  vulnerabilities should be reported with the pinned versions from
  `uv.lock` / `scripts/embed-python.sh`.

Fixes land on `main`; there is no supported-stable-branch program yet
(pre-1.0).
