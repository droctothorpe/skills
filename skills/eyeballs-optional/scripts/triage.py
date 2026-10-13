#!/usr/bin/env python3
"""Collect pinned Git facts and evaluate EO policy gates; never infer semantic EO."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

POLICY_PATH = ".eyeballs.json"
EO_PATHS = {
    "internal_maintenance", "mechanical_transformation",
    "consumer_preserving_reorganization", "exact_revert",
    "behavior_preserving_migration", "established_contract_repair",
    "established_pattern_extension", "previously_decided_implementation",
    "low_stakes_presentation_correction",
}
EXCLUSION_MODES = {
    "none": set(),
    "generated_and_lockfiles": {"generated_output", "lockfile_bookkeeping"},
    "verified_mechanical": {
        "generated_output", "lockfile_bookkeeping", "verified_transformation",
    },
}
DEFAULT_POLICY = {
    "version": 1,
    "limits": {
        "max_substantive_lines": 2000,
        "max_raw_lines": None,
        "max_changed_files": None,
    },
    "size_exclusions": "verified_mechanical",
    "always_er_paths": [],
    "disabled_eo_paths": [],
    "inspection": "full",
}


class TriageError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, message, code="CONFIG_INVALID"):
    if not condition:
        raise TriageError(code, message)


def read_json(data, code="CONFIG_INVALID"):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"Duplicate JSON key: {key}", code)
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"Non-JSON constant: {value}")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise TriageError(code, f"Invalid JSON: {error}") from error


def object_keys(value, allowed, label, code="CONFIG_INVALID"):
    require(isinstance(value, dict), f"{label} must be an object", code)
    require(not (value.keys() - allowed), f"Unknown keys in {label}: {sorted(value.keys() - allowed)}", code)


def path_pattern(pattern):
    require(isinstance(pattern, str) and pattern, "Path patterns must be nonempty strings")
    parts = pattern.split("/")
    require(
        all(part not in ("", ".", "..") for part in parts)
        and not any(char in pattern for char in "\\[]{}!\x00\n\r")
        and all("**" not in part or part == "**" for part in parts),
        f"Unsupported relative path pattern: {pattern!r}",
    )
    regex = ""
    for index, part in enumerate(parts):
        if part == "**":
            regex += ".*" if index == len(parts) - 1 else "(?:[^/]+/)*"
        else:
            regex += "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in part)
            if index < len(parts) - 1:
                regex += "/"
    return re.compile(regex, re.DOTALL)


def validate_policy(value):
    object_keys(value, DEFAULT_POLICY.keys(), "policy")
    require(type(value.get("version")) is int and value["version"] == 1, "Policy version must be 1")
    if "limits" in value:
        object_keys(value["limits"], DEFAULT_POLICY["limits"].keys(), "limits")
        for key, limit in value["limits"].items():
            require(
                (limit is None and key != "max_substantive_lines")
                or (type(limit) is int and limit >= 0),
                f"{key} must be a nonnegative integer" + (" or null" if key != "max_substantive_lines" else ""),
            )
    if "size_exclusions" in value:
        require(isinstance(value["size_exclusions"], str) and value["size_exclusions"] in EXCLUSION_MODES, "Unknown size_exclusions mode")
    if "inspection" in value:
        require(value["inspection"] in ("full", "stop_on_first_er"), "Unknown inspection mode")
    for key in ("always_er_paths", "disabled_eo_paths"):
        if key not in value:
            continue
        require(isinstance(value[key], list) and all(isinstance(item, str) for item in value[key]), f"{key} must be an array of strings")
        require(len(set(value[key])) == len(value[key]), f"Duplicate entries in {key}")
        for item in value[key]:
            if key == "always_er_paths":
                path_pattern(item)
            else:
                require(item in EO_PATHS, f"Unknown EO path: {item}")
    return value


def merge_policy(base, patch):
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if key == "limits":
            result[key].update(value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve_policy(repository=None, overrides=None):
    policy = merge_policy(DEFAULT_POLICY, validate_policy(repository)) if repository is not None else copy.deepcopy(DEFAULT_POLICY)
    if overrides is None:
        return policy
    candidate = merge_policy(policy, validate_policy(overrides))
    # One-off overrides can tighten, but never relax, the resolved policy.
    for key, old in policy["limits"].items():
        new = candidate["limits"][key]
        require(old is None or (new is not None and new <= old), f"Override relaxes {key}")
    for key in ("always_er_paths", "disabled_eo_paths"):
        require(set(policy[key]) <= set(candidate[key]), f"Override removes entries from {key}")
    require(EXCLUSION_MODES[candidate["size_exclusions"]] <= EXCLUSION_MODES[policy["size_exclusions"]], "Override broadens size exclusions")
    return candidate


def git(repo, *args, attr_source=None):
    # Honor --repo rather than ambient repository/index/object redirects.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="", GIT_ATTR_NOSYSTEM="1",
    )
    if attr_source:
        env["GIT_ATTR_SOURCE"] = attr_source
    command = [
        "git", "--no-pager", "--literal-pathspecs", "-C", str(repo),
        "-c", "core.attributesFile=" + os.devnull, "-c", "diff.renameLimit=0", *args,
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise TriageError("INSPECTION_INCOMPLETE", f"Git {' '.join(args[:2])} failed: {detail}")
    return result.stdout


def pin_commit(repo, sha):
    require(bool(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha)), "Use a full base/head commit SHA, not a moving ref", "INSPECTION_INCOMPLETE")
    require(git(repo, "cat-file", "-t", sha).strip() == b"commit", f"Not a commit: {sha}", "INSPECTION_INCOMPLETE")
    return sha.lower()


def decode_path(value):
    return value.decode("utf-8", "surrogateescape")


def parse_raw(data):
    tokens = data.split(b"\0")
    files = []
    index = 0
    while index < len(tokens) - 1:
        header = tokens[index].split()
        require(len(header) == 5 and header[0].startswith(b":"), "Malformed Git raw inventory", "INSPECTION_INCOMPLETE")
        status = header[4].decode("ascii")
        old_path = None
        path = decode_path(tokens[index + 1])
        index += 2
        if status.startswith("R"):
            old_path, path = path, decode_path(tokens[index])
            index += 1
        files.append({
            "path": path, "previous_path": old_path, "status": status,
            "old_mode": header[0][1:].decode("ascii"), "new_mode": header[1].decode("ascii"),
        })
    return files


def parse_numstat(data):
    tokens = data.split(b"\0")
    counts = {}
    index = 0
    while index < len(tokens) - 1:
        added, deleted, path = tokens[index].split(b"\t", 2)
        index += 1
        old_path = None
        if not path:
            old_path, path = decode_path(tokens[index]), tokens[index + 1]
            index += 2
        key = (old_path, decode_path(path))
        require(key not in counts, "Duplicate Git numstat entry", "INSPECTION_INCOMPLETE")
        counts[key] = (None, None) if b"-" in (added, deleted) else (int(added), int(deleted))
    return counts


def collect(repo, base, head):
    version = git(repo, "--version").decode().strip()
    numbers = re.search(r"(\d+)\.(\d+)", version)
    require(numbers is not None and tuple(map(int, numbers.groups())) >= (2, 43), "Git 2.43+ is required for pinned attributes (GIT_ATTR_SOURCE)", "INSPECTION_INCOMPLETE")
    base, head = pin_commit(repo, base), pin_commit(repo, head)
    # info/attributes overrides even GIT_ATTR_SOURCE; refuse unpinned rules.
    attributes = Path(os.fsdecode(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/attributes").rstrip(b"\n")))
    if attributes.exists():
        rules = [line for line in attributes.read_bytes().splitlines() if line.strip() and not line.lstrip().startswith(b"#")]
        require(not rules, "Local info/attributes would override pinned attributes; use a clean repository", "INSPECTION_INCOMPLETE")
    ancestors = git(repo, "merge-base", "--all", base, head).decode().split()
    require(len(ancestors) == 1, "A unique merge base is required; retrieve missing history or inspect ambiguous ancestry", "INSPECTION_INCOMPLETE")
    merge_base = ancestors[0]
    options = [
        "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-relative",
        "--no-renames", "--find-renames=100%", "--diff-algorithm=myers",
        "--no-indent-heuristic", "--ignore-submodules=none",
    ]
    files = parse_raw(git(repo, *options, "--raw", "-z", merge_base, head, "--", attr_source=head))
    counts = parse_numstat(git(repo, *options, "--numstat", "-z", merge_base, head, "--", attr_source=head))
    require(len(files) == len(counts), "Inventory and numstat disagree", "INSPECTION_INCOMPLETE")
    for file in files:
        key = (file["previous_path"], file["path"])
        require(key in counts, f"Missing counts: {file['path']}", "INSPECTION_INCOMPLETE")
        file["added"], file["deleted"] = counts[key]
        modes = {file["old_mode"], file["new_mode"]}
        if "160000" in modes:
            file["kind"] = "submodule"
            file["added"] = file["deleted"] = None
        elif file["added"] is None:
            file["kind"] = "binary"
        else:
            file["kind"] = "symlink" if "120000" in modes else "text"
    tree = git(repo, "ls-tree", "--full-tree", "-z", base, "--", POLICY_PATH)
    repository = None
    policy_blob = None
    if tree:
        mode, kind, oid = tree.split(b"\t", 1)[0].split()
        require(mode in (b"100644", b"100755") and kind == b"blob", "Repository policy must be a regular JSON file")
        policy_blob = oid.decode()
        repository = validate_policy(read_json(git(repo, "cat-file", "blob", policy_blob)))
    return {
        "base_sha": base, "head_sha": head, "merge_base_sha": merge_base,
        "git_version": version, "files": sorted(files, key=lambda file: file["path"]),
        "policy_source": {"path": POLICY_PATH, "base_sha": base, "blob_sha": policy_blob, "present": repository is not None},
    }, repository


def exclusion_totals(facts, policy, ledger=None):
    if ledger is None:
        return 0, []
    code = "EXCLUSIONS_INVALID"
    object_keys(ledger, {"version", "base_sha", "head_sha", "files"}, "exclusions", code)
    require(type(ledger.get("version")) is int and ledger["version"] == 1, "Exclusion ledger version must be 1", code)
    require(ledger.get("base_sha") == facts["base_sha"] and ledger.get("head_sha") == facts["head_sha"], "Exclusions do not match the pinned base/head", code)
    require(isinstance(ledger.get("files"), list), "Exclusions require a files array", code)
    files = {file["path"]: file for file in facts["files"]}
    seen = set()
    total = 0
    for entry in ledger["files"]:
        object_keys(entry, {"path", "category", "added", "deleted", "evidence"}, "exclusion entry", code)
        path = entry.get("path")
        require(isinstance(path, str) and path in files and path not in seen, "Exclusion path must be changed and appear only once", code)
        seen.add(path)
        category = entry.get("category")
        require(isinstance(category, str) and category in EXCLUSION_MODES[policy["size_exclusions"]], f"Exclusion category not allowed: {category}", code)
        require(isinstance(entry.get("evidence"), str) and entry["evidence"].strip(), f"Missing exclusion evidence: {path}", code)
        for side in ("added", "deleted"):
            count = entry.get(side)
            available = files[path][side]
            require(type(count) is int and available is not None and 0 <= count <= available, f"Invalid excluded {side} count: {path}", code)
            total += count
        require(entry["added"] + entry["deleted"] > 0, f"Empty exclusion: {path}", code)
    return total, sorted(ledger["files"], key=lambda entry: entry["path"])


def evaluate(facts, policy, ledger=None):
    excluded, entries = exclusion_totals(facts, policy, ledger)
    files = facts["files"]
    unknown = [file["path"] for file in files if file["added"] is None]
    added = sum(file["added"] for file in files if file["added"] is not None)
    deleted = sum(file["deleted"] for file in files if file["deleted"] is not None)
    known_raw = added + deleted
    gates = []
    touched = {path for file in files for path in (file["path"], file["previous_path"]) if path is not None}
    policy_changes = sorted(path for path in touched if path == POLICY_PATH or path.startswith(POLICY_PATH + "/"))
    gates.append({"code": "REVIEW_POLICY_CHANGED", "status": "triggered" if policy_changes else "passed", "paths": policy_changes})
    patterns = [path_pattern(pattern) for pattern in policy["always_er_paths"]]
    matches = sorted(path for path in touched if any(pattern.fullmatch(path) for pattern in patterns))
    gates.append({"code": "ALWAYS_ER_PATH", "status": "triggered" if matches else "passed", "paths": matches})
    for key, code, subtotal, incomplete in (
        ("max_raw_lines", "SIZE_RAW_EXCEEDED", known_raw, bool(unknown)),
        ("max_changed_files", "FILE_COUNT_EXCEEDED", len(files), False),
        ("max_substantive_lines", "SIZE_SUBSTANTIVE_EXCEEDED", known_raw - excluded, bool(unknown)),
    ):
        limit = policy["limits"][key]
        status = "disabled" if limit is None else "triggered" if subtotal > limit else "unknown" if incomplete else "passed"
        gates.append({"code": code, "status": status, "limit": limit, "observed": None if incomplete else subtotal, "known_subtotal": subtotal})
    triggered = [gate for gate in gates if gate["status"] == "triggered"]
    pending = any(gate["status"] == "unknown" for gate in gates)
    return {
        "gate_status": "blocked" if triggered else "pending" if pending else "passed",
        "reason_codes": [gate["code"] for gate in triggered] + (["SIZE_UNKNOWN"] if pending else []),
        "gates": gates,
        "size": {
            "added": None if unknown else added, "deleted": None if unknown else deleted,
            "raw": None if unknown else known_raw, "known_raw_subtotal": known_raw,
            "excluded": excluded, "substantive": None if unknown else known_raw - excluded,
            "known_substantive_subtotal": known_raw - excluded, "unknown_line_files": unknown,
            "changed_files": len(files),
        },
        "exclusions": entries,
        "semantic_assessment": "required; passed gates alone are not EO",
    }


def run(args):
    facts, repository = collect(args.repo, args.base, args.head)
    override_bytes = Path(args.overrides).read_bytes() if args.overrides else None
    overrides = validate_policy(read_json(override_bytes)) if override_bytes is not None else None
    policy = resolve_policy(repository, overrides)
    ledger = None
    if args.exclusions:
        ledger = read_json(Path(args.exclusions).read_bytes(), "EXCLUSIONS_INVALID")
        object_keys(ledger, {"version", "base_sha", "head_sha", "files"}, "exclusions", "EXCLUSIONS_INVALID")
    return {
        "schema_version": 1, **facts, "policy": policy,
        "policy_sha256": hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "override_source": {"path": args.overrides, "sha256": hashlib.sha256(override_bytes).hexdigest()} if override_bytes is not None else None,
        **evaluate(facts, policy, ledger),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="Local Git repository containing the pinned commits")
    parser.add_argument("--base", required=True, help="Full PR base commit SHA")
    parser.add_argument("--head", required=True, help="Full PR head commit SHA")
    parser.add_argument("--overrides", help="JSON policy overrides; tightening only")
    parser.add_argument("--exclusions", help="SHA-bound, evidenced JSON exclusion ledger")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except TriageError as error:
        result = {"schema_version": 1, "gate_status": "error", "reason_codes": [error.code], "error": str(error)}
    except OSError as error:
        result = {"schema_version": 1, "gate_status": "error", "reason_codes": ["INPUT_UNAVAILABLE"], "error": str(error)}
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 2 if result["gate_status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
