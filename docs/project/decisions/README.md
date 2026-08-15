# Architectural decision records

Each non-trivial durable decision is one collision-resistant Markdown file:

```text
YYYYMMDD-HHMM-short-slug.md
```

Required frontmatter:

```yaml
---
status: proposed | accepted | rejected | superseded
date: YYYY-MM-DD
decision: one-line decision
---
```

The body records context/problem, considered alternatives, the decision,
rationale, consequences, and conditions for reversal. A superseded ADR keeps
its original text, changes status to `superseded`, adds `superseded_by`, and
links to the replacement. The replacement may add `supersedes`.

There is no manually maintained ADR index. `scripts/project-preflight`
discovers and validates every ADR file, so the directory itself is the list.
Use Git history for chronology; do not append a decision log here.
