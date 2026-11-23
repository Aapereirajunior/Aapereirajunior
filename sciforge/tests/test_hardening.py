"""Testes de endurecimento: portabilidade Windows, STDIO limpo do MCP, receitas Goose, roteamento
multi-modelo por fase, provedor Claude/OpenRouter e varreduras estáticas sem atalhos."""

from __future__ import annotations

import ast
import asyncio
import builtins
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from conftest import FIXTURES, HAS_R, ROOT, FakeProvider, fake_settings
from sciforge.core import platform_utils
from sciforge.core.config import load_settings
from sciforge.core.llm_client import AnthropicProvider, LLMClient, LLMRefusal, LLMRequest, PrivacyViolation
from sciforge.core.state_manager import StateManager
from sciforge.tools import integrity_checks as ic

SRC = ROOT / "src"
PY_FILES = sorted(p for p in SRC.rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# =============================================================================================
# 1. Auditoria sem atalhos
# =============================================================================================

def test_no_placeholders_or_empty_functions():
    problems = []
    for path in PY_FILES + sorted((ROOT / "scripts_r").glob("*.R")) + sorted((ROOT / "tests").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"\b(TODO|FIXME|XXX)\b", line) and "re.search" not in line:
                problems.append(f"{path.name}:{i}: marcador pendente")
        if path.suffix != ".py" or path.parent.name == "tests":
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Raise) and node.exc is not None:
                name = node.exc.func.id if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name) else \
                    getattr(node.exc, "id", "")
                if name == "NotImplementedError":
                    problems.append(f"{path.name}:{node.lineno}: NotImplementedError")
            if isinstance(node, ast.Pass):
                problems.append(f"{path.name}:{node.lineno}: `pass`")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    body = body[1:]
                if body and all(isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and b.value.value is Ellipsis
                                for b in body):
                    problems.append(f"{path.name}:{node.lineno}: função {node.name} com corpo `...`")
                is_abstract = any(getattr(d, "id", getattr(d, "attr", "")) == "abstractmethod" for d in node.decorator_list)
                if not body and not is_abstract and not _in_protocol(path, node):
                    problems.append(f"{path.name}:{node.lineno}: função {node.name} sem implementação")
    assert problems == [], "\n".join(problems)


def _in_protocol(path: Path, fn: ast.AST) -> bool:
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.ClassDef) and any(getattr(b, "id", "") == "Protocol" for b in node.bases):
            if any(getattr(n, "name", None) == getattr(fn, "name", "") and n.lineno == fn.lineno for n in node.body):
                return True
    return False


FORBIDDEN_DRAFT_TOKENS = ("rascunho", "draft_files", "read_draft_file", "graficos_preliminares", "phase1_intake",
                          "guarded_read")


