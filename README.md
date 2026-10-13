# Skills

I use this repository to maintain various LLM skills.

Name | Description
--- | ---
[eyeballs-optional](./skills/eyeballs-optional/SKILL.md) | Classifies PRs as eyeballs required (ER) or eyeballs optional (EO) using configurable policy gates and evidence-led impact assessment.

## Configure eyeballs-optional

Copy [`config.example.json`](./skills/eyeballs-optional/config.example.json) to `.eyeballs.json` in the repository whose PRs you triage. Customize substantive/raw changed-line ceilings, a changed-file ceiling, required-review paths, permitted size exclusions, disabled EO paths, and inspection scope. Defaults retain the 2,000-substantive-line ceiling; raw-line and file-count ceilings are opt-in.

The dependency-free Python helper collects pinned Git facts and evaluates hard gates. Semantic evidence is still required for EO; passing the helper is not approval. Policy comes from the PR's base commit, so a PR cannot raise its own limits.

See [Policy and deterministic gates](./skills/eyeballs-optional/references/policy.md) for configuration, commands, evidence ledgers, and output semantics. Requires Python 3.10+ and Git 2.43+.

Run the regression suite:

```sh
python3 -m unittest discover -s skills/eyeballs-optional/tests -v
```
