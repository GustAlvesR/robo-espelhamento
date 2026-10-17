import sys

from rotas_extras import scraper
from rotas_extras.config import Config


def test_localizar_navegador_configurado(tmp_path):
    cfg = Config()
    exe = tmp_path / "msedge.exe"
    exe.write_text("")
    cfg.navegador.executavel = str(exe)
    assert scraper.localizar_navegador(cfg) == str(exe)
    cfg.navegador.executavel = str(tmp_path / "nao_existe.exe")
    assert scraper.localizar_navegador(cfg) is None


def test_comando_usa_perfil_do_robo_sem_automacao(tmp_path):
    cmd = scraper.comando_login_manual("msedge.exe", tmp_path / "perfil_edge", "https://app/#view=ROTA%20EXTRA")
    assert cmd[0] == "msedge.exe"
    assert f"--user-data-dir={tmp_path / 'perfil_edge'}" in cmd
    assert cmd[-1] == "https://app/#view=ROTA%20EXTRA"
    assert not any("remote-debugging" in a or "automation" in a for a in cmd)


def test_abrir_navegador_manual_inicia_processo(tmp_path):
    cfg = Config()
    cfg.base_dir = tmp_path
    cfg.app.url_rotas = "https://app"
    cfg.navegador.executavel = sys.executable  # qualquer executável serve para testar o disparo
    proc = scraper.abrir_navegador_manual(cfg)
    proc.wait(timeout=30)
    assert (tmp_path / "perfil_edge").is_dir()
