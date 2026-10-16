from pathlib import Path

import pytest

from rotas_extras.config import ErroConfig, carregar_config

EXEMPLO = Path(__file__).resolve().parents[1] / "config.example.yaml"


def _copiar(tmp_path, texto=None):
    destino = tmp_path / "config.yaml"
    destino.write_text(texto if texto is not None else EXEMPLO.read_text(encoding="utf-8"), encoding="utf-8")
    return destino


def test_exemplo_carrega(tmp_path, monkeypatch):
    for v in ("ROTAS_TEAMS_WEBHOOK_URL", "ROTAS_EMAIL_DESTINATARIOS", "ROTAS_URL_ROTAS"):
        monkeypatch.delenv(v, raising=False)
    cfg = carregar_config(_copiar(tmp_path))
    assert cfg.monitor.intervalo_s == 45
    assert len(cfg.hub.tabela.colunas) == 16
    assert not hasattr(cfg, "auto_aceite")
    assert cfg.caminho("dados/x.json") == tmp_path / "dados/x.json"
    assert cfg.regex_nome.fullmatch("BRSP21-BRSP11-SSP34.SOW.VEN")


def test_env_sobrescreve_segredos(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ROTAS_EMAIL_DESTINATARIOS=a@x.com; b@x.com\n", encoding="utf-8")
    monkeypatch.delenv("ROTAS_EMAIL_DESTINATARIOS", raising=False)
    monkeypatch.setenv("ROTAS_TEAMS_WEBHOOK_URL", "https://webhook")
    cfg = carregar_config(_copiar(tmp_path))
    assert cfg.notificacao.teams.webhook_url == "https://webhook"
    assert cfg.notificacao.email.destinatarios == ["a@x.com", "b@x.com"]
    monkeypatch.delenv("ROTAS_EMAIL_DESTINATARIOS", raising=False)


def test_chave_desconhecida_e_erro(tmp_path):
    with pytest.raises(ErroConfig, match="ativ0"):
        carregar_config(_copiar(tmp_path, 'app: {url_rotas: "x"}\nfiltros: {ativ0: true}\n'))


def test_auto_aceite_nao_existe(tmp_path):
    with pytest.raises(ErroConfig, match="auto_aceite"):
        carregar_config(_copiar(tmp_path, 'app: {url_rotas: "x"}\nauto_aceite: {ativo: true}\n'))


def test_url_obrigatoria(tmp_path, monkeypatch):
    monkeypatch.delenv("ROTAS_URL_ROTAS", raising=False)
    with pytest.raises(ErroConfig, match="url_rotas"):
        carregar_config(_copiar(tmp_path, "monitor: {intervalo_s: 45}\n"))


def test_tipo_errado(tmp_path):
    with pytest.raises(ErroConfig, match="true/false"):
        carregar_config(_copiar(tmp_path, 'app: {url_rotas: "x"}\nnavegador: {headless: "sim"}\n'))
