# Contributing

Licensed [MIT](LICENSE); contributions require the one-time [CLA](CLA.md) signature (the
bot prompts on your first PR). Note: this service's optional `alfred` extra depends on
`alfred-sdk`, which is licensed AGPL-3.0-or-later as part of the `alfred` monorepo — see
that dependency's terms if you touch `alfred_ext/`.

Development: `uv sync --all-extras`, then `uv run ruff check . && uv run ruff format
--check . && uv run mypy app/ && uv run pytest`.

PRs: branch `<type>/<slug>`, conventional-commit PR title, squash-only, `ci-ok` must be
green. Same conventions as [alfred](https://github.com/anirudhlath/alfred/blob/master/CONTRIBUTING.md).
