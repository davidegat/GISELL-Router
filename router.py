from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

try:                                              
    import orjson

    def _json_dumps(data: Any) -> bytes:
        return orjson.dumps(data)

    _HAS_ORJSON = True
except ModuleNotFoundError:                    
    def _json_dumps(data: Any) -> bytes:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    _HAS_ORJSON = False


class FastJSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return _json_dumps(content)


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
SECRETS_PATH = APP_DIR / "secrets.json"
APP_NAME = "GISELL Router"
APP_VERSION = "0.3.0"
APP_AUTHOR = "Davide (gat)"
APP_LICENSE = "CC BY-NC 4.0"
SERVICE_NAME = "gisell-router.service"

LANG_IT_PATH = APP_DIR / "lang_it.json"
LANG_EN_PATH = APP_DIR / "lang_en.json"


def _load_language_pack(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read language file {path.name}: {exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise RuntimeError(f"Invalid language file: {path.name}")
    return data


LANGUAGE_PACKS = {
    "it": _load_language_pack(LANG_IT_PATH),
    "en": _load_language_pack(LANG_EN_PATH),
}
_REQUEST_LANGUAGE: ContextVar[str] = ContextVar("gisell_request_language", default="it")


def _normalize_language(value: str | None) -> str:
    raw = (value or "").lower()
    return "en" if raw.startswith("en") else "it"


def text(key: str, /, **params: Any) -> str:
    lang = _REQUEST_LANGUAGE.get()
    template = LANGUAGE_PACKS.get(lang, LANGUAGE_PACKS["it"]).get(key, LANGUAGE_PACKS["it"].get(key, key))
    try:
        return template.format(**params)
    except (KeyError, ValueError):
        return template


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "settings": {
        "host": "127.0.0.1",
        "port": 8765,
        "request_timeout_s": 90,
        "first_token_timeout_s": 45,
        "connect_timeout_s": 10,
        "cooldown_s": 30,
        "context_messages_on_failover": 10,
                                                                                     
                                                                          
        "failover_on_explicit_model": True,
                                                                        
        "expose_virtual_router_model": True,
                                                                             
                                                                        
        "override_client_model": False,
    },
    "providers": [],
    "routes": [],
    "active_route_id": None,
}

PRESETS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "label": "OpenRouter",
        "label_key": "provider.template.openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "needs_key": True,
    },
    "groq": {
        "label": "Groq",
        "label_key": "provider.template.groq",
        "base_url": "https://api.groq.com/openai/v1",
        "needs_key": True,
    },
    "openai": {
        "label": "OpenAI",
        "label_key": "provider.template.openai",
        "base_url": "https://api.openai.com/v1",
        "needs_key": True,
    },
    "google_ai_studio": {
        "label": "Google AI Studio",
        "label_key": "provider.template.google",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "needs_key": True,
        "note_key": "provider.google_note",
    },
    "mistral": {
        "label": "Mistral",
        "label_key": "provider.template.mistral",
        "base_url": "https://api.mistral.ai/v1",
        "needs_key": True,
    },
    "together": {
        "label": "Together",
        "label_key": "provider.template.together",
        "base_url": "https://api.together.ai/v1",
        "needs_key": True,
    },
    "deepinfra": {
        "label": "DeepInfra",
        "label_key": "provider.template.deepinfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "needs_key": True,
    },
    "fireworks": {
        "label": "Fireworks AI",
        "label_key": "provider.template.fireworks",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "needs_key": True,
    },
    "cerebras": {
        "label": "Cerebras",
        "label_key": "provider.template.cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "needs_key": True,
    },
    "ollama": {
        "label": "Ollama",
        "label_key": "provider.template.ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "needs_key": False,
    },
    "codex": {
        "label": "Codex / ChatGPT",
        "label_key": "provider.template.codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "needs_key": False,
        "auth_mode": "codex_oauth",
        "wire": "codex",
        "note_key": "provider.codex_note",
    },
    "custom": {
        "label": "custom",
        "label_key": "provider.template.custom",
        "base_url": "",
        "needs_key": False,
    },
}

VIRTUAL_MODEL_ID = "router"
VIRTUAL_MODEL_ALIASES = {"router", "auto", "default", "local-llm-router"}

                                                                            
                   
 
                                                                         
                                                                            
                                                                            
                                                                              
                              
 
                                                                            
                                                                             
                                                       
                                                                            

PRESET_KIND = "preset"


def is_preset(entry: dict[str, Any] | None) -> bool:
                                                                         
                                      
    return bool(entry) and str(entry.get("kind", "")) in {PRESET_KIND, "group"}


def _clean_members(raw: Any, known: set[str], owner_id: str) -> list[str]:
    """Tiene solo riferimenti esistenti, senza auto-riferimenti ne' duplicati."""
    out: list[str] = []
    if isinstance(raw, list):
        for mid in raw:
            if isinstance(mid, str) and mid in known and mid != owner_id and mid not in out:
                out.append(mid)
    return out


def _atomic_json_write(path: Path, data: dict[str, Any], private: bool = False) -> None:
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600 if private else 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        _atomic_json_write(path, default, private=path == SECRETS_PATH)
        return json.loads(json.dumps(default))
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
    except (json.JSONDecodeError, OSError):
        pass
    return json.loads(json.dumps(default))


class Store:
    def __init__(self) -> None:
        self.save_lock = asyncio.Lock()
        self.config = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
        self.secrets = _load_json(SECRETS_PATH, {"api_keys": {}})
        self.health: dict[str, dict[str, Any]] = {}
                                                                             
        self.inflight: dict[str, dict[str, Any]] = {}
        self.stats: dict[str, dict[str, Any]] = {}
        self.last_used: dict[str, Any] | None = None
        self.started_at = time.time()
                                                                                           
                                                                        
        self.logs: list[dict[str, Any]] = []
        self._log_seq = 0
        self._req_seq = 0
        self.client: httpx.AsyncClient | None = None
        self._models_cache: tuple[int, list[dict[str, Any]]] | None = None
        self.active_member_by_preset: dict[str, str] = {}
        self._normalize()

    def _normalize(self) -> None:
        settings = self.config.setdefault("settings", {})
        for key, value in DEFAULT_CONFIG["settings"].items():
            settings.setdefault(key, value)
        self.config.setdefault("providers", [])
        self.config.setdefault("routes", [])
        self.config.setdefault("active_route_id", None)
        self.secrets.setdefault("api_keys", {})
        self.secrets.setdefault("router_api_key", "")

                                                                          
        provider_ids = {p.get("id") for p in self.config["providers"]}
        self.config["routes"] = [
            r for r in self.config["routes"]
            if is_preset(r) or r.get("provider_id") in provider_ids
        ]
                                                                             
                                                                         
        known = {r.get("id") for r in self.config["routes"] if r.get("id")}
        for entry in self.config["routes"]:
            if is_preset(entry):
                entry["members"] = _clean_members(entry.get("members"), known, entry["id"])
                entry.setdefault("label", "Preset")
                entry.setdefault("enabled", True)
                                                                         
                                                                                            
        self.config["active_route_id"] = self.config["routes"][0]["id"] if self.config["routes"] else None

    def invalidate_models(self) -> None:
        self._models_cache = None

    async def save_config(self) -> None:
        self.invalidate_models()
        async with self.save_lock:
            await asyncio.to_thread(_atomic_json_write, CONFIG_PATH, self.config, False)

    async def save_secrets(self) -> None:
        async with self.save_lock:
            await asyncio.to_thread(_atomic_json_write, SECRETS_PATH, self.secrets, True)

    def provider(self, provider_id: str) -> dict[str, Any] | None:
        return next((p for p in self.config["providers"] if p["id"] == provider_id), None)

    def route(self, route_id: str) -> dict[str, Any] | None:
        return next((r for r in self.config["routes"] if r["id"] == route_id), None)

    def api_key(self, provider_id: str) -> str:
        return str(self.secrets.get("api_keys", {}).get(provider_id, ""))

    def route_health(self, route_id: str) -> dict[str, Any]:
        return self.health.setdefault(
            route_id,
            {
                "status": "unknown",
                "last_success": None,
                "last_failure": None,
                "last_error": None,
                "latency_ms": None,
                "cooldown_until": 0.0,
            },
        )

    def route_stats(self, route_id: str) -> dict[str, Any]:
        return self.stats.setdefault(
            route_id,
            {"requests": 0, "ok": 0, "fail": 0, "last_used": None, "total_latency_ms": 0.0},
        )

    def next_request_id(self) -> str:
        self._req_seq += 1
        return f"req{self._req_seq:06d}"

    def http(self) -> httpx.AsyncClient:
        if self.client is None:                                        
            self.client = _build_client()
        return self.client


def _build_client() -> httpx.AsyncClient:
    settings = store.config["settings"] if "store" in globals() else DEFAULT_CONFIG["settings"]
    connect = float(settings.get("connect_timeout_s", 10))
    total = float(settings.get("request_timeout_s", 90))
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(total, connect=connect, pool=connect),
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=40,
            keepalive_expiry=120.0,
        ),
        headers={"Accept-Encoding": "gzip, deflate"},
    )


store = Store()


