import io
import urllib.error
from datetime import timedelta

import pytest
from conftest import TZ

from rotas_extras.config import EmailCfg, NotificacaoCfg, TeamsCfg
from rotas_extras.notifier import (CanalOutlook, CanalTeams, ErroEnvio, Mensagem, Notificador, mensagem_rota,
                                   montar_payload_teams)
from rotas_extras.parser import parse_card
from rotas_extras.regras import ResultadoAceite, avaliar

MSG = Mensagem(titulo="T", resumo="R", alertas=["a1"], secoes=[("Hub", [("Endereço", "Rua A")])], link="https://x")


class CanalFalso:
    def __init__(self, nome, falhas, repetivel=True):
        self.nome, self.falhas, self.repetivel, self.chamadas = nome, falhas, repetivel, 0

    def enviar(self, msg):
        self.chamadas += 1
        if self.chamadas <= self.falhas:
            raise ErroEnvio("falhou", self.repetivel)


def test_payload_teams_formato_workflows():
    p = montar_payload_teams(MSG)
    assert p["type"] == "message"
    anexo = p["attachments"][0]
    assert anexo["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = anexo["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert card["actions"][0]["url"] == "https://x"
    tipos = [b["type"] for b in card["body"]]
    assert "FactSet" in tipos and "Container" in tipos


def test_retry_com_backoff_e_fallback_para_email():
    esperas = []
    teams, email = CanalFalso("teams", falhas=99), CanalFalso("email", falhas=0)
    n = Notificador(NotificacaoCfg(tentativas=3, backoff_inicial_s=2), canais=[teams, email], dormir=esperas.append)
    assert n.enviar(MSG) is True
    assert teams.chamadas == 3 and email.chamadas == 1
    assert esperas == [2, 4]


def test_erro_nao_repetivel_nao_tenta_de_novo():
    teams = CanalFalso("teams", falhas=99, repetivel=False)
    n = Notificador(NotificacaoCfg(), canais=[teams], dormir=lambda s: None)
    assert n.enviar(MSG) is False
    assert teams.chamadas == 1


def test_dry_run_nao_envia():
    teams = CanalFalso("teams", falhas=0)
    assert Notificador(NotificacaoCfg(), dry_run=True, canais=[teams]).enviar(MSG) is True
    assert teams.chamadas == 0


class RespostaFalsa:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_canal_teams_http():
    cfg = TeamsCfg(webhook_url="https://prod.westus.logic.azure.com/workflows/abc?sig=SEGREDO")
    enviados = []

    def abrir_ok(req, timeout):
        enviados.append(req)
        return RespostaFalsa(202)

    CanalTeams(cfg, abrir=abrir_ok).enviar(MSG)
    assert enviados[0].get_method() == "POST"

    def abrir_erro(codigo):
        def _abrir(req, timeout):
            raise urllib.error.HTTPError(req.full_url, codigo, "x", {}, io.BytesIO(b"detalhe"))
        return _abrir

    with pytest.raises(ErroEnvio) as e400:
        CanalTeams(cfg, abrir=abrir_erro(400)).enviar(MSG)
    assert not e400.value.repetivel and "SEGREDO" not in str(e400.value)
    with pytest.raises(ErroEnvio) as e500:
        CanalTeams(cfg, abrir=abrir_erro(502)).enviar(MSG)
    assert e500.value.repetivel


def test_canal_teams_sem_url():
    with pytest.raises(ErroEnvio) as e:
        CanalTeams(TeamsCfg()).enviar(MSG)
    assert not e.value.repetivel


def test_outlook_sem_destinatario():
    with pytest.raises(ErroEnvio):
        CanalOutlook(EmailCfg()).enviar(MSG)


def test_mensagem_rota(cfg, bruto, agora):
    cfg.app.url_rotas = "https://www.appsheet.com/start/x#view=ROTA%20EXTRA"
    r = parse_card(bruto(), TZ)
    linhas = [{"Localização Fixa": "BRSP21", "Endereço": "Rua A", "POSSUI RESTRIÇÃO": "Sim",
               "PERFIL RESTRIÇÃO": "CARRETA"}]
    av = avaliar(r, agora, cfg, linhas)
    msg = mensagem_rota(r, av, primeira_vez=agora - timedelta(minutes=5), agora=agora, cfg=cfg)
    fatos = dict(msg.secoes[0][1])
    assert fatos["Veículo"] == "1 - TOCO"
    assert fatos["Tempo restante (est.)"].startswith("≤ 25 min")
    assert "em 5h00" in fatos["ETA"]
    hub = dict(msg.secoes[1][1])
    assert hub["Endereço"] == "Rua A" and hub["Situação"] == "OK"
    assert msg.nivel == "info" and not msg.alertas

    ruim = parse_card(bruto(label="XPTO", labels=["XPTO"]), TZ)
    msg = mensagem_rota(ruim, avaliar(ruim, agora, cfg, linhas), agora, agora, cfg,
                        ResultadoAceite("simulado", "dry-run"))
    assert "nomenclatura fora do padrão" in msg.titulo and msg.nivel == "alerta"
    assert dict(msg.secoes[-1][1])["Resultado"] == "simulado"
