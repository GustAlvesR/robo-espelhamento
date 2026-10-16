"""Filtros, validações agregadas e elegibilidade ao auto-aceite (funções puras)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .address_lookup import ResultadoHub, buscar_hub
from .parser import Rota, nome_valido, normalizar, validar_datas

if TYPE_CHECKING:
    from .config import Config, FiltrosCfg


@dataclass
class ResultadoAceite:
    status: str  # aceita_provavel | nao_encontrada | nao_confirmada | dialogo_pendente | erro | simulado | limite_hora
    detalhe: str = ""
    clicou: bool = False
    screenshot: str | None = None

    @property
    def sucesso(self) -> bool:
        return self.status == "aceita_provavel"


@dataclass
class Avaliacao:
    nome_valido: bool
    anomalias_datas: list[str]
    motivo_filtro: str | None
    hub: ResultadoHub | None
    alertas: list[str] = field(default_factory=list)
    elegivel_auto_aceite: bool = False
    motivo_sem_auto_aceite: str | None = None


def aplicar_filtros(rota: Rota, filtros: "FiltrosCfg", nome_ok: bool, agora: datetime) -> str | None:
    """Retorna o motivo do filtro ou None se a rota deve ser avisada.

    Rotas com nome fora do padrão não passam pelo filtro de origem (a origem não é confiável),
    e veículo não identificado não é filtrado: nesses casos o aviso sai com alerta.
    """
    if filtros.veiculos and rota.veiculo:
        permitidos = {normalizar(v) for v in filtros.veiculos}
        if normalizar(rota.veiculo) not in permitidos:
            return f"veículo {rota.veiculo} fora dos permitidos"
    if filtros.origens and nome_ok and rota.origem:
        if not any(normalizar(rota.origem).startswith(normalizar(p)) for p in filtros.origens):
            return f"origem {rota.origem} fora das permitidas"
    if filtros.eta_minima_min and rota.eta and rota.eta > agora:
        falta = rota.eta - agora
        if falta < timedelta(minutes=filtros.eta_minima_min):
            return f"ETA em {int(falta.total_seconds() // 60)} min (mínimo {filtros.eta_minima_min})"
    return None


def avaliar(rota: Rota, agora: datetime, cfg: "Config", linhas_tabela: list[dict]) -> Avaliacao:
    """Junta validações, filtros, cruzamento com o hub e elegibilidade ao auto-aceite."""
    nome_ok = nome_valido(rota.nome, cfg.regex_nome)
    anomalias = validar_datas(rota, agora)
    filtro = aplicar_filtros(rota, cfg.filtros, nome_ok, agora)
    hub = None if filtro else buscar_hub(rota.origem if nome_ok else None, rota.veiculo, linhas_tabela, cfg.hub)

    alertas: list[str] = []
    if not nome_ok:
        alertas.append(f"Nomenclatura fora do padrão: {rota.nome or '(vazio)'}")
    alertas += anomalias
    alertas += [f"Parse: {e}" for e in rota.erros_parse]
    if rota.veiculo is None:
        alertas.append("Veículo não identificado no card")
    if hub and not hub.ok:
        alertas += [f"Hub ({hub.status}): {m}" for m in hub.mensagens]

    av = Avaliacao(nome_valido=nome_ok, anomalias_datas=anomalias, motivo_filtro=filtro, hub=hub, alertas=alertas)
    av.elegivel_auto_aceite, av.motivo_sem_auto_aceite = _elegibilidade(rota, av, cfg)
    return av


def _elegibilidade(rota: Rota, av: Avaliacao, cfg: "Config") -> tuple[bool, str | None]:
    aa = cfg.auto_aceite
    if not aa.ativo:
        return False, "auto-aceite desligado"
    if av.motivo_filtro:
        return False, f"filtrada: {av.motivo_filtro}"
    if not av.nome_valido:
        return False, "nomenclatura fora do padrão"
    if av.anomalias_datas:
        return False, "anomalia de datas"
    if rota.erros_parse or rota.veiculo is None:
        return False, "card com dados incompletos"
    if not cfg.filtros.veiculos or not cfg.filtros.origens:
        return False, "auto-aceite exige filtros de veículo e origem"
    if aa.exigir_hub_ok and (av.hub is None or not av.hub.ok):
        return False, f"hub não está OK ({av.hub.status if av.hub else 'sem dados'})"
    return True, None
