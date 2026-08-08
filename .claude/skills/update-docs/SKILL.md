---
name: update-docs
description: This skill should be used when the user asks to "update docs", "sync documentation", "check if docs need updating", or mentions documentation consistency after code changes. Identifies and updates affected documentation.
---

# Update Documentation

Identify documentation affected by code changes and update accordingly.

## Workflow

1. **Detect change scope:** `git diff origin/main...HEAD --stat`

2. **Impact analysis** — check which documents from [references/doc-targets.md](references/doc-targets.md) are affected by the changes:
   - New CfnOutputs, parameters, commands, APIs, or config items added?
   - Existing procedures or descriptions invalidated?

3. **Update affected documents only:**
   - Maintain consistency in command examples, output samples, parameter tables, and step numbering
   - Do not create unnecessary documentation files

4. **Report:** if no documentation update is needed, state so explicitly.

## Additional Resources

### Reference Files

- **[`references/doc-targets.md`](references/doc-targets.md)** — List of managed documentation files and their scope
