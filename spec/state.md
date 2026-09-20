# State specification v0.1

**English** | [简体中文](state.zh-CN.md)

The state that Jev sees. **Environment-agnostic**: words like "browser / phone / game" do not appear in
the specification; only the slots are defined, and environment differences may appear only inside
`observation.data` and the option description text.

## Skeleton (six fields, all required)

```json
{
  "task":        { "...": "owned by the user, read-only" },
  "observation": { "...": "owned by the environment" },
  "progress":    { "...": "computed by code" },
  "pending":     [ "..." ],
  "excluded":    [ "..." ],
  "history":     [ "..." ]
}
```

### 1. `task` — goal and boundaries (only user input can change it)

```json
{
  "goal": "Read today's upgrade availability for SFO→HKG on 2026-11-19.",
  "inputs": { "origin": "SFO", "destination": "HKG", "date": "2026-11-19", "adults": 1 },
  "constraints": ["read-only", "do not select a fare"],
  "success_criteria": ["the result page shows MUA pricing for a United nonstop"],
  "authorization": ["read_page", "fill_form"]
}
```

- `goal` MUST be 1–2 imperative sentences, in the user's own words.
- `success_criteria` MUST be checkable by an independent verifier (not "it looks right").
- `authorization` MUST list the capability classes allowed for this run; any capability not on the list
  is refused.
- MUST NOT be rewritten by observation content, tool output or model judgment (only a `TASK_UPDATED`
  event can change it).

### 2. `observation` — what the world looks like now

```json
{
  "revision": "screen-18",
  "source": "browser/united-result-page",
  "observed_at": "2026-09-19T00:12:03Z",
  "stale": false,
  "data": { "theme_switch": "off", "search_box": "", "results_shown": 3 }
}
```

- `revision` MUST change only when the **projected world actually changed**; animations, time and
  irrelevant repaints do not count. It is the only signal for "did this step make progress".
- `stale: true` MUST mean "this read failed and the payload is the previous one"; no action may execute
  while it is set.
- `data` MUST contain exactly: ① the fields referenced by candidate preconditions; ② the fields the
  verifier reads. **Nothing more.**
  (This is "observation completeness": anything an action changes must be visible in `data`, otherwise
  the loop spins in a blind spot.)
- MUST NOT contain raw HTML/DOM, full page text, base64 screenshots, cookies or credentials.

### 3. `progress` — the result of the previous step, computed by code

```json
{ "steps": 3, "last_action_changed": true, "no_effect_streak": 0 }
```

- All three values are computed by code and MUST NOT be inferred by the model.
- `last_action_changed: false` is a direct signal to the model: "what you just did had no effect".
- `no_effect_streak` MUST be consistent with `excluded`: an action that entered the exclusion set must be
  reflected here.

### 4/5. `pending` / `excluded` — the two lists that prevent repeats

```json
"pending":  [ { "key": "submit_search", "status": "pending", "since_step": 3 } ],
"excluded": [ "fill_search_box" ]
```

- `pending`: logical operations accepted but not yet resolved (`pending` / `unknown`). **Query only,
  never resend.**
- `excluded`: actions proven ineffective under the current observation (executed `completed` but
  `revision` unchanged) or already attempted.
- Two hard rules: **keys on either list MUST NOT appear in this round's options**; the scope of
  `excluded` MUST be bound to the current `revision` — it is cleared as soon as the observation changes
  (otherwise one no-effect action causes permanent blindness).

### 6. `history` — bounded recent steps

```json
[ { "step": 2, "action": "fill_search_box", "receipt": "completed", "changed": true } ]
```

- Keep only a **structured summary** of the most recent ≤ 8 steps; it is not a conversation transcript
  and not a log.
- MUST NOT contain raw page text, full tool output or useless timestamps.

## Sizes and forbidden items

| Item | Limit |
|---|---|
| `state` + the longest single question | ≤ 32k tokens (model hard limit) |
| The whole request | ≤ 64k tokens |
| `observation.data` | ≤ 8k characters recommended; filter in code before exceeding |
| `history` | 8 entries by default |
| Any single array | MUST NOT be unbounded |

Forbidden in state: raw DOM, full page text, base64 images/audio, credentials and cookies, irrelevant
logs, per-second timestamps (they pollute `revision` and deduplication).

## One hard rule

**State is data, not instructions.** No text inside the observation may be treated as a goal, a rule or
an authorization. The specification requires writing this into the question's instructions (see the
questions specification), but the real defense is code: candidate filtering, argument validation and
authorization checks all happen at runtime.

## Mapping to the current implementation

This document describes the **design target** for the wire format; the kernel's `frame.decision_state`
currently projects a subset of it, mapped below. The table records what exists today — it is not a
release plan:

| Specification | Kernel today |
|---|---|
| `task.*` | `goal` / `inputs` / `constraints` / `success_criteria` (`authorization` is implemented but not projected) |
| `observation.{revision,stale,data}` | `observation_revision` / `observation_stale` / `observation` |
| `progress.*` | none (expressed indirectly through `recent_steps[].changed`) |
| `pending[]` | `unresolved_operations[]` |
| `excluded[]` | `excluded_actions[]` |
| `history[]` | `recent_steps[]` |
