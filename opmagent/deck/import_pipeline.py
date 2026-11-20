"""Import a deck into draft models (import profile), and verify the round trip after approval."""
from __future__ import annotations

import html
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from ..core.errors import INVALID_INPUT, ToolError
from ..servers.data_server import DataService
from ..servers.data_server import wrap as dwrap
from ..sim.runner import run_deck
from . import roundtrip
from .build import build_deck
from .importer import ImportError_, import_deck


def _ok(r):
    if not r.get("ok"):
        raise ToolError(r.get("code", "ERROR"), r.get("message", "failed"), r.get("details"))
    return r


def coverage_report(path: Path, name: str, imp: dict) -> Path:
    cov = imp["coverage"]
    rows = "".join(f"<tr><td>{html.escape(k['section'])}</td><td>{html.escape(k['keyword'])}</td><td>{k['handling']}</td></tr>"
                   for k in cov["keywords"])
    counts = ", ".join(f"{v} {k}" for k, v in cov["counts"].items())
    reasons = "".join(f"<li>{html.escape(r)}</li>" for r in cov["schedule_reasons"]) or "<li>None</li>"
    g = imp["grid"]
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Import coverage: {html.escape(name)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #eee;padding:4px 8px;text-align:left}}</style></head><body>
<h1>Deck import: {html.escape(name)}</h1>
<p>Source: {html.escape(imp['spec']['source_deck'])}<br>Grid {g['nx']} x {g['ny']} x {g['nz']}, {g['nactive']} active cells.
Generated {datetime.now():%Y-%m-%d %H:%M}.</p>
<h2>Coverage</h2><p>{counts}. Schedule mode: <b>{cov['schedule_mode']}</b>.</p>
<p><i>mapped</i>: stored in the fluid, geo or well model. <i>applied</i>: edit operators already applied by OPM to the
stored arrays. <i>regenerated</i>: written by the deck builder. <i>passthrough</i>: carried verbatim.</p>
<h2>Why the schedule is verbatim</h2><ul>{reasons}</ul>
<h2>All keywords</h2><table><tr><th>Section</th><th>Keyword</th><th>Handling</th></tr>{rows}</table></body></html>"""
    path.write_text(doc)
    return path


def import_to_drafts(cfg, deck_path: str, name: str) -> dict:
    """Parse the deck, write fluid/geo/well drafts (origin 'imported'), coverage report, submit for review."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        raise ToolError(INVALID_INPUT, "name must be letters, digits, _ or -")
    out = _new_import_dir(cfg.file_store / "imports" / name)   # never reuse: earlier versions keep their files
    try:
        imp = import_deck(deck_path, out)
    except Exception as e:
        shutil.rmtree(out, ignore_errors=True)   # nothing registered yet: leave no empty import behind
        if isinstance(e, ImportError_):
            raise ToolError("IMPORT_UNSUPPORTED", str(e)) from e
        raise
    svc = DataService("import", cfg)
    D = lambda n, **k: _ok(dwrap(svc, n)(**k))  # noqa: E731
    src = D("register_file", path=str(Path(deck_path).resolve()), kind="deck", note="imported source deck")["file_id"]
    rep = coverage_report(out / "coverage.html", name, imp)
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    result = {"name": name, "coverage": imp["coverage"]["counts"], "schedule_mode": imp["coverage"]["schedule_mode"],
              "versions": {}, "validator": {}}

    def make(kind, header, tables, extra_header=None):
        h = {**header, **(extra_header or {}), "source_deck_file_id": src,
             "user_description": f"imported from {Path(deck_path).name}"}
        v = D("create_version", kind=kind, model_name=f"{name}_{kind.upper()}", header=h, origin="imported")
        result.setdefault("_created", {})[kind] = v["version_id"]
        for t, rows in tables.items():
            if rows:
                D("write_tables", kind=kind, version_id=v["version_id"], table=t, rows=rows)
        val = D("run_validator", kind=kind, version_id=v["version_id"])
        sub = dwrap(svc, "submit_for_review")(kind=kind, version_id=v["version_id"], qc_report_file_id=rid,
                                              metrics={"imported_from": Path(deck_path).name},
                                              accept_validator_errors=True)
        _ok(sub)
        result["versions"][kind] = v["version_id"]
        result["validator"][kind] = {"errors": [e["message"] for e in val["errors"]][:10], "warnings": len(val["warnings"])}
        return v

    try:
        _make_all(make, imp, D, deck_path, name, result)
    except Exception:
        _rollback(svc, result)
        raise
    spec = {**imp["spec"], "name": f"{name}_RT", "requirement": f"round trip of {Path(deck_path).name}"}
    spec_path = out / "deck_spec.json"
    spec_path.write_text(json.dumps(spec, default=str))
    (out / IMPORT_META).write_text(json.dumps({"name": name, "source_deck": str(Path(deck_path).resolve()),
                                               "versions": result["versions"], "created": datetime.now().isoformat()}))
    result.update(spec=str(spec_path), coverage_report=str(rep), import_dir=str(out),
                  spoken_summary=(f"Imported {Path(deck_path).name} as {name}: fluid, geo and well models are waiting for "
                                  f"your review. {imp['coverage']['counts'].get('passthrough', 0)} keywords are carried "
                                  f"verbatim; schedule is {imp['coverage']['schedule_mode']}."))
    result["ok"] = True
    return result


IMPORT_META = "import.json"


def _new_import_dir(base: Path) -> Path:
    """imports/<name>/import-NNN: one folder per import, so a re-import never overwrites files that earlier
    (possibly approved) model versions were reviewed with."""
    base.mkdir(parents=True, exist_ok=True)
    nums = [int(p.name.split("-")[1]) for p in base.glob("import-[0-9]*") if p.name.split("-")[1].isdigit()]
    out = base / f"import-{max(nums, default=0) + 1:03d}"
    out.mkdir()
    return out


def _approved_import(cfg, name: str) -> tuple[Path, dict] | None:
    """The newest import of this name whose fluid, geo and well versions are all approved."""
    svc = DataService("deck", cfg)   # sees approved models only
    approved = {k: {m["version_id"] for m in svc.list_models(kind=k)["models"]} for k in ("fluid", "geo", "well")}
    for meta in sorted((cfg.file_store / "imports" / name).glob(f"import-*/{IMPORT_META}"), reverse=True):
        v = json.loads(meta.read_text())["versions"]
        if all(v.get(k) in approved[k] for k in approved):
            return meta.parent, v
    return None


def _rollback(svc, result):
    """Remove the drafts this import created (all-or-nothing)."""
    for kind in ("well", "geo", "fluid"):
        vid = result["versions"].get(kind) or result.get("_created", {}).get(kind)
        if not vid:
            continue
        vt = f"{kind}_model_versions"
        svc.con.execute(f"UPDATE {vt} SET status='draft' WHERE id=? AND status='qc_ready'", (vid,))
        svc.con.execute(f"DELETE FROM {vt} WHERE id=? AND status='draft'", (vid,))


def _make_all(make, imp, D, deck_path, name, result):
    make("fluid", imp["fluid"]["header"], imp["fluid"]["tables"])
    g = imp["geo"]
    gh = dict(g["header"])
    if gh.get("grid_file"):
        gh["grid_file_id"] = D("register_file", path=gh.pop("grid_file"), kind="grid")["file_id"]
    props = []
    for p in g["props"]:
        if "file" in p:
            props.append({"keyword": p["keyword"], "file_id": D("register_file", path=p["file"], kind="property")["file_id"]})
        else:
            props.append({"keyword": p["keyword"], "const_value": p["const_value"]})
    geo_v = make("geo", gh, {**g["tables"], "geo_properties": props})
    w = imp["well"]
    make("well", w["header"], w["tables"], {"geo_version_id": geo_v["version_id"]})


def verify(cfg, name: str, on_progress=None) -> dict:
    """After approval: rebuild the deck, run original and rebuilt, compare static, init and dynamic results.
    on_progress(label) -> callback, called once per run ('original', 'rebuilt'), to follow each Flow run live."""
    found = _approved_import(cfg, name)
    if found:   # rebuild from exactly the versions this import created, with this import's files
        base, versions = found
        models = {k: int(versions[k]) for k in ("fluid", "geo", "well")}
    else:       # imports made before per-import folders: one shared folder, latest approved models
        base = cfg.file_store / "imports" / name
        models = {k: f"{name}_{k.upper()}" for k in ("fluid", "geo", "well")}
        if not (base / "deck_spec.json").exists():
            if any(base.glob(f"import-*/{IMPORT_META}")):
                raise ToolError(INVALID_INPUT, f"no import of {name} has all three models approved yet")
            raise ToolError(INVALID_INPUT, f"no import named {name}")
    spec = json.loads((base / "deck_spec.json").read_text())
    spec.update(models)
    d = build_deck(spec, cfg, origin="built")
    if not d["ok"]:
        return {"ok": False, "stage": "rebuild", "deck": d, "spoken_summary": d["spoken_summary"]}
    cb = on_progress or (lambda _label: None)
    orig = run_deck(spec["source_deck"], base / "run_original", cfg.flow_bin, on_progress=cb("original"))
    rebuilt = run_deck(d["path"], base / "run_rebuilt", cfg.flow_bin, on_progress=cb("rebuilt"))
    if not (orig["ok"] and rebuilt["ok"]):
        bad = "original" if not orig["ok"] else "rebuilt"
        return {"ok": False, "stage": f"run {bad}", "original": orig, "rebuilt": rebuilt,
                "spoken_summary": f"The {bad} deck failed to run; see its log."}
    cmp = roundtrip.compare(base / "run_original", base / "run_rebuilt", Path(spec["source_deck"]).stem, Path(d["path"]).stem,
                            Path(spec["source_deck"]), Path(d["path"]))
    (base / "roundtrip.json").write_text(json.dumps(cmp, indent=1, default=str))
    if cmp["passed"]:
        dyn = cmp["levels"][2]
        say = (f"Round trip passed: static, initial state and {dyn['vectors_compared']} summary vectors match the "
               f"original within tolerance.")
    else:
        last = cmp["levels"][-1]
        say = f"Round trip failed at the {last.get('level')} level: " + (
            ", ".join(last.get("failed", [])[:5]) or last.get("error", "see report")) + "."
    return {"ok": cmp["passed"], "deck": d, "comparison": cmp, "report": str(base / "roundtrip.json"), "spoken_summary": say}
