---
name: kfp-authoring
description: >
  Start a live DAG preview in Omnigent before authoring a Kubeflow pipeline.
  Use when creating a KFP pipeline, changing its graph, or previewing an existing
  Python pipeline or compiled YAML/JSON spec.
---

# KFP authoring

**Preview first.** Before planning or generating pipeline logic, start the
watcher, send the user a clickable HTML path in an intermediate message, and
request opening it when running in Omnigent (unless already open).
An empty canvas is enough: the first successful compilation is not a prerequisite
for sharing the preview. Then author the requested pipeline while the user watches.
Omnigent's ordinary file previewer supplies the collapsible canvas and refreshes
saved HTML.

1. **Locate paths and environment.** Choose the source path (it need not exist
   yet), its Python environment, and a writable HTML output path inside the session's
   workspace. Preserve any unrelated existing output. Inspect Python sources
   before compiling: compilation executes module-level code. Ask before
   executing untrusted code; use an already compiled YAML/JSON spec instead
   when available.

2. **Check the CLI.** Run `kfp visualize --help` using that environment's KFP
   executable (for example `.venv/bin/kfp`). If the command is missing, explain
   that a KFP build with `visualize` support is required; ask before changing
   dependencies. The command is `kfp visualize`, not `kfp pipeline visualize`.

3. **Seed an immediately viewable canvas.** For a new Python source, create
   the file with just `# Pipeline authoring in progress` so the CLI's input path
   exists. Preserve existing sources. If the intended HTML export does not yet
   exist, write this lightweight placeholder (no compilation needed):

   ```html
   <!doctype html>
   <html lang="en"><head><meta charset="utf-8"><title>Pipeline preview</title></head>
   <body><p role="status">Authoring pipeline… The DAG will appear here automatically.</p></body></html>
   ```

   Keep an existing export rather than replacing it with the placeholder.
   For a new YAML/JSON source, create an empty file instead of the Python comment.
   Completion: the source path exists and the HTML path can already be opened.

4. **Start one watcher.** Use the session's persistent shell/terminal tool and
   keep its handle so you can inspect output or stop it. Reuse an existing
   watcher for the same source and output. Substitute and shell-quote the
   actual paths:

   ```bash
   kfp visualize pipeline.py --watch -o pipeline.html
   ```

   Add `--function NAME` if the Python source contains multiple pipelines.
   YAML/JSON inputs use the same command. Confirm the process started, retain
   its handle, and proceed immediately to sharing/opening the preview; do not block on the
   first compile. An initial "Preview not updated" error for the unfinished
   source is expected: watch mode stays alive, preserves the placeholder HTML,
   and retries after saved edits. No `--serve` or browser launch is needed.

5. **Share and open the preview now, before authoring.** Send an intermediate
   user-visible message with the absolute HTML workspace path as inline code:
   "Preview is ready at `/absolute/workspace/pipeline.html`. It will fill in as
   I author the pipeline." Keep this clickable path as a fallback.

   **In Omnigent**, use the file-opening capability from
   [PR #11421](https://github.com/omnigent-ai/omnigent/pull/11421):

   - If available UI context or the user confirms this HTML file is already
     open in the current session, leave it alone. Otherwise, call the exposed
     `ui_open_file` tool once, using the export path relative to the **session's
     workspace root**, not the shell's current directory:

     ```json
     {"path":"pipeline.html"}
     ```

   - Use forward slashes and a path inside that workspace. The API rejects
     absolute paths, `~`, traversal (`..`), URLs, backslashes, colons, control
     characters, and paths longer than 4096 characters. The absolute path in
     the chat message is for the link, not the tool argument.
   - If the tool is unavailable but the host provides a known authenticated
     Omnigent session client, use `POST /v1/sessions/{session_id}/ui/open-file`
     with the same JSON and the current session's normal authentication.
     This requires session edit access. Use only the supplied session/server;
     do not guess connection details or search for credentials.
   - There is no viewer-state query in this API. If state is unknown, make one
     opening request and remember the session/path you requested. Do not repeat
     it on each edit or compilation, or undo the user's decision to close or
     switch away from the preview. Re-request only if the user asks.
   - `{"subscribers": N}` (HTTP 202) reports stream delivery, not confirmed
     display. Zero means no connected subscriber; even a positive count can
     include background/non-UI clients. Requests are transient, not queued for
     later visits. Keep the link available rather than retrying or claiming the
     canvas is visibly open. Respect any unsaved-edit prompt or cancellation.

   **Outside Omnigent, or if opening is unavailable/denied**, simply tell the
   user to open the HTML path. If source or diff is active in Omnigent, select
   **View preview** (turn off diff first); the opening API does not set this mode.
   Completion: the user has the link and, when supported and not already open
   or requested, one open request has been sent. Proceed even with a placeholder
   or no connected UI. This is not a final-answer-only step.

6. **Author and iterate.** Now design, generate, and save the requested pipeline
   into the same watched source path. Build it incrementally so the first valid
   DAG appears early. Let the watcher regenerate the HTML. Check its terminal output for compilation failures and fix those
   before claiming the preview is current. The preview refreshes about every
   two seconds while visible; an unchanged graph can mean compilation failed
   or the watcher stopped. Changed exports reload the graph's pan/zoom state.

Leave the watcher running for continued authoring. Closing the preview does
not stop it. When asked to stop, interrupt only the watcher you started, using
its recorded terminal/process handle.
