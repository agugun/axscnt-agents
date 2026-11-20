---
name: results-viewer
description: Open a finished simulation run in ResInsight (3D grid, properties over time, summary vectors and well plots), and complete a study's view task. Use when asked to show, view or visualise results, or when a study's view task is ready.
---

# Results viewer

## Tools
- Data server, **view** profile: `study_status`, `start_task`, `complete_task`, `fail_task`, `list_decks`, `get_deck`.
- Shell: `opmagent results open --study <name>` (the study's run), `--run <id>`, or `--case <run folder or EGRID>`;
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
