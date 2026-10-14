"""CLI integration tests: real temporary Git repos; no models or GitHub writes."""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/metarev.py"
CATEGORIES = ["correctness", "design", "tests", "behaviour"]


class MetarevTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, HOME=str(self.root), TMPDIR=str(self.root),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        METAREV_FAKE_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "file.txt").write_text("initial\n")
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.info = {"title": "Test PR", "body": "Description", "baseRefName": "main",
                     "baseRefOid": "a" * 40, "headRefOid": "b" * 40,
                     "url": "https://github.com/example/repo/pull/12", "commits": []}
        self.scenario = {"info": self.info, "diff": "diff --git a/file.txt b/file.txt\n+new\n"}
        self.fake_gh()

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True, check=check).stdout.strip()

    def fake_gh(self):
        (self.root / "scenario.json").write_text(json.dumps(self.scenario))
        gh = self.bin / "gh"
        gh.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
root = pathlib.Path(os.environ['METAREV_FAKE_ROOT'])
scenario = json.loads((root / 'scenario.json').read_text())
args = sys.argv[1:]
with (root / 'gh.log').open('a') as log:
    log.write(json.dumps(args) + '\\n')
if scenario.get('fail'):
    print('gh unavailable', file=sys.stderr)
    sys.exit(1)
if args[:2] == ['pr', 'view']:
    info = scenario['info']
    if scenario.get('drift'):
        count = sum('"view"' in line for line in (root / 'gh.log').read_text().splitlines())
        if count > 1:
            info['headRefOid'] = 'c' * 40
    print(json.dumps(info))
elif args[:2] == ['pr', 'diff']:
    print(scenario['diff'], end='')
elif args[:2] == ['pr', 'comment']:
    if scenario.get('fail_comment'):
        print('posting failed', file=sys.stderr)
        sys.exit(1)
    body = pathlib.Path(args[args.index('--body-file') + 1]).read_bytes()
    (root / 'posted.md').write_bytes(body)
    print('https://github.com/example/repo/pull/12#issuecomment-42')
else:
    sys.exit(2)
