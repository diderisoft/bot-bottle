---
name: init-entry
description: Add a new timestamped entry to the project journal at ./docs/JOURNAL.md in the current working directory, creating the file (and `docs/`) if missing. Use when the user invokes /init-entry or asks to "log this in the journal", "add a journal entry", "record this", "write this down", or similar. Entries are stream-of-thought prose, not templates — newest on top, append-only.
---

# Add a journal entry

Adds a new entry to `./docs/JOURNAL.md` in the current working directory. The journal is an append-only log of unstructured stream-of-thought entries, newest on top. Each entry is a timestamp heading followed by freeform prose — whatever was on the mind worth capturing. No title, no template, no required sections. Do NOT under ANY CIRCUMSTANCES delete old entries when appending to the top.

## Format

Each entry looks like:

```
## YYYY-MM-DD HH:MM

[github](tag://github) [postgres](tag://postgres)

<freeform prose — stream of thought, not a template. Write what was just decided
or what's confusing or what's being tried. Multiple paragraphs are fine. No
section headings, no fill-in-the-blank prompts.>
```

**Tag line rules:**

- Tags appear on the line immediately under the timestamp heading (no blank line between header and tags), then a blank line before the body.
- Format is markdown links with the custom `tag://` URI scheme: `[name](tag://name)`. Multiple tags are space-separated on one line.
- The label and the URI value are usually the same word; differ only when there's a real reason (e.g. `[GitHub Actions](tag://github)`).
- **Tags are conditional.** Include them only when the entry has a coherent theme worth grouping across future entries. If nothing themes-up cleanly, omit the line entirely. Don't tag everything.

## Step 1 — Get the entry text from the invoker

The body of the entry must come from the invoker, verbatim. Your role is mechanical — timestamp, format, insert — not authorial. Do not draft, paraphrase, expand, summarize, or "improve" the prose. Do not draft from conversation context. Do not offer a draft for the invoker to react to. Do not propose what the entry "could be."

Get a timestamp by shelling out: `date '+%Y-%m-%d %H:%M'`. Use that exact value as the entry heading.

If the invoker passed content alongside the command (e.g. `/init-entry switched to postgres because sqlite locking was killing the worker pool`), use it as the body verbatim. If they passed nothing, ask what they want to capture and wait for their words.

Show the entry as it will be written (their timestamp + their prose) before writing to disk, so they can confirm formatting and insertion. Don't edit the content unprompted.

## Step 2 — Locate or create the journal

From the current working directory:

1. If `./docs/JOURNAL.md` exists → Step 3 (insert).
2. If `./docs/` exists but no `JOURNAL.md` → Step 4 (create).
3. If `./docs/` doesn't exist → create the directory, then Step 4.

## Step 3 — Insert into existing journal

Read `./docs/JOURNAL.md`. Find the insertion point:

- Scan for the first line matching `^## ` (an existing entry heading).
- Insert the new entry **before** that line, separated by one blank line above and below.
- If no `^## ` line exists, append the entry to the end of the file with one blank line separating it from prior content.

Never edit, reorder, or delete existing entries. The journal is append-only.

## Step 4 — Create a new journal file

Write `./docs/JOURNAL.md` with this skeleton followed by the new entry, so the format stays consistent regardless of which skill seeded the file:

```
# Journal

Append-only stream of thought. Newest entries on top. Each entry is a timestamp
followed by freeform prose. Tag entries with `[name](tag://name)` links under
the header — only when a coherent theme emerges. Otherwise just write.

## YYYY-MM-DD HH:MM

<entry body>
```

Unix line endings, no trailing whitespace, no emojis.

## Step 5 — Commit and push

After the entry is written, commit and push it so the journal stays in sync with the remote. This step is best-effort: report failures inline, never retry, and never unwind the on-disk write.

1. If `git rev-parse --git-dir` fails (the cwd is not inside a git repo), skip this step entirely and note in the report that the entry was written but not committed.
2. Stage only the journal file:
   ```
   git add docs/JOURNAL.md
   ```
   Never use `git add -A` or `git add .` — unrelated working-tree changes are not the journal's concern and must not be swept into the commit.
3. Commit with a mechanical message derived from the timestamp (and tags, if any). Pass it via HEREDOC so quoting stays clean:
   ```
   git commit -m "$(cat <<'EOF'
   Journal: 2026-05-02 03:28
   EOF
   )"
   ```
   With tags, append them in parens after the timestamp: `Journal: 2026-05-02 03:28 (auth, postgres)`. The message must not paraphrase or summarize the entry body — the same no-synthesis rule that protects the body protects the commit log.
4. If the current branch has an upstream (`git rev-parse --abbrev-ref --symbolic-full-name @{u}` succeeds), run `git push`. If no upstream is configured or the push fails for any reason, report it and continue. Do not set an upstream, force-push, or otherwise paper over the failure.

If any of steps 2–4 fail, surface the failure in the report but do not amend, reset, or modify the on-disk state.

## Step 6 — Report

One short line: path to the file, "created" or "updated", and the timestamp. Append commit sha (or "not committed: <reason>") and push result (or "no upstream" / "push failed: <reason>"). Stop there.

## Hard rules

- **Human-authored body.** Entry prose comes from the invoker, not from you. Don't draft, paraphrase, expand, or summarize. If invoked without content, ask. The journal is a record of the human's thinking — your synthesis doesn't belong in it.
- **Append-only.** Never edit or delete existing entries. To revise a past thought, write a new entry that references the prior timestamp.
- **No title in the heading.** Just the timestamp. Resisting the urge to title each entry is the point — entries are stream of thought, not curated essays.
- **No template inside the body.** Don't write `**What changed:**`, `**Considered:**`, `**Ruled out:**` as sub-headings. Plain prose.
- **Tags are conditional and live directly under the header.** Use `[name](tag://name)` markdown link format. Omit the tag line when no coherent theme exists. Don't invent themes.
- **Today's timestamp only.** Don't backdate.
- **No emojis.**
- **Don't volunteer to add anything else** (indexes, table of contents, README links, summaries). The journal stays text-only.
- **Mechanical commit message.** The commit message is derived from the timestamp and tags only. Never paraphrase the entry body into the subject line.
- **Scoped commit.** Only `docs/JOURNAL.md` is staged. Never sweep in unrelated working-tree changes, never amend a previous commit, never bypass hooks.
