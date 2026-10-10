---
description: تحديث ASSET_MANAGEMENT_CHATGPT_HANDOFF.md ليطابق آخر حالة للكود ثم رفعه
argument-hint: "[full]"
---

Bring `ASSET_MANAGEMENT_CHATGPT_HANDOFF.md` up to date with the repository. It is a self-contained briefing for an AI assistant (ChatGPT) that has never seen this codebase, so every statement must match the current code.

Work incrementally by default. If the argument is `full`, or the snapshot commit cannot be found in git history, re-inspect the whole repository instead: $ARGUMENTS

1. Find what changed
   - Read the "Repository snapshot" table at the top of the handoff and take the commit in "Branch / HEAD".
   - Run `git log --oneline <that commit>..HEAD` and `git diff --stat <that commit>..HEAD`, ignoring commits that only touch the handoff itself.
   - If nothing else changed, say the handoff is current and stop without editing.
   - Check what is really published with `gh release list -R muisawe/Majd3D --limit 5`. Never call a version published unless it is listed there.
2. Verify before writing
   - Read every changed source, test, script, workflow and documentation file in full. Write only what the code or git history proves.
   - Run `python3 -W ignore -m unittest discover -s tests -t .` and record the counts and the interpreter used.
3. Edit only what the changes affect
   - Keep the structure, section numbers, the FACT / INFERENCE / UNKNOWN labels and the `path` plus function citations.
   - Update the snapshot table (HEAD commit and subject, version from `version.json`, `SCHEMA_VERSION` from `majd_studio_3d/store.py`, analysis date), the "Scope inspected" counts, each module section the diff touches, §5 when the schema changes, §6 status, §7 risks (mark fixed risks with the fixing commit, add new ones), §8.3 history, §9 next steps, the §10 summary for ChatGPT, and Appendices A and B.
   - Correct or remove statements the changes made false, and leave no contradictions between sections.
   - Keep the document's existing English style.
4. Finish
   - Commit only `ASSET_MANAGEMENT_CHATGPT_HANDOFF.md` with the message `Update ChatGPT handoff to <short HEAD sha>`, then `git push`.
   - Reply in Arabic in a few lines: which commits were covered, which sections changed, and anything that could not be verified.
