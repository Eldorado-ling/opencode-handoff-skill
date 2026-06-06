# Changelog

## v1.0.0 — 2026-06-06

Initial release.

### Features

- 5-tier verification pipeline (filename / trust / content / commit attribution / signature)
- Two transport modes: shared central repo + P2P (per-user inbox repo) with automatic preference
- Transactional receive: fetch URL content before deleting inbox file
- Reference Python implementation (`verify_inbox.py`) replaces shell pseudocode for deterministic behavior across shells
- Chinese-language trust boundary preamble + standardized user-facing messages
- Documentation: README + INSTALL + USAGE (covering minimum and hardened deployment paths)

### Security

- Strict input validation regexes (filename, URL, username, repo path, inbox subdir)
- Clone-time hardening (`-c core.symlinks=false`, `protocol.file.allow=never`, etc.)
- Trust config isolated from OpenCode's project config merger (separate `trust.json` in skill directory)
- Subprocess environment hardening (`GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, etc.)
- ASCII-only byte-level content validation (blocks Unicode/CRLF smuggling)
- Tier-1 failures never trigger shell ops on the malformed filename (DoS-safe)
- Trust boundary preamble + AGENTS.md hard rule against handoff-content-driven local writes

### Known limitations

- GPG passphrase-less keys (recommended setup) can be used by any local process
- Skill repo supply chain: pin to a specific git tag and audit diffs before pulling updates
- GitHub free tier lacks branch protection for private repos (force-push could theoretically bypass Tier 4 verification)
- Downstream `opencode-share` parser (if used) lacks built-in second-layer trust boundary — recommend forking with patches or skipping auto-extraction
