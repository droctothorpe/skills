# Skills

I use this repository to maintain various LLM skills.

Name | Description
--- | ---
[eyeballs-optional](./skills/eyeballs-optional/SKILL.md) | Classifies PRs as eyeballs required (ER) or eyeballs optional (EO) using configurable policy gates and evidence-led impact assessment.
[metarev](./skills/metarev/SKILL.md) | Reviews a PR—or current-directory changes when no PR is supplied—with multiple models in parallel, consolidates findings, and offers to post the report after approval.

## Configure metarev

Copy [`config.example.yaml`](./skills/metarev/config.example.yaml) to `~/.config/metarev/config.yaml` to change the default roster. The file replaces the defaults entirely; metarev launches one reviewer per entry through the host harness's native subagent tools. The host must support the configured models and reasoning settings; unsupported capabilities are reported rather than silently substituted. In Pi, this uses [pi-subagents](https://www.npmjs.com/package/pi-subagents).

Metarev is instruction-only: no helper scripts or Python dependencies. Requires Git; PR lookup and posting also require authenticated `gh`. Without a PR, it reviews staged, unstaged, and non-ignored untracked changes under the current directory. The full consolidated report is displayed before asking for explicit permission to post it as a PR comment.

## Configure eyeballs-optional

Copy [`config.example.json`](./skills/eyeballs-optional/config.example.json) to `.eyeballs.json` in the repository whose PRs you triage. Customize substantive/raw changed-line ceilings, a changed-file ceiling, required-review paths, permitted size exclusions, disabled EO paths, and inspection scope. Defaults retain the 2,000-substantive-line ceiling; raw-line and file-count ceilings are opt-in.

The dependency-free Python helper collects pinned Git facts and evaluates hard gates. Semantic evidence is still required for EO; passing the helper is not approval. Policy comes from the PR's base commit, so a PR cannot raise its own limits.

See [Policy and deterministic gates](./skills/eyeballs-optional/references/policy.md) for configuration, commands, evidence ledgers, and output semantics. Requires Python 3.10+ and Git 2.43+.

Run the regression suite:

```sh
python3 -m unittest discover -s skills/eyeballs-optional/tests -v
```
