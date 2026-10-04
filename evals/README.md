# Evals

Live reviewer-quality evaluation. It makes real model calls and is nondeterministic,
so it is kept out of PR CI and runs nightly. Run it yourself when you change prompts,
the validator, the parser or the renderer.

```bash
uv run python -m evals.run
uv run python -m evals.run --quality-runs 5 --baseline eval-results-main.json
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--model` | `ROBORAK_EVAL_MODEL` | The reviewer model. |
| `--judge-model` | `ROBORAK_EVAL_JUDGE_MODEL`, else the reviewer model | The model that grades output quality, always at temperature 0. |
| `--output` | `evals/eval-results.json` | Full results, per case. |
| `--quality-runs` | `3` | How many passes to make over the output-quality corpus. |
| `--baseline` | none | An earlier results file to compare output quality against. |

The run reports three separate sections, and only the first two decide the exit code.

## Detection and anchoring (`metrics`, gated)

`cases.yaml` holds 30 labeled defect and clean-control cases, plus the
evidence-policy controls. These measure whether the reviewer finds the right defect
on the right line, and whether it stays quiet otherwise:

| Metric | Gate |
| --- | --- |
| `recall` | >= 0.80 |
| `clean_false_positive_rate` | <= 0.10 |
| `unproven_blocker_rate` | <= 0.10 |
| `blocker_recall` | >= 0.80 |
| `anchor_accuracy` | >= 0.95 |
| `parse_success` | >= 0.99 |

## Chunking (`chunking_comparison`, gated)

`chunking_cases.yaml` runs every case through both the semantic planner and the
directory baseline. The semantic planner must not lose recall or add false positives
compared with the baseline.

## Output quality (`output_quality`, report-only)

`quality_cases.yaml` asks a different question: is the finding worth reading? An
LLM judge grades the finding as it would be published (`Form.PUBLISHED`), not the
model's raw candidate.

These scores never fail the run. Both the reviewer and the judge are models, so a
single run can come out lower than the last one with nothing wrong. Read them as a
trend: compare a branch with `main` through `--baseline`, and use `--quality-runs`
to see how much a number moves between runs before you trust a change in it.

### Case kinds

| `kind` | What it is | What is graded |
| --- | --- | --- |
| `clean_control` | A correct change. | Nothing. Every finding counts as an unsupported claim. |
| `supported_defect` | The diff alone proves the defect (`expected_category`, `expected_line`). | The finding within 3 lines of the defect. A miss is recorded as `matched: false` and left ungraded, because recall belongs to the detection corpus. |
| `unverified_concern` | A plausible risk the diff cannot prove. | Silence is fine. Otherwise the first finding is graded, and a blocker counts as an unsupported claim. |

### Rubric

The judge sees the reviewed diff and the rendered finding, and answers each check
with true or false:

| Check | Passes when |
| --- | --- |
| `states_trigger` | The finding names the concrete input or condition that causes the problem, not just that a problem exists. |
| `states_consequence` | It names the observable wrong behaviour at runtime or for a real input. |
| `states_fix` | It gives a practical fix direction, or names the specific thing to verify. "Be careful" does not count. |
| `faithful` | Every concrete claim is supported by the diff. If the finding is labelled unverified, it reads as reasoning, not as a reproduction it never ran. |
| `suggestion_safe` | Asked only when the finding carries a suggestion. The suggestion is a drop-in replacement for exactly the lines it replaces, fixes the problem, and introduces no new defect. |

**Unsupported claims** are scored in two places:

- A claim about code, callers or behaviour that the diff does not show fails
  `faithful`. So does an unverified finding that is worded as if it was reproduced.
- `unsupported_claims` counts findings the case itself rules out: any finding on a
  `clean_control`, and any blocker on an `unverified_concern`.

**Unsafe or inapplicable suggestions** fail `suggestion_safe`. The judge is given the
new-file lines the suggestion would replace, so it can catch three problems: a patch
that does not fit those lines, one that fixes the wrong thing, and one that breaks
surrounding code. A suggestion aimed at a file the case never touched gets an empty
replacement and cannot pass. A finding without a suggestion is left out of
`suggestion_safe` entirely, so it counts neither for nor against that rate.

**A judge that cannot answer is not a pass.** If the reply is missing a field, is
unparseable, or the call fails, the verdict is recorded as `null`. That lowers
`judge_completion` and grades nothing. A check that nothing graded is reported as
`null`, never as `1.0`.

### Reading the results

`output_quality.summary` gives the mean, min and max of every metric across the
quality runs:

- `overall` is the pass rate across all graded checks.
- Each rubric check has its own rate.
- `unsupported_claims` counts the claims the cases rule out (see above).
- `judge_completion` is the share of grading attempts the judge answered.

`output_quality.runs[]` keeps each pass, with a `by_kind` breakdown and the per-case
rows. With `--baseline`, `output_quality.delta` gives the current mean minus the
baseline mean for every metric that both runs produced. A positive delta is an
improvement for every metric except `unsupported_claims`.

A useful comparison:

```bash
git switch main && uv run python -m evals.run --output eval-results-main.json
git switch my-branch && uv run python -m evals.run --baseline eval-results-main.json
```

A shift smaller than the min/max spread is noise. A shift larger than the spread,
and in the same direction on a second run, is worth a look.

## Adding a case from a review-quality report

Reports come in through the **Review quality** issue template. Turn one into a case
without copying anything the reporter would not publish:

1. **Reduce, don't copy.** Write the smallest synthetic `before`/`after` pair that
   still reproduces the problem. Usually that is one function and one or two changed
   lines. Never paste the reporter's code verbatim, even if they marked it as public.
2. **Rename the domain.** Replace product, customer, table and service names with
   generic ones (`orders`, `users`, `billing`), and drop comments, URLs and literals
   that point back to the original codebase. Keep only the shape that triggers the
   behaviour.
3. **Choose the corpus.**
   - Wrong detection, a miss, a bad anchor or wrong severity goes in `cases.yaml`.
   - A finding that was correct but unclear, unsupported or carried an unsafe fix
     goes in `quality_cases.yaml`.
4. **Choose the `kind`.**
   - `clean_control` if the change was correct and the finding should not exist.
   - `supported_defect` if the defect is provable from the diff. Set
     `expected_category` and `expected_line` in new-file coordinates.
   - `unverified_concern` if it is a real risk that the diff cannot prove.
5. **Link it.** Add `source: "#<issue>"` so the case can be traced back to the report.
6. **Check it.** Run `uv run pytest tests/test_evals.py`, which validates the corpus
   shape. Then run the eval once with the case and confirm it behaves as the report
   describes before you count on it.
