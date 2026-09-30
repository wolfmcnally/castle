# Contributing

Castle uses Python 3.12+, uv and a locked development environment. Run `./bin/setup` and `./bin/check` from the checkout root. The full gate covers lint, formatting, all retained product and methodology tests and policy checks. The product lives under `project/`; canonical agent skills, including teach/learn, live under `.claude/`. Tests use disposable fixtures and refuse external network connections. Run the gate on both macOS and Linux; Darwin-only native clone and merge tests may skip on Linux, while portable refusal behavior remains tested.

Keep changes focused and include tests that distinguish the intended behavior from a plausible wrong implementation. Preserve journal authority, immutable object bytes, lifecycle refusals and platform limitations. A persisted schema change requires a migration design; do not silently accept a second format or operate on live stores. Describe behavior changes and validation in your pull request.

Contributions are provided under the repository’s MIT license. Report security issues privately as described in SECURITY.md. Maintainers approve releases; ordinary pull requests do not publish packages or migrate data.
