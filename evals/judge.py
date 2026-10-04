"""An LLM rubric that grades a rendered finding for trigger, consequence, fix and safety.

Eval-only, deliberately outside ``src/``: this judge prompt never ships with the
reviewer. The reviewer-quality corpus already checks that a finding lands on the
right category and line; this adds the second half of issue #95 -- whether the
finding the reader actually sees *says* what breaks, what it costs, and how to fix
it, and does so without claiming a reproduction it never ran. Issue #98 adds the
last check: a suggested patch must apply to the lines it replaces and must not make
things worse. The rubric itself is documented in ``evals/README.md``.

A judge that cannot answer (an unparseable reply, a model failure) returns ``None``
rather than a pass. An inconclusive check is never recorded as a passing one, the
same bar the reviewer's own investigation stage holds itself to.
"""

from __future__ import annotations

import yaml

from roborak.llm.client import LLMClient, LLMError

JUDGE_SYSTEM = """\
You grade one code-review finding for clarity and factual support. You are given the
diff that was reviewed and the finding as the author will read it. Judge only what the
finding says against what the diff shows; do not review the code yourself.

Answer each question with true or false:

- states_trigger: does the finding name the concrete input or condition that provokes
  the problem, not merely that a problem exists?
- states_consequence: does it name the observable wrong behaviour -- what goes wrong
  for a real input or at runtime?
- states_fix: does it give a practical fix direction, or name the specific thing to
  verify? A vague "be careful" does not count.
- faithful: is every concrete claim supported by the diff? A claim about code, callers
  or behaviour the diff does not show is unsupported and fails this check. When the
  finding is labelled unverified, it must also read as reasoning rather than a
  reproduction it actually ran.
"""

SUGGESTION_QUESTION = """\
- suggestion_safe: the finding offers a verbatim replacement for the quoted lines. Is it
  a drop-in replacement for exactly those lines that fixes the problem without breaking
  surrounding code or introducing a new defect? A suggestion that does not apply to the
  quoted lines is not safe.
"""

JUDGE_FIELDS = ("states_trigger", "states_consequence", "states_fix", "faithful")
SUGGESTION_FIELD = "suggestion_safe"


def judge_system(*, has_suggestion: bool) -> str:
    """The rubric, asking about suggestion safety only when there is a suggestion.

    A finding without a suggestion has nothing to grade there; asking anyway would
    invite a reflexive ``true`` that inflates the score.
    """
    fields = (*JUDGE_FIELDS, SUGGESTION_FIELD) if has_suggestion else JUDGE_FIELDS
    reply = "\n".join(f"{field}: <true|false>" for field in fields)
    extra = SUGGESTION_QUESTION if has_suggestion else ""
    return (
        f"{JUDGE_SYSTEM}{extra}\nRespond with YAML only, no prose and no code fences:\n\n{reply}\n"
    )


def build_judge_prompt(
    *,
    diff: str,
    rendered_finding: str,
    evidence_unverified: bool,
    replaced_lines: str | None = None,
) -> str:
    """The user message pairing the reviewed diff with the finding under grading.

    ``replaced_lines`` is the new-file text the finding's suggestion would replace.
    The rendered finding already carries the suggestion; without the lines it lands
    on, the judge could not tell a patch that applies from one that does not.
    """
    label = (
        "The finding is labelled UNVERIFIED (reasoning only)."
        if evidence_unverified
        else "The finding claims verified evidence."
    )
    prompt = f"{label}\n\n# Diff\n\n{diff}\n\n# Finding\n\n{rendered_finding}\n"
    if replaced_lines is not None:
        prompt += f"\n# Lines the suggestion replaces\n\n{replaced_lines}\n"
    return prompt


def parse_judge_reply(text: str, *, has_suggestion: bool = False) -> dict[str, bool] | None:
    """Coerce a judge reply into booleans, or ``None`` when it cannot be trusted.

    A missing or non-boolean field fails the whole verdict: a judge that answered
    only half the rubric has not graded the finding, and guessing the rest would be
    the recorded-a-pass-we-did-not-make mistake this module exists to avoid.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None
    fields = (*JUDGE_FIELDS, SUGGESTION_FIELD) if has_suggestion else JUDGE_FIELDS
    verdict: dict[str, bool] = {}
    for field in fields:
        value = loaded.get(field)
        if not isinstance(value, bool):
            return None
        verdict[field] = value
    return verdict


def judge_finding(
    llm: LLMClient,
    *,
    diff: str,
    rendered_finding: str,
    evidence_unverified: bool,
    replaced_lines: str | None = None,
) -> dict[str, bool] | None:
    """Grade one rendered finding, or ``None`` if the judge could not answer.

    Passing ``replaced_lines`` means the finding carries a suggestion, so the verdict
    must include ``suggestion_safe``.
    """
    has_suggestion = replaced_lines is not None
    user = build_judge_prompt(
        diff=diff,
        rendered_finding=rendered_finding,
        evidence_unverified=evidence_unverified,
        replaced_lines=replaced_lines,
    )
    try:
        response = llm.complete(judge_system(has_suggestion=has_suggestion), user)
    except LLMError:
        return None
    return parse_judge_reply(response.text, has_suggestion=has_suggestion)
