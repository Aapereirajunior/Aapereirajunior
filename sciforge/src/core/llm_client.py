"""Roteador LLM: Gemini API, Claude (SDK oficial `anthropic`), OpenAI, OpenRouter e Ollama local.

Responsabilidades:
- roteamento por FASE e por papel (default, writer, reviewer, vision, local...) definido em
  settings.yaml (`llm.phases` tem prioridade sobre `llm.roles`);
- aplicação do privacy_mode (Trava 5): chamadas marcadas `sensitive=True` ou feitas nas fases
  protegidas só podem ir a um provedor local; em `strict`, nenhuma chamada sai da máquina;
- contagem de tokens (usage real das APIs, estimativa como fallback) e custo, via BudgetGuard;
- firewall anti-retalhos (Trava 1): um `prompt_guard` inspeciona todo prompt das Fases 2-6;
- extração robusta de JSON e validação contra contratos Pydantic com re-tentativa.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .budget_guard import BudgetGuard

T = TypeVar("T", bound=BaseModel)

# Fases que manipulam dados individuais/logs de execução: só modelo local em strict/hybrid.
PROTECTED_PHASES = {"phase1", "phase4"}


class PrivacyViolation(RuntimeError):
    """Uma chamada a provedor de nuvem foi bloqueada pelo privacy_mode."""


class LLMError(RuntimeError):
    """Erro de comunicação com o provedor ou resposta inválida."""


class LLMRefusal(LLMError):
    """O modelo recusou a requisição (stop_reason = refusal) mesmo após os fallbacks."""


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str


@dataclass
class LLMRequest:
    system: str
    user: str
    images: list[Path] = field(default_factory=list)
    json_mode: bool = False
    temperature: float = 0.3
    max_tokens: int = 8192


class Provider(Protocol):
    name: str
    local: bool

    async def generate(self, model: str, req: LLMRequest) -> LLMResponse:
        """Envia a requisição ao provedor e devolve texto + contagem de tokens."""


def estimate_tokens(text: str) -> int:
    """Estimativa conservadora (≈4 caracteres por token) usada quando a API não informa usage."""
    return max(1, len(text or "") // 4)


def _image_b64(path: Path) -> tuple[str, str]:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return mime, base64.b64encode(Path(path).read_bytes()).decode("ascii")


async def _post_with_retry(client: httpx.AsyncClient, url: str, *, json_body: dict, headers: dict | None = None,
                           params: dict | None = None, retries: int = 4) -> dict:
    delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.post(url, json=json_body, headers=headers, params=params)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code} em {url.split('?')[0]}: {resp.text[:500]}")
            return resp.json()
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt >= retries:
                break
            await asyncio.sleep(delay)
            delay *= 2
    raise LLMError(f"Falha de rede após {retries + 1} tentativas: {last_exc}")


class GeminiProvider:
    local = False

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.api_key = os.environ.get(cfg.get("api_key_env", "GEMINI_API_KEY"), "")
        self.base_url = cfg.get("base_url", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.timeout = float(cfg.get("timeout_s", 180))

    async def generate(self, model: str, req: LLMRequest) -> LLMResponse:
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY não configurada")
        parts: list[dict[str, Any]] = [{"text": req.user}]
        for img in req.images:
            mime, data = _image_b64(img)
            parts.append({"inline_data": {"mime_type": mime, "data": data}})
        body: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": req.system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": req.temperature, "maxOutputTokens": req.max_tokens},
        }
        if req.json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            data = await _post_with_retry(
                client, f"{self.base_url}/models/{model}:generateContent",
                json_body=body, headers={"x-goog-api-key": self.api_key})
        try:
            cand = data["candidates"][0]
            text = "".join(p.get("text", "") for p in cand["content"]["parts"])
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Resposta Gemini inesperada: {json.dumps(data)[:500]}") from exc
        usage = data.get("usageMetadata", {})
        return LLMResponse(text, int(usage.get("promptTokenCount", estimate_tokens(req.system + req.user))),
                           int(usage.get("candidatesTokenCount", estimate_tokens(text))), self.name, model)


class OpenAICompatProvider:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.local = bool(cfg.get("local", False))
        self.api_key = os.environ.get(cfg.get("api_key_env", "OPENAI_API_KEY"), "")
        base_env = cfg.get("base_url_env")
        self.base_url = (os.environ.get(base_env) if base_env else None) or cfg.get(
            "base_url", "https://api.openai.com/v1")
        self.base_url = self.base_url.rstrip("/")
        self.timeout = float(cfg.get("timeout_s", 180))
        # OpenRouter (e outros gateways) aceitam cabeçalhos extras de identificação do app.
        self.extra_headers = {str(k): str(v) for k, v in (cfg.get("extra_headers") or {}).items()}
        self.json_mode_supported = bool(cfg.get("json_mode", True))

    async def generate(self, model: str, req: LLMRequest) -> LLMResponse:
        if not self.api_key and not self.local:
            raise LLMError(f"Chave de API ausente para o provedor {self.name}")
        content: Any = req.user
        if req.images:
            content = [{"type": "text", "text": req.user}]
            for img in req.images:
                mime, data = _image_b64(img)
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": content}],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.json_mode and self.json_mode_supported:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        headers.update(self.extra_headers)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            data = await _post_with_retry(client, f"{self.base_url}/chat/completions", json_body=body,
                                          headers=headers)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Resposta OpenAI-compat inesperada: {json.dumps(data)[:500]}") from exc
        usage = data.get("usage", {})
        return LLMResponse(text, int(usage.get("prompt_tokens", estimate_tokens(req.system + req.user))),
                           int(usage.get("completion_tokens", estimate_tokens(text))), self.name, model)


class OllamaProvider:
    local = True

    def __init__(self, name: str, cfg: dict):
        self.name = name
        host_env = cfg.get("host_env", "OLLAMA_HOST")
        self.base_url = (os.environ.get(host_env) or cfg.get("base_url", "http://localhost:11434")).rstrip("/")
        if not self.base_url.startswith("http"):
            self.base_url = "http://" + self.base_url
        self.timeout = float(cfg.get("timeout_s", 900))

    async def generate(self, model: str, req: LLMRequest) -> LLMResponse:
        user_msg: dict[str, Any] = {"role": "user", "content": req.user}
        if req.images:
            user_msg["images"] = [_image_b64(p)[1] for p in req.images]
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": req.system}, user_msg],
            "stream": False,
            "options": {"temperature": req.temperature, "num_predict": req.max_tokens, "num_ctx": 32768},
        }
        if req.json_mode:
            body["format"] = "json"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            data = await _post_with_retry(client, f"{self.base_url}/api/chat", json_body=body, retries=2)
        text = (data.get("message") or {}).get("content", "")
        return LLMResponse(text, int(data.get("prompt_eval_count", estimate_tokens(req.system + req.user))),
                           int(data.get("eval_count", estimate_tokens(text))), self.name, model)


class AnthropicProvider:
    """Claude via SDK oficial `anthropic` (streaming + fallback de recusa no servidor).

    - Não envia `temperature` (removido nos modelos atuais) nem `thinking` (adaptativo por padrão).
    - Profundidade controlada por `output_config.effort` (configurável por provedor; padrão "high").
    - `fallbacks: "default"` (beta server-side-fallback-2026-07-01) reexecuta automaticamente uma
      requisição recusada em outro modelo; defina `fallbacks: null` no settings.yaml para desativar.
    """

    local = False
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, name: str, cfg: dict, client: Any = None):
        self.name = name
        self.api_key_env = cfg.get("api_key_env", "ANTHROPIC_API_KEY")
        self.timeout = float(cfg.get("timeout_s", 600))
        self.effort = cfg.get("effort", "high")
        self.fallbacks = cfg.get("fallbacks", "default")
        self.base_url = cfg.get("base_url")
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # dependência declarada em pyproject.toml
                raise LLMError("Pacote `anthropic` ausente: pip install anthropic") from exc
            kwargs: dict[str, Any] = {"timeout": self.timeout, "max_retries": 4}
            if os.environ.get(self.api_key_env):
                kwargs["api_key"] = os.environ[self.api_key_env]
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    async def generate(self, model: str, req: LLMRequest) -> LLMResponse:
        content: list[dict[str, Any]] = []
        for img in req.images:
            mime, data = _image_b64(img)
            content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
        user_text = req.user
        if req.json_mode:
            user_text += "\n\nResponda somente com JSON válido, sem texto fora do objeto."
        content.append({"type": "text", "text": user_text})
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max(req.max_tokens, 16000),
            "system": req.system,
            "messages": [{"role": "user", "content": content}],
        }
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            params["betas"] = [self.FALLBACK_BETA]
            params["fallbacks"] = self.fallbacks
        try:
            async with self.client().beta.messages.stream(**params) as stream:
                message = await stream.get_final_message()
        except LLMError:
            raise
        except Exception as exc:  # erros tipados do SDK (status, conexão) viram LLMError
            raise LLMError(f"Claude ({model}): {type(exc).__name__}: {exc}") from exc
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMRefusal(f"Claude recusou a requisição (categoria: {category or 'não informada'})")
        text = "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")
        usage = message.usage
        return LLMResponse(text, int(getattr(usage, "input_tokens", 0) or estimate_tokens(req.system + req.user)),
                           int(getattr(usage, "output_tokens", 0) or estimate_tokens(text)), self.name,
                           getattr(message, "model", model) or model)


_PROVIDER_KINDS = {"gemini": GeminiProvider, "openai": OpenAICompatProvider, "openrouter": OpenAICompatProvider,
                   "anthropic": AnthropicProvider, "ollama": OllamaProvider}


def _try_json(text: str) -> tuple[Any, bool]:
    try:
        return json.loads(text), True
    except json.JSONDecodeError:
        return None, False


def extract_json(text: str) -> Any:
    """Extrai o primeiro objeto/array JSON de uma resposta (tolera cercas ```json e texto ao redor)."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    candidates = [fence.group(1).strip()] if fence else []
    candidates.append(text)
    for cand in candidates:
        parsed, ok = _try_json(cand)
        if ok:
            return parsed
        for opener, closer in (("{", "}"), ("[", "]")):
            start = cand.find(opener)
            if start == -1:
                continue
            depth, in_str, esc = 0, False, False
            for i in range(start, len(cand)):
                ch = cand[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cand[start:i + 1])
                        except json.JSONDecodeError:
                            break
    raise LLMError(f"Nenhum JSON válido encontrado na resposta: {text[:300]}")


def extract_code_block(text: str, lang: str = "r") -> str:
    """Extrai o maior bloco de código ```r ... ``` (ou o texto inteiro se não houver cercas)."""
    blocks = re.findall(rf"```(?:{lang}|{lang.upper()})?\s*\n(.*?)```", text or "", flags=re.S)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return (text or "").strip() + "\n"


class LLMClient:
    """Fachada única usada por todos os agentes."""

    def __init__(self, settings: dict, budget: BudgetGuard | None = None,
                 providers: dict[str, Provider] | None = None,
                 prompt_guard: Callable[[str, str], None] | None = None):
        self.settings = settings
        self.budget = budget
        self.privacy_mode: str = settings.get("privacy_mode", "hybrid")
        llm_cfg = settings.get("llm", {})
        self.roles: dict[str, dict] = llm_cfg.get("roles", {})
        self.phases: dict[str, dict] = llm_cfg.get("phases") or {}
        self.pricing: dict[str, list[float]] = llm_cfg.get("pricing", {})
        self.temperature = float(llm_cfg.get("temperature", 0.3))
        self.max_tokens = int(llm_cfg.get("max_output_tokens", 8192))
        self.prompt_guard = prompt_guard
        if providers is not None:
            self.providers = providers
        else:
            self.providers = {}
            for name, cfg in (llm_cfg.get("providers") or {}).items():
                kind = cfg.get("kind", name)
                if kind not in _PROVIDER_KINDS:
                    raise ValueError(f"Tipo de provedor desconhecido: {kind}")
                self.providers[name] = _PROVIDER_KINDS[kind](name, cfg)

    # ------------------------------------------------------------------ roteamento
    def _spec_for(self, phase: str, role: str) -> tuple[Optional[dict], str]:
        """Ordem: llm.phases[fase][papel] → llm.phases[fase]["*"] → llm.roles[papel] → llm.roles.default."""
        phase_cfg = self.phases.get(phase) or {}
        if role in phase_cfg:
            return phase_cfg[role], f"phases.{phase}.{role}"
        if "*" in phase_cfg and role not in ("local", "local_vision"):
            return phase_cfg["*"], f"phases.{phase}.*"
        if role in self.roles:
            return self.roles[role], f"roles.{role}"
        return self.roles.get("default"), "roles.default"

    def resolve(self, role: str, phase: str, sensitive: bool, vision: bool) -> tuple[Provider, str]:
        must_be_local = (
            self.privacy_mode == "strict"
            or (self.privacy_mode == "hybrid" and (sensitive or phase in PROTECTED_PHASES))
        )
        if must_be_local:
            role = "local_vision" if vision and ("local_vision" in self.roles or "local_vision" in (self.phases.get(phase) or {})) else "local"
        elif vision and role in ("default", "fast"):
            role = "vision"
        spec, origin = self._spec_for(phase, role)
        if not spec:
            raise LLMError(f"Papel LLM não configurado: {role} (fase {phase})")
        provider = self.providers.get(spec["provider"])
        if provider is None:
            raise LLMError(f"Provedor '{spec['provider']}' ({origin}) não está em llm.providers")
        if must_be_local and not getattr(provider, "local", False):
            raise PrivacyViolation(
                f"privacy_mode={self.privacy_mode}: chamada da {phase} exige provedor local, mas {origin} aponta "
                f"para '{provider.name}'. Ajuste llm.roles.local (ou llm.phases.{phase}.local) em settings.yaml.")
        return provider, spec["model"]

    def routing_table(self) -> dict[str, dict[str, str]]:
        """Resumo de qual provedor/modelo atende cada papel em cada fase (para `sciforge models`)."""
        table: dict[str, dict[str, str]] = {}
        for phase in ("phase1", "phase2", "phase3", "phase4", "phase5", "phase6"):
            table[phase] = {}
            for role in ("default", "fast", "vision", "writer", "reviewer"):
                try:
                    provider, model = self.resolve(role, phase, sensitive=False, vision=role == "vision")
                    table[phase][role] = f"{provider.name}:{model}"
                except (LLMError, PrivacyViolation) as exc:
                    table[phase][role] = f"ERRO: {exc}"
        return table

    def cost(self, model: str, input_tokens: int, output_tokens: int, provider: Provider) -> float:
        if getattr(provider, "local", False):
            return 0.0
        price = self.pricing.get(model)
        if not price:
            return 0.0
        return input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]

    # ------------------------------------------------------------------ chamadas
    async def complete(self, *, system: str, user: str, phase: str, role: str = "default",
                       images: list[Path] | None = None, json_mode: bool = False, sensitive: bool = False,
                       temperature: float | None = None, max_tokens: int | None = None) -> str:
        if self.prompt_guard is not None:
            self.prompt_guard(phase, system + "\n" + user)
        images = [Path(p) for p in (images or [])]
        provider, model = self.resolve(role, phase, sensitive, vision=bool(images))
        req = LLMRequest(system=system, user=user, images=images, json_mode=json_mode,
                         temperature=self.temperature if temperature is None else temperature,
                         max_tokens=max_tokens or self.max_tokens)
        if self.budget is not None:
            self.budget.check(phase, estimated_tokens=estimate_tokens(system + user) + req.max_tokens // 4)
        resp = await provider.generate(model, req)
        if self.budget is not None:
            self.budget.record(phase, resp.provider, resp.model, resp.input_tokens, resp.output_tokens,
                               self.cost(model, resp.input_tokens, resp.output_tokens, provider))
        return resp.text

    async def complete_json(self, *, system: str, user: str, phase: str, schema: Type[T] | None = None,
                            role: str = "default", images: list[Path] | None = None, sensitive: bool = False,
                            retries: int = 2, temperature: float | None = None) -> Any:
        """Pede JSON, valida contra `schema` (se dado) e re-pergunta com o erro em caso de falha."""
        prompt = user
        last_error: Optional[str] = None
        for _ in range(retries + 1):
            if last_error:
                prompt = (f"{user}\n\n---\nSua resposta anterior foi rejeitada pelo validador:\n{last_error}\n"
                          "Responda novamente APENAS com JSON válido que satisfaça o contrato.")
            text = await self.complete(system=system, user=prompt, phase=phase, role=role, images=images,
                                       json_mode=True, sensitive=sensitive, temperature=temperature)
            try:
                data = extract_json(text)
                return schema.model_validate(data) if schema is not None else data
            except (LLMError, ValidationError) as exc:
                last_error = str(exc)[:2000]
        raise LLMError(f"JSON inválido após {retries + 1} tentativas: {last_error}")
