# Contributing

**English** | [简体中文](CONTRIBUTING.zh-CN.md)

Contributions are welcome. Issues and pull requests are handled on GitHub; this file is the short
version of what a change has to satisfy.

## Development environment

```sh
git clone https://github.com/kevin-zhan/jev-loop.git
cd jev-loop
uv sync
```

Python 3.12+ is required and the runtime has zero third-party dependencies. `npm test` (which loads the
pi package over RPC) additionally needs Node and the pi CLI.

## Checks that must pass before a commit

```sh
uv run pytest          # everything is offline; no paid APIs are called
uv run ruff check .
uv run jev-loop-doctor --offline   # local configuration preflight; no network request
npm test               # only needed when extensions/ or package.json changed
```

- Tests must not call paid models. When a real wire shape needs checking, write an explicit script and
  state its cost in the pull request. The offline suite covers the transport with a loopback HTTP
  fixture, including auth failures, timeouts, redirects and error-body redaction.
- Live Jev runs are never part of the test suite. A live script has to state its request budget, use
  synthetic inputs and a dedicated temporary directory, and report how many decision requests it
  actually made; failures and timeouts count against the budget and are never retried automatically.
- Credentials come from the process environment (`TYPESAFE_API_KEY` by default) or an explicit
  `request_fn` injection — never from a command-line flag, a task/spec, a bundle config, an event or
  a cognition message, and never by scanning a `.env` file, keychain or another project. The doctor
  is the offline preflight and must not print a credential value, length or hash.
- New behavior needs matching tests. The expected coverage includes: a rejected frame binding, the four
  receipt states, unresolved operations never being resent, an action with no effect still allowing
  progress after the candidate set narrows, a reversible cycle suspending by default
  (`awaiting_evidence`) and failing with `cycle_detected` only after escalations hit the limit,
  verification `unknown` not counting as success, and reducer purity.

## Design constraints (read `AGENTS.md` and `docs/design.md` first)

- The kernel (`src/jev_loop/core/`) must not depend on Jev, HTTP, a browser or any external state
  beyond the clock. Model-facing code lives in `src/jev_loop/policies/`; environment behavior belongs
  in adapters.
- Events are the single source of truth: every state change goes through `core/events.py` and the pure
  reducer in `core/reduce.py`. Do not mutate `RuntimeState` directly, and never do I/O or call a model
  inside the reducer.
- Do not remove the guards (`NO_PROGRESS` / `REPEAT` / `CYCLE`) to "make the loop smoother". Changing
  guard semantics requires updating the tests and the guard table in `docs/design.md`.
- Operations in `pending` / `unknown` may only be queried, never resent; `idempotency=NONE` is never
  retried blindly.
- Never put real API keys, cookies or login sessions into code, tests, events or snapshots.
- When changing the pi package or the bundle contract, update `docs/pi-integration.md` as well.
- Documentation is English-canonical: edit the English file, then keep its `*.zh-CN.*` counterpart
  information-equal (and vice versa for wording fixes that change meaning).

## Commit messages

`type(scope): summary`, for example `fix(host): preserve unconfirmed resource claims`. One commit does
one thing; do not mix unrelated formatting into a functional change.

## License

By contributing, you agree that your contribution is licensed under this repository's
[MIT License](LICENSE).
