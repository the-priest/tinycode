"""Model presets: recommended small local models plus per-family settings.

Pick one with `tinycode --model <key>`, `/model` in the app, or the installer
menu. Any other Ollama tag works too; settings then come from the family it
looks like (or safe generic defaults). Values you set yourself in config.toml
always win over a preset's settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Preset:
    key: str
    tag: str                 # Ollama model tag
    label: str
    family: str
    size: str                # download size (approx.)
    params: str              # total / active parameters
    speed: str               # relative speed on a CPU
    notes: str
    uncensored: bool = False


# Sampling per model family. Sources: the model cards / official recipes.
#  - Ling-3.0: top_p 0.95, top_k 20, no repetition penalty (penalties hurt code,
#    where identifiers repeat); 0.6 is what inclusionAI uses for SWE-bench runs.
#    "<|role_end|>" is the end token, which the model sometimes spells out.
#  - Qwen3.5 (thinking mode): temperature 0.6, top_p 0.95, top_k 20.
#  - LFM2.5: low temperature + min_p for tool use, light repetition penalty.
FAMILIES: dict[str, dict] = {
    "ling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "repeat_penalty": 1.0,
             "stop": ["<|role_end|>"]},
    "qwen": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "repeat_penalty": 1.0},
    "lfm": {"temperature": 0.3, "top_p": 0.95, "top_k": 50, "repeat_penalty": 1.05,
            "min_p": 0.15},
    "generic": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "repeat_penalty": 1.0},
}

PRESETS: list[Preset] = [
    Preset("ling", "hf.co/bloomer010/Ling-3.0-tiny-GGUF:Q4_K_XL", "Ling-3.0-tiny", "ling",
           "5.3 GB", "7.9B / 1.3B active (MoE)", "fastest",
           "best tool calling and instructions at this speed; weaker at complex logic"),
    Preset("ling-official", "hf.co/inclusionAI/Ling-3.0-tiny-GGUF:Q4_K_M",
           "Ling-3.0-tiny (official)", "ling", "~5 GB", "7.9B / 1.3B active (MoE)", "fastest",
           "inclusionAI's own GGUF (tool calling verified on Ollama by inclusionAI)"),
    Preset("qwen4b", "qwen3.5:4b", "Qwen3.5-4B", "qwen", "~3.4 GB", "4B dense", "~3x slower",
           "noticeably better at code (LiveCodeBench 55.8); good middle ground"),
    Preset("qwen9b", "qwen3.5:9b", "Qwen3.5-9B", "qwen", "~6.6 GB", "9B dense", "~6x slower",
           "best coder under 10B (LiveCodeBench 65.6, BFCL-v4 66.1); needs patience on CPU"),
    Preset("lfm", "hf.co/LiquidAI/LFM2.5-8B-A1B-GGUF:Q4_K_M", "LFM2.5-8B-A1B", "lfm",
           "~5 GB", "8.3B / 1.5B active (MoE)", "fastest",
           "built for on-device tool calling; much weaker at coding than Ling"),
    Preset("ling-uncensored", "hf.co/mradermacher/Ling-3.0-tiny-uncensored-abliterated-GGUF:Q4_K_M",
           "Ling-3.0-tiny (uncensored)", "ling", "~5 GB", "7.9B / 1.3B active (MoE)", "fastest",
           "community abliterated build: refusals removed", uncensored=True),
    Preset("qwen4b-uncensored", "huihui_ai/qwen3.5-abliterated:4b", "Qwen3.5-4B (uncensored)",
           "qwen", "~3.4 GB", "4B dense", "~3x slower",
           "huihui_ai abliterated build: refusals removed", uncensored=True),
    Preset("qwen9b-uncensored", "huihui_ai/qwen3.5-abliterated:9b", "Qwen3.5-9B (uncensored)",
           "qwen", "~6.6 GB", "9B dense", "~6x slower",
           "huihui_ai abliterated build: refusals removed", uncensored=True),
]
BY_KEY = {p.key: p for p in PRESETS}
DEFAULT = BY_KEY["ling"]


def find(name: str) -> Optional[Preset]:
    """A preset by key or by exact Ollama tag."""
    if not name:
        return None
    n = name.strip()
    return BY_KEY.get(n.lower()) or next((p for p in PRESETS if p.tag == n), None)


def family_of(tag: str) -> str:
    t = (tag or "").lower()
    p = find(tag)
    if p:
        return p.family
    if "ling" in t or "ring-" in t or "bailing" in t:
        return "ling"
    if "qwen" in t:
        return "qwen"
    if "lfm" in t or "liquid" in t:
        return "lfm"
    return "generic"


def label_for(tag: str) -> str:
    p = find(tag)
    if p:
        return p.label
    base = tag.split("/")[-1]
    return base.replace("-GGUF", "").replace("-gguf", "")


# uncensored twin of each preset, both ways
_TWINS = {"ling": "ling-uncensored", "ling-official": "ling-uncensored",
          "qwen4b": "qwen4b-uncensored", "qwen9b": "qwen9b-uncensored"}


def counterpart(tag: str) -> Optional[Preset]:
    """The uncensored build of a preset, or the regular build of an uncensored
    one. None when there is no twin (e.g. a custom tag)."""
    p = find(tag)
    if not p:
        return None
    if p.uncensored:
        key = next((k for k, v in _TWINS.items() if v == p.key), None)
    else:
        key = _TWINS.get(p.key)
    return BY_KEY.get(key) if key else None


def is_uncensored(tag: str) -> bool:
    p = find(tag)
    if p:
        return p.uncensored
    t = (tag or "").lower()
    return any(w in t for w in ("abliterat", "uncensor", "heretic"))


SAMPLING_KEYS = ("temperature", "top_p", "top_k", "repeat_penalty")


def apply(cfg, explicit: set[str] = frozenset()) -> None:
    """Resolve a preset key in cfg.model and apply the family's settings to
    every sampling field the user did not set explicitly."""
    p = find(cfg.model)
    if p:
        cfg.model = p.tag
        if "model_label" not in explicit:
            cfg.model_label = p.label
    elif "model_label" not in explicit:
        cfg.model_label = label_for(cfg.model)
    fam = FAMILIES[family_of(cfg.model)]
    for k in SAMPLING_KEYS:
        if k not in explicit and k in fam:
            setattr(cfg, k, fam[k])
    cfg.extra["stop"] = list(fam.get("stop", []))
    cfg.extra["min_p"] = fam.get("min_p")
    cfg.extra["family"] = family_of(cfg.model)


def table(installed: set[str] = frozenset()) -> str:
    rows = []
    for p in PRESETS:
        mark = "✓" if p.tag in installed else " "
        rows.append(f" {mark} {p.key:<18} {p.label:<28} {p.size:<9} {p.speed:<11} "
                    f"{p.notes}")
    return "\n".join(rows)
