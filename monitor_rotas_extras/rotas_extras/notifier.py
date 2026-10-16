"""Notificações: Teams (webhook do Power Automate/Workflows com Adaptive Card) e e-mail via Outlook (COM)."""
from __future__ import annotations

import html
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .config import Config, EmailCfg, NotificacaoCfg, TeamsCfg
    from .parser import Rota
    from .regras import Avaliacao, ResultadoAceite

log = logging.getLogger(__name__)

_COR_NIVEL = {"info": "Accent", "sucesso": "Good", "alerta": "Warning", "erro": "Attention"}


@dataclass
class Mensagem:
    titulo: str
    resumo: str
    nivel: str = "info"  # info | sucesso | alerta | erro
    secoes: list[tuple[str | None, list[tuple[str, str]]]] = field(default_factory=list)
    alertas: list[str] = field(default_factory=list)
    link: str | None = None


class ErroEnvio(Exception):
    def __init__(self, msg: str, repetivel: bool = True):
        super().__init__(msg)
        self.repetivel = repetivel


class Canal(Protocol):
    nome: str

    def enviar(self, msg: Mensagem) -> None: ...


# --------------------------------------------------------------------------------------------
# Formatação
# --------------------------------------------------------------------------------------------

def fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%d/%m/%Y %H:%M") if dt else "?"


def fmt_delta(td: timedelta) -> str:
    total_min = int(abs(td.total_seconds()) // 60)
    h, m = divmod(total_min, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def montar_adaptive_card(msg: Mensagem) -> dict:
    """Adaptive Card 1.4 (versão aceita pelo Teams em desktop e celular)."""
    corpo: list[dict] = [{"type": "TextBlock", "text": msg.titulo, "weight": "Bolder", "size": "Medium",
                          "wrap": True, "color": _COR_NIVEL.get(msg.nivel, "Default")},
                         {"type": "TextBlock", "text": msg.resumo, "wrap": True, "spacing": "Small"}]
    if msg.alertas:
        corpo.append({"type": "Container", "style": "attention", "spacing": "Medium",
                      "items": [{"type": "TextBlock", "text": f"⚠ {a}", "wrap": True, "color": "Attention"}
                                for a in msg.alertas]})
    for titulo, fatos in msg.secoes:
        if titulo:
            corpo.append({"type": "TextBlock", "text": titulo, "weight": "Bolder", "spacing": "Medium",
                          "separator": True})
        corpo.append({"type": "FactSet", "facts": [{"title": k, "value": v or "-"} for k, v in fatos]})
    card: dict = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                  "version": "1.4", "msteams": {"width": "Full"}, "body": corpo}
    if msg.link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Abrir no AppSheet", "url": msg.link}]
    return card


def montar_payload_teams(msg: Mensagem) -> dict:
    """Formato esperado pelo template 'Enviar alertas de webhook' do Workflows (Power Automate)."""
    return {"type": "message", "summary": msg.titulo,
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
                             "content": montar_adaptive_card(msg)}]}


def montar_html_email(msg: Mensagem) -> str:
    e = html.escape
    partes = [f"<h2 style='margin:0'>{e(msg.titulo)}</h2><p>{e(msg.resumo)}</p>"]
    if msg.alertas:
        itens = "".join(f"<li>{e(a)}</li>" for a in msg.alertas)
        partes.append(f"<div style='background:#fde7e9;padding:8px;border-left:4px solid #c50f1f'>"
                      f"<b>Alertas</b><ul style='margin:4px 0'>{itens}</ul></div>")
    for titulo, fatos in msg.secoes:
        if titulo:
            partes.append(f"<h3 style='margin-bottom:4px'>{e(titulo)}</h3>")
        linhas = "".join(f"<tr><td style='padding:2px 12px 2px 0;color:#555'>{e(k)}</td>"
                         f"<td>{e(v or '-')}</td></tr>" for k, v in fatos)
        partes.append(f"<table style='border-collapse:collapse;font-size:13px'>{linhas}</table>")
    if msg.link:
        partes.append(f"<p><a href='{e(msg.link)}'>Abrir no AppSheet</a></p>")
    return "<div style='font-family:Segoe UI,Arial,sans-serif'>" + "".join(partes) + "</div>"


