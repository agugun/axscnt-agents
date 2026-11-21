"""NeqSim-based PVT engine: EOS characterisation, lab-experiment simulation, tuning and
black-oil table generation for OPM (METRIC units: bar, degC, sm3/sm3, rm3/sm3, cP, kg/m3).

Black-oil method (depletion + separator, Whitson-Torp style):
  * deplete the reservoir fluid at reservoir temperature from Psat down the pressure grid,
    removing the equilibrium gas at each step (as in a DL experiment);
  * flash each step's equilibrium oil through the separator train to stock tank:
    Rs = separator gas (sm3) / stock-tank oil (sm3), Bo = reservoir oil volume / stock-tank oil volume;
  * compress each saturated oil to higher grid pressures for the undersaturated PVTO rows;
  * PVDG from each step's equilibrium gas (dry-gas assumption: liquid dropped from that gas is ignored);
  * PVTW from the McCain correlations at the given salinity.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from neqsim import jneqsim

P_SC = 1.01325          # bar
T_SC = 15.56            # degC (60 F)
EOS_CLASSES = {"PR": "SystemPrEos", "PR78": "SystemPrEos1978", "SRK": "SystemSrkEos"}

_ts = jneqsim.thermo.system
_Ops = jneqsim.thermodynamicoperations.ThermodynamicOperations


@dataclass
class FluidSpec:
    """Everything needed to rebuild a characterised fluid deterministically."""
    composition: dict[str, float]            # NeqSim component name -> mol %
    plus_name: str = "C7"
    plus_mol: float = 0.0                    # mol %
    plus_mw: float = 0.200                   # kg/mol
    plus_density: float = 0.85               # g/cm3 (specific gravity)
    temperature_c: float = 100.0
    eos: str = "PR"
    volume_shift: bool = True
    pseudo_components: int = 6
    kij_c1_plus: float | None = None         # methane / plus-fraction pseudo-components BIP (None = NeqSim default)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Stage:
    pressure: float     # bar
    temperature: float  # degC


DEFAULT_SEPARATOR = [Stage(30.0, 40.0), Stage(P_SC, T_SC)]


# ---------------------------------------------------------------- fluid building
def build(spec: FluidSpec, pressure: float = 100.0):
    if spec.eos not in EOS_CLASSES:
        raise ValueError(f"eos must be one of {list(EOS_CLASSES)}")
    sys_ = getattr(_ts, EOS_CLASSES[spec.eos])(273.15 + spec.temperature_c, pressure)
    for name, mol in spec.composition.items():
        if mol > 0:
            sys_.addComponent(name, float(mol))
    if spec.plus_mol > 0:
        sys_.addPlusFraction(spec.plus_name, float(spec.plus_mol), float(spec.plus_mw), float(spec.plus_density))
        ch = sys_.getCharacterization()
        ch.setLumpingModel("PVTlumpingModel")
        ch.getLumpingModel().setNumberOfPseudoComponents(int(spec.pseudo_components))
        ch.characterisePlusFraction()
    sys_.setMixingRule("classic")
    if spec.kij_c1_plus is not None:
        _set_kij_c1_plus(sys_, float(spec.kij_c1_plus))
    sys_.useVolumeCorrection(bool(spec.volume_shift))
    sys_.init(0)
    return sys_


def _set_kij_c1_plus(sys_, k: float):
    ph0 = sys_.getPhase(0)
    names = [str(ph0.getComponent(i).getComponentName()) for i in range(ph0.getNumberOfComponents())]
    if "methane" not in names:
        return
    c1 = names.index("methane")
    heavy = [i for i, n in enumerate(names) if n.endswith("_PC")]
    for ph in range(sys_.getMaxNumberOfPhases()):
        mr = sys_.getPhase(ph).getMixingRule()
        for j in heavy:
            mr.setBinaryInteractionParameter(c1, j, k)
            mr.setBinaryInteractionParameter(j, c1, k)


def _flash(s, p: float, t_c: float):
    s.setPressure(float(p))
    s.setTemperature(273.15 + float(t_c))
    _Ops(s).TPflash()
    s.initProperties()
    return s


def _phase(s, kind: str):
    return s.getPhase(kind) if s.hasPhaseType(kind) else None


def _liquid(s):
    """The oil-like phase (NeqSim may label a single dense phase 'oil' or 'gas')."""
    if s.hasPhaseType("oil"):
        return s.getPhase("oil")
    return s.getPhase(0) if s.getNumberOfPhases() == 1 else None


def _total_volume(s) -> float:
    return sum(s.getPhase(i).getVolume("m3") for i in range(s.getNumberOfPhases()))


def components(spec: FluidSpec) -> list[dict]:
    """The characterised components (after plus-fraction splitting and lumping): name, mol %, molar mass g/mol."""
    ph = build(spec).getPhase(0)
    return [{"name": str(ph.getComponent(i).getComponentName()), "mol_percent": 100.0 * ph.getComponent(i).getz(),
             "mw_g_mol": 1000.0 * ph.getComponent(i).getMolarMass(),
             "pseudo": str(ph.getComponent(i).getComponentName()).endswith("_PC")}
            for i in range(ph.getNumberOfComponents())]


def phase_envelope(spec: FluidSpec) -> dict:
    """P-T phase envelope (degC, bar): bubble and dew branches, cricondenbar and cricondentherm."""
    ops = _Ops(build(spec))
    ops.calcPTphaseEnvelope()

    def branch(t_key, p_key):
        return [(t - 273.15, p) for t, p in zip(list(ops.get(t_key)), list(ops.get(p_key)))
                if not (math.isnan(t) or math.isnan(p))]

    def point(key):
        t, p = list(ops.get(key))[:2]
        return {"temperature_c": t - 273.15, "pressure": p}

    return {"bubble": branch("bubT", "bubP"), "dew": branch("dewT", "dewP"),
            "cricondenbar": point("cricondenbar"), "cricondentherm": point("cricondentherm")}


def saturation_pressure(spec: FluidSpec) -> float:
    s = build(spec)
    sim = jneqsim.pvtsimulation.simulation.SaturationPressure(s)
    sim.run()
    return float(sim.getSaturationPressure())


# ---------------------------------------------------------------- separator train
def separator_flash(oil_system, stages: list[Stage]) -> dict:
    """Flash an oil composition through separator stages to stock tank.

    Returns stock-tank oil volume (m3 at SC), separator gas volume at SC (m3), and densities.
    """
    s = oil_system.clone()
    gas_vol_sc = 0.0
    gas_mass = 0.0
    for st in stages:
        _flash(s, st.pressure, st.temperature)
        g = _phase(s, "gas")
        if g is not None and s.getNumberOfPhases() > 1:
            gsys = s.phaseToSystem("gas")
            _flash(gsys, P_SC, T_SC)
            for i in range(gsys.getNumberOfPhases()):  # liquid dropped at SC is counted as gas mass (small)
                ph = gsys.getPhase(i)
                gas_vol_sc += ph.getVolume("m3") if i == 0 else 0.0
                gas_mass += ph.getNumberOfMolesInPhase() * ph.getMolarMass()
            s = s.phaseToSystem("oil")
        elif g is not None:  # all gas: nothing left as oil
            return {"sto_volume": 0.0, "gas_volume_sc": gas_vol_sc, "sto_density": None, "gas_density_sc": None}
    _flash(s, P_SC, T_SC)
    oil = _liquid(s)
    if s.getNumberOfPhases() > 1 and _phase(s, "gas") is not None:  # last bit of gas at SC
        g = s.getPhase("gas")
        gas_vol_sc += g.getVolume("m3")
        gas_mass += g.getNumberOfMolesInPhase() * g.getMolarMass()
    return {
        "sto_volume": oil.getVolume("m3"),
        "gas_volume_sc": gas_vol_sc,
        "sto_density": oil.getDensity("kg/m3"),
        "gas_density_sc": gas_mass / gas_vol_sc if gas_vol_sc > 0 else None,
    }


def separator_test(spec: FluidSpec, stages: list[Stage] | None = None) -> dict:
    """GOR (sm3/sm3), Bo at bubble point (rm3/sm3) and stock-tank densities from the separator train."""
    stages = stages or DEFAULT_SEPARATOR
    psat = saturation_pressure(spec)
    s = _flash(build(spec), psat + 0.01, spec.temperature_c)
    v_res = _total_volume(s)
    sep = separator_flash(s, stages)
    api = 141.5 / (sep["sto_density"] / 999.0) - 131.5
    return {"psat_bar": psat, "gor_sm3_sm3": sep["gas_volume_sc"] / sep["sto_volume"],
            "bo_at_pb": v_res / sep["sto_volume"], "sto_density": sep["sto_density"],
            "gas_density_sc": sep["gas_density_sc"], "api": api}


# ---------------------------------------------------------------- lab experiments
def simulate_cce(spec: FluidSpec, pressures: list[float]) -> list[dict]:
    """Constant composition expansion: relative volume V/Vsat (and Y-function below Psat)."""
    psat = saturation_pressure(spec)
    base = build(spec)
    v_sat = _total_volume(_flash(base.clone(), psat + 0.01, spec.temperature_c))
    out = []
    for p in pressures:
        v = _total_volume(_flash(base.clone(), p, spec.temperature_c))
        rv = v / v_sat
        row = {"pressure": p, "relative_volume": rv}
        if p < psat and rv > 1:
            row["y_function"] = (psat - p) / (p * (rv - 1))
        out.append(row)
    return out


def simulate_dl(spec: FluidSpec, pressures: list[float]) -> list[dict]:
    """Differential liberation at reservoir T: Rs_D and Bo_D relative to residual oil at SC."""
    psat = saturation_pressure(spec)
    steps = sorted({float(p) for p in pressures if p < psat} | {P_SC}, reverse=True)
    s = _flash(build(spec), psat + 0.01, spec.temperature_c)
    records = [{"pressure": psat, "oil_volume": _liquid(s).getVolume("m3"), "gas_sc": 0.0,
                "oil_density": _liquid(s).getDensity("kg/m3")}]
    for p in steps:
        _flash(s, p, spec.temperature_c)
        gas_sc = 0.0
        if s.getNumberOfPhases() > 1 and _phase(s, "gas") is not None:
            g = s.phaseToSystem("gas")
            _flash(g, P_SC, T_SC)
            gas_sc = g.getPhase(0).getVolume("m3")
            s = s.phaseToSystem("oil")
            _flash(s, p, spec.temperature_c)
        records.append({"pressure": p, "oil_volume": _liquid(s).getVolume("m3"), "gas_sc": gas_sc,
                        "oil_density": _liquid(s).getDensity("kg/m3")})
    resid = _flash(s.clone(), P_SC, T_SC)
    v_resid = _liquid(resid).getVolume("m3")
    out, cum_below = [], 0.0
    for rec in reversed(records):          # from lowest pressure up
        out.append({"pressure": rec["pressure"], "rs_d": cum_below / v_resid,
                    "bo_d": rec["oil_volume"] / v_resid, "oil_density": rec["oil_density"]})
        cum_below += rec["gas_sc"]
    out.reverse()
    return out


def simulate_viscosity(spec: FluidSpec, pressures: list[float]) -> list[dict]:
    """Oil (and gas, if present) viscosity of the reservoir fluid at reservoir T."""
    base = build(spec)
    out = []
    for p in pressures:
        s = _flash(base.clone(), p, spec.temperature_c)
        oil, gas = _liquid(s), _phase(s, "gas") if s.getNumberOfPhases() > 1 else None
        out.append({"pressure": p, "mu_o": oil.getViscosity("cP") if oil else None,
                    "mu_g": gas.getViscosity("cP") if gas is not None else None})
    return out


# ---------------------------------------------------------------- tuning
def tune_psat(spec: FluidSpec, psat_lab: float, tol: float = 0.002, max_iter: int = 50,
              k_range: tuple[float, float] = (-0.10, 0.30)) -> tuple[FluidSpec, dict]:
    """Match the lab saturation pressure by adjusting the methane / C7+ binary interaction
    coefficient (bisection; Psat rises monotonically with it)."""
    if spec.plus_mol <= 0:
        raise ValueError("tuning Psat needs a plus fraction")

    def psat_at(k):
        return saturation_pressure(FluidSpec(**{**spec.to_dict(), "kij_c1_plus": k}))

    before = saturation_pressure(spec)
    lo, hi = k_range
    p_lo, p_hi = psat_at(lo), psat_at(hi)
    if not p_lo <= psat_lab <= p_hi:
        raise ValueError(f"cannot reach Psat {psat_lab:.1f} bar: the C1-C7+ interaction coefficient range "
                         f"{lo}..{hi} gives {p_lo:.1f}-{p_hi:.1f} bar; check the composition or lab data")
    mid, p_mid = lo, p_lo
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        p_mid = psat_at(mid)
        if abs(p_mid - psat_lab) / psat_lab < tol:
            break
        if p_mid < psat_lab:
            lo = mid
        else:
            hi = mid
    tuned = FluidSpec(**{**spec.to_dict(), "kij_c1_plus": round(mid, 6)})
    after = saturation_pressure(tuned)
    return tuned, {"parameter": "kij_c1_plus", "before": spec.kij_c1_plus, "after": tuned.kij_c1_plus,
                   "psat_lab": psat_lab, "psat_before": before, "psat_after": after,
                   "psat_error_before": (before - psat_lab) / psat_lab, "psat_error_after": (after - psat_lab) / psat_lab}


# ---------------------------------------------------------------- black-oil tables
def pressure_grid(p_min: float, p_max: float, psat: float, n: int = 12) -> list[float]:
    """Grid from p_min to p_max that contains Psat exactly."""
    below = max(2, round(n * (psat - p_min) / (p_max - p_min))) if psat > p_min else 0
    above = max(2, n - below)
    lo = [p_min + (psat - p_min) * i / below for i in range(below)] if below else []
    hi = [psat + (p_max - psat) * i / above for i in range(above + 1)]
    return [round(p, 4) for p in lo] + [psat] + [round(p, 4) for p in hi[1:]]  # Psat kept exact


def mccain_water(t_c: float, p_bar: float, salinity_ppm: float = 0.0) -> dict:
    """PVTW at a reference pressure from McCain (1990) correlations."""
    tf = t_c * 9 / 5 + 32
    p_psi = p_bar * 14.5038
    dvwt = -1.0001e-2 + 1.33391e-4 * tf + 5.50654e-7 * tf ** 2
    dvwp = -1.95301e-9 * p_psi * tf - 1.72834e-13 * p_psi ** 2 * tf - 3.58922e-7 * p_psi - 2.25341e-10 * p_psi ** 2
    bw = (1 + dvwt) * (1 + dvwp)
    s = salinity_ppm / 1e4  # wt %
    a = 109.574 - 8.40564 * s + 0.313314 * s ** 2 + 8.72213e-3 * s ** 3
    b = -1.12166 + 2.63951e-2 * s - 6.79461e-4 * s ** 2 - 5.47119e-5 * s ** 3 + 1.55586e-6 * s ** 4
    mu1 = a * tf ** b
    mu = mu1 * (0.9994 + 4.0295e-5 * p_psi + 3.1062e-9 * p_psi ** 2)
    cw = 1.0 / (7.033 * p_psi + 541.5 * s - 537.0 * tf + 403300.0) * 14.5038  # 1/bar (Osif)
    rho_sc = 999.0 * (1 + 0.00695 * s)  # approx brine density at SC, kg/m3
    return {"p_ref": p_bar, "bw": bw, "cw": cw, "mu_w": mu, "viscosibility": 0.0, "density_sc": rho_sc}


def generate_black_oil(spec: FluidSpec, p_min: float, p_max: float, *, n: int = 12,
                       stages: list[Stage] | None = None, salinity_ppm: float = 0.0,
                       p_ref_water: float | None = None) -> dict:
    """Generate PVTO, PVDG, PVTW and DENSITY rows in the data server's table shape."""
    stages = stages or DEFAULT_SEPARATOR
    t = spec.temperature_c
    psat = saturation_pressure(spec)
    if not p_min < psat:
        raise ValueError(f"p_min {p_min} must be below Psat {psat:.1f} bar")
    p_max = max(p_max, psat * 1.1)
    grid = pressure_grid(p_min, p_max, psat, n)
    sat_ps = sorted([p for p in grid if p <= psat + 1e-9], reverse=True)   # Psat first, then down
    above_all = [p for p in grid if p > psat + 1e-9]

    s = _flash(build(spec), psat + 0.01, t)   # single-phase at bubble point
    oils = []  # (p_sat_i, oil_system at p_sat_i)
    gases = []  # (p, gas_system at p)
    for i, p in enumerate(sat_ps):
        if i > 0:
            _flash(s, p, t)
            if s.getNumberOfPhases() > 1 and _phase(s, "gas") is not None:
                gases.append((p, s.phaseToSystem("gas")))
                s = s.phaseToSystem("oil")
                _flash(s, p, t)
        oils.append((p, s.clone()))

    pvto = []
    for p_i, oil in oils:
        sep = separator_flash(oil, stages)
        v_sto = sep["sto_volume"]
        rs = sep["gas_volume_sc"] / v_sto
        o = _flash(oil.clone(), p_i, t)
        pvto.append({"rs": rs, "pressure": p_i, "bo": _total_volume(o) / v_sto,
                     "mu_o": _liquid(o).getViscosity("cP"), "is_saturated": 1})
        for p_u in [p for p in grid if p > p_i + 1e-9]:
            u = _flash(oil.clone(), p_u, t)
            pvto.append({"rs": rs, "pressure": p_u, "bo": _total_volume(u) / v_sto,
                         "mu_o": _liquid(u).getViscosity("cP"), "is_saturated": 0})
    pvto.sort(key=lambda r: (r["rs"], r["pressure"]))

    # gas: equilibrium gas below Psat; above Psat compress the gas found just below Psat
    pvdg = []
    def gas_row(gsys, p):
        g = _flash(gsys.clone(), p, t)
        v_res = _total_volume(g)
        sc = _flash(gsys.clone(), P_SC, T_SC)
        v_sc = sc.getPhase(0).getVolume("m3")
        ph = _phase(g, "gas") or g.getPhase(0)
        return {"pressure": p, "bg": v_res / v_sc, "mu_g": ph.getViscosity("cP")}
    for p, gsys in gases:
        pvdg.append(gas_row(gsys, p))
    if gases:
        first_gas = max(gases, key=lambda x: x[0])[1]
        for p in [psat] + above_all:
            pvdg.append(gas_row(first_gas, p))
    pvdg.sort(key=lambda r: r["pressure"])

    st = separator_flash(_flash(build(spec), psat + 0.01, t), stages)
    water = mccain_water(t, p_ref_water or psat, salinity_ppm)
    tables = {
        "pvto": [{k: (round(v, 8) if isinstance(v, float) else v) for k, v in r.items()} for r in pvto],
        "pvdg": [{k: round(v, 8) for k, v in r.items()} for r in pvdg],
        "pvtw": [{k: round(water[k], 10) for k in ("p_ref", "bw", "cw", "mu_w", "viscosibility")}],
        "density": [{"oil": round(st["sto_density"], 4), "water": round(water["density_sc"], 4),
                     "gas": round(st["gas_density_sc"], 6)}],
    }
    summary = {"psat_bar": psat, "rs_at_pb": max(r["rs"] for r in pvto if r["is_saturated"]),
               "bo_at_pb": max((r for r in pvto if r["is_saturated"]), key=lambda r: r["rs"])["bo"],
               "sto_density": st["sto_density"], "pressure_grid": grid,
               "assumptions": ["depletion at reservoir T, oil flashed through separator train for Rs/Bo",
                               "PVDG from equilibrium gas (dry gas; condensate dropped at SC ignored); "
                               "gas above Psat extrapolated by compressing the first liberated gas",
                               f"PVTW from McCain correlations at {salinity_ppm:g} ppm",
                               "units METRIC"]}
    return {"tables": tables, "summary": summary}
