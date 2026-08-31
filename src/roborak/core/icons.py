"""How the vocabulary is shown: one glyph set, owned in one place.

``severity`` says what a finding *is*; this says what it looks like. They are
separate because the second is a design decision with a constraint the first does
not have: both forges strip CSS and ``style`` attributes from comment markdown, so
a published report cannot color anything. A glyph is the only mark that survives
the sanitizer, which makes the choice of glyphs load-bearing rather than
decorative -- it is the whole of the visual vocabulary a reader gets.

Two rules hold the set together:

* **Severity is one shape in four colours.** ``🔴 🟠 🟡 🔵`` are the same circle at
  the same size, so the badge line keeps an even rhythm down the page and nothing
  about a finding's shape is doing work its label does not. What separates them is
  hue and the word beside them, which is never dropped.
* **A glyph means one thing.** Anything used for a section is not reused for a
  status, so ``🧭`` is always the blast radius and never a review pass. The one
  deliberate exception is ``❌``, which marks failure wherever failure appears.

The roborak logo (``render.markdown.LOGO_URL``) is not part of this: it is a
hosted image that names the author once per report, where these are inline text
repeated on every finding.
"""

from __future__ import annotations

from roborak.core.severity import Category, Effort, Severity

SEVERITY_ICON: dict[Severity, str] = {
    Severity.CRITICAL: "🔴",
    Severity.MAJOR: "🟠",
    Severity.MINOR: "🟡",
    Severity.INFO: "🔵",
}
"""One circle, four colours, all the same size."""

SEVERITY_GLYPH = "●"
"""The same circle for the terminal, which has the colour an emoji is carrying
elsewhere: ``rich`` styles it from ``SEVERITY_STYLE``, and a panel border wants
something narrower than an emoji."""

SEVERITY_WORD: dict[Severity, str] = {
    Severity.CRITICAL: "Critical",
    Severity.MAJOR: "Major",
    Severity.MINOR: "Minor",
    Severity.INFO: "Trivial",
}
"""The word on its own, for places that already carry the circle themselves."""

SEVERITY_LABEL: dict[Severity, str] = {
    severity: f"{SEVERITY_ICON[severity]} {word}" for severity, word in SEVERITY_WORD.items()
}

CATEGORY_LABEL: dict[Category, str] = {
    Category.SECURITY: "🔒 Security",
    Category.BUG: "🎯 Functional Correctness",
    Category.PERFORMANCE: "⏱️ Performance",
    Category.LOGIC: "🧠 Logic",
    Category.RELIABILITY: "🚦 Reliability & Operations",
    Category.MAINTAINABILITY: "📐 Maintainability & Code Quality",
    Category.TESTING: "🧪 Testing",
    Category.STYLE: "🎨 Style",
    Category.DOCS: "📝 Documentation",
}

EFFORT_LABEL: dict[Effort, str] = {
    Effort.QUICK_WIN: "⚡ Quick win",
    Effort.MODERATE: "🔨 Moderate",
    Effort.HEAVY_LIFT: "🏗️ Heavy lift",
}

# Buckets. Plain constants rather than a ``Bucket``-keyed map: ``core.buckets``
# builds the titles, and importing its enum here would close a cycle.
ACTIONABLE = "🛠️"
OUTSIDE_DIFF = "⚠️"
NITPICK = "🧹"
REQUIREMENT_GAP = "📋"

# Sections of the report, in the order they appear.
WALKTHROUGH = "🗂️"
FLOW = "🗺️"
IMPACT = "🧭"
VERIFICATION = "🔬"
SUPPLY = "📦"
EVIDENCE = "🔎"
AGENT = "🤖"
INFO = "ℹ️"
RUN_CONFIG = "⚙️"
COMMITS = "📥"
FILES = "📒"
REVIEW_PLAN = "🧩"
COVERAGE = "🚧"
ERROR = "❌"

# Statuses. ``ERROR`` doubles as the failed status on purpose -- one mark for
# failure wherever it is reported.
PASSED = "✅"
FAILED = ERROR
TIMED_OUT = "⌛"
WARNED = "⚠️"
NEUTRAL = "⚪"
UNKNOWN = "❔"
BLOCKED = "⛔"
LINKED = "🔗"

# The same three statuses for the panel view, which has colour of its own and
# wants a mark narrow enough to sit inside a panel border.
PASSED_GLYPH = "✓"
WARNED_GLYPH = "⚠"

# What a dependency did. One family: movement, then trust.
SOURCE_CHANGED = "🔀"
INTEGRITY_LOST = "🔓"
INTEGRITY_CHANGED = "🔁"
ADDED = "🆕"
REMOVED = "🗑"
UPGRADED = "⬆"
DOWNGRADED = "⬇"