def mensagem_rota(rota: "Rota", av: "Avaliacao", primeira_vez: datetime, agora: datetime, cfg: "Config",
                  aceite: "ResultadoAceite | None" = None) -> Mensagem:
    """Mensagem de nova rota com dados da rota, do hub, alertas e resultado do auto-aceite."""
    expira = primeira_vez + timedelta(minutes=cfg.monitor.janela_oferta_min)
    restante = expira - agora
    restante_txt = f"≤ {fmt_delta(restante)} (até ~{expira:%H:%M})" if restante.total_seconds() > 0 \
        else f"provavelmente expirada (~{expira:%H:%M})"
    eta_txt = fmt_dt(rota.eta) if rota.eta else (rota.eta_texto or "?")
    if rota.eta:
        diff = rota.eta - agora
        eta_txt += f" ({'em' if diff.total_seconds() > 0 else 'há'} {fmt_delta(diff)})"

    fatos_rota = [
        ("Rota", rota.nome or "?"),
        ("Veículo", rota.veiculo_texto or rota.veiculo or "?"),
        ("Tipo", rota.tipo or "-"),
        ("Origem", rota.origem or "?"),
        ("ETA", eta_txt),
        ("CPT", fmt_dt(rota.cpt) if rota.cpt else (rota.cpt_texto or "?")),
        ("Tempo limite retorno", rota.tempo_limite_texto or "-"),
        ("Tempo restante (est.)", restante_txt),
        ("Seção", rota.secao or "-"),
        ("Detectada em", f"{primeira_vez:%d/%m %H:%M:%S}"),
    ]
    secoes: list[tuple[str | None, list[tuple[str, str]]]] = [(None, fatos_rota)]

    hub = av.hub
    if hub:
        from .address_lookup import (COL_ENDERECO, COL_MICRO, COL_MOTIVO, COL_PERFIL, COL_POSSUI,
                                     COL_RESTRICAO_APP, valor)
        fatos_hub = [("Situação", hub.status), ("Avaliação", "; ".join(hub.mensagens) or "-")]
        if hub.linha:
            ln = hub.linha
            fatos_hub += [("Casou por", hub.coluna_match or "-"), ("Endereço", valor(ln, COL_ENDERECO) or "-"),
                          ("Micro região", valor(ln, COL_MICRO) or "-"),
                          ("Possui restrição", valor(ln, COL_POSSUI) or "-"),
                          ("Perfil restrição", valor(ln, COL_PERFIL) or "-"),
                          ("Motivo restrição", valor(ln, COL_MOTIVO) or "-"),
                          ("Restrição APP Sheet", valor(ln, COL_RESTRICAO_APP) or "-")]
        for i, cand in enumerate(hub.candidatos[:3], 1):
            fatos_hub.append((f"Candidato {i}", f"{valor(cand, COL_ENDERECO) or '?'} | "
                                                f"perfil: {valor(cand, COL_PERFIL) or '-'}"))
        secoes.append(("Hub de origem", fatos_hub))

    if aceite:
        secoes.append(("Auto-aceite", [("Resultado", aceite.status), ("Detalhe", aceite.detalhe or "-")]))
    elif cfg.auto_aceite.ativo:
        secoes.append(("Auto-aceite", [("Resultado", "não tentado"), ("Motivo", av.motivo_sem_auto_aceite or "-")]))

    if aceite and aceite.sucesso:
        nivel, prefixo = "sucesso", "Rota extra ACEITA automaticamente"
    elif av.alertas:
        nivel, prefixo = "alerta", "Rota extra (com alertas)"
    else:
        nivel, prefixo = "info", "Nova rota extra"
    if not av.nome_valido:
        prefixo = "Rota extra com nomenclatura fora do padrão"
    resumo = (f"{rota.veiculo or '?'} | ETA {fmt_dt(rota.eta)} | CPT {fmt_dt(rota.cpt)} | "
              f"restam {restante_txt}")
    return Mensagem(titulo=f"🚚 {prefixo}: {rota.nome or '?'}", resumo=resumo, nivel=nivel, secoes=secoes,
                    alertas=av.alertas, link=cfg.app.url_rotas or None)


# --------------------------------------------------------------------------------------------
# Canais
# --------------------------------------------------------------------------------------------

def _host(url: str) -> str:
    return urlsplit(url).netloc or "?"


