import type { FlagGroup } from "../components/cmd";

const TARGETING: FlagGroup = {
  title: "What to review",
  flags: [
    { name: "--dir, -C", arg: "path", help: "Repository to review, or any directory: one without git is reviewed file by file. Defaults to the current directory." },
    { name: "--mr", arg: "str", help: "GitLab merge request: an iid or a full URL." },
    { name: "--pr", arg: "str", help: "GitHub pull request: a number or a full URL." },
    { name: "--issue", arg: "str", help: "Issue this change should solve: a number or a URL." },
    { name: "--base, -b", arg: "str", help: "Base ref to compare against, e.g. main." },
    { name: "--committed", help: "Review only committed changes." },
    { name: "--uncommitted", help: "Review only staged and unstaged edits." },
    { name: "--include-untracked", help: "Also review untracked files." },
    { name: "--no-discussions", help: "Do not use existing MR/PR comments as context." },
  ],
};

const PUBLISHING: FlagGroup = {
  title: "Publishing",
  flags: [
    { name: "--post", help: "Publish the review back to the merge/pull request." },
    { name: "--no-summary", help: "With --post, skip the overview comment." },
    { name: "--repost", help: "Post findings even if a previous run already did." },
    { name: "--no-post", help: "Never offer to publish at the end of a review." },
  ],
};

const PIPELINE: FlagGroup = {
  title: "Which stages run",
  flags: [
    { name: "--no-walkthrough", help: "Skip the overview; one model call instead of two." },
    { name: "--no-llm", help: "Static analysis only; makes no model calls." },
    { name: "--no-static", help: "Skip static analysis; model only." },
    {
      name: "--no-verify",
      help: "Skip the configured test-verification commands.",
    },
    {
      name: "--no-impact",
      help: "Skip blast-radius analysis of changed symbols.",
    },
    {
      name: "--no-supply-chain",
      help: "Skip the dependency, CI, container and infrastructure analysis.",
    },
    {
      name: "--no-investigate",
      help: "Skip the bounded repository reads that confirm or drop candidate findings.",
    },
    {
      name: "--trust-static",
      help: "Allow repository-provided static tools to execute directly in CI.",
    },
    {
      name: "--trust-verify",
      help: "Allow repository-provided test commands to execute directly in CI.",
    },
    { name: "--model, -m", arg: "str", help: "Override the configured model." },
    { name: "--config", arg: "path", help: "Path to a config file." },
  ],
};

const FILTERING: FlagGroup = {
  title: "Filtering",
  flags: [
    {
      name: "--severity, -s",
      arg: "critical|major|minor|info",
      help: "Lowest severity to report.",
    },
    { name: "--max-findings", arg: "int", help: "Cap the number of findings." },
    { name: "--full-file", help: "Allow findings on lines the change did not touch." },
    {
      name: "--fail-on",
      arg: "critical|major|minor|info",
      help: "Exit non-zero when a finding reaches this severity.",
    },
  ],
};

const OUTPUT: FlagGroup = {
  title: "Output",
  flags: [
    { name: "--panels", help: "Show rich panels with code context instead of the report." },
    { name: "--full", help: "Add the agent prompts and review info the terminal hides." },
    { name: "--json", help: "Print the full result as JSON." },
    { name: "--agent", help: "Print JSON shaped for another agent to act on." },
    { name: "--prompt-only", help: "Print findings as instructions for a coding agent." },
    { name: "--markdown", arg: "path", help: "Also write a markdown report to this path." },
  ],
};

export const REVIEW_GROUPS = [TARGETING, PUBLISHING, PIPELINE, FILTERING, OUTPUT];

export const DESCRIBE_GROUPS: FlagGroup[] = [
  {
    title: "Options",
    flags: [
      { name: "--dir, -C", arg: "path", help: "Repository to describe." },
      { name: "--mr", arg: "str", help: "GitLab merge request." },
      { name: "--pr", arg: "str", help: "GitHub pull request." },
      { name: "--issue", arg: "str", help: "Issue this change should solve." },
      { name: "--base, -b", arg: "str", help: "Base ref." },
      { name: "--no-discussions", help: "Do not use existing MR/PR comments as context." },
      { name: "--model, -m", arg: "str", help: "Override the configured model." },
      { name: "--config", arg: "path", help: "Path to a config file." },
      { name: "--json", help: "Print JSON." },
      { name: "--markdown", arg: "path", help: "Write the walkthrough to this path." },
    ],
  },
];

export const IMPROVE_GROUPS: FlagGroup[] = [
  {
    title: "Options",
    flags: [
      { name: "--dir, -C", arg: "path", help: "Repository." },
      { name: "--mr", arg: "str", help: "GitLab merge request." },
      { name: "--pr", arg: "str", help: "GitHub pull request." },
      { name: "--issue", arg: "str", help: "Issue this change should solve." },
      { name: "--base, -b", arg: "str", help: "Base ref." },
      { name: "--uncommitted", help: "Only staged and unstaged edits." },
      { name: "--no-discussions", help: "Do not use existing MR/PR comments as context." },
      { name: "--model, -m", arg: "str", help: "Override the configured model." },
      { name: "--max-findings", arg: "int", help: "Cap the number of suggestions." },
      { name: "--config", arg: "path", help: "Path to a config file." },
      { name: "--json", help: "Print the full result as JSON." },
      { name: "--agent", help: "JSON for another agent." },
      { name: "--prompt-only", help: "Instructions for a coding agent." },
      { name: "--panels", help: "Show rich panels with code context instead of the report." },
      {
        name: "--fail-on",
        arg: "critical|major|minor|info",
        help: "Exit non-zero when a suggestion reaches this severity.",
      },
    ],
  },
];

export const ASK_GROUPS: FlagGroup[] = [
  {
    title: "Options",
    flags: [
      { name: "--dir, -C", arg: "path", help: "Repository." },
      { name: "--mr", arg: "str", help: "GitLab merge request." },
      { name: "--pr", arg: "str", help: "GitHub pull request." },
      { name: "--issue", arg: "str", help: "Issue this change should solve." },
      { name: "--base, -b", arg: "str", help: "Base ref." },
      { name: "--uncommitted", help: "Only staged and unstaged edits." },
      { name: "--no-discussions", help: "Do not use existing MR/PR comments as context." },
      { name: "--model, -m", arg: "str", help: "Override the configured model." },
      { name: "--config", arg: "path", help: "Path to a config file." },
      { name: "--plain", help: "Print the raw answer, unformatted." },
    ],
  },
];

export const GLOBAL_FLAGS: FlagGroup[] = [
  {
    title: "Global options",
    flags: [
      { name: "--verbose, -v", help: "Show debug logging." },
      { name: "--quiet, -q", help: "Errors only." },
      { name: "--version, -V", help: "Show the version and exit." },
    ],
  },
];
