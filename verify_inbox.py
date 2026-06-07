#!/usr/bin/env python3
"""
opencode-handoff inbox verifier — reference implementation.

Single source of truth for the verification pipeline. Called by the SKILL.md
flow with three arguments. Outputs JSON to stdout. Exits 0 on success, non-zero
on top-level failures (missing trust.json, identity failure, bad args).

Usage:
    python verify_inbox.py <clone_path> <owner>/<repo> <inbox_subdir>

Arguments:
    clone_path  : absolute path to a git clone of the inbox repo
    owner/repo  : GitHub repo (for commit author/committer/signature lookups)
    inbox_subdir: where the .txt files live inside the clone.
                  '.' for P2P (root of own inbox repo)
                  'inboxes/<me>' for shared mode

Reads `trust.json` from this script's own directory (NOT configurable — pinning
the trust config to the skill's directory prevents project-level opencode.json
config hijacking from inserting trusted senders).

Output schema (all fields always present, may be null):
    {
      "ok": bool,                    # false = top-level failure, no files processed
      "skill_status": str,           # "ok" | "trust_missing" | "trust_parse_fail" | ...
      "skill_status_detail": str,    # human-readable detail
      "me": str|null,                # lowercased authenticated GitHub username
      "require_signed_commits": bool,
      "files": [
        {
          "filename": str,
          "tier_failed": int|null,   # 1..5, null = all passed
          "tier_failed_reason": str|null,
          "action": "consume" | "delete" | "keep",
          "claimed_sender": str|null,
          "url": str|null,
          "commit_sha": str|null,
          "author_login": str|null,
          "committer_login": str|null,
          "signature_verified": bool|null
        },
        ...
      ]
    }

The caller (the agent) then:
    - "consume": emit trust-boundary preamble → fetch URL content → present in
                 trust-boundary block → git rm + commit + push (in that order;
                 the rm only happens after content has been safely captured).
    - "delete": git rm + commit + push silently; count for batch summary.
    - "keep":   leave file in place; surface tier_failed_reason to user.

The agent NEVER re-implements verification logic in shell. Doing so has
historically produced incorrect results (wrong tier order, CRLF false
positives, byte-handling errors). This script is the spec.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple


# Module-level constants. SKILL_DIR is fixed at import; do not parameterize.
SKILL_DIR = Path(__file__).parent.resolve()
TRUST_FILE = SKILL_DIR / "trust.json"

# Strict regexes — match the SKILL.md "Input validation rules" table exactly.
FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z--from-([a-zA-Z0-9-]+)\.txt$"
)
# Trailing CR is tolerated to survive Windows working-tree autocrlf conversions
# (we read from blob, but receiver clones may have different config). URL body
# is still ASCII alphanumeric only.
URL_RE = re.compile(
    rb"^https://(?:opncd\.ai/share|opencode\.ai/s)/[A-Za-z0-9]+/?\r?\n$"
)
# GitHub username: 1-39 chars, alphanumeric and hyphens, no leading/trailing
# hyphen. POSIX-ERE-compatible two-alternative form (no lookahead).
USERNAME_RE = re.compile(
    r"^[a-zA-Z0-9]$|^[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9]$"
)
USERNAME_MAX_LEN = 39
REPO_PATH_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")
# inbox_subdir must be either '.' (P2P root) or 'inboxes/<username>' (shared).
SUBDIR_RE = re.compile(
    r"^\.$|^inboxes/([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])$"
)

MAX_CONTENT_BYTES = 200

# Subprocess environment hardening — applied to ALL git/gh calls so they ignore
# system-level config that could change behavior in adversarial ways.
HARDENED_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",            # ignore /etc/gitconfig
    "GIT_TERMINAL_PROMPT": "0",            # never prompt for credentials interactively
    "GIT_OPTIONAL_LOCKS": "0",             # avoid background lock churn
    "GIT_PAGER": "cat",                    # never paginate
    "LC_ALL": "C",                         # predictable error messages
}


def fail(status: str, detail: str) -> None:
    """Emit a top-level failure and exit. Caller (the agent) must check exit code."""
    print(json.dumps({
        "ok": False,
        "skill_status": status,
        "skill_status_detail": detail,
        "me": None,
        "require_signed_commits": None,
        "files": [],
    }, indent=2, ensure_ascii=False))
    sys.exit(1)


def run(cmd) -> Tuple[int, bytes, bytes]:
    """Run a subprocess with hardened env. Returns (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, env=HARDENED_ENV, check=False)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return -1, b"", str(e).encode("utf-8", "replace")


def get_me() -> Optional[str]:
    """Resolve authenticated GitHub username (lowercased, validated). None on failure."""
    rc, out, _ = run(["gh", "api", "user", "--jq", ".login"])
    if rc != 0:
        return None
    me = out.decode("utf-8", "replace").strip().lower()
    if not me:
        return None
    if not USERNAME_RE.match(me) or len(me) > USERNAME_MAX_LEN:
        return None
    return me


