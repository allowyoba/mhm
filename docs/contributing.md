# Contributing

Use branches such as `feat/<name>`, `fix/<name>` and `ci/<name>`. Keep names lowercase and describe the change in plain English.

Commits contain a single lowercase Conventional Commit subject, with no body or trailers. For example: `fix: keep proxy blocked after a traffic limit`. Use the same format for pull request titles and leave PR descriptions empty. Aim for at most 72 characters. Do not add tool attribution or generation metadata.

Comments explain non-obvious constraints. Keep them short, lowercase and in simple English, with no trailing period. Preserve the exact case of identifiers and commands.

Primary documentation is in English. Keep supplementary Russian instructions under `docs/`. Document purpose, commands, parameters and actual limitations. Verify examples against the implementation.

Run `make lint test` before pushing. Packaging changes require build and install checks on the relevant platform. Local checks do not replace CI coverage for other operating systems.

[Русская версия](ru/contributing.md)
