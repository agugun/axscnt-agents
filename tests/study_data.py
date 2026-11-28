"""Raw data for a small study, the way an engineer might hand it over: an example lab report, tops and LAS logs,
and well tables in one workbook with non-standard column names."""
from pathlib import Path

import pandas as pd

from opmagent.fluid.excel import write_template
from tests.test_geo_modeler import SPEC, write_inputs


def write_raw(inbox: Path, *, with_logs: bool = True, with_spec: bool = True, controls_date: str = "2026-01-01",
              rates: tuple[float, float] = (300, 350)) -> None:
    """controls_date: when the wells' (prediction) controls start; later than the start when history comes first.
    rates: the producer's oil rate and the injector's water rate under those controls."""
    inbox.mkdir(parents=True, exist_ok=True)
    write_template(inbox / "PVT lab report WELL-A.xlsx", example=True)
    tops, logs = write_inputs(inbox)
    pd.read_csv(tops).rename(columns={"well_name": "WELL", "x": "EASTING", "y": "NORTHING", "tvd": "TVDSS",
                                      "surface": "HORIZON"}).to_csv(inbox / "picks.csv", index=False)
    Path(tops).unlink()
    if not with_logs:
        for p in logs:
            Path(p).unlink()
    if with_spec:
        import json
        (inbox / "grid.json").write_text(json.dumps(SPEC))
    wells = pd.DataFrame([{"WELL": "P1", "Type": "PRODUCER", "Phase": "OIL", "Easting": 1700, "Northing": 1700,
                           "Datum": 2000},
                          {"WELL": "I1", "Type": "INJECTOR", "Phase": "WATER", "Easting": 300, "Northing": 300,
                           "Datum": 2000}])
    surveys = pd.DataFrame([{"Well": w, "MD": md, "X": x, "Y": y, "TVD": md}
                            for w, x, y in (("P1", 1700, 1700), ("I1", 300, 300)) for md in (0.0, 2200.0)])
    perfs = pd.DataFrame([{"Well": "P1", "Date": "2026-01-01", "MD_TOP": 2018, "MD_BOTTOM": 2066},
                          {"Well": "I1", "Date": "2026-01-01", "MD_TOP": 2004, "MD_BOTTOM": 2052}])
    ctl = pd.DataFrame([{"Well": "P1", "Date": controls_date, "Control": "ORAT", "Rate": rates[0], "BHP": 100},
                        {"Well": "I1", "Date": controls_date, "Control": "RATE", "Rate": rates[1], "BHP": 350}])
    with pd.ExcelWriter(inbox / "well data.xlsx") as x:
        wells.to_excel(x, sheet_name="Wells", index=False)
        surveys.to_excel(x, sheet_name="Deviation", index=False)
        perfs.to_excel(x, sheet_name="Perfs", index=False)
        ctl.to_excel(x, sheet_name="Controls", index=False)
    (inbox / "notes.txt").write_text("call the geologist about zone 2")
    (inbox / "mystery.bin").write_bytes(b"\x00\x01")


def load_raw(con, name: str, **kw) -> dict:
    """Write the raw data into the study's inbox and load it (the data manager's job)."""
    from opmagent.core import studies
    from opmagent.data.load import load
    write_raw(Path(studies._study(con, name)["folder"]) / "inbox", **kw)
    return load(con, name)
