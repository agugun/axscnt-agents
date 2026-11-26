# Review loops (geo, res, well)

This scenario checks that the engineer and the geo, res and well agents can go back and forth on their models through
the review workbench, from the first comment to a new version that answers it. The geo loop:

```
geo build v1 ──► opmagent geo review v1 (workbench)
   engineer: pins a question on a cell ──► agent (waiting): reads what is there, replies with a view
   engineer: answers in the thread     ──► agent: proposes a spec change
   engineer: accepts the proposal      ──► agent: builds v2 from it, answers the thread
   both: the diff v2 against v1
   agent: previews a change; engineer: previews kv/kh 0.05, asks the agent to build it ──► agent: builds v3
   engineer: ends the session
```

Then a res model on geo v1 (`opmagent res review`) and a well model in it (`opmagent well review`):

```
res:  engineer: a question on EQLNUM 1's OWC ──► res agent: previews OWC 2060 (no Flow run), proposes it
      engineer: accepts                      ──► res agent: `res build --from-proposal` (v2), answers
      engineer: previews SATNUM 2 nw 3 -> 2.5 over the base curves, asks for it ──► res agent: builds v3
well: engineer: a question on P1's perforation ──► well agent: answers from the perforation's cells
      (no previews: a well change is a data edit)
```

Design: [docs/design/geo-review.md](../docs/design/geo-review.md).

## Run it (scripted)

```bash
testing/geo_review_loop.sh            # about 50 s; a new temporary workspace
KEEP=1 testing/geo_review_loop.sh     # then leave the three workbenches running and open the printed links
```

The engineer's side goes through the running workbenches over HTTP with their tokens, as the browser does. The agents'
side goes through the geo, res and well profiles' tools, and the builds through `opmagent geo build
--from-proposal` and `opmagent res build --from-proposal`.

## Run it by hand (with the agent)

1. In a workspace with a geo version built from tops and logs (`opmagent geo build ...`), start the workbench:
   ```bash
   opmagent geo review <version id>
   ```
   It opens the browser at a link with the session token (or `--no-open` prints it, for the Claude desktop browser
   pane).
2. In Claude Code, ask the geo modeler: "review session on geo version <id>". It calls `await_review_activity`, and
   the workbench shows **geo agent listening**.
3. Click a cell on a layer map, write a question and post it. The agent wakes up, reads `review_context` and replies
   in the thread with views you can open.
4. Reply in the thread. Ask for a change; the agent posts a proposal card. Accept it (or reject it with a reason).
5. The agent builds the new version from the proposal and answers the thread. The card links to the diff.
6. Look at the Diff tab.
7. Open **What-if**, change a setting (say kv/kh), press **Preview**, look at the difference map and the numbers,
   and **Ask the agent to build this**. The agent builds it and answers.
8. **End session**.

## Success criteria

| # | Check | Pass when |
| --- | --- | --- |
| 1 | The workbench serves the version and guards writes | meta answers; a write without the token gets 403 |
| 2 | A click becomes an anchor | a click in B's column on layer 2 anchors `cell 9,3,2 B PORO` |
| 3 | The waiting agent hears a comment | `await_review_activity` returns `waiting_on_you` with that thread |
| 4 | The agent sees what the engineer pointed at | `review_context` gives the cell's PORO |
| 5 | Both sides see whose turn it is | after the agent's reply, the workbench shows it listening and 1 thread waiting on the engineer |
| 6 | The agent hears a reply while waiting | the engineer's reply ends the wait |
| 7 | Only an accepted proposal is built | `--from-proposal` on a proposed one fails; after Accept it builds v2, 10 x 10 x 6 |
| 8 | The diff says what changed | spec `layers` and `petro.vsh_cutoff`, maps compared by zone, the comment it answers; the NTG difference map draws |
| 9 | Previews | the agent's preview of 4 + 3 layers is 10 x 10 x 7; the engineer's kv/kh 0.05 preview changes only mean PERMZ |
| 10 | The engineer's own change | it is a proposal by the reviewer, accepted at once; the waiting agent hears it; v3 built from it changes only mean PERMZ, to the preview's value |
| 11 | The loop closes | the agent answers the thread with v2; End session releases the waiting agent (`session_ended`) |
| 12 | The res workbench | it serves res v1 (two SATNUMs); a click on EQLNUM 1's OWC anchors `EQLNUM 1 OWC` with its oil pore volume |
| 13 | The res agent | it hears the comment, previews OWC 2060 with no Flow run (oil pore volume moves to water, the same amount), and proposes it with the preview |
| 14 | Res builds | `res build --from-proposal` refuses before the accept and builds v2 after it; the diff holds only the OWC and the two pore volumes, and the comment it answers |
| 15 | The engineer's res change | the preview reads `scal.2.nw`; its curves are drawn over the base's dotted ones; requested and built as v3 ("by the engineer") |
| 16 | The well workbench | it serves the well model with no previews or proposals; a click on P1's perforation gives its cells in the geo grid |
| 17 | The well agent | it hears the comment, answers from the perforation's layers; a preview is refused; ending each session releases its agent |

The scripted run prints PASS or FAIL per check and "All checks passed." at the end.
