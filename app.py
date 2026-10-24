"""Root-level Render entrypoint; the application lives in ./forge."""
import importlib.util
import sys
from pathlib import Path

FORGE_DIR = Path(__file__).resolve().parent / "forge"
sys.path.insert(0, str(FORGE_DIR))
spec = importlib.util.spec_from_file_location("forge_application", FORGE_DIR / "app.py")
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load Forge application from ./forge/app.py")
forge_application = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = forge_application
spec.loader.exec_module(forge_application)
app = forge_application.app
