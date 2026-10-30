"""Perseus: a safe tool-calling dashboard for open models on a rented GPU box."""

from pathlib import Path

__version__ = "0.1.0"

ROOT = Path(__file__).resolve().parent.parent          # the git checkout: code and config only
CONFIG_TEMPLATES = ROOT / "Config" / "templates"   # provider, settings and tooling templates
