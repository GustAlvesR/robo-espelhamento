"""Valida o JavaScript de extração contra páginas que imitam o AppSheet (tests/fixtures)."""
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


def test_leitura_nunca_clica_nos_cards(pagina):
    _abrir(pagina, "rotas.html")
    scraper.extrair_cards(pagina, Config())
    scraper.extrair_cards(pagina, Config())
    assert pagina.evaluate("window.cliques || 0") == 0
    assert pagina.locator('[role="button"][aria-label="ACEITAR"]').count() == 5


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
