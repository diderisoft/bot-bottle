# About these docs

| Kind | Says | Kept |
|---|---|---|
| Reference pages | what is true now, linking the decision record that says why | Edited in place. A change that alters what a page describes updates it in the same pull request. |
| Decision records | why: one per policy, convention, boundary or load-bearing choice a reader of this repository needs to understand | ADR-lite, numbered `NNNN-kebab.md`. |
| PRDs and research notes | feature design and research | In a private design repository. |

Docs here may mention a PRD by number, but never depend on it. When a choice
in one matters to a reader, it is written into a reference page or a decision
record. This repository makes sense with nothing else.

## Writing these docs

- One idea per sentence. No semicolons. At most one parenthetical per
  paragraph.
- Paragraphs of 1-3 sentences, under about 50 words. Anything longer becomes
  a list or a table.
- Lead each section with the answer in one sentence, then the detail. Put the
  example (command, YAML, table) before the explanation.
- Use tables for comparisons and "which X does what". Use short lists for
  steps and limits. No bullet runs past 3 lines.
- Cut meta and hedging, such as "This page describes…", "Note that…" or "in
  practice".
- Name internals only where a reader verifying a security claim needs them,
  and only once.
- Keep every fact. Detail too fine for the main flow moves to a "Details"
  subsection.
