# Geo review workbench

Status: phases 1 and 2 built (see **Phase 1 / Phase 2 as built** under Plan); phases 3 and 4 proposed. Branch: `feat/geo-review-phase2`.

## Summary

Building a geo model is not a one-pass job. The engineer looks at it, points at something ("W2's porosity in
layer 5 is too high", "TopRes misses the W3 top by 8 m"), the agent investigates and answers or proposes a change,
the engineer agrees or pushes back, and a new version is built and compared with the last one. Today each step of
that loop goes through a different channel: a static QC HTML, ResInsight, free-text `opmagent review comment`, and
investigation notebooks read after the fact. Nothing ties a comment to the place in the model it is about. Nothing
shows a reply next to it, and nothing shows what changed between two versions.

The **geo review workbench** is a local web app, `opmagent geo review <version>`, that puts the loop in one place:

```
              engineer (browser)                                  geo agent (Claude Code)
 ┌─────────────────────────────────────────┐            ┌────────────────────────────────────┐
 │ model panel: maps, sections, well tracks │            │ await_review_activity(version)     │
 │   click a well / layer / surface ─► pin  │            │ read thread + pinned view          │
 │ thread per pin: comment, reply, figures  │◄── DB ───►│ investigate (geo_qc_lib, notebook) │
 │ proposal card: spec diff ─► accept       │            │ reply / post_review_view / propose │
 │ version diff: vN vs vN+1                 │            │ accepted ─► build vN+1, answer     │
 └─────────────────────────────────────────┘            └────────────────────────────────────┘
                 approve / reject: unchanged (reviewer profile or `opmagent review`)
```

The database stays the only channel between the two. The web app is the engineer's. The agent never talks to it,
and the app never runs the agent. Ownership stays as it is: the engineer comments, accepts proposals and approves
versions. The agent investigates, answers and builds. Every model change is still an ordinary reviewed version.

## What already exists

- **Model data, read-only:** `geo_qc_lib.load()` reads a version as stored (arrays checked by sha256, tables from
  the database). Its figures are plotly: `fig_surfaces`, `fig_layer_maps`, `fig_section`, `fig_well`,
  `fig_distributions`, `fig_poroperm`, `fig_at_wells`. Its checks are `tops_misfit`, `at_wells`, `leave_one_out`,
  `property_checks`, `volumes` and the rest.
- **QC findings:** `qc_summary.json` per version (pass / warn / fail with messages).
- **Comments:** `review_feedback` (kind, version, `item` free text, comment, severity change / question / note,
  status open / addressed / wontfix, one `response`). Only the reviewer writes comments, and a comment can't be
  changed. The modeler answers each one once, with the version that addresses it.
- **Investigations:** `create_investigation_notebook` / `run_investigation_notebook`, HTML registered as evidence.
- **Static 3D view:** EGRID + INIT for ResInsight (`opmagent results open --geo <id>`).
- **Proposal pattern:** history-match proposals (`hm_proposals`): the agent proposes, the engineer accepts or
  rejects, and acceptance turns into work for the owner.

The workbench mostly arranges these. The new parts are the anchors, threads, view specs, proposals and the diff.

## Design

### 1. View specs: one vocabulary for the engineer and the agent

Every figure in the workbench is drawn from a **view spec**, a small JSON naming a `geo_qc_lib` figure and its
arguments:

```json
{"fig": "section", "version_id": 42, "kw": "PORO", "well": "W2", "along": "y"}
{"fig": "layer_map", "version_id": 42, "kw": "PERMX", "layer": 5, "log": true}
{"fig": "well", "version_id": 42, "well": "W2"}
{"fig": "surfaces", "version_id": 42, "surface": "TopRes"}
{"fig": "diff_map", "version_id": 43, "base_version_id": 42, "kw": "PORO", "layer": 5}
```

The server renders a spec with the same library the QC notebook uses, so the workbench, the notebook and the
agent all see the same picture. A comment stores the spec of the view it was made on. The agent replies with
specs too, so its "look at this" is a view the engineer can open, pan and pin again, not a PNG. Only figures in a
fixed registry can be named, and the arguments are checked. A spec is data, never code.

### 2. Anchors: what a comment is about

A click on the model panel turns into an **anchor**, a structured `item`:

| Clicked | Anchor |
| --- | --- |
| a well marker | `{"well": "W2"}` |
| a cell on a map or section | `{"cell": [i, j, k], "kw": "PORO"}` (plus `well` if it is a well cell) |
| a layer tab | `{"layer": 5}`, or `{"zone": "Lower"}` |
| a surface | `{"surface": "TopRes"}`, or `{"surface": "TopRes", "well": "W3"}` for a top misfit |
| a QC finding | `{"finding": "<check id>"}` |
| nothing (general) | `{}` |

The anchor goes into `review_feedback.item` as JSON. Older free-text items still work: the column stays text, and
a value that isn't a JSON object is read as `{"text": ...}`. With the anchor and the view spec, the agent knows
exactly what was meant, and its tools can go straight to the data (`at_wells` row, cell values, log samples over
that layer's depth range).

### 3. Threads

One comment is often not enough. "Why is this high?" gets "the Vsh cut-off keeps a shaly interval", which gets
"use 0.4 then", which gets a proposal. `review_feedback` keeps its rule that a comment never changes. A new table
holds the conversation under it:

`review_messages`: `id, feedback_id → review_feedback, author_role ('reviewer' | 'modeler'), author, body,
views_json (list of view specs), notebook_file_id (optional evidence), created_at`. Append-only (trigger, like
`review_feedback`).

- The engineer's first message is the `review_feedback` row itself. Follow-ups are `review_messages`.
- The agent's replies are `review_messages` with `author_role = 'modeler'`. The modeler profile can write only
  that role. It still can't write `review_feedback`.
- A thread is **waiting on the agent** when the engineer wrote last and it is open. It is **waiting on the
  engineer** when the agent wrote last or a proposal is open. The workbench and `study_status` show both counts.
- Closing is the same as now: `answer_review_feedback(id, ..., "addressed", addressed_in_version_id)` or
  `"wontfix"`. The engineer can reopen a thread by writing in it (status back to `open`). That is the one status
  change the reviewer makes, and it is logged as a message.

### 4. Proposals: agree on a change before it is built

The agent doesn't rebuild on every comment. When a thread leads to a change, it posts a **geo proposal**:

`geo_proposals`: `id, feedback_ids (the threads it answers), base_version_id, spec_patch_json (JSON merge patch
on the geo spec, e.g. {"petro": {"vsh_cutoff": 0.4}, "layers": [8, 4]}), curves_patch_json, rationale,
expected_json (what it should change: "W2 layer 5 PORO 0.27 → ~0.22", "Lower HCPV −6 %"), preview_json (§6),
status ('proposed' | 'accepted' | 'rejected' | 'built' | 'superseded'), decided_by, decided_at, built_version_id`.

- The workbench shows a proposal as a card: the spec diff, the rationale, the expected effect and the preview if
  there is one. Accept or reject (with a comment, which goes into the thread).
- Accept is the engineer's only, as with HM proposals. The agent sees it accepted, builds the new version from
  `base spec ⊕ patch`, and records `built_version_id`. The new version's assumptions say "geo proposal 7". Then it
  answers the threads with that version.
- One open proposal per base version. A new one supersedes the old.
- In a study, a proposal that needs other data (a curve the data manager didn't load) is a `raise_request`, as
  now, not a proposal.

### 5. Version diff

`opmagent geo review <v_new> --against <v_old>`, or the diff tab, which defaults to the version the new one was
built from:

- **Spec diff:** the JSON patch between the two specs, and the curves used.
- **Numbers:** cells, zone pore volume and HCPV, mean φ / k per zone, top misfit RMS per surface, at-well error
  RMS, and leave-one-out error, side by side with the delta.
- **QC findings:** fixed, still there, new.
- **Maps:** difference maps per keyword and layer (same grid), or, if the layering changed, per zone (zone
  averages), with the two versions' maps side by side.
- **Threads:** the comments this version addresses, each with the view spec it was made on drawn for both
  versions. This is how the engineer checks "did it fix what I pointed at".

### 6. What-if preview (phase 3)

`build_grid` writes to a folder and has no side effects on the database. A preview runs it into a scratch folder
under `<file_store>/previews/` with `base spec ⊕ patch`, computes the diff numbers and the anchored views, and
attaches them to the proposal (`preview_json`). Nothing is registered, and no version is made. Previews are
deleted after `[geo_review] preview_keep_days`.

- The agent can preview its own proposal before posting it, so it can say what the change actually does, not
  just what it expects.
- The engineer can turn the same knobs in the proposal card (cut-offs, kv/kh, poro-perm a/b, layers per zone) and
  ask for a preview. A preview from the engineer becomes a proposal with `author_role = 'reviewer'`, which the
  agent then builds like any accepted one.
- If a full build on Norne-size data is too slow for an interactive preview, the preview skips the property
  kriging outside the anchored layers. This is measured in phase 3 and decided then.

### 7. How the agent hears the engineer

Through the database, as chosen. Three ways in, from simple to live:

1. **On request:** the engineer tells the agent in Claude Code ("there are comments on the geo model").
   `get_review_feedback()` already lists open threads, and now includes the messages, anchors and views.
2. **In a study:** `study_status` reports threads waiting on the agent and accepted proposals, and the coordinator
   hands the geo task back, as it does for feedback today.
3. **Review session (live):** the engineer says "review session on v42". The agent calls
   `await_review_activity(version_id, timeout_s=600)`, a blocking tool that returns as soon as a thread is waiting
   on it or a proposal is decided (or on timeout, and the agent calls again). The agent answers, posts, and waits
   again. It stops when the engineer ends the session in the workbench (a `session_end` message) or after
   `[geo_review] session_idle_min`. Each wait is one tool call, so the session costs nothing while the engineer is
   thinking.

The workbench shows whether a review session is active ("geo agent is listening") from the agent's last
`await_review_activity` heartbeat in a small `review_sessions` table.

### 8. The web app

- `opmagent geo review <version> [--against <v>] [--port 0]` starts a local server, bound to 127.0.0.1 only, and
  opens the browser (or prints the URL for the Claude desktop browser pane). Python standard library HTTP server
  or Starlette (decided in phase 1, preferably with no new dependency). The frontend is static HTML + plotly.js
  from the package, with no build step and no CDN.
- **Layout:** the version picker, QC badges and the session indicator at the top. On the left, the model panel
  with tabs (Surfaces, Layers, Sections, Wells, Distributions, Diff). On the right, the threads list (filter:
  waiting on me / on agent / all), the open thread and proposal cards. Pins are drawn on the figures as markers.
- **Reads** use `geo_qc_lib.load` with a cache per version. **Writes** go through the reviewer functions in
  `core/review.py` and the new `core/geo_review.py`, never raw SQL from the request handler.
- It works for any geo version, including ones from a deck. A deck-based version has no logs, so the well tracks
  show the model column only.

**Who is the reviewer.** The app writes as the reviewer (`OPMAGENT_REVIEWER` or the OS user, as now). An agent on
the same machine could reach a localhost port, so the server takes a random token at start. It is printed in the
engineer's terminal and passed in the URL fragment, never written to disk or the database, and every write needs
it. The app is not on the agents' tool lists, and `opmagent setup` doesn't allow `opmagent geo review` for the
agents.

**Approve / reject** stay where they are in phase 1 and 2: the app shows the exact `opmagent review approve geo
<id>` command with the QC report it was reviewed against. Moving approval into the app behind the token is a
decision for after phase 2 (see Decisions).

### 9. Tools and commands

Data server, geo profile (new):

| Tool | Does |
| --- | --- |
| `get_review_thread(feedback_id)` | the comment, anchor, view spec and all messages |
| `reply_review(feedback_id, body, views=[], notebook_path=None)` | a modeler message (views checked against the registry) |
| `render_review_view(spec)` | the figure's data summary as text for the agent (values at the anchor, ranges), so the agent can reason about what the engineer sees without a picture |
| `propose_geo_change(base_version_id, feedback_ids, spec_patch, rationale, expected, preview=True)` | a proposal (with a preview in phase 3) |
| `get_geo_proposal(id)` / `list_geo_proposals(version_id)` | status, decision, comment |
| `mark_proposal_built(id, version_id)` | after the build |
| `await_review_activity(version_id, timeout_s)` | blocks until something is waiting on the agent |

Changed: `get_review_feedback` also returns the anchor (parsed), the view spec and the messages.
`opmagent geo build` / `study build geo` take `--from-proposal <id>`, which applies the patch and records it.

Reviewer (CLI and the app): `opmagent geo review`, `opmagent review thread <id> [--reply ...]`,
`opmagent review proposal <id> accept|reject`.

The geo-modeler skill gets a **Review session** section: how to read an anchor, answer with views not prose
alone, when to propose versus investigate, never build without an accepted proposal (or an explicit instruction in
chat), and always answer the threads with the built version.

### 10. Tables

New: `review_messages`, `geo_proposals`, `review_sessions`. Changed: none (`review_feedback.item` holds JSON from
now on, which is still text). One new trigger: `review_messages` append-only. `geo_proposals.status` moves only
along proposed → accepted | rejected | superseded, accepted → built. Only the reviewer decides (the same
FORBIDDEN check as `hm_proposals`). This is additive, so existing databases are migrated in place.

The names are kind-neutral (`review_messages`, not `geo_review_messages`) on purpose: res and well get the same
workbench later, with their own figures in the registry and their own proposal table or a shared one (see
Decisions).

## Plan

**Phase 1: read-only workbench and anchored comments.**
`opmagent geo review`, the view registry over `geo_qc_lib`, the model panel tabs, pins, and anchored comments
written to `review_feedback` with the token. `get_review_feedback` returns anchors and views.
Gate: on hello, a comment pinned on a W1 cell in layer 2 arrives at the agent with the anchor and the spec.
`render_review_view` returns that cell's PORO, the W1 upscaled value and the layer's range. Browser check with
the built-in browser.

**Phase 1 as built** (`opmagent/geo/review_views.py`, `opmagent/geo/review_app.py`, `review_static/index.html`):

- `opmagent geo review <version> [--port N] [--no-open]` serves the workbench on 127.0.0.1 and prints the link with
  the session token in the fragment. Standard library `http.server`, one request at a time, no new dependency
  (decision 2). plotly.js comes from the installed plotly package (`plotly>=5.22` for label shadows), so there's no CDN.
  A tab that opens a restarted server's link takes the new token from the fragment.
- **Item format:** `review_feedback.item` holds `{"anchor": {...}, "view": {...}}`. Cells are **1-based** `[i, j, k]`,
  as in the deck. `core/review.get` parses every item into `anchor`, `view` and a short `label` (free text reads as
  `{"text": ...}`). So `get_review_feedback`, `opmagent review feedback` and `study_status` all show the label.
- **Views:** `surfaces`, `layer_map` (any layer, not only the QC notebook's three), `section` (through a well, or
  through a cell: `fig_section(..., cell=(i, j))` is new in `geo_qc_lib`), `well`, `at_wells`, `distributions`,
  `poroperm`. Every trace carries `meta.role`, and `locate()` turns a click into an anchor on the server. Comment
  pins are drawn on layer maps and sections, and a click on a pin opens its comment.
- **Workbench:** a cell, well, top, well-track depth, at-wells point or QC finding becomes the anchor. The panel
  shows the model's values there (every keyword, zone, depths, nearest well, the well's upscaled vs model values
  per layer, top misfits) and offers the next view (section along x / y through the cell, the layer map, the well
  track). "show view" on a comment reopens the view it was made on. The approve / reject commands are shown; the
  QC report is served at `/qc`.
- **Agent tools** (geo and reviewer profiles, read-only): `review_context(feedback_id)` gives the comment, the
  values at its anchor and a summary of its view. `render_review_view(spec, anchor)` gives the same for any view the
  agent wants to look at.
- **Safety:** writes need the token (`X-Review-Token`) and a Host of 127.0.0.1 / localhost on the server's port.
  Comments go through the reviewer profile's `add_review_comment` (audited). A finding anchor must be one of the
  version's. `opmagent setup` puts `Bash(opmagent geo review:*)` under `ask` for the agents.
- **Not yet:** versions from a deck have no QC payload, so the workbench refuses them for now (phase 2: read the
  model without logs). Picking a version switches the page; there is no side-by-side diff (phase 2).
- **Gate:** `tests/test_geo_review.py` runs on the logs-based test field (4 wells, 10 x 10 x 5) rather than hello,
  whose geo model has no logs to anchor to. A click on a layer map in B's column anchors
  `{"cell": [9, 3, 2], "kw": "PORO", "well": "B"}`. The comment posted there (token required) reaches the geo
  agent with that anchor and view, and `review_context` returns the cell's PORO, B's upscaled vs model values per
  layer and the layer map's values at every well. Bad specs (unknown figure, keyword or argument, layer out of
  range) are refused. The real server answers on 127.0.0.1 and refuses a foreign Host. Browser check: every tab,
  cell / top / finding anchors, posting, pins and "show view", in the Claude desktop browser pane, light and dark.

**Phase 2: threads, proposals, version diff, review session.**
`review_messages`, `geo_proposals`, `review_sessions`, the new tools, `--from-proposal`, the diff tab, and
`await_review_activity`.
Gate: the scripted loop on SPE9-from-deck and a synthetic logs case. The engineer pins a finding, the agent
replies with a section view and proposes `vsh_cutoff` 0.5 → 0.4, the engineer accepts, the agent builds v2 and
answers the thread, and the diff shows the anchored cell's change, the HCPV delta and the QC finding fixed. The
same loop as a `testing/geo_review_loop.sh` scenario.

**Phase 2 as built** (`core/review.py` threads and sessions, `core/proposals.py`, `geo/proposal_build.py`,
`geo/review_diff.py`, the workbench page):

- **Decision 3:** a kind-neutral `model_proposals` table (`model_kind` CHECK `geo` for now), so res and well need
  no new table in phase 4. `hm_proposals` stays as it is.
- **Threads carry everything.** `review_messages` (append-only: triggers refuse update and delete) has a `kind`:
  `message`, `answer` (the modeler's answer is kept here too, so a reopened thread keeps its history), `reopen` (the
  engineer writing in an answered thread: status back to open, response cleared), `proposal`, `accepted`,
  `rejected`, `built`. A thread waits on the modeler while it is open and the engineer wrote last (or nobody has
  replied), and on the reviewer while it is open and the modeler wrote last. So a decision on a proposal needs no
  event of its own: it is a reviewer message, and the thread waits on the modeler again.
- **Proposals:** a JSON merge patch on the base version's spec, limited to `layers`, `areal`, `petro`, `curves`,
  `contacts` and `min_thickness` (other surfaces need other tops: a data request). The merged spec is checked
  (layers per zone, cut-off ranges), and a patch that changes nothing is refused. Each proposal answers at least one
  open thread. A new proposal supersedes a waiting one on the same base, and an accepted one blocks new ones until
  it is built (a partial unique index plus triggers for the status moves). Accept or reject is the reviewer's only:
  `decide_geo_proposal` (reviewer profile), the workbench, or `opmagent review proposal accept|reject <id>`, where
  accept needs `accept` typed at the terminal, so an agent's Bash can't accept.
- **`--from-proposal`:** `opmagent geo build --from-proposal <id>` and `opmagent study build geo <study>
  --from-proposal <id>` build only an accepted proposal. They use the base's data (the study's tables, or the base's
  recorded tops and LAS files, refused if those changed since), with the merged spec. The version's assumptions name
  the proposal, its spec records `proposal_id`, and the proposal is marked `built` with a message in its threads.
  The agent then answers the threads with the version.
