from datetime import datetime

import pytest
from conftest import NOME, TZ

from rotas_extras.parser import nome_valido, parse_card, validar_datas
from rotas_extras.regras import aplicar_filtros, avaliar


@pytest.mark.parametrize("nome,esperado", [
    (NOME, True),
    ("brsp21-brsp11-ssp34.sow.ven", True),
    (NOME + " ", True),
    ("BRMG3-XPTO.A", True),
    ("BRSP21-BRSP11", False),
    ("BRSP21.SOW", False),
    ("XPTO-BRSP11.SOW", False),
    ("BRS21-BRSP11.SOW", False),
    ("", False),
])
def test_nomenclatura(cfg, nome, esperado):
    assert nome_valido(nome, cfg.regex_nome) is esperado


def test_datas_ok(bruto, agora):
    assert validar_datas(parse_card(bruto(), TZ), agora) == []


def test_eta_depois_do_cpt(bruto, agora):
    r = parse_card(bruto(descricao="DATA E HORA ETA 16/10/2026 23:00 | DATA E HORA CPT 16/10/2026 22:00"), TZ)
    assert any("não é anterior ao CPT" in a for a in validar_datas(r, agora))


def test_eta_igual_cpt(bruto, agora):
    r = parse_card(bruto(descricao="DATA E HORA ETA 16/10/2026 22:00 | DATA E HORA CPT 16/10/2026 22:00"), TZ)
    assert any("não é anterior" in a for a in validar_datas(r, agora))


def test_eta_no_passado(bruto):
    r = parse_card(bruto(), TZ)
    assert any("já passou" in a for a in validar_datas(r, datetime(2026, 10, 16, 20, 30, tzinfo=TZ)))


def test_datas_ausentes(bruto, agora):
    r = parse_card(bruto(descricao="", texto=NOME), TZ)
    anomalias = validar_datas(r, agora)
    assert "ETA ausente ou ilegível" in anomalias and "CPT ausente ou ilegível" in anomalias


def test_filtro_veiculo(cfg, bruto, agora):
    r = parse_card(bruto(), TZ)
    cfg.filtros.veiculos = ["TRUCK"]
    assert "veículo TOCO" in aplicar_filtros(r, cfg.filtros, True, agora)
    cfg.filtros.veiculos = ["toco", "TRUCK"]
    assert aplicar_filtros(r, cfg.filtros, True, agora) is None


def test_veiculo_nao_identificado_nao_e_filtrado(cfg, bruto, agora):
    r = parse_card(bruto(texto=NOME), TZ)
    cfg.filtros.veiculos = ["TRUCK"]
    assert r.veiculo is None
    assert aplicar_filtros(r, cfg.filtros, True, agora) is None


def test_filtro_origem_por_prefixo(cfg, bruto, agora):
    r = parse_card(bruto(), TZ)
    cfg.filtros.origens = ["BRSP2"]
    assert aplicar_filtros(r, cfg.filtros, True, agora) is None
    cfg.filtros.origens = ["BRMG"]
    assert "origem BRSP21" in aplicar_filtros(r, cfg.filtros, True, agora)
    # nome fora do padrão: origem não é confiável, então não filtra (vai avisar com alerta)
    assert aplicar_filtros(r, cfg.filtros, False, agora) is None


def test_filtro_eta_minima(cfg, bruto):
    r = parse_card(bruto(), TZ)
    cfg.filtros.eta_minima_min = 60
    assert "ETA em 30 min" in aplicar_filtros(r, cfg.filtros, True, datetime(2026, 10, 16, 19, 30, tzinfo=TZ))
    assert aplicar_filtros(r, cfg.filtros, True, datetime(2026, 10, 16, 18, 0, tzinfo=TZ)) is None
    # ETA já passada não é filtrada: sai como anomalia
    assert aplicar_filtros(r, cfg.filtros, True, datetime(2026, 10, 16, 21, 0, tzinfo=TZ)) is None


LINHA_OK = {"Localização Fixa": "BRSP21", "Endereço": "Rua A, 1", "POSSUI RESTRIÇÃO": "Não"}


def test_avaliar_nomenclatura_invalida_gera_alerta(cfg, bruto, agora):
    r = parse_card(bruto(label="ROTA ESTRANHA", labels=["ROTA ESTRANHA"]), TZ)
    av = avaliar(r, agora, cfg, [LINHA_OK])
    assert av.motivo_filtro is None
    assert any("Nomenclatura fora do padrão" in a for a in av.alertas)


def test_auto_aceite_elegivel_so_com_tudo_ok(cfg, bruto, agora):
    r = parse_card(bruto(), TZ)
    av = avaliar(r, agora, cfg, [LINHA_OK])
    assert not av.elegivel_auto_aceite and av.motivo_sem_auto_aceite == "auto-aceite desligado"

    cfg.auto_aceite.ativo = True
    cfg.filtros.veiculos, cfg.filtros.origens = ["TOCO"], ["BRSP"]
    av = avaliar(r, agora, cfg, [LINHA_OK])
    assert av.hub.status == "OK"
    assert av.elegivel_auto_aceite, av.motivo_sem_auto_aceite

    av = avaliar(r, agora, cfg, [])
    assert not av.elegivel_auto_aceite and "hub" in av.motivo_sem_auto_aceite

    tarde = datetime(2026, 10, 16, 21, 0, tzinfo=TZ)
    assert not avaliar(r, tarde, cfg, [LINHA_OK]).elegivel_auto_aceite


def test_auto_aceite_bloqueado_por_hub_incompativel(cfg, bruto, agora):
    cfg.auto_aceite.ativo = True
    cfg.filtros.veiculos, cfg.filtros.origens = ["TOCO"], ["BRSP"]
    linha = {**LINHA_OK, "POSSUI RESTRIÇÃO": "Sim", "PERFIL RESTRIÇÃO": "TOCO"}
    av = avaliar(parse_card(bruto(), TZ), agora, cfg, [linha])
    assert av.hub.status == "INCOMPATIVEL"
    assert not av.elegivel_auto_aceite
