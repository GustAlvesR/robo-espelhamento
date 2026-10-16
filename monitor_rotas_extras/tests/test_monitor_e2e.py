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
    cfg.auto_aceite.ativo = True
    cfg.auto_aceite.max_por_hora = 1
    cfg.auto_aceite.timeout_confirmacao_s = 3

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
    assert len(titulos) == 3, titulos
    assert any("ACEITA automaticamente" in t and "BRSP21" in t for t in titulos)
    assert any("nomenclatura fora do padrão" in t for t in titulos)

    status = {k.split("|")[0] + "|" + k.split("|")[1]: v["status"] for k, v in monitor.estado.rotas.items()}
    assert list(status.values()).count("filtrada") == 2
    aceites = [v["aceite"]["status"] for v in monitor.estado.rotas.values() if "aceite" in v]
    assert sorted(aceites) == ["aceita_provavel", "limite_hora"]
    assert (tmp_path / "logs" / "auto_aceite_auditoria.jsonl").read_text(encoding="utf-8").count("\n") == 2

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
    monitor.cfg.auto_aceite.ativo = False
    monitor.notificador.canais = [quebrado]
    monitor.ciclo_protegido()
    assert quebrado.chamadas == 1  # depois da 1ª falha, o resto do ciclo não tenta de novo
    pendentes = [k for k in monitor.estado.rotas if monitor.estado.precisa_processar(k)]
    assert len(pendentes) == 3

    monitor.notificador.canais = [monitor.canal]
    monitor.ciclo_protegido()
    assert len(monitor.canal.mensagens) == 3
    assert not any(monitor.estado.precisa_processar(k) for k in monitor.estado.rotas)
