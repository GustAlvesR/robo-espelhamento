"""Ciclo completo do Monitor contra as páginas simuladas (navegador real, notificação falsa)."""
from datetime import datetime
from pathlib import Path

import pytest
from conftest import FIX, TZ

from rotas_extras import main as main_mod
from rotas_extras import scraper
from rotas_extras.config import Config
from rotas_extras.notifier import Notificador
from rotas_extras.state import Estado


class CanalMemoria:
    nome = "memoria"

    def __init__(self):
        self.mensagens = []

    def enviar(self, msg):
        self.mensagens.append(msg)


@pytest.fixture
def monitor(navegador, tmp_path, monkeypatch):
    cfg = Config()
    cfg.base_dir = tmp_path
    cfg.app.url_rotas = (FIX / "rotas.html").as_uri()
    cfg.app.url_tabela_endereco = (FIX / "tabela.html").as_uri()
    cfg.navegador.espera_cards_s = 1
    cfg.filtros.veiculos = ["TOCO", "TRUCK"]
    cfg.filtros.origens = ["BRSP21"]

    def abrir_contexto(pw, cfg, forcar_visivel=False):
        return navegador.new_context(viewport={"width": 1200, "height": 800})

    monkeypatch.setattr(scraper, "abrir_contexto", abrir_contexto)
    canal = CanalMemoria()
    estado = Estado(tmp_path / "estado.json")
    m = main_mod.Monitor(cfg, pw=None, notificador=Notificador(cfg.notificacao, canais=[canal]), estado=estado)
    m.agora = lambda: datetime(2026, 10, 16, 15, 0, tzinfo=TZ)
    m.canal = canal
    yield m
    m.encerrar()


def test_dois_ciclos(monitor, tmp_path):
    assert monitor.atualizar_tabela()
    assert len(monitor.cache.linhas) == 60

    monitor.ciclo_protegido()
    assert monitor.estado.contadores["falhas_seguidas"] == 0
    titulos = [m.titulo for m in monitor.canal.mensagens]
    # BRSP21 TOCO, BRSP21 TRUCK (mesmo nome, ETA/CPT diferentes) e o nome fora do padrão; BRSP10 e BRMG03 filtradas
    assert len(titulos) == 3, titulos
    assert sum("Rota extra ofertada: BRSP21-BRSP11-SSP34.SOW.VEN" in t for t in titulos) == 2
    assert any("nomenclatura fora do padrão" in t for t in titulos)
    assert [v["status"] for v in monitor.estado.rotas.values()].count("filtrada") == 2
    hub = dict(monitor.canal.mensagens[0].secoes[1][1])
    assert hub["Situação"] == "OK" and hub["Perfil restrição"] == "CARRETA"

    # nada foi clicado e os cards continuam na tela; o 2º ciclo não repete avisos
    assert monitor.page.evaluate("window.cliques || 0") == 0
    monitor.ciclo_protegido()
    assert len(monitor.canal.mensagens) == 3
    monitor.estado.salvar()
    assert Path(monitor.estado.caminho).exists()


class CanalQuebrado:
    nome = "quebrado"

    def __init__(self):
        self.chamadas = 0

    def enviar(self, msg):
        from rotas_extras.notifier import ErroEnvio
        self.chamadas += 1
        raise ErroEnvio("fora do ar", repetivel=False)


def test_falha_de_envio_mantem_pendente_e_reenvia(monitor):
    quebrado = CanalQuebrado()
    monitor.notificador.canais = [quebrado]
    monitor.ciclo_protegido()
    assert quebrado.chamadas == 1  # depois da 1ª falha, o resto do ciclo não tenta de novo
    pendentes = [k for k in monitor.estado.rotas if monitor.estado.precisa_processar(k)]
    assert len(pendentes) == 3

    monitor.notificador.canais = [monitor.canal]
    monitor.ciclo_protegido()
    assert len(monitor.canal.mensagens) == 3
    assert not any(monitor.estado.precisa_processar(k) for k in monitor.estado.rotas)
