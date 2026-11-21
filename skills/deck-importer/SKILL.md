---
name: deck-importer
description: Import (reverse-engineer) an existing OPM/Eclipse deck into fluid, geo, res and well models for review, then prove the split is faithful by rebuilding the deck and comparing results. Use when asked to import, reverse or read an existing simulation deck.
---

# Deck importer

## Import
1. Find the deck (inbox or a path the user gives). Read back: "Import FIELDX.DATA as FieldX? Say yes."
2. `opmagent import deck --input <deck> --name <Name>`. OPM parses the deck (INCLUDEs, defaults, units).
   Cell arrays come from OPM's final properties, so EQUALS/COPY/MULTIPLY/BOX are already applied.
3. Report from the output:
   - coverage: how many keywords are mapped, applied, regenerated, passthrough (carried verbatim);
   - schedule mode: `model` (rebuilt from the well model) or `verbatim` (carried as-is, with the reasons);
   - validator findings per model. Imported data is stored as it ran in OPM; findings go to the reviewer,
     they don't block. Explain them plainly (e.g. "several wells switch from producer to injector").
   - `IMPORT_UNSUPPORTED` → explain the feature (e.g. thermal, compositional) and stop.
4. Tell the user the four models (`<Name>_FLUID`, `<Name>_GEO`, `<Name>_RES`, `<Name>_WELL`) are waiting for review,
   with the coverage report path. You cannot approve them.

## Verify (after approval)
`opmagent import verify --name <Name>`: rebuilds the deck from the approved models, runs original and
rebuilt, and compares (1) static properties and cell geometry, (2) initial pressure and saturations,
(3) all summary vectors over time. Report pass/fail and, on failure, the first difference it names.
Long fields take a while (two full runs); tell the user it's running.

## Rules
- Never edit imported tables or arrays; a re-import creates new versions.
- Open test decks: `tools/fetch_open_data.sh` downloads SPE1, SPE9 and Norne (ODbL).
