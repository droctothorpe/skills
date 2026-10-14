---
name: metarev
description: "Multi-model PR review: spawns one reviewer subagent per configured model in parallel, then consolidates their findings into a single report. Use when the user asks for a metarev or wants a PR reviewed by multiple models."
---

Review one PR with several models in parallel, then consolidate. Every reviewer gets the same brief; the only variable is the model, so disagreements between reports are model signal, not prompt noise.

## 1. Pin the review target

Resolve whatever the user gave (PR number, URL, branch) to a diff:

- GitHub PR: `gh pr view <ref> --json title,body,baseRefName,url` and `gh pr diff <ref>`.
- Local branch: `git diff <base>...HEAD` (three-dot, against the merge-base) plus `git log <base>..HEAD --oneline`.

Save the diff to a file (e.g. `/tmp/metarev-<ref>.diff`) so each child reads it instead of receiving it inline. Fail here, before spawning anything, if the ref doesn't resolve or the diff is empty.

## 2. Resolve the model roster

Read `~/.config/metarev/config.yaml` if it exists. If it doesn't, use these defaults:

```yaml
models:
  - model: github-copilot/gpt-6-astra
    reasoning: high
  - model: claude-bridge/claude-fable-5
    reasoning: high
```

The config file replaces the roster entirely (no merging). Each entry becomes a subagent model string `<model>:<reasoning>`, using the pi-subagents thinking suffix (`off`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`). An entry without `reasoning` uses the agent default (no suffix).

## 3. Launch one reviewer per model, in parallel

Use pi-subagents (enable the tools first if they aren't loaded). Launch via a workflow script with `runs.all([...])`: one fresh-context `reviewer` child per roster entry, each with `model: "<model>:<reasoning>"` and a label like `Review PR #123 (gpt-6-astra)`.

Every child gets the identical prompt, containing:

- PR title, description, and base branch.
- The diff file path and commit list, with an instruction to read the diff file.
- The brief: "Review this change. Report (a) correctness bugs, (b) design concerns, (c) missing or inadequate tests, (d) risky or unintended behaviour changes. Cite `file:line` for every finding and rank findings by severity. State explicitly if you found nothing in a category. Under 500 words."

Reviewers are read-only: no edits, no commits.

## 4. Consolidate

When all children return, merge their reports into one:

- **Consensus** first: findings flagged by more than one model, deduplicated, worst severity kept, with the agreeing models named.
- **Per-model** after: findings unique to one model, attributed to it, verbatim or lightly cleaned.
- End with a one-line verdict per model (approve / request changes / blocking issue) and the overall worst finding.

If a child fails (quota, availability, bad model id), report which model failed and consolidate the remaining reports; never silently substitute a different model.

## 5. Print, then offer to comment

Print the full consolidated report to stdout / the user-visible conversation so the user can review it before being asked to publish it.

If the target is a local branch, resolve its associated PR with `gh pr view --json url`. If no PR is found, leave the report local and explain that there is no PR to comment on.

Once the report has been displayed and a PR is identified, use `ask_user_question` to ask: "Post this consolidated report as a comment on <PR URL>?" Offer two options:

- **Post comment** — Publish the displayed report as one PR comment.
- **Keep local** — Leave the report in this session without posting.

Only an explicit choice to post authorizes publication; a decline, cancellation, or missing response leaves the report local. If the user requests changes, display the revised report and ask again before posting.

On approval, save the exact displayed report to a Markdown file and run `gh pr comment <PR URL> --body-file <report-path>`. Return the comment URL on success; report any failure without claiming it was posted.