def load_trust() -> Tuple[set, bool]:
    """Load trust.json. Exits via fail() on any parse/schema error. Returns (set, bool)."""
    if not TRUST_FILE.exists():
        fail("trust_missing", f"trust.json not found at {TRUST_FILE}")
    try:
        with TRUST_FILE.open("rb") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        fail("trust_parse_fail", f"trust.json JSON error: {e}")
    except Exception as e:
        fail("trust_parse_fail", f"trust.json read error: {e}")

    if not isinstance(data, dict):
        fail("trust_schema_fail", "trust.json top-level is not an object")

    trusted = data.get("trusted_senders", [])
    if not isinstance(trusted, list) or not trusted:
        fail("trust_empty", "trusted_senders is missing, empty, or not a list")
    # Validate each entry is a string
    if not all(isinstance(s, str) for s in trusted):
        fail("trust_schema_fail", "trusted_senders contains non-string entries")

    trusted_set = {s.lower() for s in trusted if s}
    if not trusted_set:
        fail("trust_empty", "trusted_senders contained only empty strings")

    # require_signed_commits: must be an actual bool (not "false" string etc.).
    raw_signed = data.get("require_signed_commits", True)
    if not isinstance(raw_signed, bool):
        fail(
            "trust_schema_fail",
            f"require_signed_commits must be true/false (got {type(raw_signed).__name__}: {raw_signed!r})",
        )
    return trusted_set, raw_signed


def list_inbox_files(clone: Path, subdir: str) -> list[str]:
    """List tracked .txt files in the inbox subdir via git ls-files."""
    path_spec = "*.txt" if subdir == "." else f"{subdir.rstrip('/')}/*.txt"
    rc, out, _ = run(["git", "-C", str(clone), "ls-files", "--", path_spec])
    if rc != 0:
        return []
    files = [line for line in out.decode("utf-8", "replace").splitlines() if line]
    # Sort lexicographically — timestamps in filenames give chronological order.
    return sorted(files)


def verify_tier_1(filename: str) -> Optional[str]:
    """Tier 1: filename pattern check. Returns lowercased claimed-sender or None."""
    basename = filename.rsplit("/", 1)[-1]
    m = FILENAME_RE.match(basename)
    if not m:
        return None
    return m.group(1).lower()


def read_blob(clone: Path, filepath: str) -> Optional[bytes]:
    """Read raw blob bytes from git (NOT working tree, avoids autocrlf/symlinks)."""
    rc, out, _ = run(["git", "-C", str(clone), "show", f"HEAD:{filepath}"])
    if rc != 0:
        return None
    return out


def blob_size(clone: Path, filepath: str) -> Optional[int]:
    """Get blob size from git (pre-flight check before reading)."""
    rc, out, _ = run(["git", "-C", str(clone), "cat-file", "-s", f"HEAD:{filepath}"])
    if rc != 0:
        return None
    try:
        return int(out.decode("ascii", "strict").strip())
    except (ValueError, UnicodeDecodeError):
        return None


def verify_tier_3(clone: Path, filepath: str) -> Tuple[bool, str]:
    """Tier 3: content well-formed. Returns (ok, url_or_reason)."""
    size = blob_size(clone, filepath)
    if size is None:
        return False, "无法获取 blob 大小"
    if size > MAX_CONTENT_BYTES:
        return False, f"内容超过 {MAX_CONTENT_BYTES} 字节（实际 {size}）"

    blob = read_blob(clone, filepath)
    if blob is None:
        return False, "读取 blob 失败"

    if URL_RE.fullmatch(blob):
        url = blob.rstrip(b"\r\n").decode("ascii", "strict")
        return True, url

    # Diagnose why the regex failed (for human-readable reason)
    if b"\x00" in blob:
        return False, "内容包含空字节"
    try:
        blob.decode("ascii", "strict")
    except UnicodeDecodeError:
        return False, "内容包含非 ASCII 字节（可能是 Unicode 走私）"
    if not blob.endswith(b"\n"):
        return False, "内容没有以 LF 结尾"
    newline_count = blob.count(b"\n")  # actual LF byte count (was buggy literal '\\n' before)
    if newline_count != 1:
        return False, f"内容有 {newline_count} 个换行（应为 1）"
    return False, "内容不是合法的 OpenCode share URL"


def get_add_sha(clone: Path, filepath: str) -> Optional[str]:
    """Get SHA of the commit that ADDED this file (most recent if re-added)."""
    rc, out, _ = run([
        "git", "-C", str(clone),
        "log", "--diff-filter=A", "--format=%H", "--", filepath,
    ])
    if rc != 0:
        return None
    lines = [l for l in out.decode("utf-8", "replace").splitlines() if l]
    return lines[0] if lines else None


