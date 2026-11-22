"""Provider defaults and conservative local environment loading."""
from __future__ import annotations

import os
from pathlib import Path

PROVIDERS = {
    "openrouter": {"key": "OPENROUTER_API_KEY", "model": "google/gemini-2.5-flash-lite",
                   "endpoint": "https://openrouter.ai/api/v1/chat/completions", "input_rate": "0.10", "output_rate": "0.40"},
    "xai": {"key": "XAI_API_KEY", "model": "grok-4.3",
            "endpoint": "https://api.x.ai/v1/chat/completions", "input_rate": "1.25", "output_rate": "2.50"},
}
ALLOWED_ENV = {"OPENROUTER_API_KEY", "XAI_API_KEY", "TM_PROVIDER", "TM_MODEL", "TM_MODE",
               "TM_INPUT_USD_PER_MILLION", "TM_OUTPUT_USD_PER_MILLION"}


def load_environment(path=None):
    root = Path(__file__).resolve().parents[1]
    paths = [Path(path)] if path is not None else [root / ".env", root / ".env.txt"]
    if path is not None and not paths[0].is_file():
        raise ValueError("Selected environment file does not exist")
    for source in paths:
        if not source.is_file():
            continue
        for line in source.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().removeprefix("export ")
            key, separator, value = line.partition("=")
            if separator and key.strip() in ALLOWED_ENV:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                os.environ.setdefault(key.strip(), value)


def selected_provider(explicit=None):
    provider = explicit or os.environ.get("TM_PROVIDER")
    if not provider:
        provider = "openrouter" if os.environ.get("OPENROUTER_API_KEY") else "xai" if os.environ.get("XAI_API_KEY") else "openrouter"
    if provider not in PROVIDERS:
        raise ValueError("TM_PROVIDER must be openrouter or xai")
    return provider
