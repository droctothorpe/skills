"""Deterministic policy tests; semantic agent evaluations are separate fixtures."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "triage.py"
spec = importlib.util.spec_from_file_location("triage", SCRIPT)
triage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(triage)


def facts(added=100, deleted=20, path="src/main.py"):
    return {
        "base_sha": "a" * 40, "head_sha": "b" * 40,
        "files": [{"path": path, "previous_path": None, "added": added, "deleted": deleted}],
    }


def ledger(fact, added=100, deleted=20, category="verified_transformation"):
    return {
        "version": 1, "base_sha": fact["base_sha"], "head_sha": fact["head_sha"],
        "files": [{
            "path": fact["files"][0]["path"], "added": added, "deleted": deleted,
            "category": category, "evidence": "All changed hunks verified against the pinned codemod and references.",
        }],
    }


class PolicyTests(unittest.TestCase):
    def test_example_matches_defaults(self):
        self.assertEqual(json.loads((SKILL / "config.example.json").read_text()), triage.DEFAULT_POLICY)

    def test_default_resolution_does_not_mutate_defaults(self):
        resolved = triage.resolve_policy()
        resolved["limits"]["max_substantive_lines"] = 0
        self.assertEqual(triage.DEFAULT_POLICY["limits"]["max_substantive_lines"], 2000)

    def test_invalid_policies_fail_closed(self):
        invalid = [
            None, [], "defaults", {}, {"version": True}, {"version": 2},
            {"version": 1, "ceiling": 20}, {"version": 1, "limits": []},
            {"version": 1, "limits": {"max_files": 1}},
            {"version": 1, "limits": {"max_substantive_lines": None}},
            {"version": 1, "limits": {"max_raw_lines": True}},
            {"version": 1, "limits": {"max_changed_files": -1}},
            {"version": 1, "limits": {"max_raw_lines": 1.5}},
            {"version": 1, "limits": {"max_raw_lines": "100"}},
            {"version": 1, "size_exclusions": []},
            {"version": 1, "inspection": "fast"},
            {"version": 1, "disabled_eo_paths": ["typo"]},
            {"version": 1, "always_er_paths": "src/**"},
            {"version": 1, "always_er_paths": ["src/**", "src/**"]},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(triage.TriageError):
                triage.validate_policy(value)

    def test_strict_json(self):
        for raw in ('{"version": 1, "version": 1}', '{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}', '{', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(triage.TriageError):
                triage.read_json(raw)

    def test_tightening_overrides(self):
        repo = {"version": 1, "always_er_paths": ["auth/**"], "limits": {"max_raw_lines": 1000}}
        overrides = {
            "version": 1, "always_er_paths": ["auth/**", "infra/**"],
            "limits": {"max_substantive_lines": 400, "max_raw_lines": 500},
            "disabled_eo_paths": ["exact_revert"], "size_exclusions": "none",
            "inspection": "stop_on_first_er",
        }
        result = triage.resolve_policy(repo, overrides)
        self.assertEqual(result["limits"]["max_raw_lines"], 500)
        self.assertEqual(result["size_exclusions"], "none")
        self.assertEqual(result["always_er_paths"], ["auth/**", "infra/**"])

    def test_relaxing_overrides_are_rejected(self):
        repo = {
            "version": 1, "limits": {"max_raw_lines": 500},
            "always_er_paths": ["auth/**"], "disabled_eo_paths": ["exact_revert"],
            "size_exclusions": "generated_and_lockfiles",
        }
        for patch in (
            {"limits": {"max_raw_lines": None}}, {"limits": {"max_raw_lines": 501}},
            {"always_er_paths": []}, {"disabled_eo_paths": []},
            {"size_exclusions": "verified_mechanical"},
        ):
            with self.subTest(patch=patch), self.assertRaises(triage.TriageError):
                triage.resolve_policy(repo, {"version": 1, **patch})

    def test_repository_can_change_defaults(self):
        self.assertEqual(triage.resolve_policy({"version": 1, "limits": {"max_substantive_lines": 3000}})["limits"]["max_substantive_lines"], 3000)

    def test_path_matching(self):
        for pattern, path, expected in (
            ("**/*.py", "x.py", True), ("**/*.py", "a/b/x.py", True),
            ("src/*", "src/a/b", False), ("src/?", "src/x", True),
            ("src/?", "src/xx", False), (".github/workflows/**", ".github/workflows/test.yml", True),
            ("auth/**", "Auth/main.py", False), ("a/**/b", "a/b", True),
            ("**", "line\nbreak", True),
        ):
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(bool(triage.path_pattern(pattern).fullmatch(path)), expected)
        for pattern in ("/src/**", "../auth/**", "src/./a", "src//a", "src/**.py", "src/[ab]", "!x", "src\\x", ""):
            with self.subTest(pattern=pattern), self.assertRaises(triage.TriageError):
                triage.path_pattern(pattern)


class GateTests(unittest.TestCase):
    def test_ceiling_boundaries(self):
        for count, expected in ((1999, "passed"), (2000, "passed"), (2001, "blocked")):
            with self.subTest(count=count):
                result = triage.evaluate(facts(count, 0), triage.resolve_policy())
                self.assertEqual(result["gate_status"], expected)
        for count, expected in ((119, "blocked"), (120, "passed"), (121, "passed")):
            policy = triage.resolve_policy({"version": 1, "limits": {"max_raw_lines": count}})
            self.assertEqual(triage.evaluate(facts(), policy)["gate_status"], expected)

    def test_added_plus_deleted_not_net(self):
        result = triage.evaluate(facts(1001, 1001), triage.resolve_policy())
        self.assertEqual(result["size"]["raw"], 2002)
        self.assertIn("SIZE_SUBSTANTIVE_EXCEEDED", result["reason_codes"])

    def test_zero_limits_are_enabled(self):
        policy = triage.resolve_policy({"version": 1, "limits": {"max_raw_lines": 0, "max_changed_files": 0, "max_substantive_lines": 0}})
        self.assertEqual(triage.evaluate({**facts(), "files": []}, policy)["gate_status"], "passed")
        result = triage.evaluate(facts(1, 0), policy)
        self.assertEqual(result["reason_codes"], ["SIZE_RAW_EXCEEDED", "FILE_COUNT_EXCEEDED", "SIZE_SUBSTANTIVE_EXCEEDED"])

    def test_file_count_includes_generated_files(self):
        fact = facts(path="generated/output.ts")
        for limit, expected in ((0, "blocked"), (1, "passed")):
            policy = triage.resolve_policy({"version": 1, "limits": {"max_changed_files": limit}})
            self.assertEqual(triage.evaluate(fact, policy, ledger(fact))["gate_status"], expected)

    def test_default_optional_limits_are_disabled(self):
        result = triage.evaluate(facts(), triage.resolve_policy())
        self.assertEqual([g["status"] for g in result["gates"] if g["code"] in ("SIZE_RAW_EXCEEDED", "FILE_COUNT_EXCEEDED")], ["disabled", "disabled"])

    def test_mechanical_migration_and_absolute_raw_gate(self):
        fact = facts(3000, 3000)
        exclusion = ledger(fact, 2800, 2900)
        result = triage.evaluate(fact, triage.resolve_policy(), exclusion)
        self.assertEqual(result["size"]["substantive"], 300)
        self.assertEqual(result["gate_status"], "passed")
        policy = triage.resolve_policy({"version": 1, "limits": {"max_raw_lines": 5999}})
        self.assertEqual(triage.evaluate(fact, policy, exclusion)["reason_codes"], ["SIZE_RAW_EXCEEDED"])

    def test_lockfile_is_not_automatically_excluded(self):
        self.assertEqual(triage.evaluate(facts(3000, 0, "package-lock.json"), triage.resolve_policy())["size"]["substantive"], 3000)

    def test_unknown_counts_are_not_zero(self):
        fact = facts(None, None, "asset.bin")
        result = triage.evaluate(fact, triage.resolve_policy())
        self.assertEqual(result["gate_status"], "pending")
        self.assertIsNone(result["size"]["raw"])
        self.assertEqual(result["reason_codes"], ["SIZE_UNKNOWN"])
        fact["files"] += facts(2001, 0)["files"]
        result = triage.evaluate(fact, triage.resolve_policy())
        self.assertEqual(result["gate_status"], "blocked")
        self.assertIn("SIZE_SUBSTANTIVE_EXCEEDED", result["reason_codes"])

    def test_old_and_new_paths_match_review_rules(self):
        for old, new in (("auth/a.py", "lib/a.py"), ("lib/a.py", "auth/a.py")):
            fact = facts(0, 0, new)
            fact["files"][0]["previous_path"] = old
            policy = triage.resolve_policy({"version": 1, "always_er_paths": ["auth/**"]})
            self.assertIn("ALWAYS_ER_PATH", triage.evaluate(fact, policy)["reason_codes"])

    def test_policy_rename_is_always_er(self):
        fact = facts(0, 0, "retired.json")
        fact["files"][0]["previous_path"] = triage.POLICY_PATH
        self.assertIn("REVIEW_POLICY_CHANGED", triage.evaluate(fact, triage.resolve_policy())["reason_codes"])

    def test_passing_gates_never_emit_eo(self):
        result = triage.evaluate(facts(), triage.resolve_policy())
        self.assertNotIn("verdict", result)
        self.assertIn("required", result["semantic_assessment"])


class ExclusionTests(unittest.TestCase):
    def test_invalid_ledgers(self):
        fact = facts()
        valid = ledger(fact)
        invalid = []
        for key, value in (("base_sha", "c" * 40), ("head_sha", "c" * 40), ("version", True), ("files", {})):
            invalid.append({**valid, key: value})
        invalid.append({**valid, "files": valid["files"] * 2})
        invalid.append({**valid, "extra": True})
        for patch in (
            {"added": 101}, {"deleted": 21}, {"added": -1}, {"added": True},
            {"added": 0.5}, {"evidence": " "}, {"category": "docs"},
            {"path": "unchanged.py"}, {"added": 0, "deleted": 0}, {"extra": 1},
        ):
            invalid.append({**valid, "files": [{**valid["files"][0], **patch}]})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(triage.TriageError) as error:
                triage.exclusion_totals(fact, triage.resolve_policy(), value)
            self.assertEqual(error.exception.code, "EXCLUSIONS_INVALID")

    def test_partial_exclusions(self):
        fact = facts()
        result = triage.evaluate(fact, triage.resolve_policy(), ledger(fact, 50, 10))
        self.assertEqual(result["size"]["excluded"], 60)
        self.assertEqual(result["size"]["substantive"], 60)

    def test_exclusion_modes(self):
        fact = facts()
        for mode, categories in triage.EXCLUSION_MODES.items():
            policy = triage.resolve_policy({"version": 1, "size_exclusions": mode})
            for category in triage.EXCLUSION_MODES["verified_mechanical"]:
                with self.subTest(mode=mode, category=category):
                    if category in categories:
                        self.assertEqual(triage.exclusion_totals(fact, policy, ledger(fact, category=category))[0], 120)
                    else:
                        with self.assertRaises(triage.TriageError):
                            triage.exclusion_totals(fact, policy, ledger(fact, category=category))

    def test_unknown_counts_cannot_be_excluded(self):
        fact = facts(None, None)
        with self.assertRaises(triage.TriageError):
            triage.exclusion_totals(fact, triage.resolve_policy(), ledger(fact))


class GitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "user.name", "EO tests")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.repo / "no-hooks"))
        self.put("initial.txt", "initial\n")
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def put(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture", "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def run_helper(self, base=None, head=None):
        return triage.run(argparse.Namespace(repo=str(self.repo), base=base or self.base, head=head or self.git("rev-parse", "HEAD"), overrides=None, exclusions=None))

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo), "--base", self.base, "--head", self.git("rev-parse", "HEAD"), *args], capture_output=True, text=True)

    def test_complete_counts_with_unusual_filenames_and_no_newline(self):
        self.put("a tab\tand newline\nfile.txt", "one\ntwo")
        self.put("initial.txt", "changed\n")
        self.put("unicodé.txt", "three\n")
        self.commit()
        result = self.run_helper()
        self.assertEqual(result["size"]["raw"], 5)
        self.assertEqual(result["size"]["changed_files"], 3)
        self.assertIn("a tab\tand newline\nfile.txt", [f["path"] for f in result["files"]])

    def test_inventory_not_api_patch_or_page_limited(self):
        for i in range(105):
            self.put(f"files/{i}.txt", f"{i}\n")
        self.commit()
        result = self.run_helper()
        self.assertEqual(result["size"]["changed_files"], 105)
        self.assertEqual(result["size"]["raw"], 105)

    def test_base_policy_not_pr_or_dirty_worktree(self):
        self.put(triage.POLICY_PATH, json.dumps({"version": 1, "limits": {"max_raw_lines": 1}}))
        self.base = self.commit()
        self.put(triage.POLICY_PATH, json.dumps({"version": 1, "limits": {"max_raw_lines": 9000}}))
        self.put("new.txt", "a\nb\n")
        head = self.commit()
        self.put(triage.POLICY_PATH, "invalid working-tree policy")
        before = self.git("status", "--porcelain")
        result = self.run_helper(head=head)
        self.assertEqual(result["policy"]["limits"]["max_raw_lines"], 1)
        self.assertIn("REVIEW_POLICY_CHANGED", result["reason_codes"])
        self.assertIn("SIZE_RAW_EXCEEDED", result["reason_codes"])
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual(result, self.run_helper(head=head))

    def test_policy_added_in_pr_uses_defaults_and_is_er(self):
        self.put(triage.POLICY_PATH, '{"version": 1, "limits": {"max_substantive_lines": 9999}}')
        self.commit()
        result = self.run_helper()
        self.assertEqual(result["policy"], triage.DEFAULT_POLICY)
        self.assertIn("REVIEW_POLICY_CHANGED", result["reason_codes"])

    def test_policy_removal_is_er_under_base_policy(self):
        self.put(triage.POLICY_PATH, '{"version": 1, "limits": {"max_raw_lines": 5}}')
        self.base = self.commit()
        (self.repo / triage.POLICY_PATH).unlink()
        self.commit()
        result = self.run_helper()
        self.assertEqual(result["policy"]["limits"]["max_raw_lines"], 5)
        self.assertIn("REVIEW_POLICY_CHANGED", result["reason_codes"])

    def test_invalid_base_policy_and_null_never_fall_back(self):
        for content in ("null", "[]", '{"version": 1, "oops": 1}', "broken"):
            self.put(triage.POLICY_PATH, content)
            base = self.commit()
            with self.subTest(content=content), self.assertRaises(triage.TriageError):
                self.run_helper(base=base, head=base)

    def test_symlink_policy_is_rejected(self):
        self.put("actual.json", '{"version": 1}')
        (self.repo / triage.POLICY_PATH).symlink_to("actual.json")
        base = self.commit()
        with self.assertRaises(triage.TriageError):
            self.run_helper(base=base, head=base)

    def test_policy_comes_from_base_tip_diff_from_merge_base(self):
        ancestor = self.base
        self.git("checkout", "-qb", "pr")
        self.put("pr.txt", "pr\n")
        head = self.commit()
        self.git("checkout", "--detach", ancestor)
        self.put(triage.POLICY_PATH, '{"version": 1, "limits": {"max_raw_lines": 0}}')
        base = self.commit()
        result = self.run_helper(base=base, head=head)
        self.assertEqual(result["merge_base_sha"], ancestor)
        self.assertEqual([f["path"] for f in result["files"]], ["pr.txt"])
        self.assertIn("SIZE_RAW_EXCEEDED", result["reason_codes"])

    def test_exact_rename_counts_once_and_matches_old_path(self):
        self.put("auth/file.txt", "same\ncontent\n")
        self.put(triage.POLICY_PATH, '{"version": 1, "always_er_paths": ["auth/**"]}')
        self.base = self.commit()
        self.git("mv", "auth/file.txt", "moved.txt")
        self.commit()
        result = self.run_helper()
        self.assertEqual(result["size"]["changed_files"], 1)
        self.assertEqual(result["size"]["raw"], 0)
        self.assertEqual(result["files"][0]["status"], "R100")
        self.assertIn("ALWAYS_ER_PATH", result["reason_codes"])

    def test_edited_move_counts_delete_and_add(self):
        self.put("old.txt", "a\nb\nc\n")
        self.base = self.commit()
        self.git("mv", "old.txt", "new.txt")
        self.put("new.txt", "a\nb\nd\n")
        self.commit()
        result = self.run_helper()
        self.assertEqual(result["size"]["changed_files"], 2)
        self.assertEqual(result["size"]["raw"], 6)

    def test_binary_and_submodule_counts_are_unknown(self):
        self.put("asset.bin", b"\x00binary\n")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.base},vendor")
        self.git("add", "asset.bin")
        self.git("commit", "-qm", "binary and gitlink")
        result = self.run_helper()
        self.assertEqual({f["kind"] for f in result["files"]}, {"binary", "submodule"})
        self.assertEqual(result["gate_status"], "pending")
        self.assertIsNone(result["size"]["raw"])

    def test_mode_only_and_symlink_changes_remain_in_inventory(self):
        self.git("update-index", "--chmod=+x", "initial.txt")
        (self.repo / "link").symlink_to("initial.txt")
        self.git("add", "link")
        self.git("commit", "-qm", "mode and symlink")
        result = self.run_helper()
        self.assertEqual(result["size"]["changed_files"], 2)
        self.assertEqual({f["kind"] for f in result["files"]}, {"text", "symlink"})

    def test_attributes_are_pinned_not_from_dirty_checkout(self):
        self.put("data.txt", "a\nb\n")
        head = self.commit()
        self.put(".gitattributes", "*.txt -diff\n")
        result = self.run_helper(head=head)
        self.assertEqual(result["size"]["raw"], 2)
        self.assertEqual(result["gate_status"], "passed")

    def test_unpinned_info_attributes_are_rejected(self):
        self.put(".git/info/attributes", "# comment only\n\n")
        self.assertEqual(self.run_helper()["gate_status"], "passed")
        self.put(".git/info/attributes", "*.txt -diff\n")
        with self.assertRaises(triage.TriageError) as error:
            self.run_helper()
        self.assertEqual(error.exception.code, "INSPECTION_INCOMPLETE")

    def test_ambient_git_redirects_do_not_override_repo(self):
        head = self.git("rev-parse", "HEAD")
        with patch.dict(os.environ, {"GIT_DIR": "/nonexistent", "GIT_WORK_TREE": "/nonexistent"}):
            self.assertEqual(self.run_helper(head=head)["head_sha"], head)

    def test_subdirectory_repo_does_not_hide_root_policy(self):
        self.put(triage.POLICY_PATH, '{"version": 1, "limits": {"max_raw_lines": 0}}')
        self.base = self.commit()
        self.put("nested/new.txt", "change\n")
        head = self.commit()
        result = triage.run(argparse.Namespace(repo=str(self.repo / "nested"), base=self.base, head=head, overrides=None, exclusions=None))
        self.assertEqual(result["policy"]["limits"]["max_raw_lines"], 0)
        self.assertEqual(result["files"][0]["path"], "nested/new.txt")

    def test_external_diff_and_textconv_are_not_executed(self):
        marker = self.repo / "executed"
        command = f"touch '{marker}'"
        self.git("config", "diff.external", command)
        self.git("config", "diff.evil.textconv", command)
        self.put(".gitattributes", "*.txt diff=evil\n")
        self.base = self.commit()
        self.put("initial.txt", "changed\n")
        self.commit()
        self.assertEqual(self.run_helper()["size"]["raw"], 2)
        self.assertFalse(marker.exists())

    def test_moving_refs_missing_history_and_commits_fail_closed(self):
        for base in ("HEAD", "f" * 40):
            with self.subTest(base=base), self.assertRaises(triage.TriageError):
                self.run_helper(base=base)
        self.git("checkout", "--orphan", "unrelated")
        self.git("rm", "-rf", ".")
        self.put("unrelated.txt", "x\n")
        head = self.commit()
        with self.assertRaises(triage.TriageError):
            self.run_helper(head=head)

    def test_ambiguous_merge_bases_fail_closed(self):
        tree = self.git("rev-parse", f"{self.base}^{{tree}}")
        left = self.git("commit-tree", tree, "-p", self.base, "-m", "left")
        right = self.git("commit-tree", tree, "-p", self.base, "-m", "right")
        base = self.git("commit-tree", tree, "-p", left, "-p", right, "-m", "merge one")
        head = self.git("commit-tree", tree, "-p", right, "-p", left, "-m", "merge two")
        with self.assertRaises(triage.TriageError) as error:
            self.run_helper(base=base, head=head)
        self.assertIn("unique merge base", str(error.exception))

    def test_cli_evidenced_exclusions_and_override_provenance(self):
        self.put("initial.txt", "changed\n")
        head = self.commit()
        exclusion = ledger({**facts(1, 1, "initial.txt"), "base_sha": self.base, "head_sha": head}, 1, 1)
        self.put("ledger.json", json.dumps(exclusion))
        self.put("override.json", '{"version": 1, "limits": {"max_substantive_lines": 0}}')
        result = self.cli("--exclusions", str(self.repo / "ledger.json"), "--overrides", str(self.repo / "override.json"))
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertEqual(report["size"]["substantive"], 0)
        self.assertEqual(report["gate_status"], "passed")
        self.assertEqual(len(report["override_source"]["sha256"]), 64)
        self.assertEqual(len(report["policy_sha256"]), 64)
        exclusion["head_sha"] = self.base
        self.put("ledger.json", json.dumps(exclusion))
        result = self.cli("--exclusions", str(self.repo / "ledger.json"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["reason_codes"], ["EXCLUSIONS_INVALID"])

    def test_cli_returns_machine_readable_errors(self):
        for flag in ("--overrides", "--exclusions"):
            path = self.repo / "null.json"
            path.write_text("null")
            result = self.cli(flag, str(path))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["gate_status"], "error")
        result = self.cli("--overrides", str(self.repo / "missing.json"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["reason_codes"], ["INPUT_UNAVAILABLE"])

    def test_cli_zero_exit_does_not_mean_eo(self):
        self.put(triage.POLICY_PATH, '{"version": 1}')
        self.commit()
        result = self.cli()
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertEqual(report["gate_status"], "blocked")
        self.assertNotIn("verdict", report)


class FixtureVocabularyTests(unittest.TestCase):
    def test_semantic_fixtures_use_documented_ids(self):
        cases = json.loads((SKILL / "tests" / "semantic-cases.json").read_text())
        reference = (SKILL / "references" / "policy.md").read_text()
        skill = (SKILL / "SKILL.md").read_text()
        ids = set()
        for case in cases:
            self.assertNotIn(case["id"], ids)
            ids.add(case["id"])
            self.assertIn(case["expected_verdict"], ("EO", "ER"))
            self.assertTrue(case["scenario"])
            if case["expected_verdict"] == "EO":
                self.assertIn(case["expected_path"], triage.EO_PATHS)
            else:
                self.assertIn(f'`{case["expected_reason"]}`', reference)
        for path in triage.EO_PATHS:
            self.assertIn(f"`{path}`", skill)


if __name__ == "__main__":
    unittest.main()
