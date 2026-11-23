"""CLI do SciForge (Typer + Rich).

Comandos:
  sciforge init <projeto>             cria o workspace a partir de workspace_template/
  sciforge run <projeto>              executa as fases pendentes, pausando nos 3 portões humanos
  sciforge resume <projeto>           retoma do ponto exato de interrupção (fase/seção)
  sciforge status <projeto>           mostra fases, portões, custos e eventos recentes
  sciforge approve <projeto> <gate>   aprova um portão humano de forma não interativa (gate1|gate2|gate3)
  sciforge reset <projeto> <fase>     invalida uma fase e as posteriores (ex.: após editar o protocolo)
  sciforge render <projeto>           (re)renderiza artigo e suplemento com Quarto
  sciforge audit <projeto>            roda as auditorias de integridade sobre o .qmd atual
  sciforge csl <estilo>               baixa um estilo CSL do repositório oficial
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .agents.base import GateBlocked, PhaseContext
from .agents.phase1_intake import Phase1Intake
from .agents.phase2_research_swarm import Phase2ResearchSwarm
from .agents.phase3_architect import Phase3Architect
from .agents.phase4_analyst import Phase4Analyst
from .agents.phase5_storyboard import Phase5Storyboard
from .agents.phase6_editorial import Phase6Editorial
from .core.budget_guard import BudgetExceeded
from .core.config import CONFIG_DIR, TEMPLATE_DIR, Workspace
from .core.llm_client import PrivacyViolation
from .core.state_manager import GATES, PHASES, StateManager
from .tools.integrity_checks import FirewallViolation

app = typer.Typer(add_completion=False, help="SciForge v2 — reconstrução e redação científica (R + Quarto)")
console = Console()

PHASE_CLASSES = {
    "phase1": Phase1Intake, "phase2": Phase2ResearchSwarm, "phase3": Phase3Architect,
    "phase4": Phase4Analyst, "phase5": Phase5Storyboard, "phase6": Phase6Editorial,
}
GATE_AFTER = {v: k for k, v in GATES.items()}


def _log(msg: str) -> None:
    console.print(msg)


def _ctx(project: Path) -> PhaseContext:
    if not (project / "00_semente").exists():
        console.print(f"[red]{project} não parece um projeto SciForge (falta 00_semente/). Use `sciforge init`.[/red]")
        raise typer.Exit(2)
    return PhaseContext.create(project, log=_log)


def _gate_summary(ctx: PhaseContext, gate: str) -> str:
    ws = ctx.ws
    if gate == "gate1":
        crit = json.loads(ws.critica_revisor.read_text(encoding="utf-8")) if ws.critica_revisor.exists() else {}
        last = (crit.get("rodadas") or [{}])[-1]
        crits = "\n".join(f"- **{c['gravidade']}** · {c['vies']}: {c['descricao']}" for c in last.get("criticas", []))
        proto = ws.protocolo.read_text(encoding="utf-8") if ws.protocolo.exists() else ""
        return (f"## Protocolo proposto (trecho)\n\n{proto[:3500]}\n\n## Última rodada do Revisor #2\n\n"
                f"{crits or 'Sem críticas pendentes.'}\n\nEdite `{ws.protocolo}` se desejar antes de aprovar.")
    if gate == "gate2":
        rel = ws.estado / "04_relatorio_analise.md"
        return (rel.read_text(encoding="utf-8") if rel.exists() else "") + \
            f"\n\nTabelas em `{ws.tabelas}` · Figuras em `{ws.figuras}`"
    rel = ws.entrega / "relatorio_auditoria_final.md"
    return (rel.read_text(encoding="utf-8")[:5000] if rel.exists() else "") + \
        f"\n\nEntrega final em `{ws.entrega}` (artigo_final.qmd/.docx/.pdf, suplemento, checklist, cover letter)."


def _handle_gate(ctx: PhaseContext, gate: str, auto_approve: bool) -> bool:
    status = ctx.state.gate_status(gate)
    if status == "approved":
        return True
    console.print(Panel(Markdown(_gate_summary(ctx, gate)), title=f"PORTÃO HUMANO {gate[-1]}", border_style="yellow"))
    if auto_approve:
        ctx.state.set_gate(gate, "approved", "auto-approve")
        console.print("[yellow]Aprovado automaticamente (--auto-approve).[/yellow]")
        return True
    if not console.is_interactive:
        console.print(f"[yellow]Sessão não interativa: aprove com `sciforge approve {ctx.ws.root} {gate}` e rode `sciforge resume`.[/yellow]")
        return False
    if typer.confirm(f"Aprovar {gate} e seguir?", default=False):
        ctx.state.set_gate(gate, "approved", "aprovado no terminal")
        if gate == "gate1":
            console.print("[green]Protocolo aprovado (edições manuais em 03_protocolo_proposto.md serão usadas).[/green]")
        return True
    ctx.state.set_gate(gate, "rejected", "rejeitado no terminal")
    console.print("[red]Portão rejeitado. Edite os artefatos e use `sciforge reset` ou `sciforge resume`.[/red]")
    return False


async def run_pipeline(project: Path, auto_approve: bool = False, until: Optional[str] = None,
                       ctx: PhaseContext | None = None) -> str:
    """Executa as fases pendentes em ordem. Retorna o nome da última fase concluída ou o portão pendente."""
    ctx = ctx or _ctx(project)
    for phase in PHASES:
        gate = GATE_AFTER.get(phase)
        if ctx.state.phase_status(phase) != "done":
            prev_gate = GATE_AFTER.get(PHASES[PHASES.index(phase) - 1]) if PHASES.index(phase) > 0 else None
            if prev_gate and ctx.state.gate_status(prev_gate) != "approved":
                if not _handle_gate(ctx, prev_gate, auto_approve):
                    return prev_gate
            console.rule(f"[bold cyan]{PHASE_CLASSES[phase].title}")
            agent = PHASE_CLASSES[phase](ctx)
            await agent.execute()
            # Recria o contexto após a Fase 1 para carregar o firewall recém-ativado
            if phase == "phase1":
                ctx = PhaseContext.create(ctx.ws.root, llm=ctx.llm, log=_log)
        if gate and ctx.state.gate_status(gate) != "approved":
            if not _handle_gate(ctx, gate, auto_approve):
                return gate
        if until and phase == until:
            return phase
    return "phase6"


def _run(project: Path, auto_approve: bool, until: Optional[str]) -> None:
    try:
        result = asyncio.run(run_pipeline(project.resolve(), auto_approve, until))
    except GateBlocked as exc:
        console.print(Panel(str(exc), title="Fase bloqueada — intervenção necessária", border_style="red"))
        raise typer.Exit(3)
    except BudgetExceeded as exc:
        console.print(Panel(f"{exc}\nAjuste budget em settings.yaml (ou settings.override.yaml) e rode `sciforge resume`.",
                            title="Disjuntor de custo acionado", border_style="red"))
        raise typer.Exit(4)
    except (FirewallViolation, PrivacyViolation) as exc:
        console.print(Panel(str(exc), title="Trava de segurança", border_style="red"))
        raise typer.Exit(5)
    except FileNotFoundError as exc:
        console.print(Panel(str(exc), title="Arquivo de entrada ausente", border_style="red"))
        raise typer.Exit(2)
    except Exception as exc:  # noqa: BLE001 — erro de rede/LLM/R: estado já foi salvo
        console.print(Panel(f"{type(exc).__name__}: {exc}\n\nO progresso foi salvo. Corrija a causa e rode "
                            f"`sciforge resume {project}`.", title="Execução interrompida", border_style="red"))
        raise typer.Exit(1)
    console.print(f"[bold green]Execução pausada/concluída em: {result}[/bold green]")


@app.command()
def init(nome: str = typer.Argument(..., help="Nome/caminho do novo projeto"),
         force: bool = typer.Option(False, help="Sobrescrever se existir")) -> None:
    """Cria um workspace a partir de workspace_template/."""
    target = Path(nome).resolve()
    if target.exists() and any(target.iterdir()) and not force:
        console.print(f"[red]{target} já existe e não está vazio (use --force).[/red]")
        raise typer.Exit(1)
    shutil.copytree(TEMPLATE_DIR, target, dirs_exist_ok=True)
    for keep in target.rglob(".gitkeep"):
        keep.unlink()
    Workspace(target).ensure()
    StateManager(Workspace(target).db_path)
    console.print(Panel(
        f"Projeto criado em [bold]{target}[/bold]\n\n1. Coloque o rascunho em 00_semente/rascunho.md (.txt/.pdf/.docx)\n"
        "2. Coloque os dados em 00_semente/dados/ (.parquet/.csv/.rds/.fst)\n"
        "3. Gráficos iniciais (opcional) em 00_semente/graficos_preliminares/\n"
        "4. Normas do periódico em 00_semente/regras_revista.txt\n"
        f"5. Rode: [bold]sciforge run {nome}[/bold]", title="SciForge"))


@app.command()
def run(projeto: Path = typer.Argument(..., help="Diretório do projeto"),
        auto_approve: bool = typer.Option(False, "--auto-approve", help="Aprovar portões automaticamente"),
        until: Optional[str] = typer.Option(None, help="Parar após esta fase (phase1..phase6)")) -> None:
    """Executa o pipeline completo (ou até --until)."""
    _run(projeto, auto_approve, until)


@app.command()
def resume(projeto: Path = typer.Argument(...),
           auto_approve: bool = typer.Option(False, "--auto-approve")) -> None:
    """Retoma do ponto exato de interrupção (fases e seções concluídas não são refeitas)."""
    st = StateManager(Workspace(projeto.resolve()).db_path)
    for p in PHASES:
        if st.phase_status(p) in ("failed", "blocked", "running"):
            st.set_phase(p, "pending", "retomada")
    console.print(f"Retomando a partir de: [bold]{st.next_phase() or 'concluído'}[/bold]")
    _run(projeto, auto_approve, None)


@app.command()
def approve(projeto: Path = typer.Argument(...), gate: str = typer.Argument(..., help="gate1 | gate2 | gate3"),
            nota: str = typer.Option("aprovado via CLI", help="Comentário")) -> None:
    """Aprova um portão humano sem modo interativo."""
    StateManager(Workspace(projeto.resolve()).db_path).set_gate(gate, "approved", nota)
    console.print(f"[green]{gate} aprovado.[/green]")


@app.command()
def reset(projeto: Path = typer.Argument(...), fase: str = typer.Argument(..., help="phase1..phase6")) -> None:
    """Invalida a fase indicada e todas as posteriores."""
    StateManager(Workspace(projeto.resolve()).db_path).reset_from(fase)
    console.print(f"[yellow]Fases a partir de {fase} serão reexecutadas no próximo run/resume.[/yellow]")


@app.command()
def status(projeto: Path = typer.Argument(...)) -> None:
    """Mostra o estado das fases, portões, orçamento e eventos recentes."""
    ws = Workspace(projeto.resolve())
    ctx = PhaseContext.create(ws.root, log=_log)
    t = Table(title=f"SciForge — {ws.root.name} (privacy_mode={ctx.settings.get('privacy_mode')})")
    for col in ("Fase", "Status", "Início", "Fim", "Chamadas", "Tokens", "Custo US$", "Portão"):
        t.add_column(col)
    budget = ctx.budget.summary()
    for row in ctx.state.phases():
        b = budget.get(row["name"], {})
        gate = GATE_AFTER.get(row["name"])
        t.add_row(row["name"], row["status"], row["started_at"] or "", row["finished_at"] or "",
                  str(b.get("calls", 0)), str(b.get("tokens", 0)), f"{b.get('cost', 0):.3f}",
                  f"{gate}: {ctx.state.gate_status(gate)}" if gate else "")
    console.print(t)
    console.print(f"Custo total: US$ {ctx.state.total_cost():.3f} / teto {ctx.budget.global_max_cost:.2f}")
    steps = ctx.state.steps("phase6")
    if steps:
        console.print("Etapas da Fase 6: " + ", ".join(s["key"] for s in steps))
    for ev in ctx.state.events(10):
        console.print(f"[dim]{ev['created_at']} [{ev['level']}] {ev['phase'] or ''} {ev['message'][:200]}[/dim]")


@app.command()
def render(projeto: Path = typer.Argument(...),
           formatos: str = typer.Option("docx,pdf", help="Formatos separados por vírgula")) -> None:
    """(Re)renderiza artigo_final.qmd e material_suplementar.qmd."""
    from .tools.quarto_renderer import render_project
    res = render_project(Workspace(projeto.resolve()).entrega, [f.strip() for f in formatos.split(",") if f.strip()])
    console.print_json(json.dumps(res, ensure_ascii=False))


@app.command()
def audit(projeto: Path = typer.Argument(...)) -> None:
    """Roda as auditorias determinísticas sobre o artigo final atual."""
    from .mcp_server.server import audit_manuscript
    ws = Workspace(projeto.resolve())
    report_md = asyncio.run(audit_manuscript(ws.entrega / "artigo_final.qmd", ws.resultados, ws.evidence_ledger,
                                             fingerprint_path=ws.firewall_fingerprint, bib_path=ws.entrega / "referencias.bib",
                                             corpus_path=ws.corpus))
    console.print(Markdown(report_md))


@app.command()
def csl(estilo: str = typer.Argument(..., help="Nome do estilo (ex.: the-lancet, abnt, vancouver)")) -> None:
    """Baixa/copia um estilo CSL para config/csl/."""
    import tempfile
    from .tools.quarto_renderer import ensure_csl
    with tempfile.TemporaryDirectory() as tmp:
        ensure_csl(estilo, Path(tmp))
    console.print(f"[green]Estilo disponível em {CONFIG_DIR / 'csl'}[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
