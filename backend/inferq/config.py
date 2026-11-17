"""Configuration loading shared by the control plane, simulator and tools."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "models.yaml"


@dataclass
class Limits:
    rpm: int
    tpm: int


@dataclass
class Defaults:
    deadline_s: float = 60.0
    max_attempts: int = 4
    guard_fraction: float = 0.01


@dataclass
class SimModelConfig:
    latency_ms: float = 150.0
    jitter_ms: float = 50.0
    tail_prob: float = 0.01
    tail_ms: float = 1000.0
    p_transient: float = 0.0
    p_permanent: float = 0.0
    token_noise: tuple[float, float] = (0.8, 1.05)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "SimModelConfig":
        d = dict(d or {})
        if "token_noise" in d:
            d["token_noise"] = tuple(d["token_noise"])
        return cls(**d)

    def merged(self, overrides: dict[str, Any] | None) -> "SimModelConfig":
        base = dict(self.__dict__)
        for k, v in (overrides or {}).items():
            base[k] = tuple(v) if k == "token_noise" else v
        return SimModelConfig(**base)


@dataclass
class AppConfig:
    models: dict[str, Limits]
    defaults: Defaults = field(default_factory=Defaults)
    simulator_default: SimModelConfig = field(default_factory=SimModelConfig)
    simulator_models: dict[str, SimModelConfig] = field(default_factory=dict)

    def sim_for(self, model: str) -> SimModelConfig:
        return self.simulator_models.get(model, self.simulator_default)


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    path = Path(path or os.environ.get("INFERQ_CONFIG") or DEFAULT_CONFIG)
    raw = yaml.safe_load(path.read_text()) or {}
    models = {
        name: Limits(rpm=int(v["rpm"]), tpm=int(v["tpm"]))
        for name, v in (raw.get("models") or {}).items()
    }
    defaults = Defaults(**(raw.get("defaults") or {}))
    sim_raw = raw.get("simulator") or {}
    sim_default = SimModelConfig.from_dict(sim_raw.get("default"))
    sim_models = {
        k: sim_default.merged(v) for k, v in sim_raw.items() if k != "default"
    }
    return AppConfig(models, defaults, sim_default, sim_models)