''')
        gh.chmod(0o755)

    def cli(self, *args, ok=True, cwd=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                cwd=cwd or self.repo, env=self.env, capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("Traceback", result.stderr)
        return result

    def prepare(self, *args):
        return Path(json.loads(self.cli("prepare", *args).stdout)["run"])

    def local(self):
        (self.repo / "file.txt").write_text("changed\n")
        return self.prepare()

    def finding(self, **overrides):
        return dict(category="correctness", severity="P2", file="file.txt", line=1,
                    title="Bad return", body="This returns the wrong value.") | overrides

    def report(self, findings=None):
        findings = findings or []
        return {"findings": findings, "clear": [c for c in CATEGORIES if c not in {f['category'] for f in findings}],
                "verdict": "request changes" if findings else "approve", "limitations": []}

    def results(self, run, reports=None):
        models = json.loads((run / "manifest.json").read_text())["models"]
        return {"version": 1, "run": str(run), "reviews": [
            {"model": m, "ok": True, "report": (reports or [self.report()] * len(models))[i], "error": None}
            for i, m in enumerate(models)]}

    def collect(self, run, results=None):
        path = self.root / "status.json"
        path.write_text(json.dumps({"workflow": {"value": results or self.results(run)}}))
        return self.cli("collect", run, "--results", path)

    def rendered(self, run=None):
        run = run or self.prepare("--pr", "12")
        self.collect(run)
        self.cli("render", run)
        return run, json.loads((run / "preview.json").read_text())

    def test_local_captures_all_layers_without_changing_index(self):
        (self.repo / "file.txt").write_text("staged\n")
        self.git("add", "file.txt")
        index = self.git("write-tree")
        (self.repo / "file.txt").write_text("unstaged\n")
        (self.repo / "new file.txt").write_text("untracked\n")
        (self.repo / "--odd.txt").write_text("odd\n")
        (self.repo / ".gitignore").write_text("ignored\n")
        (self.repo / "ignored").write_text("SECRET\n")
        run = self.prepare()
        self.assertIn("+staged", (run / "staged.diff").read_text())
        self.assertIn("+unstaged", (run / "unstaged.diff").read_text())
        untracked = (run / "untracked.diff").read_text()
        self.assertIn("+untracked", untracked)
        self.assertIn("+odd", untracked)
        self.assertNotIn("SECRET", untracked)
        self.assertEqual(index, self.git("write-tree"))
        self.assertFalse((self.root / "gh.log").exists())

    def test_default_is_scoped_to_current_directory(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub/new").write_text("inside\n")
        (self.repo / "outside").write_text("outside\n")
        result = self.cli("prepare", cwd=self.repo / "sub")
        run = Path(json.loads(result.stdout)["run"])
        self.assertIn("+inside", (run / "untracked.diff").read_text())
        self.assertNotIn("+outside", (run / "untracked.diff").read_text())

    def test_empty_and_non_repository_fail_before_workflow(self):
        self.assertIn("No changes", self.cli("prepare", ok=False).stderr)
        self.cli("prepare", cwd=self.root, ok=False)
        self.assertFalse(list(self.root.glob("metarev-*")))

    def test_conflicts_stop_before_launch(self):
        self.git("checkout", "-b", "topic")
        (self.repo / "file.txt").write_text("topic\n")
        self.git("commit", "-am", "topic")
        self.git("checkout", "main")
        (self.repo / "file.txt").write_text("main\n")
        self.git("commit", "-am", "main")
        self.git("merge", "topic", check=False)
        self.assertIn("Resolve merge conflicts", self.cli("prepare", ok=False).stderr)

    def test_unborn_repository(self):
        repo = self.root / "unborn"
        repo.mkdir()
        self.git("init", cwd=repo)
        (repo / "new").write_text("new\n")
        self.git("add", ".", cwd=repo)
        run = Path(json.loads(self.cli("prepare", cwd=repo).stdout)["run"])
        self.assertIn("+new", (run / "staged.diff").read_text())

    def test_deleted_binary_and_symlink_changes(self):
        (self.repo / "file.txt").unlink()
        (self.repo / "binary").write_bytes(b"\x00\x01\xff")
        (self.repo / "link").symlink_to("missing")
        run = self.prepare()
        self.assertIn("deleted file", (run / "unstaged.diff").read_text())
        self.assertIn("Binary files", (run / "untracked.diff").read_text())
        self.assertIn("+missing", (run / "untracked.diff").read_text())

    def test_branch_uses_merge_base_and_named_branch(self):
        self.git("checkout", "-b", "topic")
        (self.repo / "file.txt").write_text("topic\n")
        self.git("commit", "-am", "topic")
        self.git("checkout", "main")
        (self.repo / "file.txt").write_text("main-only\n")
        self.git("commit", "-am", "main change")
        run = self.prepare("--base", "main", "--branch", "topic")
        patch = (run / "branch.diff").read_text()
        self.assertIn("-initial", patch)
        self.assertIn("+topic", patch)
        self.assertNotIn("main-only", patch)
        self.assertEqual(json.loads((run / "target.json").read_text())["branch"], "topic")

    def test_pr_capture_and_drift(self):
        run = self.prepare("--pr", self.info["url"])
        self.assertEqual((run / "pr.diff").read_text(), self.scenario["diff"])
        target = json.loads((run / "target.json").read_text())
        self.assertEqual(target["metadata"]["headRefOid"], "b" * 40)
        (self.root / "gh.log").unlink()
        self.scenario["drift"] = True
        self.fake_gh()
        self.assertIn("changed during capture", self.cli("prepare", "--pr", "12", ok=False).stderr)

    def test_invalid_or_empty_pr_fails(self):
        self.scenario["diff"] = ""
        self.fake_gh()
        self.assertIn("No changes", self.cli("prepare", "--pr", "12", ok=False).stderr)
        self.scenario["fail"] = True
        self.fake_gh()
        self.cli("prepare", "--pr", "12", ok=False)

    def test_config_replaces_defaults_and_preserves_off(self):
        config = self.root / ".config/metarev/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("models:\n  - model: test/a\n    reasoning: off\n  - model: test/b\n")
        run = self.local()
        self.assertEqual(json.loads((run / "manifest.json").read_text())["models"], ["test/a:off", "test/b"])

    def test_invalid_configs_fail_closed(self):
        config = self.root / "config.yaml"
        for content in ("models: []", "models:\n - model: bare", "models:\n - model: p/a\n   reasoning: nope",
                        "models:\n - model: p/a\n - model: p/a", "models: []\nmodels: []",
                        "models:\n - model: p/a\n   typo: high", "models: [", "bogus: []"):
            with self.subTest(content=content):
                config.write_text(content)
                self.cli("prepare", "--config", config, ok=False)
        self.cli("prepare", "--config", self.root / "missing", ok=False)

    @unittest.skipUnless(shutil.which("node"), "Node needed for workflow sandbox stub")
    def test_generated_workflow_identical_prompts_and_failure_collection(self):
        run = self.local()
        harness = '''const fs = require('node:fs');
