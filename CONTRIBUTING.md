# Contributing

Contributions welcome. The short version:

1. Read [`AGENTS.md`](AGENTS.md) — ground rules (one typed core, no device
   logic in frontends, vendored tree is read-only, capability honesty) and
   the commands below.
2. Discuss first for new device behavior or a new frontend feature: open an
   issue. Small fixes (bugs, tests, docs) can come straight in as PRs.
3. Before pushing:
   ```bash
   scripts/run-gates.sh      # ruff + ruff format + mypy --strict + pytest (85% coverage gate)
   scripts/test-swift.sh     # if you touched app/ (Swift bridge/UI)
   ```
4. Commits: `type(scope): summary` (feat, fix, test, docs, chore, refactor),
   small and logical.
5. Tests: pytest for the core (`tests/unit`, `tests/integration` with the
   fake-SSH harness — never contact a real device in these). If you have a
   SteamOS device, `DEVKIT_TEST_DEVICE=<name-or-ip> uv run pytest tests/hardware`
   and record the run in `docs/hardware-matrix.md` (that file is what promotes
   an operation to `supported_tested`).
6. Never commit secrets, private keys, or real device IPs/hostnames in
   fixtures — use TEST-NET (`192.0.2.x`) addresses.
7. `docs/technical-spec.md` is authoritative; if reality must diverge, update
   the spec in the same commit (appendix A records the delta).

By contributing you agree your contributions are licensed under the project's
[MIT license](LICENSE).
