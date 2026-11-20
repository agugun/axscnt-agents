"""PVT engine MCP server (NeqSim). Computes only; persisting is the data server's job.

    opmagent-pvt [--config opmagent.toml]

Fluids are cached by fluid_handle (in this process). Each result carries the fluid spec, so
the fluid modeler can store it in the model's spec_json and rebuild it later.
"""
from __future__ import annotations

import argparse
import functools
import threading
import uuid

from mcp.server.mcpserver import MCPServer

from ..core import config as cfgmod
from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError
from ..pvt import engine
from ..pvt.defaults import DEFAULT_STAGES, default_spec


class PVTService:
    def __init__(self, cfg: cfgmod.Config | None = None):
        self.cfg = cfg
        self.fluids: dict[str, engine.FluidSpec] = {}
        self.lock = threading.Lock()

    def _spec(self, handle: str) -> engine.FluidSpec:
        if handle not in self.fluids:
            raise ToolError(NOT_FOUND, f"unknown fluid_handle '{handle}'; call characterize first")
        return self.fluids[handle]

    def _store(self, spec: engine.FluidSpec) -> str:
        h = uuid.uuid4().hex[:8]
        self.fluids[h] = spec
        return h

    @staticmethod
    def _stages(stages):
        if not stages:
            return list(DEFAULT_STAGES)
        try:
            return [engine.Stage(float(s["pressure"]), float(s["temperature"])) for s in stages]
        except (KeyError, TypeError, ValueError) as e:
            raise ToolError(INVALID_INPUT, "stages must be a list of {pressure (bar), temperature (degC)}") from e

    # ------------------------------------------------------------ tools
    def characterize(self, composition: dict | None = None, plus_fraction: dict | None = None,
                     temperature_c: float | None = None, eos: str | None = None, volume_shift: bool | None = None,
                     pseudo_components: int | None = None, use_default: bool = False) -> dict:
        """Build an EOS fluid. composition: {NeqSim name: mol %}; plus_fraction: {name, mol_percent,
        mw_g_mol, sg}. Unset EOS options use the config defaults. use_default=true gives the built-in demo oil."""
        if self.cfg and self.cfg.unit_system != "METRIC":
            raise ToolError(INVALID_INPUT, "only METRIC units are supported by the PVT engine so far")
        if use_default:
            spec = default_spec()
        else:
            if not composition or temperature_c is None:
                raise ToolError(INVALID_INPUT, "composition and temperature_c are required (or use_default=true)")
            pf = plus_fraction or {}
            d = self.cfg
            spec = engine.FluidSpec(
                composition={k: float(v) for k, v in composition.items()},
                plus_name=pf.get("name", "C7"), plus_mol=float(pf.get("mol_percent", 0.0)),
                plus_mw=float(pf.get("mw_g_mol", 200.0)) / 1000.0, plus_density=float(pf.get("sg", 0.85)),
                temperature_c=float(temperature_c),
                eos=eos or (d.eos if d else "PR"),
                volume_shift=volume_shift if volume_shift is not None else (d.volume_shift if d else True),
                pseudo_components=pseudo_components or (d.pseudo_components if d else 6))
        total = sum(spec.composition.values()) + spec.plus_mol
        if not 99.0 <= total <= 101.0:
            raise ToolError(INVALID_INPUT, f"composition sums to {total:.2f} mol %, expected 100")
        try:
            psat = engine.saturation_pressure(spec)
        except Exception as e:  # noqa: BLE001  (Java exceptions surface here)
            raise ToolError(INVALID_INPUT, f"NeqSim could not build this fluid: {e}") from e
        h = self._store(spec)
        return {"ok": True, "fluid_handle": h, "spec": spec.to_dict(), "psat_bar": psat,
                "spoken_summary": f"Fluid characterised with {spec.eos}; saturation pressure {psat:.0f} bar "
                                  f"at {spec.temperature_c:.0f} degrees C."}

    def saturation_pressure(self, fluid_handle: str) -> dict:
        """Saturation (bubble) pressure at reservoir temperature, bar."""
        p = engine.saturation_pressure(self._spec(fluid_handle))
        return {"ok": True, "psat_bar": p, "spoken_summary": f"Saturation pressure {p:.0f} bar."}

    def simulate_separator(self, fluid_handle: str, stages: list[dict] | None = None) -> dict:
        """Separator test: GOR, Bo at bubble point, stock-tank oil density and API."""
        r = engine.separator_test(self._spec(fluid_handle), self._stages(stages))
        return {"ok": True, **r, "spoken_summary": f"GOR {r['gor_sm3_sm3']:.0f} standard cubic metres per cubic metre, "
                                                    f"Bo at bubble point {r['bo_at_pb']:.3f}, API {r['api']:.0f}."}

    def simulate_cce(self, fluid_handle: str, pressures: list[float]) -> dict:
        """Constant composition expansion at reservoir temperature: relative volume and Y-function."""
        rows = engine.simulate_cce(self._spec(fluid_handle), pressures)
        return {"ok": True, "rows": rows, "spoken_summary": f"CCE simulated at {len(rows)} pressures."}

    def simulate_dl(self, fluid_handle: str, pressures: list[float]) -> dict:
        """Differential liberation at reservoir temperature: Rs_D, Bo_D, oil density."""
        rows = engine.simulate_dl(self._spec(fluid_handle), pressures)
        return {"ok": True, "rows": rows, "spoken_summary": f"DL simulated at {len(rows)} steps."}

    def simulate_viscosity(self, fluid_handle: str, pressures: list[float]) -> dict:
        """Oil and gas viscosity of the reservoir fluid at reservoir temperature."""
        rows = engine.simulate_viscosity(self._spec(fluid_handle), pressures)
        return {"ok": True, "rows": rows, "spoken_summary": f"Viscosity at {len(rows)} pressures."}

    def tune(self, fluid_handle: str, psat_lab: float, lab_dl: list[dict] | None = None,
             lab_viscosity: list[dict] | None = None) -> dict:
        """Tune the EOS to the lab saturation pressure (C7+ molar mass), then report Bo, Rs and
        viscosity errors against lab DL / viscosity data and the configured tolerances."""
        spec = self._spec(fluid_handle)
        try:
            tuned, psat_report = engine.tune_psat(spec, float(psat_lab))
        except ValueError as e:
            raise ToolError(INVALID_INPUT, str(e)) from e
        tol = self.cfg.tolerances if self.cfg else {"psat": 0.02, "bo": 0.02, "rs": 0.03, "mu_o": 0.10}
        checks = {"psat": {"error": psat_report["psat_error_after"], "tolerance": tol["psat"]}}
        if lab_dl:
            model = {round(r["pressure"], 3): r for r in engine.simulate_dl(tuned, [d["pressure"] for d in lab_dl])}
            bo_err, rs_err = [], []
            for d in lab_dl:
                m = model.get(round(float(d["pressure"]), 3))
                if not m:
                    continue
                if d.get("bo") is not None:
                    bo_err.append((m["bo_d"] - d["bo"]) / d["bo"])
                if d.get("rs") and d["rs"] > 0:
                    rs_err.append((m["rs_d"] - d["rs"]) / d["rs"])
            if bo_err:
                checks["bo"] = {"error": max(bo_err, key=abs), "tolerance": tol["bo"]}
            if rs_err:
                checks["rs"] = {"error": max(rs_err, key=abs), "tolerance": tol["rs"]}
        if lab_viscosity:
            model = engine.simulate_viscosity(tuned, [v["pressure"] for v in lab_viscosity])
            errs = [(m["mu_o"] - v["mu_o"]) / v["mu_o"] for m, v in zip(model, lab_viscosity) if m["mu_o"] and v.get("mu_o")]
            if errs:
                checks["mu_o"] = {"error": max(errs, key=abs), "tolerance": tol["mu_o"]}
        for c in checks.values():
            c["passed"] = abs(c["error"]) <= c["tolerance"]
        failed = [k for k, c in checks.items() if not c["passed"]]
        h = self._store(tuned)
        say = ("All matches within tolerance." if not failed else
               "Outside tolerance: " + ", ".join(f"{k} {checks[k]['error']*100:+.1f} percent" for k in failed) + ".")
        return {"ok": True, "fluid_handle": h, "spec": tuned.to_dict(), "psat_tuning": psat_report, "checks": checks,
                "within_tolerance": not failed,
                "note": "Bo/Rs/viscosity are checked, not yet tuned; a miss needs your decision.",
                "spoken_summary": f"Tuned saturation pressure to {psat_report['psat_after']:.0f} bar. {say}"}

    def generate_black_oil(self, fluid_handle: str, p_min: float, p_max: float, n: int = 12,
                           stages: list[dict] | None = None, salinity_ppm: float = 0.0) -> dict:
        """Generate OPM black-oil tables (PVTO, PVDG, PVTW, DENSITY) in the data server's row format."""
        try:
            r = engine.generate_black_oil(self._spec(fluid_handle), float(p_min), float(p_max), n=int(n),
                                          stages=self._stages(stages), salinity_ppm=float(salinity_ppm))
        except ValueError as e:
            raise ToolError(INVALID_INPUT, str(e)) from e
        s = r["summary"]
        return {"ok": True, **r, "spoken_summary": f"Black-oil tables ready: bubble point {s['psat_bar']:.0f} bar, "
                                                    f"Rs {s['rs_at_pb']:.0f}, Bo {s['bo_at_pb']:.3f}."}


