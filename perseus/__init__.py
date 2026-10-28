"""Perseus: a safe tool-calling dashboard for open models on a rented GPU box."""

from pathlib import Path

__version__ = "0.1.0"

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".perseus"
LOG_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "perseus.toml"
EXAMPLE_PATH = ROOT / "perseus.example.toml"
RESOLVED_PATH = ROOT / "perseus.resolved.toml"
