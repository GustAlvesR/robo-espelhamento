from datetime import datetime, timedelta

from conftest import DESC, NOME, TZ

from rotas_extras.parser import extrair_origem, normalizar, parse_card, parse_tempo_limite


def test_card_completo(bruto):
    r = parse_card(bruto(), TZ)
    assert r.nome == NOME
    assert r.origem == "BRSP21"
    assert (r.qtd_veiculos, r.veiculo, r.veiculo_texto) == (1, "TOCO", "1 - TOCO")
    assert r.tipo == "CAPTAÇÃO"
    assert r.eta == datetime(2026, 10, 16, 20, 0, tzinfo=TZ)
    assert r.cpt == datetime(2026, 10, 16, 22, 0, tzinfo=TZ)
    assert r.eta_texto == "16/10/2026 20:00"
    assert r.tempo_limite == timedelta(minutes=18)
    assert r.tempo_limite_texto == "00:18:00"
    assert r.secao == "FULLS SP"
    assert r.erros_parse == []


def test_truck_sem_tempo_limite(bruto):
    texto = f"1 - TRUCK\nCAPTAÇÃO\n{NOME}\n{DESC}\nACEITAR"
    r = parse_card(bruto(texto=texto), TZ)
    assert r.veiculo == "TRUCK"
    assert r.tempo_limite is None and r.tempo_limite_texto is None


def test_chave_usa_nome_eta_cpt(bruto):
    a = parse_card(bruto(), TZ)
    b = parse_card(bruto(descricao=DESC.replace("20:00", "21:00")), TZ)
    c = parse_card(bruto(), TZ)
    assert a.chave != b.chave
    assert a.chave == c.chave
    assert a.chave == f"{NOME}|2026-10-16 20:00|2026-10-16 22:00"


def test_card_sem_datas_nao_quebra(bruto):
    r = parse_card(bruto(descricao="", texto=f"1 - TOCO\n{NOME}\nACEITAR"), TZ)
    assert r.eta is None and r.cpt is None
    assert r.chave.startswith(NOME)


def test_data_invalida_vira_erro_de_parse(bruto):
    r = parse_card(bruto(descricao="DATA E HORA ETA 31/02/2026 20:00 | DATA E HORA CPT 16/10/2026 22:00"), TZ)
    assert r.eta is None
    assert r.eta_texto == "31/02/2026 20:00"
    assert any("ETA com data inválida" in e for e in r.erros_parse)


def test_eta_com_segundos(bruto):
    r = parse_card(bruto(descricao="DATA E HORA ETA 16/10/2026 20:00:30 | DATA E HORA CPT 16/10/2026 22:00"), TZ)
    assert r.eta == datetime(2026, 10, 16, 20, 0, 30, tzinfo=TZ)


def test_nome_pelo_texto_quando_label_vazio(bruto):
    r = parse_card(bruto(label="", labels=[]), TZ)
    assert r.nome == NOME


def test_label_fora_do_padrao_e_mantido_inteiro(bruto):
    r = parse_card(bruto(label="ROTA TESTE 123", labels=["ROTA TESTE 123"]), TZ)
    assert r.nome == "ROTA TESTE 123"


def test_sem_nome_registra_erro(bruto):
    r = parse_card(bruto(label="", labels=[], texto="1 - TOCO\nACEITAR"), TZ)
    assert r.nome == ""
    assert "nome da rota não encontrado" in r.erros_parse


def test_tipo_coleta_nao_e_confundido_com_eta(bruto):
    r = parse_card(bruto(texto=f"1 - TRUCK\nCOLETA\n{NOME}\n{DESC}"), TZ)
    assert r.tipo == "COLETA"


def test_tempo_limite_regex():
    assert parse_tempo_limite("TEMPO LIMITE DE RETORNO: 01:30:00") == (timedelta(hours=1, minutes=30), "01:30:00")
    assert parse_tempo_limite("tempo limite de retorno 00:45") == (timedelta(minutes=45), "00:45")
    assert parse_tempo_limite("nada") == (None, None)


def test_normalizar_e_origem():
    assert normalizar("  Captação   Endereço ") == "CAPTACAO ENDERECO"
    assert extrair_origem(NOME) == "BRSP21"
    assert extrair_origem("brmg03.SOW") == "BRMG03"