- **Diff** (`review_diff`, `/api/diff`, the Diff tab): spec keys changed, numbers side by side (cells, mean PORO,
  tops misfit RMS per surface, at-well RMS for phi / log10 perm / NTG, leave-one-out RMS, pore volume / HCPV /
  mean phi / perm per zone), and QC findings fixed / still / new. Maps compare by layer on the same grid, by zone
  mean when the layering changed, and not at all when the areal grid changed. The comments the version answers
  link to their view on either version. Default base: the proposal's base, else the version before. A new
  `diff_map` view (blue-red, centred on zero) is also open to `render_review_view`.
- **Review session:** `await_review_activity(model_name | version_id, timeout_s ≤ 1800)` returns `waiting_on_you`
  with the threads (at once if something already waits), `session_ended`, or `timeout`. It polls every 2 s on its
  own connection, outside the data server's lock, so the agent's other tools keep working. `review_sessions` holds
  one open session per model. The workbench shows "geo agent listening" (a wait in the last 60 s) and the counts
  waiting on each side, and "End session" ends it. `study_status` says how many open comments wait on the modeler.
- **Workbench:** threads with the agent's messages, "open view" on each view it attached, proposal cards (changes,
  expected, Accept / Reject with a reason, then the decision and "diff vN vs vM"), Reply / Reopen with an optional
  "with current view", the "waiting on me" filter, and polling every 4 s, so the agent's replies show up without a
  reload. Links between versions keep the token (session storage), and `?view=` / `?diff=` open a view or a diff.