def verify_tier_4(repo: str, sha: str, claimed: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """Tier 4: commit author AND committer must equal claimed sender."""
    # One API call; pull both fields at once.
    rc, out, _ = run([
        "gh", "api", f"repos/{repo}/commits/{sha}",
        "--jq", "[.author.login, .committer.login] | @tsv",
    ])
    if rc != 0:
        return False, None, None
    parts = out.decode("utf-8", "replace").strip().split("\t")
    author = parts[0].lower() if len(parts) >= 1 and parts[0] else None
    committer = parts[1].lower() if len(parts) >= 2 and parts[1] else None
    if not author or author == "null" or not committer or committer == "null":
        return False, author, committer
    if author != claimed or committer != claimed:
        return False, author, committer
    return True, author, committer


def verify_tier_5(repo: str, sha: str) -> bool:
    """Tier 5: GitHub-side GPG signature verification."""
    rc, out, _ = run([
        "gh", "api", f"repos/{repo}/commits/{sha}",
        "--jq", ".commit.verification.verified",
    ])
    if rc != 0:
        return False
    return out.decode("utf-8", "replace").strip() == "true"


def process_file(
    clone: Path,
    repo: str,
    filename: str,
    trusted_set: set,
    require_signed: bool,
) -> dict:
    """Run all tiers on one file. Returns the result dict per output schema."""
    result = {
        "filename": filename,
        "tier_failed": None,
        "tier_failed_reason": None,
        "action": None,
        "claimed_sender": None,
        "url": None,
        "commit_sha": None,
        "author_login": None,
        "committer_login": None,
        "signature_verified": None,
    }

    # Tier 1: filename pattern
    claimed = verify_tier_1(filename)
    if claimed is None:
        result["tier_failed"] = 1
        result["tier_failed_reason"] = "文件名格式不符合规范（不做任何 shell 操作）"
        result["action"] = "keep"
        return result
    result["claimed_sender"] = claimed

    # Tier 2: trust allowlist
    if claimed not in trusted_set:
        result["tier_failed"] = 2
        result["tier_failed_reason"] = f"发件人 '{claimed}' 不在 trusted_senders 列表中"
        result["action"] = "delete"
        return result

    # Tier 3: content well-formed (size + byte composition + URL regex)
    ok3, url_or_reason = verify_tier_3(clone, filename)
    if not ok3:
        result["tier_failed"] = 3
        result["tier_failed_reason"] = url_or_reason
        result["action"] = "keep"
        return result
    result["url"] = url_or_reason

    # Tier 4: commit author + committer attribution
    sha = get_last_modifying_sha(clone, filename)
    result["commit_sha"] = sha
    if not sha:
        result["tier_failed"] = 4
        result["tier_failed_reason"] = "无法定位最近修改此文件的 commit"
        result["action"] = "keep"
        return result
    ok4, author, committer = verify_tier_4(repo, sha, claimed)
    result["author_login"] = author
    result["committer_login"] = committer
    if not ok4:
        result["tier_failed"] = 4
        result["tier_failed_reason"] = (
            f"声称发件人 '{claimed}'，但 commit 作者 '{author}' / committer '{committer}' 不匹配"
        )
        result["action"] = "keep"
        return result

    # Tier 5: GPG signature (optional)
    if require_signed:
        verified = verify_tier_5(repo, sha)
        result["signature_verified"] = verified
        if not verified:
            result["tier_failed"] = 5
            result["tier_failed_reason"] = "commit 未签名或签名验证失败（trust.json 要求签名）"
            result["action"] = "keep"
            return result

    # All tiers passed
    result["action"] = "consume"
    return result


def main() -> None:
    if len(sys.argv) != 4:
        fail("usage", "usage: verify_inbox.py <clone_path> <owner>/<repo> <inbox_subdir>")
    clone_arg = sys.argv[1]
    repo = sys.argv[2]
    subdir = sys.argv[3]

    # Validate all CLI args against strict regexes before any IO
    if not REPO_PATH_RE.match(repo):
        fail("bad_repo_arg", f"repo argument '{repo}' fails regex check")
    if not SUBDIR_RE.match(subdir):
        fail("bad_subdir_arg", f"inbox_subdir '{subdir}' must be '.' or 'inboxes/<username>'")

    try:
        clone = Path(clone_arg).resolve()
    except Exception as e:
        fail("bad_clone_arg", f"clone path resolve failed: {e}")

    if not clone.is_dir() or not (clone / ".git").exists():
        fail("clone_not_found", f"clone path '{clone}' is not a git repo")

    me = get_me()
    if not me:
        fail("identity_fail", "gh api user failed or returned invalid username")

    trusted_set, require_signed = load_trust()

    files = list_inbox_files(clone, subdir)
    results = [process_file(clone, repo, f, trusted_set, require_signed) for f in files]

    print(json.dumps({
        "ok": True,
        "skill_status": "ok",
        "skill_status_detail": "",
        "me": me,
        "require_signed_commits": require_signed,
        "files": results,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
