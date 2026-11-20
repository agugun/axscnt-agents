---
name: geo-modeler
description: Build a geo model (corner-point grid, porosity/permeability/NTG, SCAL, rock, contacts) from well tops, LAS logs and a short spec, and submit it for human review. Use when asked to build or update a geological / static model.
---

# Geo modeler

Python only (lasio, numpy, gstools). Inputs:
- **tops** CSV: well_name, surface, x, y, tvd (one row per well and surface)
- **logs**: LAS files (DEPT as TVD, a TVD curve, or MD with `--surveys`) or CSV (well_name, tvd, phi[, perm][, vsh])
- **spec** JSON, written from the user's description:
  ```json
  {"surfaces": ["TopRes", "MidRes", "BaseRes"], "layers": [6, 4],
   "areal": {"dx": 100, "dy": 100, "margin": 500},
   "contacts": {"datum_depth": 2000, "datum_pressure": 200, "owc_depth": 2100, "goc_depth": null},
   "petro": {"vsh_cutoff": 0.5, "kv_kh": 0.1, "poro_perm": [0.01, 30]},
   "scal": {"swc": 0.2, "sorw": 0.2, "sorg": 0.1, "nw": 2, "now": 2},
   "rock": {"p_ref": 200, "cr": 4.5e-5}}
  ```
  Ask for contacts, datum depth and pressure, and the zone layering if the user hasn't said; never guess them.
  `areal` can also give x0, y0, nx, ny explicitly; otherwise the grid covers the wells plus the margin.

## Workflow
1. Find the files (`find_inbox_file`), write the spec from the user's words, read it back briefly.
2. `opmagent geo build --tops T --logs L1.las L2.las --spec spec.json --name <Name> [--surveys S]`
3. Report: grid size, hydrocarbon pore volume per zone, QC report path, and the assumptions
   (kriging, layering, poro-perm transform if no perm log, Corey SCAL, no faults).
4. It's then waiting for review. Never approve.

Method: surfaces kriged from tops (IDW below 3 wells), layers split proportionally, logs upscaled per layer
(phi arithmetic, perm geometric, NTG from Vsh cutoff) and kriged per layer. Vertical pillars, no faults yet.