def append_session_log(
    level: str,
    message_key: str,
    *,
    kind: str = "system",
    route_id: str | None = None,
    request_id: str | None = None,
    message_params: dict[str, Any] | None = None,
    detail: str | None = None,
    detail_key: str | None = None,
    detail_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a language-neutral event to the in-memory session log."""
    store._log_seq += 1
    route = store.route(route_id) if route_id else None
    provider = store.provider(route["provider_id"]) if route else None
    entry = {
        "id": store._log_seq,
        "at": time.time(),
        "level": str(level),
        "kind": str(kind),
        "message_key": str(message_key),
        "message_params": message_params or {},
        "detail": str(detail) if detail else None,
        "detail_key": str(detail_key) if detail_key else None,
        "detail_params": detail_params or {},
        "request_id": request_id,
        "route_id": route_id,
        "label": (route.get("label") or route.get("model")) if route else None,
        "model": route.get("model") if route else None,
        "provider": provider.get("name") if provider else None,
    }
    store.logs.append(entry)
    return entry


append_session_log("info", "log.router_started", kind="system")


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.client = _build_client()
    try:
        yield
    finally:
        client, store.client = store.client, None
        if client is not None:
            await client.aclose()


app = FastAPI(
    title="Local LLM Router",
    version=APP_VERSION,
    lifespan=lifespan,
    default_response_class=FastJSONResponse,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Router-Route", "X-Router-Model", "X-Router-Provider"],
)

@app.middleware("http")
async def language_context_middleware(request: Request, call_next):
    token = _REQUEST_LANGUAGE.set(_normalize_language(request.headers.get("Accept-Language")))
    try:
        return await call_next(request)
    finally:
        _REQUEST_LANGUAGE.reset(token)



class ProviderCreate(BaseModel):
    preset: str = "custom"
    name: str = Field(min_length=1, max_length=100)
    base_url: str = ""
    api_key: str = ""
    auth_path: str = ""


class ProviderPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None


class ModelCreate(BaseModel):
    model: str = Field(min_length=1)
    label: str = ""


class RouteMove(BaseModel):
    direction: int


class RoutePatch(BaseModel):
    enabled: bool | None = None
    label: str | None = None
    model: str | None = None
    provider_id: str | None = None


class PresetCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    members: list[str] = Field(default_factory=list)


class PresetPatch(BaseModel):
    label: str | None = None
    members: list[str] | None = None
    enabled: bool | None = None


                                                                            
                                
                                                                            

_SLUG_RE = re.compile(r"[^A-Za-z0-9._:@/-]+")


def _slugify_model_id(raw: str) -> str:
    slug = _SLUG_RE.sub("-", raw.strip()).strip("-")
    return slug or "model"


def exposed_models() -> list[dict[str, Any]]:
    """Un id stabile e univoco per ogni route abilitata.

    L'id preferito e' l'etichetta scelta dall'utente; se due route collidono si
    aggiunge un suffisso derivato dall'id interno, cosi resta deterministico tra
    un riavvio e l'altro.
    """
    key = hash(
        tuple(
            (
                r["id"], r.get("provider_id"), r.get("label"), r.get("model"),
                bool(r.get("enabled", True)), r.get("kind"), tuple(r.get("members") or ()),
            )
            for r in store.config["routes"]
        )
        + tuple((p["id"], bool(p.get("enabled", True)), p.get("name")) for p in store.config["providers"])
    )
    cached = store._models_cache
    if cached is not None and cached[0] == key:
        return cached[1]

    entries: list[dict[str, Any]] = []
    taken: set[str] = set()
    for route in store.config["routes"]:
        if is_preset(route):
            members = expand_chain([route])
            candidate = _slugify_model_id(str(route.get("label") or "preset"))
            if candidate.lower() in taken or candidate.lower() in VIRTUAL_MODEL_ALIASES:
                candidate = f"{candidate}-{route['id'][:6]}"
            taken.add(candidate.lower())
            entries.append(
                {
                    "exposed_id": candidate,
                    "route_id": route["id"],
                    "kind": PRESET_KIND,
                    "model": "",
                    "members": [r["id"] for r, _ in members],
                    "provider_name": "Preset",
                    "provider_id": "",
                                                                               
                                                                        
                    "enabled": bool(route.get("enabled", True) and members),
                }
            )
            continue

        provider = store.provider(route["provider_id"])
        if provider is None:
            continue
        candidate = _slugify_model_id(str(route.get("label") or route.get("model") or ""))
        if candidate.lower() in taken or candidate.lower() in VIRTUAL_MODEL_ALIASES:
            candidate = f"{candidate}-{route['id'][:6]}"
        taken.add(candidate.lower())
        entries.append(
            {
                "exposed_id": candidate,
                "route_id": route["id"],
                "kind": "model",
                "model": route["model"],
                "provider_name": provider.get("name", ""),
                "provider_id": provider["id"],
                "enabled": bool(route.get("enabled", True) and provider.get("enabled", True)),
            }
        )

    store._models_cache = (key, entries)
    return entries


def models_payload() -> dict[str, Any]:
    created = int(time.time())
    data: list[dict[str, Any]] = []
    if bool(store.config["settings"].get("expose_virtual_router_model", True)):
        data.append(
            {
                "id": VIRTUAL_MODEL_ID,
                "object": "model",
                "created": created,
                "owned_by": "local-llm-router",
                "description": text("api.model_router_description"),
            }
        )
    for entry in exposed_models():
        if not entry["enabled"]:
            continue
        if entry.get("kind") == PRESET_KIND:
            n = len(entry.get("members") or [])
            data.append(
                {
                    "id": entry["exposed_id"],
                    "object": "model",
                    "created": created,
                    "owned_by": "local-llm-router",
                    "description": text("api.preset_description", count=n),
                    "root": entry["exposed_id"],
                    "parent": None,
                    "permission": [],
                }
            )
            continue
        data.append(
            {
                "id": entry["exposed_id"],
                "object": "model",
                "created": created,
                "owned_by": entry["provider_name"] or "local-llm-router",
                "root": entry["model"],
                "parent": None,
                "permission": [],
            }
        )
    return {"object": "list", "data": data}


def resolve_requested_route(requested: Any) -> str | None:
    """Mappa il campo model della richiesta su una route. None = failover normale."""
    if bool(store.config["settings"].get("override_client_model", False)):
        return None
    if not isinstance(requested, str):
        return None
    wanted = requested.strip()
    if not wanted or wanted.lower() in VIRTUAL_MODEL_ALIASES:
        return None

    lowered = wanted.lower()
    entries = exposed_models()
    for entry in entries:
        if entry["exposed_id"].lower() == lowered:
            return entry["route_id"]
    for entry in entries:
        if entry["route_id"].lower() == lowered:
            return entry["route_id"]
    for route in store.config["routes"]:
        if str(route.get("label", "")).strip().lower() == lowered:
            return route["id"]
    for route in store.config["routes"]:
        if str(route.get("model", "")).strip().lower() == lowered:
            return route["id"]
    return None


                                                                            
                         
                                                                            


def public_state() -> dict[str, Any]:
    exposed_by_route = {e["route_id"]: e["exposed_id"] for e in exposed_models()}
    providers = []
    for provider in store.config["providers"]:
        p = dict(provider)
        p["has_api_key"] = bool(store.api_key(p["id"]))
        p["auth_mode"] = provider_auth_mode(provider)
        p["wire"] = provider_wire(provider)
        providers.append(p)

    routes = []
    for route in store.config["routes"]:
        r = dict(route)
        r["exposed_id"] = exposed_by_route.get(route["id"], route.get("label") or route.get("model") or "")
        if is_preset(route):
            r["kind"] = PRESET_KIND
            r["members"] = list(route.get("members") or [])
            r["member_count"] = len(expand_chain([route]))
        routes.append(r)

    settings = store.config["settings"]
    return {
        "settings": settings,
        "providers": providers,
        "routes": routes,
        "active_route_id": store.config.get("active_route_id"),
        "health": store.health,
        "presets": PRESETS,
        "local_api": f"http://{settings['host']}:{settings['port']}/v1",
        "router_api_key_enabled": bool(router_api_key()),
        "app": {"name": APP_NAME, "version": APP_VERSION, "author": APP_AUTHOR, "license": APP_LICENSE},
    }


def _expand_into(
    entries: list[dict[str, Any] | None],
    by_id: dict[str, dict[str, Any]],
    only_enabled: bool,
    seen: set[str],
    out: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    for entry in entries:
        if entry is None:
            continue
        if only_enabled and not entry.get("enabled", True):
            continue
        if is_preset(entry):
            pid = entry["id"]
            if pid in seen:                                                 
                continue
            seen.add(pid)
            _expand_into(
                [by_id.get(m) for m in entry.get("members") or []],
                by_id, only_enabled, seen, out,
            )
            seen.discard(pid)                                    
            continue
        provider = store.provider(entry.get("provider_id", ""))
        if provider is None or (only_enabled and not provider.get("enabled", True)):
            continue
        out.append((entry, provider))


def expand_chain(
    entries: list[dict[str, Any] | None], only_enabled: bool = True
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Voci miste (modelli + preset) -> lista piatta di route concrete, in ordine."""
    by_id = {r["id"]: r for r in store.config["routes"]}
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    _expand_into(entries, by_id, only_enabled, set(), out)
    return out


def _expand_global_items(
    items: list[dict[str, Any]],
    resume_preset_id: str | None = None,
    resume_member_id: str | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    out: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for item in items:
        if not item.get("enabled", True):
            continue
        if is_preset(item):
            members = expand_chain([item])
            if item["id"] == resume_preset_id and resume_member_id:
                member_idx = next(
                    (i for i, (route, _) in enumerate(members) if route["id"] == resume_member_id),
                    None,
                )
                if member_idx is not None:
                    members = members[member_idx:]
            for route, provider in members:
                out.append((route, provider, item["id"]))
            continue
        provider = store.provider(item.get("provider_id", ""))
        if provider is not None and provider.get("enabled", True):
            out.append((item, provider, item["id"]))
    return out


def enabled_route_sequence(
    preferred_route_id: str | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    routes = store.config["routes"]
    if not routes:
        return []

    if preferred_route_id:
        preferred = store.route(preferred_route_id)
        pinned = _expand_global_items([preferred]) if preferred else []
        if pinned and not bool(store.config["settings"].get("failover_on_explicit_model", True)):
            return pinned
        idx = next((i for i, r in enumerate(routes) if r["id"] == preferred_route_id), None)
        rest_items = routes if idx is None else routes[idx + 1:] + routes[:idx]
        rest = _expand_global_items(rest_items)
        now = time.time()
        ready_rest = [
            attempt for attempt in rest
            if store.route_health(attempt[0]["id"]).get("cooldown_until", 0) <= now
        ]
        return pinned + (ready_rest or rest)

    active_id = store.config.get("active_route_id")
    idx = next((i for i, r in enumerate(routes) if r["id"] == active_id), 0)
    ordered_items = routes[idx:] + routes[:idx]
    active = store.route(active_id) if active_id else None
    resume_member = (
        store.active_member_by_preset.get(active_id)
        if active_id and is_preset(active)
        else None
    )
    ordered = _expand_global_items(
        ordered_items,
        resume_preset_id=active_id if is_preset(active) else None,
        resume_member_id=resume_member,
    )
    if not ordered:
        return []

    now = time.time()
    ready = [
        attempt for attempt in ordered
        if store.route_health(attempt[0]["id"]).get("cooldown_until", 0) <= now
    ]
    return ready or ordered


def remember_success(
    active_item_id: str,
    concrete_route_id: str,
    preferred_route_id: str | None,
) -> None:
    if preferred_route_id:
        return
    item = store.route(active_item_id)
    if item is None:
        return
    store.config["active_route_id"] = active_item_id
    if is_preset(item):
        store.active_member_by_preset[active_item_id] = concrete_route_id

def compact_messages(messages: Any, limit: int) -> Any:
    """Preserva system/developer e limita il resto al contesto recente.

    Evita di troncare nel mezzo di un blocco tool: se il primo elemento scelto e'
    role=tool, retrocede fino all'assistant che ha aperto le tool_calls.
    """
    if not isinstance(messages, list) or limit <= 0:
        return messages

    persistent = [m for m in messages if isinstance(m, dict) and m.get("role") in {"system", "developer"}]
    conversational = [m for m in messages if not (isinstance(m, dict) and m.get("role") in {"system", "developer"})]
    if len(conversational) <= limit:
        return messages

    start = len(conversational) - limit
                                                                    
    while start > 0 and isinstance(conversational[start], dict) and conversational[start].get("role") == "tool":
        start -= 1
    return persistent + conversational[start:]


def prepare_body(original: dict[str, Any], model: str, endpoint: str, is_fallback: bool) -> dict[str, Any]:
                                                                               
                                                                      
    body = dict(original)
    body["model"] = model
    if is_fallback:
        limit = int(store.config["settings"].get("context_messages_on_failover", 10))
        if endpoint == "chat/completions" and isinstance(body.get("messages"), list):
            body["messages"] = compact_messages(body["messages"], limit)
        elif endpoint == "responses" and isinstance(body.get("input"), list):
            body["input"] = body["input"][-limit:] if limit > 0 else body["input"]
    return body


                                                                            
                       
 
                                                                                 
                                                                                 
                                                                                
                                                         
                                                                            

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_INSTRUCTIONS = (
    "You are Codex, based on GPT-5. You are running as a coding agent in the Codex CLI "
    "on a user's computer."
)
                                                                                 
                             
CODEX_REFRESH_MARGIN_S = 300.0
                                                                                
                                                                           
CODEX_KNOWN_MODELS = ("gpt-5.2", "gpt-5.2-codex", "gpt-5.3-codex", "gpt-5.4")


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _jwt_claims(token: str) -> dict[str, Any]:
    """Legge il payload di un JWT senza verificarne la firma (serve solo exp/account)."""
    try:
        part = token.split(".")[1]
        padded = part + "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class CodexAuthError(RuntimeError):
    pass


class CodexAuth:
    """Carica, aggiorna e riscrive i token OAuth di Codex."""

    _instances: dict[str, CodexAuth] = {}

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self._raw: dict[str, Any] = {}
        self._access: str = ""
        self._refresh: str = ""
        self._account: str = ""
        self._exp: float = 0.0
        self._mtime: float | None = None
        self.last_error: str | None = None
        self.last_refresh: float | None = None

    @classmethod
    def for_path(cls, path: Path) -> CodexAuth:
        key = str(path)
        inst = cls._instances.get(key)
        if inst is None:
            inst = cls(path)
            cls._instances[key] = inst
        return inst

    def _load_from_disk(self) -> None:
        if not self.path.exists():
            raise CodexAuthError(
                text("error.codex_auth_missing", path=self.path)
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CodexAuthError(text("error.codex_auth_read", path=self.path, error=exc)) from exc
        tokens = raw.get("tokens") if isinstance(raw, dict) else None
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            raise CodexAuthError(
                text("error.codex_access_token_missing")
            )
        self._raw = raw
        self._access = str(tokens.get("access_token") or "")
        self._refresh = str(tokens.get("refresh_token") or "")
        claims = _jwt_claims(self._access)
        self._exp = float(claims.get("exp") or 0)
        account = tokens.get("account_id") or ""
        if not account:
            auth_claim = claims.get("https://api.openai.com/auth")
            if isinstance(auth_claim, dict):
                account = auth_claim.get("chatgpt_account_id") or ""
        self._account = str(account or "")
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None

    def _file_changed(self) -> bool:
        try:
            return self.path.stat().st_mtime != self._mtime
        except OSError:
            return True

    async def _refresh_token(self, client: httpx.AsyncClient) -> None:
        if not self._refresh:
            raise CodexAuthError(text("error.codex_refresh_missing"))
        payload = {
            "client_id": CODEX_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh,
            "scope": "openid profile email",
        }
        try:
            resp = await client.post(CODEX_TOKEN_URL, json=payload, timeout=30.0)
        except httpx.HTTPError as exc:
            raise CodexAuthError(text("error.codex_refresh_failed", detail=exc)) from exc
        if not resp.is_success:
            raise CodexAuthError(text("error.codex_refresh_failed", detail=f"HTTP {resp.status_code}"))
        try:
            data = resp.json()
        except ValueError as exc:
            raise CodexAuthError(text("error.codex_token_non_json")) from exc

        access = data.get("access_token")
        if not access:
            raise CodexAuthError(text("error.codex_token_missing"))
        self._access = str(access)
        if data.get("refresh_token"):
            self._refresh = str(data["refresh_token"])
        claims = _jwt_claims(self._access)
        self._exp = float(claims.get("exp") or 0)
        if not self._account:
            auth_claim = claims.get("https://api.openai.com/auth")
            if isinstance(auth_claim, dict):
                self._account = str(auth_claim.get("chatgpt_account_id") or "")

                                                                              
                                                                    
        merged = dict(self._raw)
        tokens = dict(merged.get("tokens") or {})
        tokens["access_token"] = self._access
        tokens["refresh_token"] = self._refresh
        if self._account:
            tokens["account_id"] = self._account
        merged["tokens"] = tokens
        merged["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._raw = merged
        try:
            await asyncio.to_thread(_atomic_json_write, self.path, merged, True)
            self._mtime = self.path.stat().st_mtime
        except OSError:
                                                                                 
            pass
        self.last_refresh = time.time()

    async def credentials(self, client: httpx.AsyncClient) -> tuple[str, str]:
        async with self.lock:
            try:
                if not self._access or self._file_changed():
                    self._load_from_disk()
                if self._exp and time.time() >= self._exp - CODEX_REFRESH_MARGIN_S:
                    await self._refresh_token(client)
                self.last_error = None
                return self._access, self._account
            except CodexAuthError as exc:
                self.last_error = str(exc)
                raise

    def status(self) -> dict[str, Any]:
        expires_in = round(self._exp - time.time()) if self._exp else None
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "has_token": bool(self._access),
            "account_id": self._account[:8] + "…" if self._account else None,
            "expires_in_s": expires_in,
            "last_refresh": self.last_refresh,
            "last_error": self.last_error,
        }


def codex_auth_for(provider: dict[str, Any]) -> CodexAuth:
    custom = provider.get("auth_path")
    path = Path(custom).expanduser() if custom else codex_home() / "auth.json"
    return CodexAuth.for_path(path)


def provider_wire(provider: dict[str, Any]) -> str:
    wire = provider.get("wire")
    if wire:
        return str(wire)
    preset = PRESETS.get(str(provider.get("preset", "")), {})
    return str(preset.get("wire", "openai"))


def provider_auth_mode(provider: dict[str, Any]) -> str:
    mode = provider.get("auth_mode")
    if mode:
        return str(mode)
    preset = PRESETS.get(str(provider.get("preset", "")), {})
    return str(preset.get("auth_mode", "api_key"))


                                                                            
                                                                          
                                                                            


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in {"text", "input_text", "output_text"} and block.get("text"):
                    parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return "" if content is None else str(content)


def chat_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    """Converte un body Chat Completions nel formato Responses accettato da Codex."""
    extra_instructions: list[str] = []
    items: list[dict[str, Any]] = []

    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in {"system", "developer"}:
                                                                              
                                                                       
            extra_instructions.append(_text_of(message.get("content")))
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": _text_of(message.get("content")),
                }
            )
            continue
        if role == "assistant":
            text = _text_of(message.get("content"))
            if text:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": text}]})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "name": str(fn.get("name") or ""),
                        "arguments": str(fn.get("arguments") or "{}"),
                        "call_id": str(call.get("id") or ""),
                    }
                )
            continue
        items.append({"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": _text_of(message.get("content"))}]})

    if extra_instructions:
        joined = "\n\n".join(x for x in extra_instructions if x)
        if joined:
            items.insert(0, {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": joined}]})

    out: dict[str, Any] = {
        "model": body.get("model"),
        "instructions": CODEX_INSTRUCTIONS,
        "input": items,
        "store": False,
        "stream": True,
    }

    tools = []
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "function":
            fn = tool.get("function") or {}
            tools.append(
                {
                    "type": "function",
                    "name": fn.get("name"),
                    "description": fn.get("description"),
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                }
            )
    if tools:
        out["tools"] = tools
        if body.get("tool_choice") is not None:
            out["tool_choice"] = body["tool_choice"]

    if isinstance(body.get("reasoning_effort"), str):
        out["reasoning"] = {"effort": body["reasoning_effort"]}
    if body.get("parallel_tool_calls") is not None:
        out["parallel_tool_calls"] = body["parallel_tool_calls"]
    return out


def sanitize_codex_responses_body(body: dict[str, Any]) -> dict[str, Any]:
    """Il backend Codex accetta solo store=false + stream=true e rifiuta
    previous_response_id e i limiti di token."""
    out = dict(body)
    out["store"] = False
    out["stream"] = True
    out.pop("previous_response_id", None)
    out.pop("max_output_tokens", None)
    out.pop("max_completion_tokens", None)
    out.pop("max_tokens", None)
    out.setdefault("instructions", CODEX_INSTRUCTIONS)
    return out


def _sse_events(buffer: str) -> tuple[list[dict[str, Any]], str]:
    """Estrae gli eventi SSE completi, restituendo anche il resto non terminato."""
    events: list[dict[str, Any]] = []
    while True:
        idx = buffer.find("\n\n")
        if idx < 0:
            break
        block, buffer = buffer[:idx], buffer[idx + 2:]
        payload = "".join(
            line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")
        )
        if not payload or payload.strip() == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
                                                                             
                                                                               
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events, buffer


class ResponsesToChatTranslator:
    """Trasforma lo stream Responses di Codex in chunk Chat Completions."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.id = "chatcmpl-" + uuid.uuid4().hex[:24]
        self.created = int(time.time())
        self.buffer = ""
        self.tool_index = 0
        self.finished = False
        self.text_parts: list[str] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.usage: dict[str, Any] | None = None
        self.error: str | None = None

    def _chunk(self, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def feed(self, text: str) -> list[dict[str, Any]]:
        self.buffer += text
        events, self.buffer = _sse_events(self.buffer)
        chunks: list[dict[str, Any]] = []
        for ev in events:
            chunks.extend(self._handle(ev))
        return chunks

    def _handle(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        kind = ev.get("type") or ""
        if kind == "response.output_text.delta":
            piece = ev.get("delta")
            if isinstance(piece, str) and piece:
                self.text_parts.append(piece)
                return [self._chunk({"content": piece})]
            return []

        if kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            piece = ev.get("delta")
            if isinstance(piece, str) and piece:
                return [self._chunk({"reasoning_content": piece})]
            return []

        if kind == "response.output_item.done":
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                call = {
                    "index": self.tool_index,
                    "id": str(item.get("call_id") or item.get("id") or f"call_{self.tool_index}"),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }
                self.tool_index += 1
                self.tool_calls.append(call)
                return [self._chunk({"tool_calls": [call]})]
            return []

        if kind in {"response.completed", "response.incomplete"}:
            resp = ev.get("response")
            if isinstance(resp, dict) and isinstance(resp.get("usage"), dict):
                self.usage = self._map_usage(resp["usage"])
            self.finished = True
            return [self._chunk({}, finish="tool_calls" if self.tool_calls else "stop")]

        if kind in {"response.failed", "error"}:
            resp = ev.get("response") if isinstance(ev.get("response"), dict) else ev
            err = resp.get("error") if isinstance(resp, dict) else None
            self.error = str((err or {}).get("message") if isinstance(err, dict) else err or text("error.codex_generic"))
            self.finished = True
            return [self._chunk({}, finish="stop")]

        return []

    @staticmethod
    def _map_usage(usage: dict[str, Any]) -> dict[str, Any]:
        prompt = usage.get("input_tokens") or 0
        completion = usage.get("output_tokens") or 0
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": usage.get("total_tokens") or (prompt + completion),
        }

    def aggregate(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.text_parts) or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": c["id"], "type": "function", "function": c["function"]} for c in self.tool_calls
            ]
        out = {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if self.tool_calls else "stop",
                }
            ],
        }
        if self.usage:
            out["usage"] = self.usage
        return out


def upstream_headers(provider: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    key = store.api_key(provider["id"])
    if key:
        headers["Authorization"] = f"Bearer {key}"
    extra = provider.get("headers")
    if isinstance(extra, dict):
        for k, v in extra.items():
            headers[str(k)] = str(v)
    return headers


async def build_headers(provider: dict[str, Any]) -> dict[str, str]:
    """Header upstream, risolvendo l'OAuth Codex quando serve."""
    headers = upstream_headers(provider)
    if provider_auth_mode(provider) == "codex_oauth":
        access, account = await codex_auth_for(provider).credentials(store.http())
        headers["Authorization"] = f"Bearer {access}"
        if account:
            headers["chatgpt-account-id"] = account
        headers["OpenAI-Beta"] = "responses=experimental"
        headers["originator"] = "codex_cli_rs"
        headers["session_id"] = str(uuid.uuid4())
        headers["Accept"] = "text/event-stream"
    return headers


def codex_plan(endpoint: str, body: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    """(endpoint upstream, body adattato, serve traduzione della risposta)."""
    if endpoint in {"chat/completions", "completions"}:
        return "responses", chat_to_responses(body), True
    return "responses", sanitize_codex_responses_body(body), False


def _sse(payload: dict[str, Any] | str) -> bytes:
    if isinstance(payload, str):
        return f"data: {payload}\n\n".encode()
    return b"data: " + _json_dumps(payload) + b"\n\n"


def valid_json_response(endpoint: str, payload: Any) -> tuple[bool, str | None]:
    if isinstance(payload, dict) and payload.get("error"):
        return False, str(payload.get("error"))

    if endpoint in {"chat/completions", "completions"}:
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            return False, text("error.choices_missing")
        first = choices[0]
        if not isinstance(first, dict):
            return False, text("error.choices_invalid")
        if endpoint == "completions":
            return (True, None) if first.get("text") is not None else (False, text("error.choice_text_missing"))
        message = first.get("message")
        if not isinstance(message, dict):
            return False, text("error.choice_message_missing")
        has_content = message.get("content") is not None
        has_tools = bool(message.get("tool_calls") or message.get("function_call"))
                                                                                   
                                                                               
        has_reasoning = bool(message.get("reasoning_content") or message.get("reasoning"))
        has_refusal = message.get("refusal") is not None
        if not (has_content or has_tools or has_reasoning or has_refusal):
            return False, text("error.message_no_content")
        return True, None

    if endpoint == "responses":
        if isinstance(payload, dict) and ("output" in payload or "output_text" in payload or payload.get("object") == "response"):
            return True, None
        return False, text("error.responses_unrecognized")

    return True, None


def mark_failure(route_id: str, error: str, status_code: int | None = None) -> None:
    now = time.time()
    final_error = f"HTTP {status_code}: {error}" if status_code else error
    h = store.route_health(route_id)
    h.update(
        {
            "status": "error",
            "last_failure": now,
            "last_error": final_error,
            "cooldown_until": now + float(store.config["settings"].get("cooldown_s", 30)),
        }
    )
    append_session_log(
        "error",
        "log.model_failed",
        kind="failure",
        route_id=route_id,
        detail=final_error,
    )


def mark_success(route_id: str, latency_ms: float) -> None:
    h = store.route_health(route_id)
    h.update(
        {
            "status": "ok",
            "last_success": time.time(),
            "last_error": None,
            "latency_ms": round(latency_ms, 1),
            "cooldown_until": 0.0,
        }
    )
    append_session_log(
        "success",
        "log.valid_response",
        kind="success",
        message_params={"ms": round(latency_ms, 1)},
        route_id=route_id,
    )


def record_use(route: dict[str, Any], provider: dict[str, Any], latency_ms: float | None, streaming: bool) -> None:
    s = store.route_stats(route["id"])
    s["requests"] += 1
    s["ok"] += 1
    s["last_used"] = time.time()
    if latency_ms is not None:
        s["total_latency_ms"] += latency_ms
    store.last_used = {
        "route_id": route["id"],
        "model": route["model"],
        "label": route.get("label") or route["model"],
        "provider": provider.get("name", ""),
        "at": time.time(),
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "stream": streaming,
    }


def record_attempt_failure(route_id: str) -> None:
    s = store.route_stats(route_id)
    s["requests"] += 1
    s["fail"] += 1


def start_tracking(endpoint: str, streaming: bool, client_model: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": store.next_request_id(),
        "endpoint": endpoint,
        "stream": bool(streaming),
        "client_model": client_model if isinstance(client_model, str) else None,
        "started": time.time(),
        "route_id": None,
        "active_item_id": None,
        "model": None,
        "provider": None,
        "phase": "waiting_provider",
        "failover_index": None,
    }
    store.inflight[entry["id"]] = entry
    append_session_log(
        "info",
        "log.new_request",
        kind="request",
        request_id=entry["id"],
        message_params={"endpoint": endpoint, "mode": "streaming" if streaming else "non_streaming"},
        detail_key="log.client_model" if isinstance(client_model, str) else None,
        detail_params={"model": client_model} if isinstance(client_model, str) else None,
    )
    return entry


def stop_tracking(entry: dict[str, Any] | None) -> None:
    if entry is not None:
        store.inflight.pop(entry["id"], None)


async def set_active(route_id: str) -> None:
    route = store.route(route_id)
    if route is not None and is_preset(route):
        store.active_member_by_preset.pop(route_id, None)
    if store.config.get("active_route_id") != route_id:
        store.config["active_route_id"] = route_id
        await store.save_config()


def route_display(route: dict[str, Any], provider: dict[str, Any]) -> str:
    return f"{provider['name']} / {route['model']}"


def route_headers(route: dict[str, Any], provider: dict[str, Any]) -> dict[str, str]:
    return {
        "X-Router-Route": route["id"],
        "X-Router-Model": route["model"],
        "X-Router-Provider": provider.get("name", ""),
    }


def _endpoint_url(provider: dict[str, Any], endpoint: str) -> str:
    return f"{str(provider.get('base_url', '')).rstrip('/')}/{endpoint}"


def _failure_payload(errors: list[str], message: str) -> FastJSONResponse:
    return FastJSONResponse(
        {
            "error": {
                "message": message,
                "type": "router_all_backends_failed",
                "code": "all_backends_failed",
                "details": errors,
            }
        },
        status_code=502,
    )


                                                                            
       
                                                                            


async def codex_collect(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    model: str,
    timeout: httpx.Timeout,
    translate: bool,
) -> tuple[bytes, str | None]:
    """Consuma lo stream Codex e restituisce una risposta JSON completa."""
    tr = ResponsesToChatTranslator(model)
    raw = bytearray()
    request = client.build_request("POST", url, headers=headers, json=body, timeout=timeout)
    response = await client.send(request, stream=True)
    try:
        if not 200 <= response.status_code < 300:
            detail = (await response.aread()).decode("utf-8", "replace")[:300].replace("\n", " ")
            return b"", f"HTTP {response.status_code}: {detail}"
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            if translate:
                tr.feed(chunk.decode("utf-8", "replace"))
            else:
                raw.extend(chunk)
    finally:
        await response.aclose()

    if translate:
        if tr.error:
            return b"", tr.error
        if not tr.text_parts and not tr.tool_calls:
            return b"", text("error.codex_no_content")
        return _json_dumps(tr.aggregate()), None

                                                                        
    events, _ = _sse_events(raw.decode("utf-8", "replace"))
    for ev in reversed(events):
        if ev.get("type") in {"response.completed", "response.incomplete"} and isinstance(ev.get("response"), dict):
            return _json_dumps(ev["response"]), None
    return b"", text("error.codex_completed_event_missing")


async def proxy_nonstreaming(
    endpoint: str, body: dict[str, Any], preferred: str | None, track: dict[str, Any] | None = None
) -> Response:
    pairs = enabled_route_sequence(preferred)
    if not pairs:
        raise HTTPException(503, text("error.no_enabled_models"))

    errors: list[str] = []
    client = store.http()
    settings = store.config["settings"]
    timeout = httpx.Timeout(
        float(settings.get("request_timeout_s", 90)),
        connect=float(settings.get("connect_timeout_s", 10)),
    )

    for index, (route, provider, active_item_id) in enumerate(pairs):
        prepared = prepare_body(body, route["model"], endpoint, is_fallback=index > 0)
        wire = provider_wire(provider)
        call_endpoint = endpoint
        translate = False
        if wire == "codex":
            call_endpoint, prepared, translate = codex_plan(endpoint, prepared)
        url = _endpoint_url(provider, call_endpoint)
        if track is not None:
            track.update({
                "route_id": route["id"],
                "active_item_id": active_item_id,
                "model": route["model"],
                "provider": provider.get("name", ""),
                "phase": "waiting_response" if index == 0 else "failover",
                "failover_index": index if index > 0 else None,
            })
        append_session_log(
            "info",
            "log.attempt",
            kind="attempt",
            route_id=route["id"],
            message_params={"number": index + 1, "label": route.get("label") or route["model"]},
            request_id=track.get("id") if track is not None else None,
            detail=f"provider={provider.get('name', '')} · model={route['model']} · endpoint={endpoint}",
        )
        started = time.perf_counter()
        try:
            headers = await build_headers(provider)
        except CodexAuthError as exc:
            mark_failure(route["id"], str(exc))
            record_attempt_failure(route["id"])
            errors.append(f"{route_display(route, provider)} → {exc}")
            continue

        if wire == "codex":
                                                                              
                                                                 
            try:
                payload_bytes, agg_error = await codex_collect(
                    client, url, headers, prepared, route["model"], timeout, translate
                )
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                mark_failure(route["id"], error)
                record_attempt_failure(route["id"])
                errors.append(f"{route_display(route, provider)} → {error}")
                continue
            if agg_error:
                mark_failure(route["id"], agg_error)
                record_attempt_failure(route["id"])
                errors.append(f"{route_display(route, provider)} → {agg_error}")
                continue
            latency = (time.perf_counter() - started) * 1000
            mark_success(route["id"], latency)
            record_use(route, provider, latency, streaming=False)
            remember_success(active_item_id, route["id"], preferred)
            return Response(
                content=payload_bytes,
                status_code=200,
                media_type="application/json",
                headers=route_headers(route, provider),
            )

        try:
            response = await client.post(url, headers=headers, json=prepared, timeout=timeout)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
            mark_failure(route["id"], error)
            record_attempt_failure(route["id"])
            errors.append(f"{route_display(route, provider)} → {error}")
            continue

        if not 200 <= response.status_code < 300:
            snippet = response.text[:500].strip().replace("\n", " ")
            mark_failure(route["id"], snippet or response.reason_phrase, response.status_code)
            record_attempt_failure(route["id"])
            errors.append(f"{route_display(route, provider)} → HTTP {response.status_code}: {snippet[:160]}")
            continue

        raw = response.content
        try:
            payload = response.json()
        except ValueError:
            mark_failure(route["id"], text("error.response_non_json"))
            record_attempt_failure(route["id"])
            errors.append(f"{route_display(route, provider)} → {text('error.response_non_json')}")
            continue

        valid, validation_error = valid_json_response(endpoint, payload)
        if not valid:
            mark_failure(route["id"], validation_error or text("error.response_invalid"))
            record_attempt_failure(route["id"])
            errors.append(f"{route_display(route, provider)} → {validation_error}")
            continue

        latency = (time.perf_counter() - started) * 1000
        mark_success(route["id"], latency)
        record_use(route, provider, latency, streaming=False)
        remember_success(active_item_id, route["id"], preferred)
                                                                 
        return Response(
            content=raw,
            status_code=response.status_code,
            media_type="application/json",
            headers=route_headers(route, provider),
        )

    return _failure_payload(errors, text("error.all_backends_failed"))


async def _prime_sse(iterator: AsyncIterator[bytes], first_token_timeout: float) -> bytes:
    """Attende il primo evento SSE utile, per poter fare failover prima di inviare
    qualsiasi byte al client."""
    buffered = bytearray()
    deadline = time.monotonic() + first_token_timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timeout in attesa del primo token")
        try:
            chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
        except StopAsyncIteration as exc:
            raise ValueError(text("error.stream_closed")) from exc
        if not chunk:
            continue
        buffered.extend(chunk)
                                                                            
                                                                               
        if b"data:" in buffered:
            return bytes(buffered)
        if len(buffered) > 65536:
            raise ValueError(text("error.stream_unrecognized"))


async def proxy_streaming(
    endpoint: str, body: dict[str, Any], preferred: str | None, track: dict[str, Any] | None = None
) -> Response:
    pairs = enabled_route_sequence(preferred)
    if not pairs:
        raise HTTPException(503, text("error.no_enabled_models"))

    errors: list[str] = []
    settings = store.config["settings"]
    timeout = httpx.Timeout(
        float(settings.get("request_timeout_s", 90)),
        connect=float(settings.get("connect_timeout_s", 10)),
        read=float(settings.get("request_timeout_s", 90)),
    )
    first_token_timeout = float(settings.get("first_token_timeout_s", 45))
    client = store.http()

    for index, (route, provider, active_item_id) in enumerate(pairs):
        prepared = prepare_body(body, route["model"], endpoint, is_fallback=index > 0)
        prepared["stream"] = True
        wire = provider_wire(provider)
        call_endpoint = endpoint
        translate = False
        if wire == "codex":
            call_endpoint, prepared, translate = codex_plan(endpoint, prepared)
        url = _endpoint_url(provider, call_endpoint)
        if track is not None:
            track.update({
                "route_id": route["id"],
                "active_item_id": active_item_id,
                "model": route["model"],
                "provider": provider.get("name", ""),
                "phase": "waiting_token" if index == 0 else "failover",
                "failover_index": index if index > 0 else None,
            })
        append_session_log(
            "info",
            "log.attempt",
            kind="attempt",
            route_id=route["id"],
            message_params={"number": index + 1, "label": route.get("label") or route["model"]},
            request_id=track.get("id") if track is not None else None,
            detail=f"provider={provider.get('name', '')} · model={route['model']} · endpoint={endpoint} · streaming",
        )
        started = time.perf_counter()
        response: httpx.Response | None = None
        try:
            headers_up = await build_headers(provider)
            request = client.build_request(
                "POST", url, headers=headers_up, json=prepared, timeout=timeout
            )
            response = await client.send(request, stream=True)
            if not 200 <= response.status_code < 300:
                raw = await response.aread()
                snippet = raw.decode("utf-8", "replace")[:500].replace("\n", " ")
                mark_failure(route["id"], snippet or response.reason_phrase, response.status_code)
                record_attempt_failure(route["id"])
                errors.append(f"{route_display(route, provider)} → HTTP {response.status_code}: {snippet[:160]}")
                await response.aclose()
                continue

                                                                                    
                                                                       
            iterator = response.aiter_bytes()
            first_chunk = await _prime_sse(iterator, first_token_timeout)
        except (httpx.HTTPError, asyncio.TimeoutError, TimeoutError, ValueError, CodexAuthError) as exc:
            error = str(exc) if isinstance(exc, CodexAuthError) else f"{type(exc).__name__}: {exc}"
            mark_failure(route["id"], error)
            record_attempt_failure(route["id"])
            errors.append(f"{route_display(route, provider)} → {error}")
            if response is not None:
                await response.aclose()
            continue

        latency = (time.perf_counter() - started) * 1000
        mark_success(route["id"], latency)
        record_use(route, provider, latency, streaming=True)
        remember_success(active_item_id, route["id"], preferred)
        if track is not None:
                                                                                   
            track["phase"] = "streaming"
            track["_handoff"] = True

        translator = ResponsesToChatTranslator(route["model"]) if translate else None

        async def generate(
            first: bytes = first_chunk,
            rest: AsyncIterator[bytes] = iterator,
            resp: httpx.Response = response,
            tracked: dict[str, Any] | None = track,
            tr: ResponsesToChatTranslator | None = translator,
        ) -> AsyncIterator[bytes]:
                                                                                     
                                                                                 
            try:
                if tr is None:
                    yield first
                    async for chunk in rest:
                        if chunk:
                            yield chunk
                    return
                for out in tr.feed(first.decode("utf-8", "replace")):
                    yield _sse(out)
                async for chunk in rest:
                    if not chunk:
                        continue
                    for out in tr.feed(chunk.decode("utf-8", "replace")):
                        yield _sse(out)
                if not tr.finished:
                    yield _sse(tr._chunk({}, finish="tool_calls" if tr.tool_calls else "stop"))
                yield _sse("[DONE]")
            finally:
                await resp.aclose()
                stop_tracking(tracked)

        headers = route_headers(route, provider)
        headers.update({"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
        return StreamingResponse(
            generate(),
            status_code=200,
            media_type="text/event-stream",
            headers=headers,
        )

    return _failure_payload(errors, text("error.all_backends_failed_stream"))


async def proxy_openai(endpoint: str, request: Request) -> Response:
    require_router_api_key(request)
    raw = await request.body()
    if not raw:
        raise HTTPException(400, text("error.empty_body"))
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, text("error.invalid_json", error=exc)) from exc
    if not isinstance(body, dict):
        raise HTTPException(400, text("error.body_not_object"))

    preferred = resolve_requested_route(body.get("model"))
    streaming = body.get("stream") is True
    track = start_tracking(endpoint, streaming, body.get("model"))
    try:
        if streaming:
            return await proxy_streaming(endpoint, body, preferred, track)
        return await proxy_nonstreaming(endpoint, body, preferred, track)
    finally:
                                                                         
        if not track.get("_handoff"):
            stop_tracking(track)


def router_api_key() -> str:
    return str(store.secrets.get("router_api_key") or "")


def require_router_api_key(request: Request) -> None:
    """Richiede autenticazione solo quando e' configurata una chiave locale."""
    expected = router_api_key()
    if not expected:
        return

    auth = str(request.headers.get("Authorization") or "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = str(request.headers.get("X-API-Key") or "").strip()

    if not token or not pysecrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail=text("error.router_key_invalid"),
            headers={"WWW-Authenticate": "Bearer"},
        )


                                                                            
                            
                                                                            


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "active_route_id": store.config.get("active_route_id")}


@app.get("/v1/models")
@app.get("/models")
async def models(request: Request) -> dict[str, Any]:
    require_router_api_key(request)
    return models_payload()


@app.get("/v1/models/{model_id:path}")
async def model_detail(model_id: str, request: Request) -> dict[str, Any]:
    require_router_api_key(request)
    for item in models_payload()["data"]:
        if item["id"].lower() == model_id.strip().lower():
            return item
    raise HTTPException(404, text("error.model_not_found", model=model_id))


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    return await proxy_openai("chat/completions", request)


@app.post("/v1/completions")
async def completions(request: Request) -> Response:
    return await proxy_openai("completions", request)


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    return await proxy_openai("responses", request)


                                                                            
                 
                                                                            


class SettingsPatch(BaseModel):
    override_client_model: bool | None = None
    expose_virtual_router_model: bool | None = None
    failover_on_explicit_model: bool | None = None
    cooldown_s: float | None = None
    request_timeout_s: float | None = None
    first_token_timeout_s: float | None = None
    context_messages_on_failover: int | None = None


@app.patch("/api/settings")
async def patch_settings(payload: SettingsPatch) -> dict[str, Any]:
    settings = store.config["settings"]
    for key, value in payload.model_dump(exclude_none=True).items():
        settings[key] = value
    await store.save_config()
    return public_state()


@app.get("/api/providers/{provider_id}/codex-status")
async def codex_status(provider_id: str) -> dict[str, Any]:
    provider = store.provider(provider_id)
    if not provider:
        raise HTTPException(404, text("error.provider_not_found"))
    if provider_auth_mode(provider) != "codex_oauth":
        raise HTTPException(400, text("error.provider_not_codex"))
    auth = codex_auth_for(provider)
    try:
        await auth.credentials(store.http())
        return {"ok": True, **auth.status()}
    except CodexAuthError as exc:
        return {"ok": False, "error": str(exc), **auth.status()}


@app.get("/api/router-key")
async def router_key_status() -> dict[str, Any]:
    return {"enabled": bool(router_api_key())}


@app.post("/api/router-key")
async def create_router_key() -> dict[str, Any]:
    key = "gisell_" + pysecrets.token_urlsafe(32)
    store.secrets["router_api_key"] = key
    await store.save_secrets()
    return {"enabled": True, "key": key}


@app.delete("/api/router-key")
async def delete_router_key() -> dict[str, Any]:
    store.secrets["router_api_key"] = ""
    await store.save_secrets()
    return {"enabled": False}


def _service_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def _systemd_quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _run_system(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _service_unit_text() -> str:
    py = _systemd_quote(sys.executable)
    script = _systemd_quote(str(Path(__file__).resolve()))
    return f"""[Unit]\nDescription={APP_NAME}\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nExecStart={py} {script}\nRestart=on-failure\nRestartSec=3\nEnvironment=PYTHONUNBUFFERED=1\n\n[Install]\nWantedBy=default.target\n"""


def _require_systemd() -> None:
    if not sys.platform.startswith("linux"):
        raise HTTPException(400, text("error.linux_only"))
    if not shutil.which("systemctl"):
        raise HTTPException(400, text("error.systemctl_unavailable"))


def _service_status_sync() -> dict[str, Any]:
    linux = sys.platform.startswith("linux")
    unit = _service_unit_path()
    result: dict[str, Any] = {
        "linux": linux,
        "systemd": bool(shutil.which("systemctl")) if linux else False,
        "installed": unit.exists() if linux else False,
        "enabled": False,
        "active": False,
        "linger": False,
        "unit_path": str(unit),
    }
    if not result["systemd"]:
        return result
    enabled = _run_system(["systemctl", "--user", "is-enabled", SERVICE_NAME])
    active = _run_system(["systemctl", "--user", "is-active", SERVICE_NAME])
    result["enabled"] = enabled.stdout.strip() == "enabled"
    result["active"] = active.stdout.strip() == "active"
    if shutil.which("loginctl"):
        user = os.environ.get("USER") or str(os.getuid())
        linger = _run_system(["loginctl", "show-user", user, "-p", "Linger", "--value"])
        result["linger"] = linger.stdout.strip().lower() == "yes"
    return result


@app.get("/api/service")
async def service_status() -> dict[str, Any]:
    return await asyncio.to_thread(_service_status_sync)


@app.post("/api/service")
async def install_service() -> dict[str, Any]:
    _require_systemd()
    unit = _service_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    tmp = unit.with_suffix(unit.suffix + ".tmp")
    await asyncio.to_thread(tmp.write_text, _service_unit_text(), "utf-8")
    await asyncio.to_thread(os.replace, tmp, unit)
    for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", SERVICE_NAME]):
        cp = await asyncio.to_thread(_run_system, cmd)
        if cp.returncode != 0:
            raise HTTPException(500, (cp.stderr or cp.stdout or text("error.systemctl_failed")).strip())
    return await asyncio.to_thread(_service_status_sync)


@app.delete("/api/service")
async def uninstall_service() -> dict[str, Any]:
    _require_systemd()
    unit = _service_unit_path()
    await asyncio.to_thread(_run_system, ["systemctl", "--user", "disable", SERVICE_NAME])
    if unit.exists():
        await asyncio.to_thread(unit.unlink)
    await asyncio.to_thread(_run_system, ["systemctl", "--user", "daemon-reload"])
    return await asyncio.to_thread(_service_status_sync)


@app.post("/api/service/linger")
async def enable_linger() -> dict[str, Any]:
    _require_systemd()
    if not shutil.which("loginctl"):
        raise HTTPException(400, text("error.loginctl_unavailable"))
    user = os.environ.get("USER") or str(os.getuid())
    cp = await asyncio.to_thread(_run_system, ["loginctl", "enable-linger", user])
    if cp.returncode != 0:
        raise HTTPException(500, (cp.stderr or cp.stdout or text("error.enable_linger_failed")).strip())
    return await asyncio.to_thread(_service_status_sync)


@app.delete("/api/service/linger")
async def disable_linger() -> dict[str, Any]:
    _require_systemd()
    if not shutil.which("loginctl"):
        raise HTTPException(400, text("error.loginctl_unavailable"))
    user = os.environ.get("USER") or str(os.getuid())
    cp = await asyncio.to_thread(_run_system, ["loginctl", "disable-linger", user])
    if cp.returncode != 0:
        raise HTTPException(500, (cp.stderr or cp.stdout or text("error.disable_linger_failed")).strip())
    return await asyncio.to_thread(_service_status_sync)


@app.get("/api/live")
async def api_live() -> dict[str, Any]:
    """Stato leggero per il polling della UI: chi sta rispondendo adesso."""
    now = time.time()
    exposed = {e["route_id"]: e["exposed_id"] for e in exposed_models()}
    in_flight = [
        {
            "id": e["id"],
            "route_id": e["route_id"],
            "active_item_id": e.get("active_item_id"),
            "exposed_id": exposed.get(e["route_id"]),
            "model": e["model"],
            "provider": e["provider"],
            "label": (store.route(e["route_id"]) or {}).get("label") if e["route_id"] else None,
            "endpoint": e["endpoint"],
            "stream": e["stream"],
            "client_model": e["client_model"],
            "phase": e["phase"],
            "failover_index": e.get("failover_index"),
            "elapsed_ms": round((now - e["started"]) * 1000),
        }
        for e in sorted(store.inflight.values(), key=lambda x: x["started"])
    ]

    stats = {}
    for route_id, s in store.stats.items():
        ok = s["ok"]
        stats[route_id] = {
            "requests": s["requests"],
            "ok": ok,
            "fail": s["fail"],
            "last_used": s["last_used"],
            "avg_latency_ms": round(s["total_latency_ms"] / ok, 1) if ok else None,
        }

    return {
        "now": now,
        "started_at": store.started_at,
        "active_route_id": store.config.get("active_route_id"),
        "override_client_model": bool(store.config["settings"].get("override_client_model", False)),
        "in_flight": in_flight,
        "last_used": store.last_used,
        "stats": stats,
        "health": store.health,
    }


@app.get("/api/logs")
async def api_logs(after: int = 0) -> dict[str, Any]:
    """Restituisce solo le righe di log successive all'ID indicato."""
    rows = [row for row in store.logs if int(row.get("id", 0)) > max(0, int(after))]
    return {
        "logs": rows,
        "last_id": store._log_seq,
        "count": len(store.logs),
    }


@app.delete("/api/logs")
async def clear_api_logs() -> dict[str, Any]:
    """Pulisce solo la finestra/log in memoria della sessione corrente."""
    store.logs.clear()
    return {"ok": True, "last_id": store._log_seq, "count": 0}


@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    return public_state()


@app.post("/api/providers")
async def add_provider(payload: ProviderCreate) -> dict[str, Any]:
    preset = PRESETS.get(payload.preset, PRESETS["custom"])
    base_url = (payload.base_url or preset["base_url"]).strip().rstrip("/")
    if not base_url:
        raise HTTPException(400, text("error.base_url_required"))
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, text("error.base_url_scheme"))
    provider_id = uuid.uuid4().hex[:12]
    provider = {
        "id": provider_id,
        "name": payload.name.strip(),
        "preset": payload.preset,
        "base_url": base_url,
        "enabled": True,
        "headers": {},
    }
    if preset.get("auth_mode"):
        provider["auth_mode"] = preset["auth_mode"]
    if preset.get("wire"):
        provider["wire"] = preset["wire"]
    if payload.auth_path.strip():
        provider["auth_path"] = payload.auth_path.strip()
    store.config["providers"].append(provider)
    if payload.api_key.strip():
        store.secrets["api_keys"][provider_id] = payload.api_key.strip()
        await store.save_secrets()
    await store.save_config()
    return public_state()


@app.patch("/api/providers/{provider_id}")
async def patch_provider(provider_id: str, payload: ProviderPatch) -> dict[str, Any]:
    provider = store.provider(provider_id)
    if not provider:
        raise HTTPException(404, text("error.provider_not_found"))
    if payload.name is not None:
        provider["name"] = payload.name.strip() or provider["name"]
    if payload.base_url is not None:
        base_url = payload.base_url.strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise HTTPException(400, text("error.base_url_scheme"))
        provider["base_url"] = base_url or provider["base_url"]
    if payload.enabled is not None:
        provider["enabled"] = payload.enabled
    if payload.api_key is not None:
        if payload.api_key.strip():
            store.secrets["api_keys"][provider_id] = payload.api_key.strip()
        else:
            store.secrets["api_keys"].pop(provider_id, None)
        await store.save_secrets()
    await store.save_config()
    return public_state()


@app.delete("/api/providers/{provider_id}")
async def delete_provider(provider_id: str) -> dict[str, Any]:
    if not store.provider(provider_id):
        raise HTTPException(404, text("error.provider_not_found"))
    removed_route_ids = {r["id"] for r in store.config["routes"] if r.get("provider_id") == provider_id}
    store.config["providers"] = [p for p in store.config["providers"] if p["id"] != provider_id]
    store.config["routes"] = [r for r in store.config["routes"] if r.get("provider_id") != provider_id]
    had_key = store.secrets["api_keys"].pop(provider_id, None) is not None
    for rid in removed_route_ids:
        store.health.pop(rid, None)
    if store.config.get("active_route_id") in removed_route_ids:
        store.config["active_route_id"] = store.config["routes"][0]["id"] if store.config["routes"] else None
    if had_key:
        await store.save_secrets()
    await store.save_config()
    return public_state()


@app.post("/api/providers/{provider_id}/models")
async def add_model(provider_id: str, payload: ModelCreate) -> dict[str, Any]:
    provider = store.provider(provider_id)
    if not provider:
        raise HTTPException(404, text("error.provider_not_found"))
    model = payload.model.strip()
    if not model:
        raise HTTPException(400, text("error.model_id_required"))
    if any(r.get("provider_id") == provider_id and r.get("model") == model for r in store.config["routes"]):
        raise HTTPException(409, text("error.model_duplicate"))
    route = {
        "id": uuid.uuid4().hex[:12],
        "provider_id": provider_id,
        "model": model,
        "label": payload.label.strip() or model,
        "enabled": True,
    }
    store.config["routes"].append(route)
    if store.config.get("active_route_id") is None:
        store.config["active_route_id"] = route["id"]
    await store.save_config()
    return public_state()


def _validated_members(raw: list[str], owner_id: str) -> list[str]:
    """Membri esistenti, senza duplicati, auto-riferimenti o cicli fra preset."""
    by_id = {r["id"]: r for r in store.config["routes"]}
    if any(m not in by_id for m in raw):
        raise HTTPException(404, text("error.member_missing"))
    if owner_id in raw:
        raise HTTPException(400, text("error.preset_cycle"))
    members = _clean_members(raw, set(by_id), owner_id)

                                                                       
    stack, seen = list(members), set()
    while stack:
        mid = stack.pop()
        if mid == owner_id:
            raise HTTPException(400, text("error.preset_cycle"))
        if mid in seen:
            continue
        seen.add(mid)
        entry = by_id.get(mid)
        if is_preset(entry):
            stack.extend(entry.get("members") or [])
    return members


@app.post("/api/presets")
async def create_preset(payload: PresetCreate) -> dict[str, Any]:
    preset_id = uuid.uuid4().hex[:12]
    preset = {
        "id": preset_id,
        "kind": PRESET_KIND,
        "label": payload.label.strip() or "Preset",
        "members": _validated_members(payload.members, preset_id),
        "enabled": True,
    }
    store.config["routes"].append(preset)
    if store.config.get("active_route_id") is None:
        store.config["active_route_id"] = preset_id
    await store.save_config()
    return public_state()


@app.patch("/api/presets/{preset_id}")
async def patch_preset(preset_id: str, payload: PresetPatch) -> dict[str, Any]:
    preset = store.route(preset_id)
    if not is_preset(preset):
        raise HTTPException(404, text("error.preset_not_found"))
    assert preset is not None
    if payload.label is not None:
        preset["label"] = payload.label.strip() or preset.get("label") or "Preset"
    if payload.members is not None:
        preset["members"] = _validated_members(payload.members, preset_id)
    if payload.enabled is not None:
        preset["enabled"] = payload.enabled
    await store.save_config()
    return public_state()


@app.delete("/api/routes/{route_id}")
async def delete_route(route_id: str) -> dict[str, Any]:
    if not store.route(route_id):
        raise HTTPException(404, text("error.model_not_found", model=route_id if "route_id" in locals() else ""))
    store.config["routes"] = [r for r in store.config["routes"] if r["id"] != route_id]
                                                                          
    for entry in store.config["routes"]:
        if is_preset(entry) and route_id in (entry.get("members") or []):
            entry["members"] = [m for m in entry["members"] if m != route_id]
    store.health.pop(route_id, None)
    if store.config.get("active_route_id") == route_id:
        store.config["active_route_id"] = store.config["routes"][0]["id"] if store.config["routes"] else None
    await store.save_config()
    return public_state()


@app.patch("/api/routes/{route_id}")
async def patch_route(route_id: str, payload: RoutePatch) -> dict[str, Any]:
    route = store.route(route_id)
    if not route:
        raise HTTPException(404, text("error.model_not_found", model=route_id if "route_id" in locals() else ""))
    if is_preset(route):
        raise HTTPException(400, text("error.entry_is_preset", id=route_id))

    new_provider_id = route["provider_id"]
    if payload.provider_id is not None and payload.provider_id != route["provider_id"]:
        if not store.provider(payload.provider_id):
            raise HTTPException(404, text("error.destination_provider_not_found"))
        new_provider_id = payload.provider_id

    new_model = route["model"]
    if payload.model is not None:
        new_model = payload.model.strip()
        if not new_model:
            raise HTTPException(400, text("error.model_id_required"))

    if (new_model, new_provider_id) != (route["model"], route["provider_id"]):
        clash = any(
            r["id"] != route_id and r.get("provider_id") == new_provider_id and r.get("model") == new_model
            for r in store.config["routes"]
        )
        if clash:
            raise HTTPException(409, text("error.model_duplicate"))
                                                                     
        store.health.pop(route_id, None)
        store.stats.pop(route_id, None)

    label_was_default = route.get("label") in (None, "", route["model"])
    route["provider_id"] = new_provider_id
    route["model"] = new_model
    if payload.label is not None:
        route["label"] = payload.label.strip() or new_model
    elif label_was_default:
        route["label"] = new_model
    if payload.enabled is not None:
        route["enabled"] = payload.enabled

    await store.save_config()
    return public_state()


@app.post("/api/routes/{route_id}/move")
async def move_route(route_id: str, payload: RouteMove) -> dict[str, Any]:
    routes = store.config["routes"]
    idx = next((i for i, r in enumerate(routes) if r["id"] == route_id), None)
    if idx is None:
        raise HTTPException(404, text("error.model_not_found", model=route_id if "route_id" in locals() else ""))
    new_idx = max(0, min(len(routes) - 1, idx + (-1 if payload.direction < 0 else 1)))
    if new_idx != idx:
        routes[idx], routes[new_idx] = routes[new_idx], routes[idx]
        await store.save_config()
    return public_state()


@app.post("/api/routes/{route_id}/activate")
async def activate_route(route_id: str) -> dict[str, Any]:
    if not store.route(route_id):
        raise HTTPException(404, text("error.model_not_found", model=route_id if "route_id" in locals() else ""))
    await set_active(route_id)
    return public_state()


@app.post("/api/providers/{provider_id}/discover-models")
async def discover_models(provider_id: str) -> dict[str, Any]:
    provider = store.provider(provider_id)
    if not provider:
        raise HTTPException(404, text("error.provider_not_found"))
    if provider_wire(provider) == "codex":
                                                                               
                                                                  
        return {"models": list(CODEX_KNOWN_MODELS), "note": text("provider.codex_static_models")}

    url = _endpoint_url(provider, "models")
    try:
        headers = await build_headers(provider)
        response = await store.http().get(url, headers=headers, timeout=20.0)
    except CodexAuthError as exc:
        raise HTTPException(502, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, text("error.connection", error=exc)) from exc
    if not response.is_success:
        raise HTTPException(502, text("error.provider_http", status=response.status_code, detail=response.text[:300]))
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(502, text("error.provider_non_json")) from exc
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("models") or []
    elif isinstance(payload, list):
        data = payload
    else:
        data = []
    model_ids = sorted(
        {
            str(x.get("id") or x.get("name"))
            for x in data
            if isinstance(x, dict) and (x.get("id") or x.get("name"))
        }
    )
    return {"models": model_ids}


@app.post("/api/routes/{route_id}/test")
async def test_route(route_id: str) -> dict[str, Any]:
    route = store.route(route_id)
    if not route:
        raise HTTPException(404, text("error.model_not_found", model=route_id if "route_id" in locals() else ""))
    if is_preset(route):
        raise HTTPException(400, text("error.preset_not_testable"))
    provider = store.provider(route["provider_id"])
    if not provider:
        raise HTTPException(404, text("error.provider_not_found"))

    body: dict[str, Any] = {
        "model": route["model"],
        "messages": [{"role": "user", "content": "Reply only: OK"}],
        "max_tokens": 3,
        "temperature": 0,
        "stream": False,
    }
    started = time.perf_counter()
    try:
        headers = await build_headers(provider)
    except CodexAuthError as exc:
        mark_failure(route_id, str(exc))
        return {"ok": False, "error": str(exc), "state": public_state()}

    if provider_wire(provider) == "codex":
        call_endpoint, prepared, translate = codex_plan("chat/completions", body)
        url = _endpoint_url(provider, call_endpoint)
        try:
            _, err = await codex_collect(
                store.http(), url, headers, prepared, route["model"],
                httpx.Timeout(60.0, connect=15.0), translate,
            )
        except httpx.HTTPError as exc:
            mark_failure(route_id, str(exc))
            return {"ok": False, "error": str(exc), "state": public_state()}
        if err:
            mark_failure(route_id, err)
            return {"ok": False, "error": err, "state": public_state()}
        latency = (time.perf_counter() - started) * 1000
        mark_success(route_id, latency)
        return {"ok": True, "latency_ms": round(latency, 1), "state": public_state()}

    url = _endpoint_url(provider, "chat/completions")
    try:
        response = await store.http().post(url, headers=headers, json=body, timeout=30.0)
    except httpx.HTTPError as exc:
        mark_failure(route_id, str(exc))
        return {"ok": False, "error": str(exc), "state": public_state()}

    if not response.is_success:
        detail = response.text[:300]
        mark_failure(route_id, detail, response.status_code)
        return {"ok": False, "error": f"HTTP {response.status_code}: {detail}", "state": public_state()}
    try:
        payload = response.json()
    except ValueError:
        mark_failure(route_id, text("error.response_non_json"))
        return {"ok": False, "error": text("error.response_non_json"), "state": public_state()}
    valid, error = valid_json_response("chat/completions", payload)
    if not valid:
        mark_failure(route_id, error or text("error.response_invalid"))
        return {"ok": False, "error": error, "state": public_state()}
    latency = (time.perf_counter() - started) * 1000
    mark_success(route_id, latency)
    return {"ok": True, "latency_ms": round(latency, 1), "state": public_state()}


@app.post("/api/routes/test-all")
async def test_all_routes() -> dict[str, Any]:
    """Testa tutte le route in parallelo invece che una alla volta."""
    route_ids = [r["id"] for r in store.config["routes"] if not is_preset(r)]
    if not route_ids:
        return {"results": {}, "state": public_state()}
    results = await asyncio.gather(*(test_route(rid) for rid in route_ids), return_exceptions=True)
    summary: dict[str, Any] = {}
    for rid, result in zip(route_ids, results):
        if isinstance(result, BaseException):
            summary[rid] = {"ok": False, "error": str(result)}
        else:
            summary[rid] = {"ok": result["ok"], "error": result.get("error"), "latency_ms": result.get("latency_ms")}
    return {"results": summary, "state": public_state()}


WEBUI_PATH = APP_DIR / "webui.html"


def _load_webui() -> str:
    try:
        return WEBUI_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Cannot read {WEBUI_PATH.name}: {exc}") from exc


HTML = _load_webui().replace("__GISELL_LANGUAGE_PACKS__", json.dumps(LANGUAGE_PACKS, ensure_ascii=False))



@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML


if __name__ == "__main__":
    host = str(store.config["settings"].get("host", "127.0.0.1"))
    port = int(store.config["settings"].get("port", 8765))
    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=False,
        log_level="warning",
        timeout_keep_alive=120,
    )
