# GitHub Copilot Instructions

This workspace has Superpowers installed for GitHub Copilot.

Before responding to any task, check the workspace skills in `.github/skills/` and invoke `using-superpowers` when starting a conversation or task. If another Superpowers skill applies, invoke it before taking action and follow its instructions.

The `.github/skills/*/SKILL.md` files are Copilot entrypoints. The canonical Superpowers source lives in `superpowers-main/skills/<skill-name>/`. When a wrapper skill is invoked, read and follow the referenced canonical `SKILL.md`; load any referenced files relative to that canonical skill directory.