---
description: Create an implementation plan file from a short task description
argument-hint: "[Short description of what to implement or change]"
allowed-tools: Read, Write, Glob, Grep, Bash(git status:*)
---

You are helping to create a detailed implementation plan for this codebase. Always adhere to any rules or requirements set out in any CLAUDE.md files when responding.

User input: $ARGUMENTS

## High level behavior

Your job is to turn the user input into a thorough, file-ready implementation plan saved to `_plans/`. The plan is a blueprint — it should be specific enough that you (or another agent) could execute it without ambiguity, but it must NOT contain code.

## Step 1. Check the working directory

Run `git status` and abort if there are any uncommitted, unstaged, or untracked changes. Tell the user to commit or stash first. DO NOT PROCEED.

## Step 2. Parse the arguments

From `$ARGUMENTS`, derive:

1. `plan_title` — Short, human-readable title in Title Case. E.g. "Session Slot Leak Fix".
2. `plan_slug` — Kebab-case file name slug:
   - Lowercase, only `a-z`, `0-9`, `-`
   - Replace spaces/punctuation with `-`
   - Collapse multiple `-` into one; trim from start and end
   - Max 40 characters
   - E.g. `session-slot-leak-fix`

If you cannot infer a sensible title and slug, ask the user to clarify.

## Step 3. Explore the codebase

Read the relevant source files, tests, and config to understand the current implementation. Use Glob and Grep to find all affected modules. You need enough context to write a precise, non-speculative plan.

Focus on:
- Which files will change and why
- What the current behavior is vs. the desired behavior
- Any edge cases or constraints from existing tests or architecture

## Step 4. Write the plan

Save the plan to `_plans/<plan_slug>.md` using this exact structure:

```
# <plan_title>

## Context

<2–4 sentences: what this plan implements, why it's needed, and any important constraints.>

---

## Key Design Decisions

- <Bullet per significant decision: approach chosen and why>
- ...

---

## Files to Change

| File | Change |
|------|--------|
| `src/...` | <what changes and why> |
| `tests/...` | <what changes and why> |

---

## Implementation Steps

### 1. <Step name>

- <Concrete action>
- <Concrete action>
- ...

### 2. <Step name>

- ...

(Continue until all changes are fully specified.)

---

## Verification

1. <How to confirm step N worked>
2. <Run tests: exact command>
3. <Any manual checks>
```

Rules:
- No code snippets — describe behavior and structure in plain English
- Every step must be specific enough to execute without re-reading the issue
- Reference exact file paths and function/class names where known

## Step 5. Report back

After saving the file, respond with this short summary:

```
Plan file: _plans/<plan_slug>.md
Title: <plan_title>

<3–5 sentence summary of the approach>
```

Do not print the full plan in chat unless the user asks.