class CanalTeams:
    """POST do Adaptive Card no webhook do Power Automate. Usa proxy do sistema (ou o configurado)."""
    nome = "teams"

    def __init__(self, cfg: "TeamsCfg", proxy: str = "", abrir: Callable | None = None):
        self.cfg = cfg
        if abrir is None:
            handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
            abrir = urllib.request.build_opener(*handlers).open
        self._abrir = abrir

    def enviar(self, msg: Mensagem) -> None:
        if not self.cfg.webhook_url:
            raise ErroEnvio("webhook do Teams não configurado", repetivel=False)
        corpo = json.dumps(montar_payload_teams(msg), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.cfg.webhook_url, data=corpo, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with self._abrir(req, timeout=self.cfg.timeout_s) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            trecho = e.read()[:300].decode("utf-8", "replace")
            # A URL tem assinatura (segredo): logamos só o host.
            raise ErroEnvio(f"Teams HTTP {e.code} em {_host(self.cfg.webhook_url)}: {trecho}",
                            repetivel=e.code in (408, 429) or e.code >= 500) from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            raise ErroEnvio(f"Teams sem conexão ({_host(self.cfg.webhook_url)}): {e}") from e
        if not 200 <= status < 300:
            raise ErroEnvio(f"Teams respondeu HTTP {status}")


class CanalOutlook:
    """E-mail pelo Outlook desktop via COM (pywin32). Não funciona com o 'Novo Outlook'."""
    nome = "email"

    def __init__(self, cfg: "EmailCfg"):
        self.cfg = cfg

    def enviar(self, msg: Mensagem) -> None:
        if not self.cfg.destinatarios:
            raise ErroEnvio("nenhum destinatário de e-mail configurado", repetivel=False)
        try:
            import pythoncom
            import win32com.client
        except ImportError as e:
            raise ErroEnvio("pywin32 não instalado (pip install pywin32)", repetivel=False) from e
        pythoncom.CoInitialize()
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            mail = outlook.CreateItem(0)
            mail.To = "; ".join(self.cfg.destinatarios)
            mail.Subject = f"{self.cfg.assunto_prefixo} {msg.titulo}".strip()
            mail.HTMLBody = montar_html_email(msg)
            if msg.nivel in ("alerta", "erro", "sucesso"):
                mail.Importance = 2
            mail.Send()
        except pythoncom.com_error as e:
            raise ErroEnvio(f"Outlook COM: {e}") from e
        finally:
            pythoncom.CoUninitialize()


class Notificador:
    """Tenta os canais em ordem (Teams, depois e-mail), com retry e backoff exponencial."""

    def __init__(self, cfg: "NotificacaoCfg", dry_run: bool = False, canais: list[Canal] | None = None,
                 dormir: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.dry_run = dry_run
        self._dormir = dormir
        if canais is None:
            canais = []
            if cfg.teams.ativo:
                canais.append(CanalTeams(cfg.teams, cfg.proxy))
            if cfg.email.ativo:
                canais.append(CanalOutlook(cfg.email))
        self.canais = canais

    def enviar(self, msg: Mensagem) -> bool:
        """True se algum canal entregou. No dry-run apenas loga."""
        if self.dry_run:
            log.info("[DRY-RUN] Notificação não enviada: %s | %s | alertas=%s", msg.titulo, msg.resumo, msg.alertas)
            return True
        if not self.canais:
            log.error("Nenhum canal de notificação ativo")
            return False
        for canal in self.canais:
            for tentativa in range(1, self.cfg.tentativas + 1):
                try:
                    canal.enviar(msg)
                    log.info("Notificação enviada via %s: %s", canal.nome, msg.titulo)
                    return True
                except ErroEnvio as e:
                    log.warning("Falha no envio via %s (tentativa %d/%d): %s", canal.nome, tentativa,
                                self.cfg.tentativas, e)
                    if not e.repetivel or tentativa == self.cfg.tentativas:
                        break
                    self._dormir(self.cfg.backoff_inicial_s * 2 ** (tentativa - 1))
                except Exception:
                    log.exception("Erro inesperado no canal %s; passando para o próximo", canal.nome)
                    break
        log.error("Todos os canais falharam para: %s", msg.titulo)
        return False
