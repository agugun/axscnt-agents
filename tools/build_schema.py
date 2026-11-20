"""Build opmagent/core/schema.sql = schema_base.sql + generated lifecycle triggers."""
import pathlib, shutil, subprocess, sys
root = pathlib.Path(__file__).resolve().parents[1]
base = root / "opmagent/core/schema_base.sql"; out = root / "opmagent/core/schema.sql"
shutil.copy(base, out)
subprocess.check_call([sys.executable, str(root / "tools/gen_triggers.py"), str(out)])
print("wrote", out)
