# Attribution

This skill is vendored, unmodified apart from script paths, from
**UI UX Pro Max** by nextlevelbuilder: https://github.com/nextlevelbuilder/ui-ux-pro-max-skill
(MIT License, see `LICENSE`), commit `56ea9cf` (2026-10-14).

Changes for this project:
- `${CLAUDE_PLUGIN_ROOT}/.claude/skills/ui-ux-pro-max/...` paths in `SKILL.md` were made
  project-relative (`.claude/skills/ui-ux-pro-max/...`), since it is installed as a project
  skill rather than a plugin.
- Upstream tests and the maintainer's `validate_data.py` were not copied.

Reviewed before installing (2026-10-21): the scripts use only the Python standard library
(no network, no subprocess). The only file writes are the optional `--persist` output
(`design-system/<project>/`) and its own temporary file.