def _code_tokens(path: Path) -> list[tuple[int, str]]:
    """Identificadores, atributos, imports e strings de código (sem docstrings e comentários)."""
    tree = _parse(path)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
                docstrings.add(id(first.value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            out.append((node.lineno, node.attr))
        elif isinstance(node, ast.alias):
            out.append((getattr(node, "lineno", 0), node.name))
        elif isinstance(node, ast.ImportFrom):
            out.append((node.lineno, node.module or ""))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            out.append((node.lineno, node.value))
    return out


@pytest.mark.parametrize("module", ["phase2_research_swarm", "phase3_architect", "phase4_analyst",
                                    "phase5_storyboard", "phase6_editorial"])
def test_phases_2_to_6_never_reference_draft(module):
    """Trava 1 (estática): Fases 2-6 não referenciam nem abrem rascunho.* ou os gráficos preliminares."""
    path = SRC / "agents" / f"{module}.py"
    hits = [(ln, tok) for ln, tok in _code_tokens(path)
            if any(f in tok.lower() for f in FORBIDDEN_DRAFT_TOKENS)]
    assert hits == [], f"{module} referencia o rascunho: {hits}"


def test_phases_5_6_never_open_draft_at_runtime(workspace, monkeypatch):
    """Trava 1 (dinâmica): qualquer abertura de rascunho.* durante as Fases 5 e 6 falha o teste."""
    from test_pipeline_components import _ctx, _seed_downstream_artifacts
    from sciforge.agents.phase5_storyboard import Phase5Storyboard
    from sciforge.agents.phase6_editorial import Phase6Editorial
    _seed_downstream_artifacts(workspace)
    opened: list[str] = []
    real_open, real_io_open = builtins.open, io.open

    def spy(file, *args, **kwargs):
        if Path(str(file)).name.startswith("rascunho.") or "graficos_preliminares" in str(file):
            opened.append(str(file))
            raise AssertionError(f"Fase 5/6 tentou abrir {file}")
        return real_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", lambda f, *a, **k: spy(f, *a, **k) if not isinstance(f, int) else real_io_open(f, *a, **k))
    ctx = _ctx(workspace, FakeProvider())
    asyncio.run(Phase5Storyboard(ctx).execute())
    asyncio.run(Phase6Editorial(ctx).execute())
    assert opened == []


# =============================================================================================
# 2. Compatibilidade Windows: pathlib, UTF-8 e subprocessos portáteis
# =============================================================================================

def test_all_file_io_declares_utf8_and_subprocess_is_portable():
    problems = []
    for path in PY_FILES + sorted((ROOT / "tests").glob("*.py")):
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "path" and getattr(node.value, "id", "") == "os":
                problems.append(f"{path.name}:{node.lineno}: usa os.path (use pathlib)")
            if not isinstance(node, ast.Call):
                continue
            kws = {k.arg for k in node.keywords}
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text") and "encoding" not in kws:
                problems.append(f"{path.name}:{node.lineno}: {func.attr}() sem encoding")
            if isinstance(func, ast.Name) and func.id == "open":
                mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else \
                    next((k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)), "r")
                if "b" not in str(mode) and "encoding" not in kws:
                    problems.append(f"{path.name}:{node.lineno}: open() em modo texto sem encoding")
            if "shell" in kws:
                problems.append(f"{path.name}:{node.lineno}: shell=True proibido")
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "subprocess" \
                    and func.attr in ("run", "Popen", "call", "check_output", "check_call"):
                if path.name not in ("platform_utils.py", "test_hardening.py"):
                    problems.append(f"{path.name}:{node.lineno}: subprocess.{func.attr} fora de platform_utils.run_command")
                elif "encoding" not in kws:
                    problems.append(f"{path.name}:{node.lineno}: subprocess sem encoding")
            if isinstance(func, ast.Attribute) and func.attr == "which" and node.args and \
                    isinstance(node.args[0], ast.Constant) and node.args[0].value in ("Rscript", "quarto", "pandoc") \
                    and path.name != "platform_utils.py":
                problems.append(f"{path.name}:{node.lineno}: shutil.which('{node.args[0].value}') — use platform_utils")
    assert problems == [], "\n".join(problems)


def test_rscript_and_quarto_resolution_on_windows_layout(tmp_path, monkeypatch):
    pf = tmp_path / "Program Files"
    for ver in ("R-4.3.1", "R-4.4.2", "R-4.10.0"):
        exe = pf / "R" / ver / "bin" / "Rscript.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
    quarto = pf / "Quarto" / "bin" / "quarto.exe"
    quarto.parent.mkdir(parents=True)
    quarto.write_bytes(b"")
    pandoc = pf / "Quarto" / "bin" / "tools" / "pandoc.exe"
    pandoc.parent.mkdir(parents=True)
    pandoc.write_bytes(b"")
    monkeypatch.setattr(platform_utils, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_utils, "IS_MAC", False)
    monkeypatch.setattr(platform_utils.shutil, "which", lambda name: None)
    monkeypatch.setattr(platform_utils, "_r_from_windows_registry", lambda: [])
    for var in ("SCIFORGE_RSCRIPT", "SCIFORGE_QUARTO", "SCIFORGE_PANDOC", "R_HOME", "ProgramW6432",
                "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ProgramFiles", str(pf))
    assert Path(platform_utils.find_rscript()).parts[-3] == "R-4.10.0"   # versão mais recente (ordem numérica)
    assert platform_utils.find_quarto() == str(quarto)
    assert platform_utils.find_pandoc() == str(pandoc)                   # pandoc embutido no Quarto
    custom = tmp_path / "meu_R" / "Rscript.exe"
    custom.parent.mkdir()
    custom.write_bytes(b"")
    monkeypatch.setenv("SCIFORGE_RSCRIPT", str(custom))
    assert platform_utils.find_rscript() == str(custom)


def test_run_command_decodes_utf8_without_shell(tmp_path):
    proc = platform_utils.run_command([sys.executable, "-c", "import sys; sys.stdout.buffer.write('ção µg/m³'.encode('utf-8'))"],
                                      cwd=tmp_path, timeout=30)
    assert proc.returncode == 0 and proc.stdout == "ção µg/m³"
    assert platform_utils.r_path(tmp_path / "a b" / "x.csv").endswith("/a b/x.csv")


@pytest.mark.skipif(not HAS_R, reason="Rscript não instalado")
def test_profile_data_r_handles_accented_paths_and_values(tmp_path):
    from sciforge.tools.data_profiler import profile_dataset
    folder = tmp_path / "dados com espaço e acentuação"
    folder.mkdir()
    csv = folder / "base_saúde.csv"
    csv.write_text("municipio,valor\nSão Paulo,1.5\nJoão Pessoa,2.5\nSão Paulo,3.0\nSão Paulo,4\nSão Paulo,5\nSão Paulo,6\n",
                   encoding="utf-8")
    prof = profile_dataset(csv)
    levels = {lv.nivel: lv for c in prof.colunas if c.nome == "municipio" for lv in c.niveis}
    assert levels["São Paulo"].n == 5 and levels["João Pessoa"].suprimido


# =============================================================================================
# 3. MCP: STDIO limpo
# =============================================================================================

def test_stdout_guard_redirects_prints_to_stderr(capsys):
    from sciforge.mcp_server.server import stdout_guard

    @stdout_guard
    def noisy() -> str:
        print("ruído que quebraria o STDIO")
        return "ok"

    @stdout_guard
    async def noisy_async() -> str:
        print("ruído assíncrono")
        return "ok"
    assert noisy() == "ok" and asyncio.run(noisy_async()) == "ok"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ruído que quebraria" in captured.err and "ruído assíncrono" in captured.err


def test_mcp_logging_never_targets_stdout():
    import logging
    from sciforge.mcp_server.server import configure_stderr_logging
    root = logging.getLogger()
    stray = logging.StreamHandler(sys.stdout)
    root.addHandler(stray)
    configure_stderr_logging()
    streams = [getattr(h, "stream", None) for h in root.handlers]
    assert sys.stdout not in streams and sys.stderr in streams


def test_mcp_stdio_session_emits_only_jsonrpc(workspace):
    """Sobe o servidor real via STDIO, executa ferramentas e exige que TODA linha do stdout seja JSON-RPC."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT / "src" / "..")}
    proc = subprocess.Popen([sys.executable, "-m", "sciforge.mcp_server.server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT), env=env,
                            text=True, encoding="utf-8")
    calls = [("sciforge_status", {"project_dir": str(workspace)}),
             ("sciforge_run_phase", {"project_dir": str(workspace), "phase": "phase3"}),
             ("sciforge_read_artifact", {"project_dir": str(workspace), "artifact": "inventario"})]
    if HAS_R:
        calls.append(("profile_r_dataset", {"file_path": str(FIXTURES / "dados_sinteticos.csv")}))
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "teste-stdio", "version": "1.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    msgs += [{"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": n, "arguments": a}}
             for i, (n, a) in enumerate(calls)]
    import threading
    watchdog = threading.Timer(180, proc.kill)
    watchdog.start()
    expected = {1, 2, *range(10, 10 + len(calls))}
    parsed: list[dict] = []
    try:
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in msgs))
        proc.stdin.flush()
        while expected - {m.get("id") for m in parsed}:
            line = proc.stdout.readline()
            if not line:
                break
            if not line.strip():
                continue
            msg = json.loads(line)        # falha se houver qualquer texto solto no stdout
            assert msg.get("jsonrpc") == "2.0", line
            parsed.append(msg)
        proc.stdin.close()
        rest = proc.stdout.read()
        for line in rest.splitlines():
            if line.strip():
                assert json.loads(line).get("jsonrpc") == "2.0", line
        err = proc.stderr.read()
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)
    by_id = {m["id"]: m for m in parsed if "id" in m}
    assert {1, 2, *range(10, 10 + len(calls))} <= set(by_id), (by_id.keys(), err[-2000:])
    tool_names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert {"sciforge_run_phase", "sciforge_approve_gate", "sciforge_read_artifact"} <= tool_names
    refused = json.loads(by_id[11]["result"]["content"][0]["text"])
    assert refused["executada"] is False and "phase1" in refused["motivo"]


# =============================================================================================
# 4. Operação por fase (MCP/Goose) e firewall nas ferramentas
# =============================================================================================

def test_mcp_phase_tools_respect_order_gates_and_firewall(workspace):
    from sciforge.mcp_server import server
    from sciforge.orchestrator import PhaseOrderError, run_single_phase
    with pytest.raises(PhaseOrderError, match="phase1"):
        asyncio.run(run_single_phase(workspace, "phase2"))
    assert json.loads(server.sciforge_approve_gate(str(workspace), "gate1", "Aprovo o protocolo"))["aprovado"] is False
    blocked = json.loads(server.sciforge_read_artifact(str(workspace), "../00_semente/rascunho.md"))
    assert "erro" in blocked
    st = StateManager(workspace / "01_estado" / "sciforge_state.sqlite")
    for p in ("phase1", "phase2", "phase3"):
        st.set_phase(p, "done")
    (workspace / "01_estado" / "03_protocolo_proposto.md").write_text("# Protocolo\n", encoding="utf-8")
    draft = (FIXTURES / "rascunho.md").read_text(encoding="utf-8")
    ic.DraftFirewall.from_draft(draft).save(workspace / "01_estado" / ".firewall_fingerprint.json")
    leak = json.loads(server.sciforge_update_protocol(str(workspace), draft, "colar rascunho"))
    assert leak["atualizado"] is False and "Firewall" in leak["motivo"]
    ok = json.loads(server.sciforge_update_protocol(str(workspace), "# Protocolo\nCaso-cruzado com lag 0-7.", "trocar desenho"))
    assert ok["atualizado"] is True
    assert json.loads(server.sciforge_approve_gate(str(workspace), "gate1", "x"))["aprovado"] is False  # sem frase humana
    assert json.loads(server.sciforge_approve_gate(str(workspace), "gate1", "Aprovo o protocolo revisado"))["aprovado"]
    status = json.loads(server.sciforge_status(str(workspace)))
    assert status["proxima_acao"].startswith("Executar phase4")
    assert "Caso-cruzado" in server.read_artifact(workspace, "protocolo")


# =============================================================================================
# 5. Receitas Goose
# =============================================================================================

RECIPES = sorted((ROOT / "goose_recipes").glob("*.yaml"))


def test_goose_recipes_are_valid_and_isolated():
    from sciforge.mcp_server.server import mcp
    tool_names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert len(RECIPES) == 6
    for i, path in enumerate(RECIPES, start=1):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for key in ("version", "title", "description", "instructions", "prompt", "parameters", "extensions"):
            assert key in data, (path.name, key)
        assert [e["name"] for e in data["extensions"]] == ["sciforge"]          # nenhuma extensão de arquivos
        ext = data["extensions"][0]
        assert ext["type"] == "stdio" and ext["cmd"] == "sciforge-mcp"
        assert data["parameters"][0]["key"] == "project_dir"
        text = data["instructions"] + data["prompt"]
        assert "{{ project_dir }}" in text and f'phase="phase{i}"' in text
        assert "FIREWALL ANTI-RETALHOS" in data["instructions"]
        for tool in set(re.findall(r"\b(sciforge_\w+|verify_doi_and_retraction)\(", text)):
            assert tool in tool_names, (path.name, tool)
        if i in (3, 4, 6):
            assert f'gate="gate{ {3: 1, 4: 2, 6: 3}[i] }"' in text and "human_decision" in text


# =============================================================================================
# 6. Roteamento multi-modelo por fase, Claude e OpenRouter
# =============================================================================================

def test_phase_routing_cloud_hybrid_and_strict(monkeypatch):
    for var in ("SCIFORGE_OLLAMA_MODEL", "SCIFORGE_OLLAMA_VISION_MODEL"):
        monkeypatch.delenv(var, raising=False)
    s = load_settings()
    s["privacy_mode"] = "cloud"
    c = LLMClient(s)
    name = lambda role, phase, vision=False: (lambda p, m: f"{p.name}:{m}")(*c.resolve(role, phase, False, vision))  # noqa: E731
    assert name("default", "phase1") == "gemini:gemini-2.5-pro"
    assert name("vision", "phase1", True) == "gemini:gemini-2.5-flash"
    assert name("default", "phase2") == "gemini:gemini-2.5-pro"
    assert name("default", "phase3") == "claude:claude-opus-5-5"
    assert name("reviewer", "phase3") == "openrouter:openai/gpt-4.1"
    assert name("vision", "phase4", True) == "gemini:gemini-2.5-flash"
    assert name("writer", "phase5") == "claude:claude-opus-5-5"
    assert name("fast", "phase6") == "gemini:gemini-2.5-flash"
    s["privacy_mode"] = "hybrid"
    c = LLMClient(s)
    assert name("default", "phase1") == "ollama:qwen2.5-coder:7b"      # padrão leve (4-8 GB de VRAM)
    assert name("vision", "phase4", True) == "ollama:qwen2.5vl:3b"
    monkeypatch.setenv("SCIFORGE_OLLAMA_MODEL", "qwen2.5-coder:14b")
    monkeypatch.setenv("SCIFORGE_OLLAMA_VISION_MODEL", "qwen2.5vl:7b")
    assert name("default", "phase4") == "ollama:qwen2.5-coder:14b"     # trocado pelo .env
    assert name("vision", "phase4", True) == "ollama:qwen2.5vl:7b"
    assert name("default", "phase3") == "claude:claude-opus-5-5"
    s["llm"]["phases"]["phase4"]["local"] = {"provider": "openrouter", "model": "x/y"}
    with pytest.raises(PrivacyViolation):
        LLMClient(s).resolve("default", "phase4", False, False)
    table = LLMClient(load_settings()).routing_table()
    assert table["phase3"]["reviewer"] == "openrouter:openai/gpt-4.1"


def test_openrouter_sends_auth_and_app_headers(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"ok\": true}"}}],
                                         "usage": {"prompt_tokens": 11, "completion_tokens": 3}})
    real = httpx.AsyncClient
    monkeypatch.setattr("sciforge.core.llm_client.httpx.AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-teste")
    s = load_settings()
    s["privacy_mode"] = "cloud"
    client = LLMClient(s)
    out = asyncio.run(client.complete_json(system="s", user="u", phase="phase3", role="reviewer"))
    assert out == {"ok": True}
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer sk-or-teste" and seen["headers"]["x-title"] == "SciForge"
    assert seen["body"]["model"] == "openai/gpt-4.1"


class _FakeStream:
    def __init__(self, message, sink, params):
        self.message, self.sink, self.params = message, sink, params

    async def __aenter__(self):
        self.sink.append(self.params)
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.message


class _FakeAnthropic:
    def __init__(self, message):
        self.calls: list[dict] = []
        outer = self

        class _Messages:
            def stream(self, **params):
                return _FakeStream(message, outer.calls, params)

        class _Beta:
            messages = _Messages()
        self.beta = _Beta()


def _msg(stop_reason="end_turn", text='{"a": 1}'):
    from types import SimpleNamespace as NS
    return NS(stop_reason=stop_reason, stop_details=NS(category="cyber") if stop_reason == "refusal" else None,
              content=[NS(type="thinking", thinking=""), NS(type="text", text=text)],
              usage=NS(input_tokens=120, output_tokens=30), model="claude-opus-5-5")


def test_anthropic_provider_request_shape_and_refusal(tmp_path):
    from sciforge.tools.visual_inspector import write_test_png
    fake = _FakeAnthropic(_msg())
    prov = AnthropicProvider("claude", {"effort": "high", "fallbacks": "default"}, client=fake)
    png = write_test_png(tmp_path / "f.png")
    resp = asyncio.run(prov.generate("claude-opus-5-5", LLMRequest(system="sys", user="olá", images=[png],
                                                                     json_mode=True, temperature=0.9)))
    assert resp.text == '{"a": 1}' and resp.input_tokens == 120 and resp.output_tokens == 30
    params = fake.calls[0]
    assert params["model"] == "claude-opus-5-5" and params["system"] == "sys"
    assert "temperature" not in params and "thinking" not in params
    assert params["output_config"] == {"effort": "high"}
    assert params["betas"] == ["server-side-fallback-2026-07-01"] and params["fallbacks"] == "default"
    assert params["max_tokens"] >= 16000
    blocks = params["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[-1]["type"] == "text" and "JSON" in blocks[-1]["text"]
    refusing = AnthropicProvider("claude", {}, client=_FakeAnthropic(_msg("refusal", "")))
    with pytest.raises(LLMRefusal, match="cyber"):
        asyncio.run(refusing.generate("claude-opus-5-5", LLMRequest(system="s", user="u")))
    no_fb = _FakeAnthropic(_msg())
    asyncio.run(AnthropicProvider("claude", {"fallbacks": None}, client=no_fb).generate(
        "claude-opus-5-5", LLMRequest(system="s", user="u")))
    assert "fallbacks" not in no_fb.calls[0] and "betas" not in no_fb.calls[0]


def test_anthropic_sdk_accepts_provider_parameters(monkeypatch):
    import inspect
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-teste")
    client = AnthropicProvider("claude", {}).client()
    sig = inspect.signature(client.beta.messages.stream)
    for param in ("model", "max_tokens", "system", "messages", "output_config", "betas", "fallbacks"):
        assert param in sig.parameters, param


def test_llm_client_records_cost_per_phase_with_fake(workspace):
    settings = load_settings(workspace, fake_settings())
    settings["llm"]["pricing"]["fake-1"] = [1.0, 2.0]
    provider = FakeProvider()
    provider.local = False
    client = LLMClient(settings, providers={"fake": provider})
    from sciforge.core.budget_guard import BudgetGuard
    st = StateManager(workspace / "01_estado" / "s.sqlite")
    client.budget = BudgetGuard(settings, st)
    asyncio.run(client.complete(system="Revisor #2", user="x", phase="phase3", role="reviewer"))
    usage = st.usage_by_phase("phase3")["phase3"]
    assert usage["calls"] == 1 and usage["cost"] > 0


def test_native_key_defaults_for_anthropic_openrouter_and_ollama(monkeypatch):
    from sciforge.core.llm_client import LLMError, OllamaProvider, OpenAICompatProvider
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    orp = OpenAICompatProvider("meu_or", {"kind": "openrouter"})             # sem chave/URL no settings
    assert orp.api_key == "sk-or-x" and orp.base_url == "https://openrouter.ai/api/v1"
    assert orp.extra_headers == {"X-Title": "SciForge"}
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        asyncio.run(OpenAICompatProvider("meu_or", {"kind": "openrouter"}).generate("m", LLMRequest(system="s", user="u")))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider("claude", {"kind": "anthropic"}).client()
    monkeypatch.delenv("SCIFORGE_OLLAMA_NUM_CTX", raising=False)
    assert OllamaProvider("ollama", {}).num_ctx == 8192
    monkeypatch.setenv("SCIFORGE_OLLAMA_NUM_CTX", "16384")
    assert OllamaProvider("ollama", {}).num_ctx == 16384
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ("ANTHROPIC_API_KEY=", "OPENROUTER_API_KEY=", "SCIFORGE_OLLAMA_MODEL=qwen2.5-coder:7b"):
        assert var in env_example


# =============================================================================================
# 7. Rodada final: modo nuvem, âncoras do Humanizador, citações narrativas, checklist EN, S2
# =============================================================================================

def test_default_settings_are_cloud_and_route_every_phase_to_cloud():
    s = load_settings()
    assert s["privacy_mode"] == "cloud"
    client = LLMClient(s)
    for phase in ("phase1", "phase2", "phase3", "phase4", "phase5", "phase6"):
        for role in ("default", "fast", "vision", "writer", "analyst", "reviewer"):
            provider, _ = client.resolve(role, phase, sensitive=True, vision=role == "vision")
            assert not getattr(provider, "local", False), (phase, role, provider.name)
    assert client.resolve("analyst", "phase4", True, False)[0].name == "claude"
    assert client.resolve("vision", "phase4", True, True)[0].name == "gemini"
    assert client.resolve("default", "phase1", True, False)[0].name == "gemini"


class _TrackingProvider(FakeProvider):
    """Provedor 'em nuvem' (local=False) que registra os papéis/modelos usados."""
    local = False


@pytest.mark.skipif(not HAS_R, reason="Rscript não instalado")
def test_phase4_cloud_mode_never_calls_ollama_and_lgpd_guards_stay_on(workspace, monkeypatch):
    from test_pipeline_components import FakeR, _seed_downstream_artifacts
    from sciforge.agents.base import PhaseContext
    from sciforge.agents.phase4_analyst import Phase4Analyst
    from sciforge.core.llm_client import OllamaProvider
    from sciforge.tools import r_sandbox

    async def no_ollama(self, model, req):
        raise AssertionError("Ollama não pode ser chamado em privacy_mode: cloud")
    monkeypatch.setattr(OllamaProvider, "generate", no_ollama)

    _seed_downstream_artifacts(workspace)
    for sub in ("tabelas", "figuras"):
        for f in (workspace / "03_entrega" / sub).glob("*"):
            f.unlink()
    (workspace / "01_estado" / "01_inventario_semente.json").write_text(
        json.dumps({"ideia_nucleo": "PM2.5 e internação."}), encoding="utf-8")

    class CloudR(FakeR):
        local = False
        first = True

        def _answer(self, system, user):
            # 1ª versão do 01_prep_dados.R tenta imprimir linhas: o static_check deve barrar mesmo em cloud.
            if "Bioestatístico" in system and "ESCREVA AGORA: 02_analise/01_prep_dados.R" in user and CloudR.first:
                CloudR.first = False
                return "```r\ndados <- read.csv('x.csv')\nhead(dados)\n```"
            if "Bioestatístico" in system and "viola as regras de privacidade" in user:
                from test_pipeline_components import PHASE4_SCRIPTS
                return f"```r\n{PHASE4_SCRIPTS['01_prep_dados.R']}```"
            return super()._answer(system, user)

    sanitized: list[str] = []
    real_sanitize = r_sandbox.sanitize_output
    monkeypatch.setattr(r_sandbox, "sanitize_output", lambda t: sanitized.append(t) or real_sanitize(t))

    extra = {"analysis": {"use_renv": False}}
    settings = load_settings(workspace, extra)                     # privacy_mode padrão: cloud
    assert settings["privacy_mode"] == "cloud"
    cloud = CloudR()
    roles = {r: {"provider": "nuvem", "model": f"cloud-{r}"} for r in ("default", "analyst", "vision", "fast", "writer", "reviewer")}
    settings["llm"]["roles"].update(roles)
    settings["llm"]["phases"] = {"phase4": {"analyst": roles["analyst"], "vision": roles["vision"], "*": roles["default"]}}
    providers = {"nuvem": cloud, "ollama": OllamaProvider("ollama", {})}
    client = LLMClient(settings, providers=providers)
    ctx = PhaseContext.create(workspace, settings_extra=extra, llm=client, log=lambda m: None)
    ctx.llm.settings = settings
    out = asyncio.run(Phase4Analyst(ctx).execute())
    assert out["tabelas"] == ["tbl-caracteristicas"]
    bio_calls = [u for s, u in cloud.calls if "Bioestatístico" in s]
    assert bio_calls and any("viola as regras de privacidade" in u for _, u in cloud.calls)
    assert sanitized, "logs do R devem passar pelo sanitizador LGPD também no modo cloud"
    # o código final não contém o head(dados) bloqueado
    assert "head(dados)" not in (workspace / "02_analise" / "01_prep_dados.R").read_text(encoding="utf-8")


def test_profile_data_r_suppression_is_independent_of_privacy_mode():
    """profile_data.R não conhece privacy_mode: a supressão < 5 e a omissão de identificadores são fixas."""
    script = (ROOT / "scripts_r" / "profile_data.R").read_text(encoding="utf-8")
    assert "privacy" not in script.lower()
    assert "min_cell" in script and "possivel_identificador" in script
    src = (SRC / "tools" / "r_sandbox.py").read_text(encoding="utf-8")
    assert "privacy" not in src.lower()   # static_check e sanitize_output não dependem do modo


def test_humanizer_cannot_drop_numbers_ci_or_p_values():
    from sciforge.agents.phase6_editorial import _same_anchors
    before = ("O risco foi maior (OR 1,57; IC 95% 1,12-2,20; p = 0,009) e manteve-se na sensibilidade "
              "(OR 1,57; IC 95% 1,12-2,20) [@di2017air]. {{tbl-caracteristicas}}")
    assert _same_anchors(before, before.replace("O risco foi maior", "A chance aumentou"))
    assert not _same_anchors(before, before.replace("; p = 0,009", ""))                      # p-valor removido
    assert not _same_anchors(before, before.replace(" e manteve-se na sensibilidade (OR 1,57; IC 95% 1,12-2,20)", ""))  # repetição removida
    assert not _same_anchors(before, before.replace(" [@di2017air]", ""))
    assert not _same_anchors(before, before.replace("{{tbl-caracteristicas}}", ""))


def test_humanizer_rejection_keeps_writer_version(workspace):
    from test_pipeline_components import _ctx, _seed_downstream_artifacts
    from sciforge.agents.phase6_editorial import Phase6Editorial
    _seed_downstream_artifacts(workspace)

    class DroppingHumanizer(FakeProvider):
        def _answer(self, system, user):
            if "Humanizador" in system:
                return user.split("TEXTO:\n", 1)[1].replace("; p = 0,009", "")
            return super()._answer(system, user)
    ctx = _ctx(workspace, DroppingHumanizer())
    agent = Phase6Editorial(ctx)
    agent.lang, agent.lk = "pt-BR", "pt"
    from sciforge.core.schemas import InventarioSemente
    agent.inv = InventarioSemente(ideia_nucleo="x.")
    text = "Associação positiva (OR 1,57; IC 95% 1,12-2,20; p = 0,009)."
    assert asyncio.run(agent._humanize_text(text, "Resultados")) == text


def test_narrative_citation_with_final_period_is_kept_in_bib(workspace):
    from test_pipeline_components import _ctx, _seed_downstream_artifacts
    from conftest import SECTIONS_FAKE
    from sciforge.agents.phase6_editorial import Phase6Editorial
    from sciforge.tools.literature_swarm import parse_bib_keys
    _seed_downstream_artifacts(workspace)
    narrative = "Os achados concordam com a coorte do Medicare descrita por @di2017air."

    class NarrativeWriter(FakeProvider):
        def _answer(self, system, user):
            if "Redator Científico" in system and "SEÇÃO A REDIGIR: Discussao" in user:
                return SECTIONS_FAKE["Discussao"].replace(" [@di2017air]", "") + " " + narrative
            return super()._answer(system, user)
    from sciforge.agents.phase5_storyboard import Phase5Storyboard
    ctx = _ctx(workspace, NarrativeWriter())
    asyncio.run(Phase5Storyboard(ctx).execute())
    asyncio.run(Phase6Editorial(ctx).execute())
    entrega = workspace / "03_entrega"
    assert narrative in (entrega / "artigo_final.qmd").read_text(encoding="utf-8")
    assert "di2017air" in parse_bib_keys((entrega / "referencias.bib").read_text(encoding="utf-8"))


def test_english_checklist_accepts_not_applicable(workspace):
    from test_pipeline_components import _ctx, _seed_downstream_artifacts
    from sciforge.agents.phase6_editorial import Phase6Editorial, _is_not_applicable
    _seed_downstream_artifacts(workspace)
    assert all(_is_not_applicable(x) for x in ("not applicable", "Not applicable (ecological design)", "N/A", "n/a", "não se aplica"))
    assert not _is_not_applicable("Nada consta")

    class EnglishKit(FakeProvider):
        def _answer(self, system, user):
            if "Kit de Submissão" in system and "MODO CHECKLIST" in user:
                return json.dumps({"itens": [{"item": "6b", "local": "not applicable", "status": "nao_se_aplica"},
                                             {"item": "12d", "local": "N/A", "status": "nao_se_aplica"},
                                             {"item": "13a", "local": "Resultados §1", "status": "atendido"}]})
            return super()._answer(system, user)
    from sciforge.agents.phase5_storyboard import Phase5Storyboard
    ctx = _ctx(workspace, EnglishKit())
    asyncio.run(Phase5Storyboard(ctx).execute())
    asyncio.run(Phase6Editorial(ctx).execute())
    checklist = (workspace / "03_entrega" / "checklist_preenchido.md").read_text(encoding="utf-8")
    row_6b = next(line for line in checklist.splitlines() if line.startswith("| 6b |"))
    row_12d = next(line for line in checklist.splitlines() if line.startswith("| 12d |"))
    assert "not applicable" in row_6b and "nao_se_aplica" in row_6b and "verificar" not in row_6b
    assert "N/A" in row_12d and "verificar" not in row_12d


def _s2_transport(seen: list[httpx.Request], status_by_path: dict[str, int] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if status_by_path and path in status_by_path:
            return httpx.Response(status_by_path[path])
        if request.url.host == "api.semanticscholar.org":
            if path.endswith("/paper/search/bulk") or path.endswith("/paper/search"):
                return httpx.Response(200, json={"data": [{"title": "Bulk paper", "year": 2022, "authors": [],
                                                           "externalIds": {"DOI": "10.1/s2"}, "abstract": "A."}]})
            if "/paper/DOI:" in path:
                return httpx.Response(200, json={"abstract": "Resumo vindo da Semantic Scholar.",
                                                 "externalIds": {"PubMedCentral": "999"}, "isOpenAccess": True})
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"display_name": "Seed", "abstract_inverted_index": None, "ids": {}})
        if request.url.host == "www.ebi.ac.uk":
            return httpx.Response(200, json={"resultList": {"result": []}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_semantic_scholar_api_key_header_bulk_and_silent_429(monkeypatch):
    from sciforge.tools.literature_swarm import LiteratureSwarm, SwarmConfig
    fast = {"openalex": 1000, "europepmc": 1000, "crossref": 1000, "semanticscholar": 1000}
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2-chave-teste")
    cfg = SwarmConfig.from_settings({"literature": {"rate_limits_rps": fast, "backoff_base_s": 0.01}})
    assert cfg.semantic_scholar_api_key == "s2-chave-teste"
    seen: list[httpx.Request] = []

    async def go(c, limit):
        async with LiteratureSwarm(c, transport=_s2_transport(seen)) as swarm:
            return await swarm.search_semantic_scholar("pm2.5", 2020, limit)
    papers = asyncio.run(go(cfg, 20))
    req = seen[-1]
    assert req.headers.get("x-api-key") == "s2-chave-teste" and req.url.path.endswith("/paper/search")
    assert papers[0].doi == "10.1/s2"
    asyncio.run(go(cfg, 500))                                     # > 100 → endpoint bulk
    assert seen[-1].url.path.endswith("/paper/search/bulk") and seen[-1].url.params["sort"] == "citationCount:desc"
    assert seen[-1].headers.get("x-api-key") == "s2-chave-teste"

    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY")
    nokey = SwarmConfig.from_settings({"literature": {"rate_limits_rps": fast, "backoff_base_s": 0.01}})
    seen.clear()

    async def go429():
        transport = _s2_transport(seen, {"/graph/v1/paper/search": 429})
        async with LiteratureSwarm(nokey, transport=transport) as swarm:
            return await swarm.search_all("pm2.5", years_back=5, sources=["semanticscholar", "openalex"])
    papers, errors = asyncio.run(go429())
    s2_reqs = [r for r in seen if r.url.host == "api.semanticscholar.org"]
    assert "x-api-key" not in s2_reqs[0].headers
    assert len(s2_reqs) == 2 and "semanticscholar" in errors      # 1 tentativa + 1 retry, sem derrubar o resto


def test_seed_reference_abstract_enrichment_by_doi(monkeypatch):
    from sciforge.tools.literature_swarm import LiteratureSwarm, SwarmConfig
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")
    cfg = SwarmConfig(rate_limits_rps={"openalex": 1000, "europepmc": 1000, "crossref": 1000, "semanticscholar": 1000},
                      backoff_base_s=0.01)
    seen: list[httpx.Request] = []

    async def go():
        async with LiteratureSwarm(cfg, transport=_s2_transport(seen)) as swarm:
            return await swarm.enrich_by_doi("https://doi.org/10.1056/NEJMoa1702747")
    info = asyncio.run(go())
    hosts = [r.url.host for r in seen]
    assert hosts == ["api.openalex.org", "www.ebi.ac.uk", "api.semanticscholar.org"]   # ordem de fallback
    assert info["resumo"] == "Resumo vindo da Semantic Scholar." and info["fonte"] == "semanticscholar"
    assert info["pmcid"] == "PMC999" and info["open_access"]


def test_phase2_keeps_valid_seed_reference_without_abstract(workspace, monkeypatch):
    from sciforge.agents.phase2_research_swarm import Phase2ResearchSwarm
    from sciforge.core.schemas import Paper
    from sciforge.tools.literature_swarm import LiteratureSwarm, parse_bib_keys
    from test_pipeline_components import _ctx
    (workspace / "01_estado" / "01_inventario_semente.json").write_text(json.dumps({
        "ideia_nucleo": "PM2.5 e internação.", "referencias_semente": ["10.1/seedsemresumo", "10.1/seedcomresumo"]}),
        encoding="utf-8")
    st = StateManager(workspace / "01_estado" / "sciforge_state.sqlite")
    st.set_phase("phase1", "done")

    async def fake_search_all(self, query, years_back=None, max_results=None, sources=None, agent=None):
        return [Paper(titulo="Artigo do enxame sobre PM2.5", doi="10.1/enxame", ano=2023, autores=["Ana Lima"],
                      resumo="PM2.5 increased admissions.", fontes=["openalex"], agentes=[agent or ""])], {}

    async def fake_verify_reference(self, ref):
        return {"consulta": ref, "existe": True, "doi": ref, "status_retratacao": "ok", "ano": 2017,
                "titulo": f"Seed {ref}", "autores": ["Rui Costa"], "revista": "J"}

    async def fake_verify_doi(self, doi):
        return {"doi": doi, "existe": True, "status_retratacao": "ok"}

    async def fake_enrich(self, doi):
        return {"resumo": "Seed abstract found via EuropePMC." if doi.endswith("comresumo") else None,
                "pmcid": None, "pmid": None, "open_access": False, "fonte": "europepmc"}
    monkeypatch.setattr(LiteratureSwarm, "search_all", fake_search_all)
    monkeypatch.setattr(LiteratureSwarm, "verify_reference", fake_verify_reference)
    monkeypatch.setattr(LiteratureSwarm, "verify_doi", fake_verify_doi)
    monkeypatch.setattr(LiteratureSwarm, "enrich_by_doi", fake_enrich)

    class LedgerFake(FakeProvider):
        def _answer(self, system, user):
            if "Fichador" in system:
                docs = json.loads(user.split("\n", 1)[1])
                return json.dumps({"entradas": [{"citekey": d["citekey"], "desenho": "série temporal",
                                                 "populacao": "adultos",
                                                 "achados_principais": ["Achado do resumo."] if d["texto"] else []}
                                                for d in docs]})
            if "Sintetizador" in system:
                return json.dumps({"agente1_estado_da_arte": ["pm2.5"], "agente2_metodologia": ["dlnm"],
                                   "agente3_controversias": ["bias"]}) if "consultas" in user else "# Estado da arte\n"
            return super()._answer(system, user)
    ctx = _ctx(workspace, LedgerFake())
    out = asyncio.run(Phase2ResearchSwarm(ctx).execute())
    assert out["semente_enriquecida"] == 1 and out["semente_no_pool"] == 2
    ledger = json.loads((workspace / "01_estado" / "02_evidence_ledger.json").read_text(encoding="utf-8"))
    by_doi = {e["doi"]: e for e in ledger["entradas"]}
    assert by_doi["10.1/seedcomresumo"]["achados_principais"] == ["Achado do resumo."]
    assert by_doi["10.1/seedsemresumo"]["achados_principais"] == []          # mantida só com metadados
    keys = parse_bib_keys((workspace / "01_estado" / "referencias_pool.bib").read_text(encoding="utf-8"))
    assert len(keys) == 3                                                       # enxame + 2 sementes


def test_validate_results_guards_zero_initial_n(tmp_path, monkeypatch):
    from sciforge.agents import phase4_analyst
    from sciforge.core.schemas import FluxoAmostral
    from conftest import RESULTS_FIXTURE
    p = tmp_path / "r.json"
    p.write_text(json.dumps(RESULTS_FIXTURE), encoding="utf-8")
    # o schema já exige n_inicial > 0; simulamos um contrato que escapou da validação
    real = phase4_analyst.ResultadosVerificados.model_validate

    def lenient(data):
        res = real(data)
        object.__setattr__(res, "fluxo_amostral", FluxoAmostral.model_construct(n_inicial=0, exclusoes=[], n_final=0))
        return res
    monkeypatch.setattr(phase4_analyst.ResultadosVerificados, "model_validate", staticmethod(lenient))
    _, problems = phase4_analyst.validate_results(p, 0.2)
    assert any("n_inicial=0" in x for x in problems)
