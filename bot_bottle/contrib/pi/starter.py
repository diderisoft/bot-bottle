"""The pi starter's model source (PRD 0091).

pi runs inside the bottle image, so the host needs no pi install. What the
starter needs is a model a bottle can reach, with a key bot-bottle can
forward through the gateway. This module finds that in the host's own pi
config (`~/.pi/agent/`), and builds the starter settings for it, or for
OpenRouter. It only reads; it never changes or mounts the user's pi files.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ...errors import SecretsFileError
from ...paths import host_secrets_path
from ...secret_source import FileSecrets

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_NAME = "OPENROUTER_API_KEY"

# pi's built-in OpenAI-compatible providers: name -> (base URL, key env var).
# A `defaultProvider` naming one of these needs no models.json entry.
BUILTIN_PROVIDERS: dict[str, tuple[str, str]] = {
    "openrouter": (OPENROUTER_BASE_URL, OPENROUTER_KEY_NAME),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
}

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# A starter's context window: the model's own, capped so a 1M-token model
# doesn't make pi keep a huge transcript per request.
MAX_CONTEXT_WINDOW = 131072
DEFAULT_MAX_TOKENS = 8192
# pi keeps `context_window - max_tokens` for input, so cap the output share.
MAX_OUTPUT_TOKENS = 16384


@dataclass(frozen=True)
class LocalPiModel:
    """The default model from the host's pi config, when a bottle can use it.

    ``key_name`` is the env var or secrets-file name that holds the key.
    ``literal_key`` is True when models.json holds the key itself; `setup`
    then asks before copying it into the host secrets file under
    ``key_name``."""

    provider: str
    base_url: str
    api: str
    model: str
    key_name: str
    literal_key: bool = False
    context_window: int = 0
    max_tokens: int = 0


def has_secret(name: str, host_env: Mapping[str, str]) -> bool:
    """Whether ``name`` is set in ``host_env`` or the host secrets file.
    Only presence is checked; the value is never returned."""
    if host_env.get(name):
        return True
    try:
        return bool(FileSecrets(host_secrets_path()).get(name))
    except SecretsFileError:
        return False


def secret_value(name: str, host_env: Mapping[str, str]) -> str:
    """``name`` from ``host_env``, else the host secrets file, else "".
    For `setup`'s own use (probing a model); never printed."""
    if host_env.get(name):
        return host_env[name]
    try:
        return FileSecrets(host_secrets_path()).get(name) or ""
    except SecretsFileError:
        return ""


def pi_agent_dir(host_env: Mapping[str, str]) -> Path:
    home = host_env.get("HOME")
    return (Path(home) if home else Path.home()) / ".pi" / "agent"


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def reachable_from_bottle(base_url: str) -> bool:
    """A public HTTPS endpoint. Localhost, private and link-local addresses
    are the host's (or its LAN's), which a bottle can't reach."""
    url = urlparse(base_url)
    host = url.hostname or ""
    if url.scheme != "https" or not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def read_local_model(host_env: Mapping[str, str]) -> LocalPiModel | None:
    """The host pi config's default provider and model, if a bottle can
    reach them and the key is an env var name or a literal; else None."""
    agent_dir = pi_agent_dir(host_env)
    settings = _read_json(agent_dir / "settings.json")
    provider = settings.get("defaultProvider")
    model = settings.get("defaultModel")
    if not isinstance(provider, str) or not isinstance(model, str) or not model:
        return None
    providers = _read_json(agent_dir / "models.json").get("providers")
    entry = providers.get(provider) if isinstance(providers, dict) else None
    if isinstance(entry, dict):
        base_url, key = entry.get("baseUrl"), entry.get("apiKey")
        api = entry.get("api", "openai-completions")
        model_entry = next((m for m in entry.get("models") or [] if isinstance(m, dict)
                            and m.get("id") == model), {})
    elif provider in BUILTIN_PROVIDERS:
        base_url, key = BUILTIN_PROVIDERS[provider]
        api, model_entry = "openai-completions", {}
    else:
        return None
    if not isinstance(base_url, str) or not reachable_from_bottle(base_url):
        return None
    if api != "openai-completions" or not isinstance(key, str) or not key:
        return None
    literal = not _ENV_NAME.match(key)
    key_name = (f"PI_{re.sub(r'[^A-Z0-9]', '_', provider.upper())}_API_KEY"
                if literal else key)
    return LocalPiModel(
        provider=provider, base_url=base_url, api=api, model=model,
        key_name=key_name, literal_key=literal,
        context_window=_int(model_entry.get("contextWindow")),
        max_tokens=_int(model_entry.get("maxTokens")),
    )


def read_local_literal_key(host_env: Mapping[str, str]) -> str:
    """The literal key in models.json for the default provider. Only `setup`
    calls this, after the user agreed to copy it into the secrets file."""
    agent_dir = pi_agent_dir(host_env)
    provider = _read_json(agent_dir / "settings.json").get("defaultProvider")
    providers = _read_json(agent_dir / "models.json").get("providers")
    entry = (providers.get(provider)
             if isinstance(providers, dict) and isinstance(provider, str) else None)
    key = entry.get("apiKey") if isinstance(entry, dict) else None
    return key if isinstance(key, str) else ""


def starter_settings(provider: str, base_url: str, model: str, key_name: str, *,
                     context_window: int = 0, max_tokens: int = 0,
                     reasoning: bool = False) -> dict[str, object]:
    """`agent_provider.settings` for a pi starter bottle."""
    window = min(context_window or MAX_CONTEXT_WINDOW, MAX_CONTEXT_WINDOW)
    settings: dict[str, object] = {
        "provider": provider,
        "base_url": base_url,
        "api": "openai-completions",
        "api_key_env": key_name,
        "models": [model],
        "context_window": window,
        "max_tokens": min(max_tokens or DEFAULT_MAX_TOKENS, MAX_OUTPUT_TOKENS, window // 4),
    }
    if reasoning:
        settings["supports_reasoning_effort"] = True
    return settings


def local_starter_settings(local: LocalPiModel) -> dict[str, object]:
    return starter_settings(local.provider, local.base_url, local.model, local.key_name,
                            context_window=local.context_window,
                            max_tokens=local.max_tokens)
