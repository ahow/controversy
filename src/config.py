"""Shared config loading: .env keys + YAML registers."""
import os
import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def params():
    return yaml.safe_load((ROOT / "config" / "parameters.yaml").read_text())


def taxonomy():
    return yaml.safe_load((ROOT / "config" / "taxonomy.yaml").read_text())["issues"]


def themes():
    return yaml.safe_load((ROOT / "config" / "themes.yaml").read_text())