TOOLS = ["characterize", "saturation_pressure", "simulate_separator", "simulate_cce", "simulate_dl",
         "simulate_viscosity", "tune", "generate_black_oil"]


def wrap(svc: PVTService, name: str):
    fn = getattr(svc, name)

    @functools.wraps(fn)
    def tool(**kwargs):
        with svc.lock:
            try:
                return fn(**kwargs)
            except ToolError as e:
                return e.to_dict()
            except Exception as e:  # noqa: BLE001  (NeqSim/Java failures)
                return ToolError("ENGINE_ERROR", f"{type(e).__name__}: {e}").to_dict()

    return tool


def build_server(cfg: cfgmod.Config | None) -> tuple[MCPServer, PVTService]:
    svc = PVTService(cfg)
    server = MCPServer("opmagent-pvt", instructions=(
        "NeqSim PVT engine. Characterize a fluid, simulate lab tests, tune, then generate black-oil tables "
        "and pass them to the data server's write_tables. METRIC units."))
    for name in TOOLS:
        server.add_tool(wrap(svc, name), name=name, description=getattr(PVTService, name).__doc__)
    return server, svc


def main(argv=None):
    p = argparse.ArgumentParser(prog="opmagent-pvt")
    p.add_argument("--config")
    a = p.parse_args(argv)
    cfg = None
    try:
        cfg = cfgmod.load(a.config)
    except FileNotFoundError:
        pass
    server, _ = build_server(cfg)
    server.run("stdio")


if __name__ == "__main__":
    main()
