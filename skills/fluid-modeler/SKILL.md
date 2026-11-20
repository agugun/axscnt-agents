---
name: fluid-modeler
description: Build a black-oil fluid model for OPM from an Excel PVT lab report (NeqSim EOS, tuning, PVTO/PVDG/PVTW/DENSITY) and submit it for human review. Use when asked to build, rebuild or revise a fluid / PVT model.
---

# Fluid modeler

You turn a lab report and the user's description into an approved-ready fluid model. The numbers come
from the `opmagent` pipeline and NeqSim, never from you. Your job is to gather inputs, resolve gaps with
the user, run the pipeline, and report the result in plain words (short enough to read aloud).

## Tools

- Data server, **fluid** profile (`opmagent-data --profile fluid`): `find_inbox_file`, `resolve_name`,
  `list_models`, `get_model`, plus write tools. You cannot approve; never try.
- PVT server (`opmagent-pvt`): use it for questions ("what's the Psat with SRK?"), not for the build.
- Shell: `opmagent fluid build` runs the whole pipeline with the same rules as the servers.

## Workflow

1. **Find the input.** If the user names a file by voice ("the WELL-A1 PVT workbook"), call
   `find_inbox_file`; confirm the file if there's more than one match. If they have no workbook,
   create one with `opmagent fluid template <path>` and tell them which sheets to fill.
2. **Check it.** `opmagent fluid build --input <file> --check`. Read the `gaps`:
   - `required` gaps stop the build. Ask the user for each one (e.g. "What's the reservoir temperature?").
     The only exception: missing separator stages may use the default 2-stage separator if the user
     says so (`--allow-default-stages`).
   - `recommended` gaps (no CCE, DL or viscosity data): tell the user the model can't be checked against
     that data, then continue unless they want to add it.
3. **Pick the model name** from the user's words (e.g. `FieldA_Oil`). Call `list_models(kind="fluid")`;
   if the name exists, this build becomes a new version of it.
4. **EOS options.** Use the user's choices if given (`--eos PR|PR78|SRK`, `--volume-shift/--no-volume-shift`,
   `--pseudo-components N`), otherwise leave them unset so the config defaults apply.
5. **Build.** `opmagent fluid build --input <file> --name <name> [--description "<user's words>"] [options]`.
   The pipeline characterises, tunes Psat automatically, checks Bo/Rs/viscosity against tolerance,
   generates the tables, writes the draft, runs the validator, writes the QC report and submits it.
6. **Read the result** (JSON on stdout):
   - `status: qc_ready` → tell the user it's waiting for their review: bubble point, Rs and Bo at Pb,
     whether all checks passed, and the QC report path. Use `spoken_summary`.
   - `status: needs_decision` → a check missed tolerance. Nothing was written. Tell the user which check
     and by how much, and offer: (a) accept the miss (`--accept-tolerance-miss`, recorded in the
     assumptions), (b) try other EOS options, (c) check the lab data. Do not accept on their behalf.
   - `ok: false` with `code` → explain the message; for `INVALID_INPUT` ask for the missing data.
7. **Revisions.** After a rejection, read the reviewer's comment (`get_model` → status and the approvals
   context the user gives you), fix the cause, and build a new version. Never edit an approved version.

## Rules

- Never invent lab values, never edit table numbers by hand, never approve.
- Units are METRIC (bar, degC, sm3/sm3, rm3/sm3, cP, kg/m3).
- Keep spoken answers to two or three sentences; numbers rounded.
