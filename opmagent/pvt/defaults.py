"""Built-in default black oil (used by hello and as a starting point)."""
from .engine import FluidSpec, Stage

DEFAULT_COMPOSITION = {  # mol %
    "nitrogen": 0.5, "CO2": 1.0, "methane": 30.0, "ethane": 7.0, "propane": 6.0,
    "i-butane": 1.0, "n-butane": 3.0, "i-pentane": 1.5, "n-pentane": 1.5, "n-hexane": 2.5,
}


def default_spec() -> FluidSpec:
    return FluidSpec(composition=dict(DEFAULT_COMPOSITION), plus_name="C7", plus_mol=46.0,
                     plus_mw=0.220, plus_density=0.86, temperature_c=100.0,
                     eos="PR", volume_shift=True, pseudo_components=6)


DEFAULT_STAGES = [Stage(30.0, 40.0), Stage(1.01325, 15.56)]
