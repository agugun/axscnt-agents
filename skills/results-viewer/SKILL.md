---
name: results-viewer
description: Open a finished simulation run in ResInsight (3D grid, properties over time, summary vectors and well plots), or a geo model's static view (its grid and properties before any simulation), and complete a study's view task. Use when asked to show, view or visualise results, or when a study's view task is ready.
---

# Results viewer

## Tools
- Data server, **view** profile: `study_status`, `start_task`, `complete_task`, `fail_task`, `list_decks`, `get_deck`,
  `static_view`, `init_view`.
- Shell: `opmagent results open --study <name>` (the study's run), `--run <id>`, `--case <run folder or EGRID>`, or
  `--geo <geo version id>` (its static view, made if needed), or `--deck <deck id> --init` (the deck at time zero);
  `--dry-run` prints the command without starting ResInsight.

## Workflow
1. In a study: `study_status(name)`; if the view task is ready, `start_task(name)`.
2. `opmagent results open --study <name>` (or `--run <id>`). It opens the grid with its properties and the
   summary vectors next to it, and returns at once; ResInsight stays open on the user's desktop.
3. Tell the user where to look: in the Project Tree, the case's Cell Result (PRESSURE, SWAT, SGAS, PERMX ...)
   with the time slider for changes over time; Summary Cases for plots such as FOPR, FWCT, FPR and WBHP per well.
4. `complete_task(name, {"opened": "<case path>"})`. That finishes the study.

If it says ResInsight isn't found, the user sets `[viewer] resinsight_bin` in opmagent.toml. If there is no
display (a remote session), say so; it can't open a window there. Never change the run or its files.

## A geo model before any simulation
"Show the geo model in ResInsight" / "open field A geo version three": `opmagent results open --geo <version id>`.
It opens the static view: the geo model as a deck, made into EGRID + INIT by an OPM Flow dry run, so ResInsight shows
the grid and PORO, PERMX/Y/Z, NTG, PORV, DEPTH, transmissibilities and the regions as OPM computes them (with the
res model's SATNUM, EQLNUM, FIPNUM and SWATINIT when a res model exists on that geo version). It works for drafts, so
the engineer can look before approving. Point them to Cell Result (static properties, no time steps). Its deck has
placeholder fluid tables only so OPM can load it: it is not a simulation deck, and never say it is.

## A deck at time zero
"Show the deck at time zero" / "how does the model start?": `opmagent results open --deck <deck id> --init`. It opens
the initialisation view: the deck run for one tiny step without wells, so time step 0 is the initial state. Point
them to PRESSURE, SWAT, SGAS and RS at step 0, and to EQLNUM / FIPNUM; the summary has FOIP, FGIP, FWIP and FPR at
time zero. `init_view(deck_id)` gives the fluids in place per region and the checks against the contacts. It is not
a run of the deck.

