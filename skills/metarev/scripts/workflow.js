// prepare prepends a JSON spec. This body runs in the pi-subagents sandbox.
// Config objects preserve runs.all's collect-failure semantics.
const results = await runs.all(spec.models.map((model, index) => ({
  key: "review-" + index,
  label: "Review " + spec.label + " (" + model + ")",
  agent: "reviewer",
  model,
  context: "fresh",
  cwd: spec.repo,
  task: spec.task,
  outputSchema: spec.schema,
  progress: false
})));
return {
  version: 1,
  run: spec.run,
  reviews: results.map((result, index) => ({
    model: spec.models[index],
    ok: result.ok === true,
    report: result.structuredOutput ?? null,
    error: result.ok ? null : String(result.error ?? result.output ?? "Reviewer failed")
  }))
};