- **Not in phase 2:** the gate's SPE9-from-deck run. Deck-based versions still have no QC payload, so the workbench
  and proposals refuse them; reading a model without logs is moved to phase 3. On the synthetic case no QC finding
  fails, so "fixed" is shown by the unit test of the diff, not by the scenario.
- **Gate:** `tests/test_geo_review_loop.py`, the loop with both sides:
  - pin, wake the waiting agent, reply with a view, the engineer's answer during a wait (other tools still answer);
  - checks on bad patches, supersede, accept (token needed);
  - build v2 (10 x 10 x 6) from it, once only;
  - diff by zone with its spec, numbers and thread, and `diff_map`;
  - answer, reopen, end the session while the agent waits;
  - rejection with a comment, and the database's status and append-only rules.

  `testing/geo_review_loop.sh` ([testing/geo-review.md](../../testing/geo-review.md)) runs the same through the real
  server and CLI in about 20 s. The browser check in the Claude desktop pane covers the thread with the agent's
  views, accepting in the card, the polled update after the CLI build, the diff tab and its "view on v1", and the
  listening indicator and End session.

**Phase 3: what-if preview.** Scratch builds, preview numbers and views on proposals, engineer knobs. Measure
preview time on Norne and decide the partial-kriging shortcut. Also: deck-based versions in the workbench (the
model read without logs or a QC payload), moved here from phase 2.

**Phase 4: res and well.** The same workbench with their figure registries (SCAL curves, contacts on sections,
well trajectories and perforations against layers).

## Decisions (open, for review)

1. **Approval in the app.** Keep it in the CLI and reviewer profile (proposed for phases 1 and 2), or allow it in
   the app behind the session token after phase 2?
2. ~~**HTTP stack.**~~ Settled in phase 1: the standard library's `http.server`.
3. ~~**Proposal table.**~~ Settled in phase 2: a kind-neutral `model_proposals` (geo only for now).
4. **The engineer's own proposals.** Allow the engineer to author a proposal from the knobs (phase 3, as
   proposed), or only to comment, so the agent stays the only one writing specs?
5. **3D.** ResInsight stays the 3D view (a button that opens the static view at the anchored cell). Or is a
   light in-browser 3D view (plotly mesh of the active cells, one property) worth adding in phase 2?