const body = fs.readFileSync(process.argv[1], 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
let calls;
new AsyncFunction('runs', body)({all: async (items) => {
  calls = items;
  return items.map((x, i) => ({ok: i === 0, structuredOutput: i === 0 ? {findings: []} : null, error: 'quota'}));
}}).then(result => console.log(JSON.stringify({calls, result})));
'''
        result = subprocess.run(["node", "-e", harness, str(run / "workflow.js")], capture_output=True, text=True, check=True)
        out = json.loads(result.stdout)
        calls = out["calls"]
        self.assertEqual(calls[0]["task"], calls[1]["task"])
        self.assertNotEqual(calls[0]["model"], calls[1]["model"])
        self.assertTrue(all(c["context"] == "fresh" and "async" not in c for c in calls))
        self.assertFalse(out["result"]["reviews"][1]["ok"])
        self.assertEqual(out["result"]["reviews"][1]["error"], "quota")

    def test_exact_dedup_worst_severity_and_deterministic_render(self):
        run = self.local()
        result = self.results(run, [self.report([self.finding()]), self.report([self.finding(severity="P1")])])
        self.collect(run, result)
        self.assertEqual(json.loads((run / "groups.json").read_text()), [["r0f0", "r1f0"]])
        first = self.cli("render", run).stdout
        self.assertEqual(first, self.cli("render", run).stdout)
        self.assertEqual(first.count("### [P1] Bad return"), 1)
        self.assertIn("Overall worst finding: [P1]", first)
        self.assertIn("not the full PR diff", first)

    def test_semantic_groups_preserve_other_findings_and_all_locations(self):
        run = self.local()
        reports = [self.report([self.finding(), self.finding(title="Unique", severity="P3")]),
                   self.report([self.finding(title="Same bug differently worded", severity="P1", line=2)])]
        self.collect(run, self.results(run, reports))
        self.assertEqual(len(json.loads((run / "groups.json").read_text())), 3)
        (run / "groups.json").write_text(json.dumps([["r1f0", "r0f0"], ["r0f1"]]))
        out = self.cli("render", run).stdout
        self.assertIn("### [P1] Same bug differently worded", out)
        self.assertIn("file.txt:2, file.txt:1", out)
        self.assertIn("### [P3] Unique", out)

    def test_group_coverage_rejects_omissions_duplicates_and_unknowns(self):
        run = self.local()
        self.collect(run, self.results(run, [self.report([self.finding()]), self.report()]))
        for groups in ([], [["r0f0", "r0f0"]], [["unknown"]], [[]], ["r0f0"]):
            (run / "groups.json").write_text(json.dumps(groups))
            self.cli("render", run, ok=False)

    def test_failed_or_invalid_reviewer_is_not_approval(self):
        run = self.local()
        results = self.results(run)
        results["reviews"][1].update(ok=False, report=None, error="quota exhausted")
        self.collect(run, results)
        out = self.cli("render", run).stdout
        self.assertIn("Partial review", out)
        self.assertIn("quota exhausted", out)
        for bad in (None, {"findings": []}, self.report([self.finding(line=0)])):
            results["reviews"][1].update(ok=True, report=bad)
            self.collect(run, results)
            self.assertIn("Partial review", self.cli("render", run).stdout)

    def test_all_failed_or_wrong_run_or_roster_rejected(self):
        run = self.local()
        raw = self.results(run)
        bads = [dict(raw, run="/wrong"), dict(raw, reviews=raw["reviews"][:1])]
        failed = copy.deepcopy(raw)
        for r in failed["reviews"]:
            r.update(ok=False, error="unavailable")
        bads.append(failed)
        for bad in bads:
            path = self.root / "results.json"
            path.write_text(json.dumps(bad))
            self.cli("collect", run, "--results", path, ok=False)

    def test_malformed_and_incomplete_workflow_results_fail_cleanly(self):
        run = self.local()
        path = self.root / "results.json"
        for value in (None, [], {"workflow": None}, {"workflow": {}}, {"workflow": {"value": None}}):
            path.write_text(json.dumps(value))
            self.cli("collect", run, "--results", path, ok=False)

    def test_no_findings_and_limitations_are_reported(self):
        run = self.local()
        result = self.results(run)
        result["reviews"][0]["report"]["limitations"] = ["Binary contents were not inspected."]
        self.collect(run, result)
        out = self.cli("render", run).stdout
        self.assertIn("Overall worst finding: No findings.", out)
        self.assertIn("Binary contents were not inspected.", out)
        self.assertIn("No findings in: correctness, design, tests, behaviour", out)

    def test_no_associated_pr_keeps_local(self):
        run = self.local()
        self.scenario["fail"] = True
        self.fake_gh()
        _, preview = self.rendered(run)
        self.assertIsNone(preview["pr_url"])
        self.assertIn("stays local", preview["note"])
        self.cli("publish", run, "--approve", preview["sha256"], ok=False)
        self.assertFalse((self.root / "posted.md").exists())

    def test_publish_requires_render_and_exact_approval(self):
        run = self.prepare("--pr", "12")
        self.cli("publish", run, "--approve", "bad", ok=False)
        self.collect(run)
        self.cli("render", run)
        preview = json.loads((run / "preview.json").read_text())
        self.cli("publish", run, ok=False)
        self.cli("publish", run, "--approve", "wrong", ok=False)
        (run / "report.md").write_text("edited report")
        self.cli("publish", run, "--approve", preview["sha256"], ok=False)
        self.assertFalse((self.root / "posted.md").exists())

    def test_publish_exact_bytes_and_idempotence(self):
        run, preview = self.rendered()
        first = self.cli("publish", run, "--approve", preview["sha256"])
        second = self.cli("publish", run, "--approve", preview["sha256"])
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn("#issuecomment-42", first.stdout)
        self.assertEqual((self.root / "posted.md").read_bytes(), (run / "report.md").read_bytes())
        calls = [json.loads(line) for line in (self.root / "gh.log").read_text().splitlines()]
        self.assertEqual(sum(c[:2] == ["pr", "comment"] for c in calls), 1)

    def test_posting_failure_has_no_success_receipt(self):
        run, preview = self.rendered()
        self.scenario["fail_comment"] = True
        self.fake_gh()
        result = self.cli("publish", run, "--approve", preview["sha256"], ok=False)
        self.assertIn("posting failed", result.stderr)
        self.assertFalse((run / "posted.json").exists())
        self.assertFalse((self.root / "posted.md").exists())

    def test_publish_rejects_changed_pr_and_does_not_claim_failure_success(self):
        run, preview = self.rendered()
        self.scenario["info"]["headRefOid"] = "c" * 40
        self.fake_gh()
        self.assertIn("PR changed", self.cli("publish", run, "--approve", preview["sha256"], ok=False).stderr)
        self.assertFalse((self.root / "posted.md").exists())
        self.scenario["fail"] = True
        self.fake_gh()
        self.cli("publish", run, "--approve", preview["sha256"], ok=False)
        self.assertFalse((run / "posted.json").exists())


if __name__ == "__main__":
    unittest.main()
