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
        "base_url": "https://openrouter.ai/api/v1",
        "needs_key": True,
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "needs_key": True,
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "needs_key": True,
    },
    "google_ai_studio": {
        "label": "Google AI Studio",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "needs_key": True,
        "note": "Usa una Gemini API key creata in Google AI Studio. Endpoint OpenAI-compatible ufficiale di Google.",
    },
    "mistral": {
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "needs_key": True,
    },
    "together": {
        "label": "Together",
        "base_url": "https://api.together.ai/v1",
        "needs_key": True,
    },
    "deepinfra": {
        "label": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "needs_key": True,
    },
    "fireworks": {
        "label": "Fireworks AI",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "needs_key": True,
    },
    "cerebras": {
        "label": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "needs_key": True,
    },
    "ollama": {
        "label": "Ollama locale (anche modelli cloud)",
        "base_url": "http://127.0.0.1:11434/v1",
        "needs_key": False,
    },
    "codex": {
        "label": "Codex / ChatGPT (login OAuth)",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "needs_key": False,
        "auth_mode": "codex_oauth",
        "wire": "codex",
        "note": "Richiede 'codex login'. Endpoint interno non documentato: usalo solo con il tuo account.",
    },
    "custom": {
        "label": "Custom OpenAI-compatible",
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
    message: str,
    *,
    kind: str = "system",
    route_id: str | None = None,
    request_id: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Aggiunge una riga al log live della sessione, arricchita con route/provider."""
    store._log_seq += 1
    route = store.route(route_id) if route_id else None
    provider = store.provider(route["provider_id"]) if route else None
    entry = {
        "id": store._log_seq,
        "at": time.time(),
        "level": str(level),
        "kind": str(kind),
        "message": str(message),
        "detail": str(detail) if detail else None,
        "request_id": request_id,
        "route_id": route_id,
        "label": (route.get("label") or route.get("model")) if route else None,
        "model": route.get("model") if route else None,
        "provider": provider.get("name") if provider else None,
    }
    store.logs.append(entry)
    return entry


append_session_log("info", "Router avviato", kind="system")


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
                "description": "Failover automatico su tutti i modelli configurati",
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
                    "description": f"Preset: failover in ordine su {n} modell{'o' if n == 1 else 'i'}",
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
                f"{self.path} non trovato. Esegui 'codex login' su questa macchina."
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CodexAuthError(f"Impossibile leggere {self.path}: {exc}") from exc
        tokens = raw.get("tokens") if isinstance(raw, dict) else None
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            raise CodexAuthError(
                "auth.json non contiene un access_token: sei loggato con API key invece che con ChatGPT? "
                "Esegui 'codex login' scegliendo l'accesso con ChatGPT."
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
            raise CodexAuthError("Token scaduto e nessun refresh_token disponibile: rifai 'codex login'.")
        payload = {
            "client_id": CODEX_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh,
            "scope": "openid profile email",
        }
        try:
            resp = await client.post(CODEX_TOKEN_URL, json=payload, timeout=30.0)
        except httpx.HTTPError as exc:
            raise CodexAuthError(f"Refresh del token fallito: {exc}") from exc
        if not resp.is_success:
            raise CodexAuthError(
                f"Refresh del token rifiutato (HTTP {resp.status_code}). Rifai 'codex login'."
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise CodexAuthError("Risposta non JSON dal servizio token") from exc

        access = data.get("access_token")
        if not access:
            raise CodexAuthError("Il servizio token non ha restituito un access_token")
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
            self.error = str((err or {}).get("message") if isinstance(err, dict) else err or "errore Codex")
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
            return False, "Manca choices[]"
        first = choices[0]
        if not isinstance(first, dict):
            return False, "choices[0] non valido"
        if endpoint == "completions":
            return (True, None) if first.get("text") is not None else (False, "Manca choices[0].text")
        message = first.get("message")
        if not isinstance(message, dict):
            return False, "Manca choices[0].message"
        has_content = message.get("content") is not None
        has_tools = bool(message.get("tool_calls") or message.get("function_call"))
                                                                                   
                                                                               
        has_reasoning = bool(message.get("reasoning_content") or message.get("reasoning"))
        has_refusal = message.get("refusal") is not None
        if not (has_content or has_tools or has_reasoning or has_refusal):
            return False, "Messaggio senza contenuto/tool call"
        return True, None

    if endpoint == "responses":
        if isinstance(payload, dict) and ("output" in payload or "output_text" in payload or payload.get("object") == "response"):
            return True, None
        return False, "Risposta Responses API non riconosciuta"

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
        "Modello fallito",
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
        f"Risposta valida in {round(latency_ms, 1)} ms",
        kind="success",
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
        "phase": "in attesa del provider",
    }
    store.inflight[entry["id"]] = entry
    append_session_log(
        "info",
        f"Nuova richiesta {endpoint} · {'streaming' if streaming else 'non streaming'}",
        kind="request",
        request_id=entry["id"],
        detail=f"model richiesto dal client: {client_model}" if isinstance(client_model, str) else None,
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
            return b"", "Nessun contenuto ricevuto da Codex"
        return _json_dumps(tr.aggregate()), None

                                                                        
    events, _ = _sse_events(raw.decode("utf-8", "replace"))
    for ev in reversed(events):
        if ev.get("type") in {"response.completed", "response.incomplete"} and isinstance(ev.get("response"), dict):
            return _json_dumps(ev["response"]), None
    return b"", "Stream Responses senza evento response.completed"


async def proxy_nonstreaming(
    endpoint: str, body: dict[str, Any], preferred: str | None, track: dict[str, Any] | None = None
) -> Response:
    pairs = enabled_route_sequence(preferred)
    if not pairs:
        raise HTTPException(503, "Nessun modello abilitato nel router")

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
                "phase": "in attesa della risposta" if index == 0 else f"failover #{index}",
            })
        append_session_log(
            "info",
            f"Tentativo #{index + 1} su {route.get('label') or route['model']}",
            kind="attempt",
            route_id=route["id"],
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
            mark_failure(route["id"], "Risposta non JSON")
            record_attempt_failure(route["id"])
            errors.append(f"{route_display(route, provider)} → Risposta non JSON")
            continue

        valid, validation_error = valid_json_response(endpoint, payload)
        if not valid:
            mark_failure(route["id"], validation_error or "Risposta non valida")
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

    return _failure_payload(errors, "Tutti i backend configurati hanno fallito")


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
            raise ValueError("Stream chiuso dal provider senza inviare dati") from exc
        if not chunk:
            continue
        buffered.extend(chunk)
                                                                            
                                                                               
        if b"data:" in buffered:
            return bytes(buffered)
        if len(buffered) > 65536:
            raise ValueError("Stream 2xx senza eventi SSE riconoscibili")


async def proxy_streaming(
    endpoint: str, body: dict[str, Any], preferred: str | None, track: dict[str, Any] | None = None
) -> Response:
    pairs = enabled_route_sequence(preferred)
    if not pairs:
        raise HTTPException(503, "Nessun modello abilitato nel router")

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
                "phase": "in attesa del primo token" if index == 0 else f"failover #{index}",
            })
        append_session_log(
            "info",
            f"Tentativo #{index + 1} su {route.get('label') or route['model']}",
            kind="attempt",
            route_id=route["id"],
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
                                                                                   
            track["phase"] = "streaming in corso"
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

    return _failure_payload(errors, "Tutti i backend configurati hanno fallito prima dell'inizio dello stream")


async def proxy_openai(endpoint: str, request: Request) -> Response:
    require_router_api_key(request)
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "Body vuoto")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, f"JSON non valido: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Il body deve essere un oggetto JSON")

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
            detail="Chiave API del router mancante o non valida",
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
    raise HTTPException(404, f"Modello '{model_id}' non trovato")


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
        raise HTTPException(404, "Provider non trovato")
    if provider_auth_mode(provider) != "codex_oauth":
        raise HTTPException(400, "Questo provider non usa il login Codex")
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
        raise HTTPException(400, "Funzione disponibile solo su Linux")
    if not shutil.which("systemctl"):
        raise HTTPException(400, "systemctl non disponibile")


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
            raise HTTPException(500, (cp.stderr or cp.stdout or "systemctl fallito").strip())
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
        raise HTTPException(400, "loginctl non disponibile")
    user = os.environ.get("USER") or str(os.getuid())
    cp = await asyncio.to_thread(_run_system, ["loginctl", "enable-linger", user])
    if cp.returncode != 0:
        raise HTTPException(500, (cp.stderr or cp.stdout or "Impossibile abilitare linger").strip())
    return await asyncio.to_thread(_service_status_sync)


@app.delete("/api/service/linger")
async def disable_linger() -> dict[str, Any]:
    _require_systemd()
    if not shutil.which("loginctl"):
        raise HTTPException(400, "loginctl non disponibile")
    user = os.environ.get("USER") or str(os.getuid())
    cp = await asyncio.to_thread(_run_system, ["loginctl", "disable-linger", user])
    if cp.returncode != 0:
        raise HTTPException(500, (cp.stderr or cp.stdout or "Impossibile disabilitare linger").strip())
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
        raise HTTPException(400, "Base URL obbligatorio")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "Base URL deve iniziare con http:// o https://")
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
        raise HTTPException(404, "Provider non trovato")
    if payload.name is not None:
        provider["name"] = payload.name.strip() or provider["name"]
    if payload.base_url is not None:
        base_url = payload.base_url.strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise HTTPException(400, "Base URL deve iniziare con http:// o https://")
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
        raise HTTPException(404, "Provider non trovato")
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
        raise HTTPException(404, "Provider non trovato")
    model = payload.model.strip()
    if not model:
        raise HTTPException(400, "ID modello obbligatorio")
    if any(r.get("provider_id") == provider_id and r.get("model") == model for r in store.config["routes"]):
        raise HTTPException(409, "Questo modello e' gia' presente per il provider")
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
        raise HTTPException(404, "Uno dei modelli indicati non esiste piu'")
    if owner_id in raw:
        raise HTTPException(400, "Ciclo: un preset non puo' contenere se stesso")
    members = _clean_members(raw, set(by_id), owner_id)

                                                                       
    stack, seen = list(members), set()
    while stack:
        mid = stack.pop()
        if mid == owner_id:
            raise HTTPException(400, "Ciclo: un preset non puo' contenere se stesso")
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
        raise HTTPException(404, "Preset non trovato")
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
        raise HTTPException(404, "Modello non trovato")
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
        raise HTTPException(404, "Modello non trovato")
    if is_preset(route):
        raise HTTPException(400, "Questa voce e' un preset: usa /api/presets/{id}")

    new_provider_id = route["provider_id"]
    if payload.provider_id is not None and payload.provider_id != route["provider_id"]:
        if not store.provider(payload.provider_id):
            raise HTTPException(404, "Provider di destinazione non trovato")
        new_provider_id = payload.provider_id

    new_model = route["model"]
    if payload.model is not None:
        new_model = payload.model.strip()
        if not new_model:
            raise HTTPException(400, "ID modello obbligatorio")

    if (new_model, new_provider_id) != (route["model"], route["provider_id"]):
        clash = any(
            r["id"] != route_id and r.get("provider_id") == new_provider_id and r.get("model") == new_model
            for r in store.config["routes"]
        )
        if clash:
            raise HTTPException(409, "Questo modello e' gia' presente per il provider")
                                                                     
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
        raise HTTPException(404, "Modello non trovato")
    new_idx = max(0, min(len(routes) - 1, idx + (-1 if payload.direction < 0 else 1)))
    if new_idx != idx:
        routes[idx], routes[new_idx] = routes[new_idx], routes[idx]
        await store.save_config()
    return public_state()


@app.post("/api/routes/{route_id}/activate")
async def activate_route(route_id: str) -> dict[str, Any]:
    if not store.route(route_id):
        raise HTTPException(404, "Modello non trovato")
    await set_active(route_id)
    return public_state()


@app.post("/api/providers/{provider_id}/discover-models")
async def discover_models(provider_id: str) -> dict[str, Any]:
    provider = store.provider(provider_id)
    if not provider:
        raise HTTPException(404, "Provider non trovato")
    if provider_wire(provider) == "codex":
                                                                               
                                                                  
        return {"models": list(CODEX_KNOWN_MODELS), "note": "elenco statico: il backend Codex non espone /models"}

    url = _endpoint_url(provider, "models")
    try:
        headers = await build_headers(provider)
        response = await store.http().get(url, headers=headers, timeout=20.0)
    except CodexAuthError as exc:
        raise HTTPException(502, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Errore collegamento: {exc}") from exc
    if not response.is_success:
        raise HTTPException(502, f"Il provider ha risposto HTTP {response.status_code}: {response.text[:300]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Il provider non ha restituito JSON") from exc
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
        raise HTTPException(404, "Modello non trovato")
    if is_preset(route):
        raise HTTPException(400, "Un preset non si testa: testa i suoi membri")
    provider = store.provider(route["provider_id"])
    if not provider:
        raise HTTPException(404, "Provider non trovato")

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
        mark_failure(route_id, "Risposta non JSON")
        return {"ok": False, "error": "Risposta non JSON", "state": public_state()}
    valid, error = valid_json_response("chat/completions", payload)
    if not valid:
        mark_failure(route_id, error or "Risposta non valida")
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


HTML = r'''<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local LLM Router</title>
<style>
:root{color-scheme:dark;--bg:#111318;--panel:#191c23;--panel2:#222631;--text:#eef1f7;--muted:#9ba4b5;--border:#343a48;--accent:#7aa2f7;--ok:#73daca;--bad:#f7768e;--warn:#e0af68}
*{box-sizing:border-box} body{margin:0;font:15px system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text)}
main{max-width:1050px;margin:0 auto;padding:28px 18px 60px}.top{display:flex;gap:16px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}.title h1{font-size:25px;margin:0 0 5px}.muted{color:var(--muted)}
.top-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.api{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px 14px;min-width:min(100%,390px)}.api code{color:var(--ok);word-break:break-all}
button,input,select{font:inherit}button{cursor:pointer;border:1px solid var(--border);background:var(--panel2);color:var(--text);border-radius:8px;padding:8px 11px}button:hover{border-color:var(--accent)}button.primary{background:var(--accent);color:#10131a;border-color:var(--accent);font-weight:650}button.danger{color:var(--bad)}button.small{padding:5px 8px;font-size:13px}
section{margin-top:26px}.sectionhead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:11px}.sectionhead h2{font-size:18px;margin:0}.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:15px;margin:10px 0}.provider-head{display:flex;gap:10px;justify-content:space-between;align-items:center;flex-wrap:wrap}.provider-name{font-weight:700}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.models{margin-top:12px;border-top:1px solid var(--border);padding-top:10px}.model{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;padding:8px 0}.model+.model{border-top:1px dashed #2e3440}[hidden]{display:none!important}.tag{display:inline-block;border:1px solid var(--border);border-radius:999px;padding:2px 7px;color:var(--muted);font-size:12px}.tag.ok{color:var(--ok);border-color:#3a665f}.tag.bad{color:var(--bad);border-color:#6b3945}.tag.active{color:var(--accent);border-color:#435d91}.tag.off{opacity:.55}.route{display:grid;grid-template-columns:44px 1fr auto;gap:10px;align-items:center;padding:10px 5px}.route+.route{border-top:1px solid var(--border)}.prio{color:var(--muted);text-align:center;font-variant-numeric:tabular-nums}.route-actions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.flash{margin-top:14px;min-height:22px}.flash.ok{color:var(--ok)}.flash.bad{color:var(--bad)}
code.mid{color:var(--warn);font-size:12.5px;word-break:break-all}
.live{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-top:18px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.live.busy{border-color:var(--accent)}
.dot{width:11px;height:11px;border-radius:50%;background:var(--muted);flex:none}
.dot.busy{background:var(--accent);animation:pulse 1s infinite}
.dot.idle{background:#3f4757}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.8)}}
.live-main{flex:1;min-width:240px}.live-model{font-weight:700;font-size:16px}
.live-sub{color:var(--muted);font-size:13px;margin-top:3px}
.stat{font-size:12px;color:var(--muted);margin-top:3px;font-variant-numeric:tabular-nums}
.switch{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
.switch input{width:16px;height:16px;accent-color:var(--accent)}
dialog{width:min(620px,calc(100% - 28px));border:1px solid var(--border);border-radius:14px;background:var(--panel);color:var(--text);padding:20px}dialog::backdrop{background:#000a}dialog h3{margin-top:0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.field{display:flex;flex-direction:column;gap:6px}.field.full{grid-column:1/-1}.field input,.field select{width:100%;background:#11141a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:9px}.dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}.empty{color:var(--muted);padding:18px 4px}.discover{max-height:260px;overflow:auto;border:1px solid var(--border);border-radius:8px;margin-top:10px}.discover button{display:block;width:100%;text-align:left;border:0;border-radius:0;background:transparent}.discover button:hover{background:var(--panel2)}
.tabs{display:flex;gap:8px;margin-top:22px;border-bottom:1px solid var(--border);padding:0 2px}.tab-btn{border:0;border-bottom:3px solid transparent;border-radius:8px 8px 0 0;background:transparent;padding:11px 16px;color:var(--muted);font-weight:700}.tab-btn:hover{color:var(--text);border-color:var(--border)}.tab-btn.active{color:var(--text);border-bottom-color:var(--accent);background:linear-gradient(180deg,transparent,#7aa2f70d)}
.tab-panel{display:none}.tab-panel.active{display:block}.log-toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin:18px 0 10px}.log-window{height:min(64vh,680px);overflow:auto;background:#0d0f14;border:1px solid var(--border);border-radius:12px;padding:8px;font:12px ui-monospace,SFMono-Regular,Consolas,monospace}.log-empty{padding:26px;color:var(--muted);text-align:center}.log-row{display:grid;grid-template-columns:78px 72px minmax(150px,260px) 1fr;gap:9px;padding:8px 7px;border-bottom:1px solid #252a34;align-items:start}.log-row:last-child{border-bottom:0}.log-time{color:var(--muted);font-variant-numeric:tabular-nums}.log-level{font-weight:800;text-transform:uppercase}.log-level.error{color:var(--bad)}.log-level.success{color:var(--ok)}.log-level.info{color:var(--accent)}.log-source{color:#c0caf5;overflow:hidden;text-overflow:ellipsis}.log-message{white-space:pre-wrap;overflow-wrap:anywhere}.log-detail{display:block;color:#c7cbd4;margin-top:4px;white-space:pre-wrap;overflow-wrap:anywhere}.status-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:18px}.status-card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:13px 14px;min-width:0}.status-label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}.status-value{font-size:18px;font-weight:750;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.status-detail{color:var(--muted);font-size:12px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.status-value.ok{color:var(--ok)}.status-value.warn{color:var(--warn)}
.preset-row{background:#202a42;box-shadow:inset 4px 0 0 var(--accent);border-radius:8px}.preset-members{margin-top:6px;display:flex;gap:6px;flex-wrap:wrap}.chip{border:1px solid var(--border);border-radius:999px;padding:2px 9px;font-size:12px;color:var(--muted)}.chip.off{opacity:.45;text-decoration:line-through}.picker{max-height:230px;overflow:auto;border:1px solid var(--border);border-radius:8px}.picker .prow{display:flex;align-items:center;gap:6px;padding:6px 8px;border-bottom:1px solid #252a34}.picker .prow:last-child{border-bottom:0}.picker .pname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.picker .empty{padding:14px;font-size:13px}
.panel-intro{color:var(--muted);margin-top:8px}.config-model-actions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.api-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.api-box{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px}.api-box h3{margin:0 0 8px;font-size:16px}.codebox{display:flex;align-items:center;gap:8px;background:#11141a;border:1px solid var(--border);border-radius:8px;padding:10px 11px;margin-top:8px}.codebox code{color:var(--ok);word-break:break-all;flex:1}.key-value{color:var(--warn)!important}.instructions{line-height:1.55}.instructions ol{padding-left:22px}.instructions li+li{margin-top:5px}pre{white-space:pre-wrap;word-break:break-word;background:#11141a;border:1px solid var(--border);border-radius:8px;padding:12px;color:var(--text);overflow:auto}.auth-on{color:var(--ok);font-weight:700}.auth-off{color:var(--muted);font-weight:700}.footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--border);color:var(--muted);font-size:13px;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}.service-state{font-weight:700;margin-bottom:8px}
@media(max-width:800px){.status-grid{grid-template-columns:1fr 1fr}.api-grid{grid-template-columns:1fr}}
@media(max-width:650px){.grid{grid-template-columns:1fr}.field.full{grid-column:auto}.model{grid-template-columns:1fr}.route{grid-template-columns:34px 1fr}.route-actions{grid-column:1/-1;justify-content:flex-start}.status-grid{grid-template-columns:1fr}.tabs{position:sticky;top:0;z-index:5;background:var(--bg);padding-top:6px}.tab-btn{flex:1}}
</style>
</head>
<body><main>
<div class="top"><div class="title"><h1>Benvenuto in GISELL Router!</h1><div class="muted">Gisell Is a Simple and Easy LLM Router</div></div><div class="top-tools"><button class="small" id="langBtn" title="Passa all'inglese">EN</button><div class="api"><div class="muted">API locale</div><code id="apiUrl">...</code> <button class="small" id="copyApi">Copia</button></div></div></div>
<div class="tabs" role="tablist" aria-label="Sezioni router"><button class="tab-btn active" id="tabModelsBtn" data-tab="modelsTab" role="tab" aria-selected="true">Modelli</button><button class="tab-btn" id="tabConfigBtn" data-tab="configTab" role="tab" aria-selected="false">Configurazione</button><button class="tab-btn" id="tabApiBtn" data-tab="apiTab" role="tab" aria-selected="false">API</button><button class="tab-btn" id="tabLogsBtn" data-tab="logsTab" role="tab" aria-selected="false">Log</button></div>
<div id="flash" class="flash"></div>

<div class="tab-panel active" id="modelsTab" role="tabpanel" aria-labelledby="tabModelsBtn">
 <div class="live" id="live"><span class="dot idle" id="liveDot"></span><div class="live-main"><div class="live-model" id="liveModel">In attesa di richieste…</div><div class="live-sub" id="liveSub">Nessuna richiesta ancora ricevuta.</div></div><label class="switch" title="Se attivo, il modello scelto qui vince anche se l'agente ne chiede un altro."><input type="checkbox" id="overrideChk"> Forza il modello attivo</label></div>
 <div class="status-grid" id="statusGrid">
  <div class="status-card"><div class="status-label">Router</div><div class="status-value ok" id="statusRouter">ONLINE</div><div class="status-detail" id="statusUptime">uptime —</div></div>
  <div class="status-card"><div class="status-label">Modello attivo</div><div class="status-value" id="statusActive">—</div><div class="status-detail" id="statusActiveProvider">—</div></div>
  <div class="status-card"><div class="status-label">Modelli</div><div class="status-value" id="statusModels">0</div><div class="status-detail" id="statusModelsDetail">0 abilitati</div></div>
  <div class="status-card"><div class="status-label">Richieste sessione</div><div class="status-value" id="statusRequests">0</div><div class="status-detail" id="statusErrors">0 errori</div></div>
 </div>
 <section><div class="sectionhead"><h2>Ordine dei modelli in uso</h2><div class="row"><span class="muted">Priorità di failover dall’alto verso il basso.</span><button id="testAllBtn">Testa tutti</button></div></div><div class="card" id="routes"></div></section>
</div>

<div class="tab-panel" id="configTab" role="tabpanel" aria-labelledby="tabConfigBtn">
 <div class="panel-intro">Gestione di provider, endpoint, credenziali e modelli configurati.</div>
 <section><div class="sectionhead"><h2>Provider e modelli</h2><button class="primary" id="addProviderBtn">+ Aggiungi provider</button></div><div id="providers"></div></section>
 <section><div class="sectionhead"><h2>Preset</h2><button class="primary" id="addPresetBtn">+ Aggiungi preset</button></div><div class="muted" style="margin-bottom:4px">Un preset raggruppa più modelli sotto un unico nome. Nell\u2019ordine di failover occupa una posizione come un modello: quando la catena lo raggiunge prova i suoi membri nell\u2019ordine indicato, poi passa alla voce successiva. L\u2019agente lo vede in /v1/models come un modello qualsiasi.</div><div id="presets"></div></section>
 <section><div class="sectionhead"><h2>Avvio automatico Linux</h2></div><div class="api-box"><div class="service-state" id="serviceState">Verifica servizio…</div><div class="muted" id="serviceDetail">Installa GISELL Router come servizio systemd dell’utente. Con linger attivo può partire al boot anche prima del login.</div><div class="row" style="margin-top:14px"><button class="primary" id="installServiceBtn">Installa servizio</button><button class="danger" id="removeServiceBtn">Rimuovi servizio</button><button id="lingerBtn">Abilita avvio al boot</button></div></div></section>
</div>

<div class="tab-panel" id="apiTab" role="tabpanel" aria-labelledby="tabApiBtn">
 <div class="panel-intro">Collegamento dei client OpenAI-compatible al router locale e protezione opzionale con chiave API.</div>
 <section>
  <div class="api-grid">
   <div class="api-box instructions">
    <h3>Collegarsi al router</h3>
    <ol>
     <li>Imposta come <b>Base URL</b> l'indirizzo indicato qui sotto.</li>
     <li>Usa <b>router</b> come modello per seguire il modello attivo e il failover configurato.</li>
     <li id="authInstruction">La chiave API non è richiesta.</li>
    </ol>
    <div class="muted">Base URL</div>
    <div class="codebox"><code id="apiBaseUrl">...</code><button class="small" id="copyApiBase">Copia</button></div>
    <div class="muted" style="margin-top:14px">Esempio curl</div>
    <pre id="curlExample"></pre>
    <div class="muted" style="margin-top:14px">Esempio Python (SDK OpenAI)</div>
    <pre id="pythonExample"></pre>
   </div>
   <div class="api-box">
    <h3>Chiave API locale</h3>
    <div id="routerKeyStatus" class="auth-off">Nessuna chiave: accesso libero</div>
    <div class="muted" style="margin-top:7px">Se crei una chiave, tutte le chiamate OpenAI-compatible al router richiederanno <code>Authorization: Bearer &lt;chiave&gt;</code>. Se la rimuovi, l'autenticazione viene disattivata.</div>
    <div class="row" style="margin-top:14px"><button class="primary" id="createRouterKey">Crea chiave</button><button class="danger" id="deleteRouterKey">Rimuovi chiave</button></div>
    <div id="newKeyArea" hidden style="margin-top:16px">
     <div class="muted">Nuova chiave — copiala ora</div>
     <div class="codebox"><code class="key-value" id="newRouterKey"></code><button class="small" id="copyRouterKey">Copia</button></div>
     <div class="muted" style="margin-top:7px">Per sicurezza la chiave salvata non viene mostrata di nuovo dopo il reload. Puoi sempre rimuoverla o generarne una nuova.</div>
    </div>
   </div>
  </div>
 </section>
</div>

<div class="tab-panel" id="logsTab" role="tabpanel" aria-labelledby="tabLogsBtn">
 <div class="panel-intro">Log live della sessione corrente: richieste, tentativi, failover, risposte valide ed errori restituiti dai modelli/provider.</div>
 <div class="log-toolbar">
  <div><b>Eventi sessione</b> <span class="muted" id="logCount">0 righe</span></div>
  <div class="row"><label class="switch"><input type="checkbox" id="logAutoScroll" checked> Auto-scroll</label><button class="danger" id="clearLogsBtn">Cancella log</button></div>
 </div>
 <div class="log-window" id="logWindow"><div class="log-empty" id="logEmpty">Nessun evento registrato.</div></div>
</div>

<dialog id="providerDialog"><h3>Aggiungi provider</h3><div class="grid">
<label class="field"><span>Preset</span><select id="preset"></select></label>
<label class="field"><span>Nome</span><input id="providerName" placeholder="OpenRouter"></label>
<label class="field full"><span>Base URL</span><input id="baseUrl" placeholder="https://..."></label>
<label class="field full" id="apiKeyField"><span>API key</span><input id="apiKey" type="password" placeholder="opzionale per endpoint locali"></label>
<label class="field full" id="authPathField" hidden><span>Percorso auth.json (vuoto = ~/.codex/auth.json)</span><input id="authPath" placeholder="~/.codex/auth.json"></label>
</div><div class="muted" id="presetNote" hidden style="margin-top:10px;font-size:13px"></div><div class="dialog-actions"><button data-close="providerDialog">Annulla</button><button class="primary" id="saveProvider">Salva</button></div></dialog>

<dialog id="editModelDialog"><h3>Modifica modello</h3><input type="hidden" id="editRouteId"><div class="grid">
<label class="field full"><span>Provider</span><select id="editRouteProvider"></select></label>
<label class="field full"><span>ID modello</span><input id="editRouteModel"></label>
<label class="field full"><span>Etichetta</span><input id="editRouteLabel"></label>
</div><div class="muted" style="margin-top:10px;font-size:13px">L'etichetta determina l'id visto dall'agente in /v1/models: se la cambi, aggiorna anche la configurazione del client.</div><div class="dialog-actions"><button data-close="editModelDialog">Annulla</button><button class="primary" id="saveEditModel">Salva</button></div></dialog>

<dialog id="editProviderDialog"><h3>Modifica provider</h3><input type="hidden" id="editProviderId"><div class="grid">
<label class="field"><span>Nome</span><input id="editProviderName"></label>
<label class="field"><span>Base URL</span><input id="editProviderUrl"></label>
<label class="field full"><span>API key</span><input id="editProviderKey" type="password" placeholder="lascia vuoto per non modificare"></label>
</div><div class="row" style="margin-top:10px"><button class="small danger" id="clearProviderKey">Rimuovi chiave salvata</button></div><div class="dialog-actions"><button data-close="editProviderDialog">Annulla</button><button class="primary" id="saveEditProvider">Salva</button></div></dialog>

<dialog id="modelDialog"><h3 id="modelTitle">Aggiungi modello</h3><input type="hidden" id="modelProviderId"><div class="grid">
<label class="field full"><span>ID modello</span><input id="modelId" placeholder="es. openai/gpt-oss-120b"></label>
<label class="field full"><span>Etichetta (opzionale)</span><input id="modelLabel" placeholder="Nome leggibile"></label>
</div><div class="row" style="margin-top:12px"><button id="discoverModels">Recupera modelli dal provider</button><span class="muted" id="discoverStatus"></span></div><div id="discoverList" class="discover" hidden></div><div class="dialog-actions"><button data-close="modelDialog">Annulla</button><button class="primary" id="saveModel">Aggiungi</button></div></dialog>
<dialog id="presetDialog"><h3 id="presetTitle">Nuovo preset</h3><input type="hidden" id="presetId"><div class="grid">
<label class="field full"><span>Nome del preset</span><input id="presetLabel" placeholder="es. Veloci"></label>
</div><div class="muted" style="margin-top:10px;font-size:13px">Il nome determina l'id visto dall'agente in /v1/models: se lo cambi, aggiorna anche la configurazione del client.</div><div class="dialog-actions"><button data-close="presetDialog">Annulla</button><button class="primary" id="savePreset">Salva</button></div></dialog>

<dialog id="memberDialog"><h3>Aggiungi modello al preset</h3><input type="hidden" id="memberPresetId"><div class="muted" style="margin-bottom:10px;font-size:13px">I membri vengono provati nell'ordine in cui compaiono nel preset; puoi riordinarli dopo con le frecce.</div><div class="picker" id="memberPicker"></div><div class="dialog-actions"><button data-close="memberDialog">Chiudi</button></div></dialog>

<footer class="footer"><span>© 2026 Davide (gat) · CC BY-NC 4.0</span><span>GISELL Router v0.3.0</span></footer>

<script>
let state=null;
const $=s=>document.querySelector(s);
const LANG_KEY='gisell-lang';
let lang=localStorage.getItem(LANG_KEY)==='en'?'en':'it';
const originals=new WeakMap();
const attrOriginals=new WeakMap();
const EN_REPL=[
 ['Benvenuto in GISELL Router!','Welcome to GISELL Router!'],
 ['API locale','Local API'],['Modelli','Models'],['Configurazione','Configuration'],
 ['In attesa di richieste…','Waiting for requests…'],['Nessuna richiesta ancora ricevuta.','No requests received yet.'],
 ['Forza il modello attivo','Force active model'],['Modello attivo','Active model'],
 ['Richieste sessione','Session requests'],['Ordine dei modelli in uso','Model failover order'],
 ['Priorità di failover dall’alto verso il basso.','Failover priority from top to bottom.'],
 ['Testa tutti','Test all'],['Gestione di provider, endpoint, credenziali e modelli configurati.','Manage configured providers, endpoints, credentials and models.'],
 ['Provider e modelli','Providers and models'],['+ Aggiungi provider','+ Add provider'],['+ Aggiungi preset','+ Add preset'],
 ['Un preset raggruppa più modelli sotto un unico nome. Nell’ordine di failover occupa una posizione come un modello: quando la catena lo raggiunge prova i suoi membri nell’ordine indicato, poi passa alla voce successiva. L’agente lo vede in /v1/models come un modello qualsiasi.','A preset groups multiple models under one name. In the failover order it occupies one position like a model: when reached, its members are tried in order, then routing continues with the next global item. The agent sees it in /v1/models like any other model.'],
 ['Avvio automatico Linux','Linux autostart'],['Verifica servizio…','Checking service…'],
 ['Installa GISELL Router come servizio systemd dell’utente. Con linger attivo può partire al boot anche prima del login.','Install GISELL Router as a user systemd service. With linger enabled it can start at boot even before login.'],
 ['Installa servizio','Install service'],['Rimuovi servizio','Remove service'],['Abilita avvio al boot','Enable boot startup'],['Disabilita avvio al boot','Disable boot startup'],
 ['Collegamento dei client OpenAI-compatible al router locale e protezione opzionale con chiave API.','Connect OpenAI-compatible clients to the local router and optionally protect it with an API key.'],
 ['Collegarsi al router','Connect to the router'],['Imposta come Base URL l’indirizzo indicato qui sotto.','Set the address below as the Base URL.'],
 ['Usa router come modello per seguire il modello attivo e il failover configurato.','Use router as the model to follow the active item and configured failover.'],
 ['La chiave API non è richiesta.','An API key is not required.'],['Esempio curl','curl example'],['Esempio Python (SDK OpenAI)','Python example (OpenAI SDK)'],
 ['Chiave API locale','Local API key'],['Nessuna chiave: accesso libero','No key: open access'],
 ['Se crei una chiave, tutte le chiamate OpenAI-compatible al router richiederanno Authorization: Bearer <chiave>. Se la rimuovi, l’autenticazione viene disattivata.','If you create a key, all OpenAI-compatible calls to the router require Authorization: Bearer <key>. Removing it disables authentication.'],
 ['Crea chiave','Create key'],['Rimuovi chiave','Remove key'],['Nuova chiave — copiala ora','New key — copy it now'],
 ['Per sicurezza la chiave salvata non viene mostrata di nuovo dopo il reload. Puoi sempre rimuoverla o generarne una nuova.','For security, the saved key is not shown again after reload. You can remove it or generate a new one at any time.'],
 ['Log live della sessione corrente: richieste, tentativi, failover, risposte valide ed errori restituiti dai modelli/provider.','Live log for the current session: requests, attempts, failovers, valid responses and model/provider errors.'],
 ['Eventi sessione','Session events'],['Cancella log','Clear log'],['Nessun evento registrato.','No events recorded.'],
 ['Aggiungi provider','Add provider'],['Nome','Name'],['Percorso auth.json (vuoto = ~/.codex/auth.json)','auth.json path (empty = ~/.codex/auth.json)'],
 ['Annulla','Cancel'],['Salva','Save'],['Modifica modello','Edit model'],['Etichetta','Label'],
 ["L'etichetta determina l'id visto dall'agente in /v1/models: se la cambi, aggiorna anche la configurazione del client.","The label determines the id exposed to the agent in /v1/models; if you change it, update the client configuration too."],
 ['Modifica provider','Edit provider'],['lascia vuoto per non modificare','leave empty to keep unchanged'],['Rimuovi chiave salvata','Remove saved key'],
 ['Aggiungi modello','Add model'],['Etichetta (opzionale)','Label (optional)'],['Nome leggibile','Readable name'],['Recupera modelli dal provider','Fetch models from provider'],
 ['Nuovo preset','New preset'],['Nome del preset','Preset name'],['Il nome determina l’id visto dall’agente in /v1/models: se lo cambi, aggiorna anche la configurazione del client.','The name determines the id exposed to the agent in /v1/models; if you change it, update the client configuration too.'],
 ['Aggiungi modello al preset','Add model to preset'],["I membri vengono provati nell'ordine in cui compaiono nel preset; puoi riordinarli dopo con le frecce.",'Members are tried in preset order; you can reorder them later with the arrows.'],['Chiudi','Close'],
 ['Copia','Copy'],['ATTIVO','ACTIVE'],['ERRORE','ERROR'],['NON TESTATO','NOT TESTED'],['DISABILITATO','DISABLED'],
 ['chiave salvata','key saved'],['nessuna chiave','no key'],['login non verificato','login not checked'],['+ Modello','+ Model'],['Modifica','Edit'],['Verifica login','Check login'],['Disabilita','Disable'],['Abilita','Enable'],['Elimina','Delete'],
 ['Nessun provider. Aggiungine uno e poi inserisci uno o più modelli.','No providers. Add one, then add one or more models.'],['Nessun modello configurato.','No models configured.'],['Aggiungi almeno un modello.','Add at least one model.'],['nessun membro','no members'],['modelli utilizzabili','available models'],['id API:','API id:'],['Usa ora','Use now'],
 ['Chiave attiva: autenticazione richiesta','Key active: authentication required'],['Rigenera chiave','Regenerate key'],['Inserisci la chiave API del router nel client.','Enter the router API key in the client.'],['La chiave API non è richiesta; lascia il campo vuoto se il client lo permette.','The API key is not required; leave it empty if the client allows it.'],
 ['righe','rows'],['nessuna richiesta','no requests'],['richieste','requests'],['fallite','failed'],['media','avg'],['ultimo uso','last used'],
 ['inattivo · ultima risposta da','idle · last response from'],['nessun modello richiesto','no model requested'],['risposta singola','single response'],['pronto · la prossima richiesta partirà da','ready · next request will start from'],['nessun modello attivo','no active model'],['errori','errors'],['in corso','in progress'],
 ['Preset creato: aggiungi i modelli con "+ Modello".','Preset created: add models with "+ Model".'],['Preset rinominato.','Preset renamed.'],['Dai un nome al preset.','Give the preset a name.'],['Tutti i modelli sono già nel preset.','All models are already in the preset.'],
 ['Provider salvato.','Provider saved.'],['Modello aggiunto.','Model added.'],['Indirizzo API copiato.','API address copied.'],['Base URL copiata.','Base URL copied.'],['Nuova chiave API creata.','New API key created.'],['Chiave API rimossa: autenticazione disattivata.','API key removed: authentication disabled.'],['Chiave copiata.','Key copied.'],['Log della sessione cancellato.','Session log cleared.'],['Login Codex valido.','Codex login valid.'],['Modello impostato come attivo.','Active item selected.'],['Test riuscito:','Test successful:'],['Test fallito','Test failed'],['Modello aggiornato.','Model updated.'],['Provider aggiornato.','Provider updated.'],['Chiave rimossa.','Key removed.'],
 ['Router avviato','Router started'],['Nuova richiesta','New request'],['Tentativo #','Attempt #'],['Modello fallito','Model failed'],['Risposta valida in','Valid response in'],
 ['Servizio non installato','Service not installed'],['Servizio installato','Service installed'],['abilitato','enabled'],['disabilitato','disabled'],['attivo','active'],['inattivo','inactive'],['linger attivo','linger enabled'],['linger disattivo','linger disabled'],
 ['Servizio installato e abilitato per i prossimi avvii.','Service installed and enabled for future starts.'],['Servizio rimosso.','Service removed.'],['Avvio al boot abilitato.','Boot startup enabled.'],['Avvio al boot disabilitato.','Boot startup disabled.'],
 ['Imposta come ','Set '],[" l'indirizzo indicato qui sotto.",' to the address shown below.'],[' come modello per seguire il modello attivo e il failover configurato.',' as the model to follow the active item and configured failover.'],
 ['Se crei una chiave, tutte le chiamate OpenAI-compatible al router richiederanno ','If you create a key, all OpenAI-compatible calls to the router require '],[". Se la rimuovi, l'autenticazione viene disattivata.",'. Removing it disables authentication.'],
 ['Inserisci la ','Enter the '],['chiave API','API key'],[' del router nel client.',' in the client.'],[' non è richiesta',' is not required'],['; lascia il campo vuoto se il client lo permette.','; leave the field empty if the client allows it.'],
 ['abilitati','enabled'],['Nessun preset configurato.','No presets configured.'],['modelli raggiungibili.','models reachable.'],
 ['il client ha chiesto','the client requested'],['in parallelo','in parallel'],['in attesa della risposta','waiting for response'],['in attesa del primo token','waiting for first token'],['streaming in corso','streaming in progress'],
 ['systemd non disponibile','systemd unavailable'],['Questa funzione richiede Linux con systemd user services.','This feature requires Linux with systemd user services.'],
 ['Rigenerare la chiave? La chiave attuale smetterà immediatamente di funzionare.','Regenerate the key? The current key will stop working immediately.'],['Rimuovere la chiave API? Il router tornerà accessibile senza autenticazione.','Remove the API key? The router will become accessible without authentication.'],
 ['Eliminare provider e tutti i suoi modelli?','Delete the provider and all its models?'],['Eliminare il preset? I modelli che contiene restano nella lista.','Delete the preset? Its models remain in the global list.'],['Rimuovere la chiave salvata per questo provider?','Remove the saved key for this provider?'],
 ['Il modello attivo ora vince sulla richiesta del client.','The active item now overrides the client request.'],['Il client può di nuovo scegliere il modello.','The client can choose the model again.'],
 ['Test…','Testing…'],['caricamento…','loading…'],['verifica…','checking…'],['verifica fallita','check failed'],['login attivo','login active'],['scade fra','expires in'],['problema:','issue:'],
 ["Richiede 'codex login'. Endpoint interno non documentato: usalo solo con il tuo account.","Requires 'codex login'. Undocumented internal endpoint: use it only with your own account."],
 ['Usa una Gemini API key creata in Google AI Studio. Endpoint OpenAI-compatible ufficiale di Google.','Use a Gemini API key created in Google AI Studio. Official Google OpenAI-compatible endpoint.'],
 ['Se attivo, il modello scelto qui vince anche se l’agente ne chiede un altro.','When enabled, the selected model wins even if the agent requests another one.'],
 ["Se attivo, il modello scelto qui vince anche se l'agente ne chiede un altro.","When enabled, the selected model wins even if the agent requests another one."],
 ['Sezioni router','Router sections'],['Usa ','Use '],['modelli','models']
];
function enText(v){let s=String(v??'');for(const [a,b] of EN_REPL)s=s.split(a).join(b);return s}
function translateRaw(raw){if(lang==='it')return raw;return enText(raw)}
function localize(root=document.body){
 document.documentElement.lang=lang;
 const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
 let n;while(n=walker.nextNode()){
  const p=n.parentElement;if(!p||p.closest('script,style,pre,code'))continue;
  if(!originals.has(n))originals.set(n,n.nodeValue);
  n.nodeValue=lang==='it'?originals.get(n):translateRaw(originals.get(n));
 }
 root.querySelectorAll?.('[title],[placeholder],[aria-label]').forEach(el=>{
  let bag=attrOriginals.get(el)||{};
  for(const a of ['title','placeholder','aria-label'])if(el.hasAttribute(a)){
   const cur=el.getAttribute(a)||'',old=bag[a],oldEn=old?enText(old):null;
   if(!old||(cur!==old&&cur!==oldEn))bag[a]=cur;
   el.setAttribute(a,lang==='it'?bag[a]:enText(bag[a]));
  }
  attrOriginals.set(el,bag);
 });
 const b=$('#langBtn');if(b){b.textContent=lang==='it'?'EN':'IT';b.title=lang==='it'?"Passa all'inglese":'Switch to Italian'}
}
function setLanguage(next){lang=next==='en'?'en':'it';localStorage.setItem(LANG_KEY,lang);localize()}
function ask(msg){return window['confirm'](lang==='en'?enText(msg):msg)}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function flash(msg,ok=true){const e=$('#flash');e.textContent=lang==='en'?enText(msg):msg;e.className='flash '+(ok?'ok':'bad');const shown=e.textContent;setTimeout(()=>{if(e.textContent===shown)e.textContent=''},5000)}
async function api(url,opt={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opt});let d;try{d=await r.json()}catch{d={detail:await r.text()}}if(!r.ok)throw new Error(d.detail||d.error?.message||d.error||JSON.stringify(d));return d}
function providerOf(id){return state.providers.find(p=>p.id===id)}
function switchTab(id){document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('active',p.id===id));document.querySelectorAll('.tab-btn').forEach(b=>{const on=b.dataset.tab===id;b.classList.toggle('active',on);b.setAttribute('aria-selected',on?'true':'false')});localStorage.setItem('gisell-tab',id)}
function duration(sec){sec=Math.max(0,Math.floor(sec||0));if(sec<60)return sec+' s';const m=Math.floor(sec/60);if(m<60)return m+' min';const h=Math.floor(m/60);if(h<24)return h+' h '+(m%60)+' min';const d=Math.floor(h/24);return d+' g '+(h%24)+' h'}
function healthTag(route){const h=state.health[route.id]||{};if(h.status==='ok')return `<span class="tag ok">OK${h.latency_ms?' · '+h.latency_ms+' ms':''}</span>`;if(h.status==='error')return '<span class="tag bad" title="'+esc(h.last_error||'')+'">ERRORE</span>';return '<span class="tag">NON TESTATO</span>'}
function render(){
 $('#apiUrl').textContent=state.local_api;
 const pe=$('#providers'); pe.innerHTML='';
 if(!state.providers.length) pe.innerHTML='<div class="empty">Nessun provider. Aggiungine uno e poi inserisci uno o più modelli.</div>';
 state.providers.forEach(p=>{
   const rs=state.routes.filter(r=>r.provider_id===p.id);
   const c=document.createElement('div'); c.className='card';
   c.innerHTML=`<div class="provider-head"><div><div class="provider-name">${esc(p.name)} ${p.enabled?'':'<span class="tag off">DISABILITATO</span>'}</div><div class="muted">${esc(p.base_url)} · ${p.auth_mode==='codex_oauth'?'login OAuth Codex':(p.has_api_key?'chiave salvata':'nessuna chiave')}</div>${p.auth_mode==='codex_oauth'?`<div class="stat" data-codex="${p.id}">login non verificato</div>`:''}</div><div class="row"><button class="small" data-action="add-model" data-id="${p.id}">+ Modello</button><button class="small" data-action="edit-provider" data-id="${p.id}">Modifica</button>${p.auth_mode==='codex_oauth'?`<button class="small" data-action="codex-check" data-id="${p.id}">Verifica login</button>`:''}<button class="small" data-action="toggle-provider" data-id="${p.id}">${p.enabled?'Disabilita':'Abilita'}</button><button class="small danger" data-action="delete-provider" data-id="${p.id}">Elimina</button></div></div><div class="models">${rs.length?rs.map(r=>`<div class="model"><div><b>${esc(r.label)}</b><div class="muted">${esc(r.model)}</div><code class="mid">id API: ${esc(r.exposed_id)}</code><div class="stat" data-stat="${r.id}">${statLine(r.id)}</div></div><div class="row">${healthTag(r)} ${r.enabled?'':'<span class="tag off">OFF</span>'}<button class="small" data-action="edit-route" data-id="${r.id}">Modifica</button><button class="small" data-action="toggle-route" data-id="${r.id}">${r.enabled?'Disabilita':'Abilita'}</button><button class="small" data-action="test" data-id="${r.id}">Test</button><button class="small danger" data-action="delete-route" data-id="${r.id}">×</button></div></div>`).join(''):'<div class="muted">Nessun modello configurato.</div>'}</div>`;
   pe.appendChild(c);
 });
 const re=$('#routes');
 if(!state.routes.length){re.innerHTML='<div class="empty">Aggiungi almeno un modello.</div>'} else re.innerHTML=state.routes.map((r,i)=>{
   const nav=`<button class="small" data-action="up" data-id="${r.id}" ${i===0?'disabled':''}>↑</button><button class="small" data-action="down" data-id="${r.id}" ${i===state.routes.length-1?'disabled':''}>↓</button>`;
   if(r.kind==='preset'){
     const chips=(r.members||[]).map(m=>{const x=state.routes.find(z=>z.id===m);return x?`<span class="chip${x.enabled?'':' off'}">${esc(x.label)}</span>`:''}).join('')||'<span class="muted">nessun membro</span>';
     return `<div class="route preset-row"><div class="prio">${i+1}</div><div><b>${esc(r.label)}</b> <span class="tag">PRESET</span> <span class="tag active" data-live-active="${r.id}" hidden>ATTIVO</span>${r.enabled?'':' <span class="tag off">OFF</span>'}<div class="muted">${r.member_count||0} modelli utilizzabili · id API: <code class="mid">${esc(r.exposed_id)}</code></div><div class="preset-members">${chips}</div></div><div class="route-actions">${nav}<button class="small" data-action="activate" data-id="${r.id}" ${!r.enabled||!r.member_count?'disabled':''}>Usa ora</button></div></div>`;
   }
   const p=providerOf(r.provider_id);
   return `<div class="route"><div class="prio">${i+1}</div><div><b>${esc(p?.name||'?')} / ${esc(r.label)}</b><div class="muted">${esc(r.model)} <span class="tag active" data-live-active="${r.id}" hidden>ATTIVO</span> ${healthTag(r)}</div></div><div class="route-actions">${nav}<button class="small" data-action="activate" data-id="${r.id}" ${!r.enabled||!p?.enabled?'disabled':''}>Usa ora</button></div></div>`}).join('');
 renderPresets();
 renderApiTab();
 renderLive();
 localize();
}
function renderApiTab(){
 const enabled=!!state.router_api_key_enabled;
 const status=$('#routerKeyStatus');
 status.textContent=enabled?'Chiave attiva: autenticazione richiesta':'Nessuna chiave: accesso libero';
 status.className=enabled?'auth-on':'auth-off';
 $('#deleteRouterKey').disabled=!enabled;
 $('#createRouterKey').textContent=enabled?'Rigenera chiave':'Crea chiave';
 $('#apiBaseUrl').textContent=state.local_api;
 $('#authInstruction').innerHTML=enabled?'Inserisci la <b>chiave API</b> del router nel client.':'La <b>chiave API non è richiesta</b>; lascia il campo vuoto se il client lo permette.';
 const auth=enabled?'  -H "Authorization: Bearer LA_TUA_CHIAVE" \\n':'';
 $('#curlExample').textContent=`curl ${state.local_api}/chat/completions \\n  -H "Content-Type: application/json" \\n${auth}  -d '{"model":"router","messages":[{"role":"user","content":"Ciao"}]}'`;
 const pyKey=enabled?'LA_TUA_CHIAVE':'non-richiesta';
 $('#pythonExample').textContent=`from openai import OpenAI

client = OpenAI(
    base_url="${state.local_api}",
    api_key="${pyKey}",
)

response = client.chat.completions.create(
    model="router",
    messages=[{"role": "user", "content": "Ciao"}],
)

print(response.choices[0].message.content)`;
}

let logCursor=0;
function logTime(ts){try{return new Date((ts||0)*1000).toLocaleTimeString('it-IT',{hour12:false})}catch{return'--:--:--'}}
function appendLogRows(rows){
 const w=$('#logWindow'),empty=$('#logEmpty');
 if(!w||!rows?.length)return;
 if(empty)empty.remove();
 const stick=$('#logAutoScroll')?.checked && (w.scrollHeight-w.scrollTop-w.clientHeight<80);
 for(const x of rows){
  const source=[x.provider,x.label||x.model].filter(Boolean).join(' / ') || (x.request_id||x.kind||'router');
  const row=document.createElement('div');row.className='log-row';
  row.innerHTML=`<div class="log-time">${esc(logTime(x.at))}</div><div class="log-level ${esc(x.level||'info')}">${esc(x.level||'info')}</div><div class="log-source" title="${esc(source)}">${esc(source)}</div><div class="log-message">${esc(x.message||'')}${x.detail?`<span class="log-detail">${esc(x.detail)}</span>`:''}</div>`;
  w.appendChild(row);localize(row);
 }
 if(stick)w.scrollTop=w.scrollHeight;
}
async function pollLogs(){
 try{
  const d=await api('/api/logs?after='+logCursor);
  if(d.logs?.length)appendLogRows(d.logs);
  logCursor=Math.max(logCursor,d.last_id||0);
  const c=$('#logCount');if(c){c.textContent=(d.count||0)+' righe';localize(c)}
 }catch{}
}
async function clearLogs(){
 const d=await api('/api/logs',{method:'DELETE'});
 logCursor=d.last_id||logCursor;
 const w=$('#logWindow');w.innerHTML='<div class="log-empty" id="logEmpty">Nessun evento registrato.</div>';
 $('#logCount').textContent='0 righe';localize($('#logsTab'));
}

let live=null;
function ago(ts){if(!ts)return'';const d=Math.max(0,Date.now()/1000-ts);if(d<1)return lang==='en'?'now':'ora';if(d<60)return Math.round(d)+(lang==='en'?' s ago':' s fa');if(d<3600)return Math.round(d/60)+(lang==='en'?' min ago':' min fa');return Math.round(d/3600)+(lang==='en'?' h ago':' h fa')}
function statLine(rid){const st=live&&live.stats&&live.stats[rid];if(!st||!st.requests)return'nessuna richiesta';const parts=[st.requests+' richieste'];if(st.fail)parts.push(st.fail+' fallite');if(st.avg_latency_ms)parts.push('media '+st.avg_latency_ms+' ms');if(st.last_used)parts.push('ultimo uso '+ago(st.last_used));return parts.join(' · ')}
function updateUsageIndicators(){
 const activeIds=new Set((live?.in_flight||[]).map(x=>x.active_item_id).filter(Boolean));
 if(!activeIds.size){
  const idleId=live?.active_route_id||state?.active_route_id;
  if(idleId)activeIds.add(idleId);
 }
 document.querySelectorAll('[data-live-active]').forEach(el=>{
  el.hidden=!activeIds.has(el.dataset.liveActive);
 });
}
function renderLive(){
 if(!live)return;
 const bar=$('#live'),dot=$('#liveDot'),m=$('#liveModel'),sub=$('#liveSub');
 $('#overrideChk').checked=!!live.override_client_model;
 updateUsageIndicators();
 const f=live.in_flight;
 if(f.length){
  const c=f[0];
  bar.classList.add('busy');dot.className='dot busy';
  m.textContent=(c.label||c.model||'?')+(f.length>1?`  (+${f.length-1} in parallelo)`:'');
  const asked=c.client_model?`il client ha chiesto "${c.client_model}"`:'nessun modello richiesto';
  sub.textContent=`${c.provider||'?'} · ${c.model} · ${c.stream?'streaming':'risposta singola'} · ${c.phase} · ${(c.elapsed_ms/1000).toFixed(1)} s · ${asked}`;
 } else if(live.last_used){
  const l=live.last_used;
  bar.classList.remove('busy');dot.className='dot idle';
  m.textContent=l.label||l.model;
  sub.textContent=`inattivo · ultima risposta da ${l.provider} (${l.model}) ${ago(l.at)}${l.latency_ms?' · '+l.latency_ms+' ms':''}`;
 } else {
  bar.classList.remove('busy');dot.className='dot idle';
  m.textContent='In attesa di richieste…';
  const act=state&&state.routes.find(r=>r.id===live.active_route_id);
  sub.textContent=act?`pronto · la prossima richiesta partira\u2019 da ${act.label}`:'Nessuna richiesta ancora ricevuta.';
 }
 document.querySelectorAll('[data-stat]').forEach(e=>{e.textContent=statLine(e.dataset.stat)});
 const active=state&&state.routes.find(r=>r.id===live.active_route_id);
 const activeProvider=active&&providerOf(active.provider_id);
 const modelRoutes=state?state.routes.filter(r=>r.kind!=='preset'):[];
 const presetRoutes=state?state.routes.filter(r=>r.kind==='preset'):[];
 const enabledRoutes=modelRoutes.filter(r=>r.enabled&&providerOf(r.provider_id)?.enabled);
 const enabledProviders=state?state.providers.filter(p=>p.enabled):[];
 const statValues=Object.values(live.stats||{});
 const reqs=statValues.reduce((n,s)=>n+(s.requests||0),0);
 const fails=statValues.reduce((n,s)=>n+(s.fail||0),0);
 $('#statusRouter').textContent=f.length?'BUSY':'ONLINE';
 $('#statusRouter').className='status-value '+(f.length?'warn':'ok');
 $('#statusUptime').textContent='uptime '+duration((live.now||Date.now()/1000)-(live.started_at||live.now));
 $('#statusActive').textContent=active?.label||'—';
 $('#statusActiveProvider').textContent=active?(active.kind==='preset'?`preset · ${active.member_count||0} modelli`:(activeProvider?.name||'?')+' · '+active.model):'nessun modello attivo';
 $('#statusModels').textContent=enabledRoutes.length+' / '+modelRoutes.length;
 $('#statusModelsDetail').textContent=(presetRoutes.length?presetRoutes.length+' preset · ':'')+enabledProviders.length+'/'+(state?.providers.length||0)+' provider';
 $('#statusRequests').textContent=reqs;
 $('#statusErrors').textContent=fails+' errori · '+f.length+' in corso';
 localize($('#modelsTab'));
}
async function pollLive(){try{live=await api('/api/live');if(state&&live.active_route_id!==state.active_route_id){state.active_route_id=live.active_route_id;render()}renderLive()}catch{}}
function startPolling(){setInterval(()=>{if(!document.hidden){pollLive();pollLogs()}},1200);document.addEventListener('visibilitychange',()=>{if(!document.hidden){pollLive();pollLogs()}})}
async function refresh(){state=await api('/api/state');render();await Promise.all([pollLive(),pollLogs()])}
function fillPresets(){const s=$('#preset');s.innerHTML=Object.entries(state.presets).map(([k,v])=>`<option value="${k}">${esc(v.label)}</option>`).join('');applyPreset()}
function applyPreset(){const k=$('#preset').value,v=state.presets[k];if(!v)return;$('#providerName').value=v.label;$('#baseUrl').value=v.base_url;
 const oauth=v.auth_mode==='codex_oauth';
 $('#apiKeyField').hidden=oauth; $('#authPathField').hidden=!oauth;
 $('#presetNote').textContent=v.note||''; $('#presetNote').hidden=!v.note;localize($('#providerDialog'))}
$('#langBtn').addEventListener('click',()=>setLanguage(lang==='it'?'en':'it'));
document.querySelectorAll('.tab-btn').forEach(b=>b.addEventListener('click',()=>switchTab(b.dataset.tab)));
const savedTab=localStorage.getItem('gisell-tab');if(savedTab&&document.getElementById(savedTab))switchTab(savedTab);
$('#addProviderBtn').addEventListener('click',()=>{fillPresets();$('#apiKey').value='';$('#providerDialog').showModal()});
$('#preset').addEventListener('change',applyPreset);
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$('#'+b.dataset.close).close()));
$('#saveProvider').addEventListener('click',async()=>{try{state=await api('/api/providers',{method:'POST',body:JSON.stringify({preset:$('#preset').value,name:$('#providerName').value,base_url:$('#baseUrl').value,api_key:$('#apiKey').value,auth_path:$('#authPath').value})});$('#providerDialog').close();render();flash('Provider salvato.')}catch(e){flash(e.message,false)}});
$('#saveModel').addEventListener('click',async()=>{try{state=await api(`/api/providers/${$('#modelProviderId').value}/models`,{method:'POST',body:JSON.stringify({model:$('#modelId').value,label:$('#modelLabel').value})});$('#modelDialog').close();render();flash('Modello aggiunto.')}catch(e){flash(e.message,false)}});
function routeName(id){const r=state.routes.find(x=>x.id===id);if(!r)return id;if(r.kind==='preset')return r.label+' (preset)';const p=providerOf(r.provider_id);return (p?.name?p.name+' / ':'')+r.label}
function presetOf(id){return state.routes.find(r=>r.id===id&&r.kind==='preset')}
async function savePresetMembers(id,members){return await api('/api/presets/'+id,{method:'PATCH',body:JSON.stringify({members})})}
function renderPresets(){
 const box=$('#presets');box.innerHTML='';
 const presets=state.routes.filter(r=>r.kind==='preset');
 if(!presets.length){box.innerHTML='<div class="empty">Nessun preset configurato.</div>';return}
 presets.forEach(g=>{
  const members=(g.members||[]).map(m=>state.routes.find(x=>x.id===m)).filter(Boolean);
  const rows=members.length?members.map((m,i)=>{
   const p=providerOf(m.provider_id);
   const sub=m.kind==='preset'?`preset · ${m.member_count||0} modelli`:`${esc(p?.name||'?')} · ${esc(m.model)}`;
   return `<div class="model"><div><b>${esc(m.label)}</b>${m.enabled?'':' <span class="tag off">OFF</span>'}<div class="muted">${sub}</div></div><div class="config-model-actions"><span class="muted">${i+1}</span><button class="small" data-action="pm-up" data-id="${g.id}" data-mid="${m.id}" ${i===0?'disabled':''}>↑</button><button class="small" data-action="pm-down" data-id="${g.id}" data-mid="${m.id}" ${i===members.length-1?'disabled':''}>↓</button><button class="small danger" data-action="pm-del" data-id="${g.id}" data-mid="${m.id}">×</button></div></div>`
  }).join(''):'<div class="muted">Nessun modello nel preset: verrà saltato dal failover e non compare in /v1/models.</div>';
  const c=document.createElement('div');c.className='card';
  c.innerHTML=`<div class="provider-head"><div><div class="provider-name">${esc(g.label)} ${g.enabled?'':'<span class="tag off">DISABILITATO</span>'}</div><div class="muted">${g.member_count||0} modelli utilizzabili · id API: <code class="mid">${esc(g.exposed_id)}</code></div></div><div class="row"><button class="small" data-action="preset-add" data-id="${g.id}">+ Modello</button><button class="small" data-action="preset-rename" data-id="${g.id}">Rinomina</button><button class="small" data-action="toggle-route" data-id="${g.id}">${g.enabled?'Disabilita':'Abilita'}</button><button class="small danger" data-action="delete-route" data-id="${g.id}">Elimina</button></div></div><div class="models">${rows}</div>`;
  box.appendChild(c);
 });
}
function renderMemberPicker(){
 const g=presetOf($('#memberPresetId').value);if(!g)return;
 const avail=state.routes.filter(r=>r.id!==g.id&&!(g.members||[]).includes(r.id));
 $('#memberPicker').innerHTML=avail.length?avail.map(r=>`<div class="prow"><span class="pname" title="${esc(routeName(r.id))}">${esc(routeName(r.id))}</span>${r.enabled?'':'<span class="tag off">OFF</span>'}<button class="small" data-action="pm-add" data-id="${g.id}" data-mid="${r.id}">+</button></div>`).join(''):'<div class="empty">Tutti i modelli sono già nel preset.</div>';localize($('#memberDialog'));
}
let service=null;
function renderService(){
 const box=$('#serviceState'),detail=$('#serviceDetail'),install=$('#installServiceBtn'),remove=$('#removeServiceBtn'),linger=$('#lingerBtn');
 if(!service){box.textContent='Verifica servizio…';return}
 if(!service.linux||!service.systemd){box.textContent='systemd non disponibile';detail.textContent='Questa funzione richiede Linux con systemd user services.';install.disabled=true;remove.disabled=true;linger.disabled=true;localize($('#configTab'));return}
 box.textContent=service.installed?'Servizio installato':'Servizio non installato';
 detail.textContent=`${service.enabled?'abilitato':'disabilitato'} · ${service.active?'attivo':'inattivo'} · ${service.linger?'linger attivo':'linger disattivo'} · ${service.unit_path}`;
 install.disabled=service.installed&&service.enabled;remove.disabled=!service.installed;linger.disabled=!service.installed;linger.textContent=service.linger?'Disabilita avvio al boot':'Abilita avvio al boot';localize($('#configTab'));
}
async function refreshService(){try{service=await api('/api/service');renderService()}catch(e){service=null;$('#serviceState').textContent=e.message;localize($('#configTab'))}}
$('#installServiceBtn').addEventListener('click',async()=>{try{service=await api('/api/service',{method:'POST'});renderService();flash('Servizio installato e abilitato per i prossimi avvii.')}catch(e){flash(e.message,false)}});
$('#removeServiceBtn').addEventListener('click',async()=>{if(!ask(lang==='en'?'Remove the systemd service?':'Rimuovere il servizio systemd?'))return;try{service=await api('/api/service',{method:'DELETE'});renderService();flash('Servizio rimosso.')}catch(e){flash(e.message,false)}});
$('#lingerBtn').addEventListener('click',async()=>{try{service=await api('/api/service/linger',{method:service?.linger?'DELETE':'POST'});renderService();flash(service.linger?'Avvio al boot abilitato.':'Avvio al boot disabilitato.')}catch(e){flash(e.message,false)}});
$('#addPresetBtn').addEventListener('click',()=>{$('#presetTitle').textContent='Nuovo preset';$('#presetId').value='';$('#presetLabel').value='';$('#presetDialog').showModal()});
$('#savePreset').addEventListener('click',async()=>{
 const label=$('#presetLabel').value.trim();
 if(!label){flash('Dai un nome al preset.',false);return}
 const id=$('#presetId').value;
 try{
  state=id?await api('/api/presets/'+id,{method:'PATCH',body:JSON.stringify({label})})
          :await api('/api/presets',{method:'POST',body:JSON.stringify({label,members:[]})});
  $('#presetDialog').close();render();flash(id?'Preset rinominato.':'Preset creato: aggiungi i modelli con "+ Modello".');
 }catch(e){flash(e.message,false)}
});
$('#testAllBtn').addEventListener('click',async e=>{const b=e.currentTarget;b.disabled=true;b.textContent='Test…';try{const d=await api('/api/routes/test-all',{method:'POST'});state=d.state;render();const ok=Object.values(d.results).filter(x=>x.ok).length;flash(`${ok}/${Object.keys(d.results).length} modelli raggiungibili.`,ok>0)}catch(err){flash(err.message,false)}finally{b.disabled=false;b.textContent='Testa tutti'}});
$('#discoverModels').addEventListener('click',async()=>{const id=$('#modelProviderId').value;$('#discoverStatus').textContent='caricamento…';$('#discoverList').hidden=true;try{const d=await api(`/api/providers/${id}/discover-models`,{method:'POST'});$('#discoverStatus').textContent=d.models.length+' modelli';const list=$('#discoverList');list.innerHTML=d.models.map(m=>`<button type="button" data-model="${esc(m)}">${esc(m)}</button>`).join('');list.hidden=false}catch(e){$('#discoverStatus').textContent='';flash(e.message,false)}});
$('#discoverList').addEventListener('click',e=>{const b=e.target.closest('[data-model]');if(b){$('#modelId').value=b.dataset.model;$('#modelLabel').value=b.dataset.model;$('#discoverList').hidden=true}});
$('#copyApi').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(state.local_api);flash('Indirizzo API copiato.')}catch{flash('Copia manualmente: '+state.local_api,false)}});
$('#copyApiBase').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(state.local_api);flash('Base URL copiata.')}catch{flash('Copia manualmente: '+state.local_api,false)}});
$('#createRouterKey').addEventListener('click',async()=>{
 if(state.router_api_key_enabled&&!ask('Rigenerare la chiave? La chiave attuale smettera\' immediatamente di funzionare.'))return;
 try{const d=await api('/api/router-key',{method:'POST'});state.router_api_key_enabled=true;$('#newRouterKey').textContent=d.key;$('#newKeyArea').hidden=false;renderApiTab();flash('Nuova chiave API creata.')}catch(e){flash(e.message,false)}
});
$('#deleteRouterKey').addEventListener('click',async()=>{
 if(!ask('Rimuovere la chiave API? Il router tornera\' accessibile senza autenticazione.'))return;
 try{await api('/api/router-key',{method:'DELETE'});state.router_api_key_enabled=false;$('#newKeyArea').hidden=true;$('#newRouterKey').textContent='';renderApiTab();flash('Chiave API rimossa: autenticazione disattivata.')}catch(e){flash(e.message,false)}
});
$('#copyRouterKey').addEventListener('click',async()=>{const k=$('#newRouterKey').textContent;try{await navigator.clipboard.writeText(k);flash('Chiave copiata.')}catch{flash('Copia manualmente la chiave.',false)}});
$('#clearLogsBtn').addEventListener('click',async()=>{try{await clearLogs();flash('Log della sessione cancellato.')}catch(e){flash(e.message,false)}});
document.body.addEventListener('click',async e=>{const b=e.target.closest('[data-action]');if(!b)return;const a=b.dataset.action,id=b.dataset.id;try{
 if(a==='add-model'){const p=providerOf(id);$('#modelProviderId').value=id;$('#modelTitle').textContent='Aggiungi modello a '+p.name;$('#modelId').value='';$('#modelLabel').value='';$('#discoverList').hidden=true;$('#discoverStatus').textContent='';$('#modelDialog').showModal();return}
 if(a==='preset-rename'){const g=presetOf(id);$('#presetTitle').textContent='Rinomina preset';$('#presetId').value=id;$('#presetLabel').value=g.label;$('#presetDialog').showModal();return}
 if(a==='preset-add'){$('#memberPresetId').value=id;renderMemberPicker();$('#memberDialog').showModal();return}
 if(a==='pm-add'||a==='pm-del'||a==='pm-up'||a==='pm-down'){
  const g=presetOf(id),mid=b.dataset.mid,m=[...(g.members||[])],i=m.indexOf(mid);
  if(a==='pm-add'&&i<0)m.push(mid);
  if(a==='pm-del'&&i>=0)m.splice(i,1);
  if(a==='pm-up'&&i>0)m[i-1]=m.splice(i,1,m[i-1])[0];
  if(a==='pm-down'&&i>=0&&i<m.length-1)m[i+1]=m.splice(i,1,m[i+1])[0];
  state=await savePresetMembers(id,m);render();
  if(a==='pm-add')renderMemberPicker();
  return;
 }
 if(a==='edit-route'){const r=state.routes.find(x=>x.id===id);$('#editRouteId').value=id;$('#editRouteModel').value=r.model;$('#editRouteLabel').value=r.label;$('#editRouteProvider').innerHTML=state.providers.map(p=>`<option value="${p.id}"${p.id===r.provider_id?' selected':''}>${esc(p.name)}</option>`).join('');$('#editModelDialog').showModal();return}
 if(a==='edit-provider'){const p=providerOf(id);$('#editProviderId').value=id;$('#editProviderName').value=p.name;$('#editProviderUrl').value=p.base_url;$('#editProviderKey').value='';$('#editProviderKey').placeholder=p.has_api_key?'chiave salvata — lascia vuoto per non modificare':'nessuna chiave salvata';$('#editProviderDialog').showModal();return}
 if(a==='codex-check'){const el=document.querySelector(`[data-codex="${id}"]`);if(el)el.textContent='verifica…';try{const d=await api(`/api/providers/${id}/codex-status`);if(el)el.textContent=d.ok?`login attivo · account ${d.account_id||'?'} · scade fra ${Math.max(0,Math.round((d.expires_in_s||0)/60))} min`:('problema: '+d.error);flash(d.ok?'Login Codex valido.':d.error,d.ok)}catch(err){if(el)el.textContent='verifica fallita';flash(err.message,false)}return}
 if(a==='delete-provider'){if(!ask('Eliminare provider e tutti i suoi modelli?'))return;state=await api('/api/providers/'+id,{method:'DELETE'})}
 if(a==='toggle-provider'){const p=providerOf(id);state=await api('/api/providers/'+id,{method:'PATCH',body:JSON.stringify({enabled:!p.enabled})})}
 if(a==='delete-route'){const r=state.routes.find(x=>x.id===id);if(r?.kind==='preset'&&!ask('Eliminare il preset? I modelli che contiene restano nella lista.'))return;state=await api('/api/routes/'+id,{method:'DELETE'})}
 if(a==='toggle-route'){const r=state.routes.find(x=>x.id===id);const url=(r.kind==='preset'?'/api/presets/':'/api/routes/')+id;state=await api(url,{method:'PATCH',body:JSON.stringify({enabled:!r.enabled})})}
 if(a==='up'||a==='down'){state=await api(`/api/routes/${id}/move`,{method:'POST',body:JSON.stringify({direction:a==='up'?-1:1})})}
 if(a==='activate'){state=await api(`/api/routes/${id}/activate`,{method:'POST'});flash('Modello impostato come attivo.')}
 if(a==='test'){b.disabled=true;b.textContent='Test…';const d=await api(`/api/routes/${id}/test`,{method:'POST'});state=d.state;if(d.ok)flash('Test riuscito: '+d.latency_ms+' ms');else flash(d.error||'Test fallito',false)}
 render();
 }catch(err){flash(err.message,false)}finally{if(a==='test'){b.disabled=false;b.textContent='Test'}}});
$('#overrideChk').addEventListener('change',async e=>{try{state=await api('/api/settings',{method:'PATCH',body:JSON.stringify({override_client_model:e.target.checked})});flash(e.target.checked?'Il modello attivo ora vince sulla richiesta del client.':'Il client puo\u2019 di nuovo scegliere il modello.')}catch(err){flash(err.message,false);e.target.checked=!e.target.checked}});
$('#saveEditModel').addEventListener('click',async()=>{try{state=await api('/api/routes/'+$('#editRouteId').value,{method:'PATCH',body:JSON.stringify({model:$('#editRouteModel').value,label:$('#editRouteLabel').value,provider_id:$('#editRouteProvider').value})});$('#editModelDialog').close();render();flash('Modello aggiornato.')}catch(e){flash(e.message,false)}});
$('#saveEditProvider').addEventListener('click',async()=>{try{const b={name:$('#editProviderName').value,base_url:$('#editProviderUrl').value};const k=$('#editProviderKey').value;if(k)b.api_key=k;state=await api('/api/providers/'+$('#editProviderId').value,{method:'PATCH',body:JSON.stringify(b)});$('#editProviderDialog').close();render();flash('Provider aggiornato.')}catch(e){flash(e.message,false)}});
$('#clearProviderKey').addEventListener('click',async()=>{if(!ask('Rimuovere la chiave salvata per questo provider?'))return;try{state=await api('/api/providers/'+$('#editProviderId').value,{method:'PATCH',body:JSON.stringify({api_key:''})});$('#editProviderDialog').close();render();flash('Chiave rimossa.')}catch(e){flash(e.message,false)}});
localize();refresh().then(()=>{startPolling();refreshService()}).catch(e=>flash(e.message,false));
</script></main></body></html>'''


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
