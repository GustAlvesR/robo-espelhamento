"""Valida o JavaScript de extração contra páginas que imitam o AppSheet (tests/fixtures)."""
from pathlib import Path

import pytest
from conftest import FIX, TZ

from rotas_extras import address_lookup, scraper
from rotas_extras.config import Config
from rotas_extras.parser import parse_card


@pytest.fixture
def pagina(navegador):
    page = navegador.new_page(viewport={"width": 1200, "height": 800})
    yield page
    page.close()


def _abrir(page, nome):
    page.goto((FIX / nome).as_uri())


def test_extrai_todos_os_cards_com_rolagem_lazy(pagina):
    _abrir(pagina, "rotas.html")
    ext = scraper.extrair_cards(pagina, Config())
    assert ext.botoes_sem_card == 0
    rotas = [parse_card(c, TZ) for c in ext.cards]
    nomes = [r.nome for r in rotas]
    assert len(rotas) == 5, nomes
    assert nomes.count("BRSP21-BRSP11-SSP34.SOW.VEN") == 2
    assert len({r.chave for r in rotas}) == 5
    r0 = next(r for r in rotas if r.tempo_limite_texto == "00:18:00")
    assert (r0.veiculo, r0.tipo, r0.secao, r0.origem) == ("TOCO", "CAPTAÇÃO", "FULLS SP", "BRSP21")
    assert r0.eta_texto == "16/10/2026 20:00" and r0.cpt_texto == "16/10/2026 22:00"
    mg = next(r for r in rotas if r.origem == "BRMG03")
    assert mg.secao == "FULLS MG" and mg.veiculo == "TRUCK"
    assert ext.cards[0]["chave_dom"].startswith("GRUPO_ROTA_EXTRA")


def test_aceite_clica_no_card_certo(pagina, tmp_path):
    _abrir(pagina, "rotas.html")
    cfg = Config()
    cfg.auto_aceite.timeout_confirmacao_s = 3
    rotas = [parse_card(c, TZ) for c in scraper.extrair_cards(pagina, cfg).cards]
    alvo = next(r for r in rotas if r.nome.startswith("BRSP21") and r.veiculo == "TRUCK")
    res = scraper.aceitar_rota(pagina, alvo, cfg, tmp_path)
    assert res.status == "aceita_provavel", res
    assert res.clicou and Path(res.screenshot).exists()
    restantes = [parse_card(c, TZ) for c in scraper.extrair_cards(pagina, cfg).cards]
    assert alvo.chave not in {r.chave for r in restantes}
    # o outro card com o mesmo nome (TOCO) continua lá
    assert any(r.nome.startswith("BRSP21") and r.veiculo == "TOCO" for r in restantes)
    assert pagina.evaluate("window.cliques") == 1


def test_aceite_com_dialogo(pagina, tmp_path):
    _abrir(pagina, "rotas.html")
    cfg = Config()
    cfg.auto_aceite.timeout_confirmacao_s = 3
    rotas = [parse_card(c, TZ) for c in scraper.extrair_cards(pagina, cfg).cards]
    mg = next(r for r in rotas if r.origem == "BRMG03")
    assert scraper.aceitar_rota(pagina, mg, cfg, tmp_path).status == "dialogo_pendente"

    _abrir(pagina, "rotas.html")
    cfg.auto_aceite.confirmar_dialogo = True
    assert scraper.aceitar_rota(pagina, mg, cfg, tmp_path).status == "aceita_provavel"


def test_aceite_de_card_inexistente(pagina, tmp_path):
    _abrir(pagina, "rotas.html")
    cfg = Config()
    falsa = parse_card({"label": "BRSP99-X.Y", "descricao": "ETA 01/01/2027 10:00 | CPT 01/01/2027 12:00",
                        "texto": ""}, TZ)
    assert scraper.aceitar_rota(pagina, falsa, cfg, tmp_path).status == "nao_encontrada"
    assert pagina.evaluate("window.cliques || 0") == 0


def test_le_tabela_virtualizada_nos_dois_eixos(pagina):
    _abrir(pagina, "tabela.html")
    cfg = Config()
    linhas = address_lookup.ler_tabela(pagina, cfg)
    assert len(linhas) == 60
    por_loc = {ln["Localização Fixa"]: ln for ln in linhas}
    assert por_loc["BRSP7"]["Endereço"] == "Av. Vitória, 7"
    assert por_loc["BRSP5"]["Restrição APP Sheet"] == "Bloqueado"
    assert por_loc["BRSP9"]["MOTIVO RESTRIÇÃO"] == "SEM OPERAÇÃO MELI"
    assert por_loc["BRSP3"]["PERFIL RESTRIÇÃO"] == "CARRETA"
    assert por_loc["BRSP59"]["ID"] == "1059" and por_loc["BRSP59"]["LINK"] == "http://link/59"
    assert por_loc["BRSP4"]["Qual?"] is None and por_loc["BRSP4"]["CNPJ"] is None
    assert not any("_problemas" in ln for ln in linhas)


def test_discover_table(pagina, tmp_path):
    _abrir(pagina, "tabela.html")
    res = address_lookup.descobrir_tabela(pagina, Config(), ["Vitória", "MACRO REGIÃO"], tmp_path)
    assert res["breadcrumb_atual"] == "TABELA DE ENDERECO"
    assert res["alvos"]["MACRO REGIÃO"]["ocorrencias"] >= 1
    assert (tmp_path / "discover_table.json").exists() and (tmp_path / "tabela.png").exists()
    texto = address_lookup.formatar_descoberta(res)
    assert "Alvo 'MACRO REGIÃO'" in texto
