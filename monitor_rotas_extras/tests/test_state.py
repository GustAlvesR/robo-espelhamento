import json
from datetime import timedelta

from conftest import TZ

from rotas_extras.parser import parse_card
from rotas_extras.state import Estado


def test_dedup_e_esquecimento(bruto, agora):
    est = Estado(None)
    r = parse_card(bruto(), TZ)
    assert est.registrar_presentes([r], agora) == [r.chave]
    assert est.precisa_processar(r.chave)
    est.marcar_notificada(r.chave, agora)
    assert est.registrar_presentes([r], agora + timedelta(minutes=1)) == []
    assert not est.precisa_processar(r.chave)

    # sumiu por pouco tempo: mantém (tolerância a leitura falha)
    assert est.esquecer_ausentes(set(), agora + timedelta(minutes=5), timedelta(minutes=10)) == []
    # sumiu de vez: esquece e, se voltar, é avisada de novo
    assert est.esquecer_ausentes(set(), agora + timedelta(minutes=12), timedelta(minutes=10)) == [r.chave]
    assert est.registrar_presentes([r], agora + timedelta(minutes=13)) == [r.chave]


def test_filtrada_nao_e_reprocessada(bruto, agora):
    est = Estado(None)
    r = parse_card(bruto(), TZ)
    est.registrar_presentes([r], agora)
    est.marcar_filtrada(r.chave, "veículo")
    assert not est.precisa_processar(r.chave)


def test_cooldown_de_alertas(agora):
    est = Estado(None)
    cd = timedelta(minutes=30)
    assert est.pode_alertar("sessao", agora, cd)
    est.marcar_alerta("sessao", agora)
    assert not est.pode_alertar("sessao", agora + timedelta(minutes=10), cd)
    assert est.pode_alertar("sessao", agora + timedelta(minutes=31), cd)
    est.marcar_alerta("vazio", agora)
    assert not est.pode_alertar("vazio", agora + timedelta(days=3), None)
    est.limpar_alerta("vazio")
    assert est.pode_alertar("vazio", agora, None)


def test_aceites_na_ultima_hora(agora):
    est = Estado(None)
    est.registrar_clique_aceite(agora - timedelta(minutes=70))
    est.registrar_clique_aceite(agora - timedelta(minutes=10))
    assert est.aceites_ultima_hora(agora) == 1


def test_persistencia_e_arquivo_corrompido(tmp_path, bruto, agora):
    caminho = tmp_path / "estado.json"
    est = Estado.carregar(caminho)
    r = parse_card(bruto(), TZ)
    est.registrar_presentes([r], agora)
    est.marcar_aceite(r.chave, "aceita_provavel", "ok", agora)
    est.salvar()
    de_novo = Estado.carregar(caminho)
    assert de_novo.aceite_da_rota(r.chave)["status"] == "aceita_provavel"
    assert json.loads(caminho.read_text(encoding="utf-8"))["versao"] == 1

    caminho.write_text("{quebrado", encoding="utf-8")
    vazio = Estado.carregar(caminho)
    assert vazio.rotas == {}
    assert list(tmp_path.glob("estado.corrompido-*.json"))


def test_origem_sem_match_uma_vez_por_dia(agora):
    est = Estado(None)
    assert est.origem_sem_match_nova_hoje("BRXX1", agora)
    assert not est.origem_sem_match_nova_hoje("BRXX1", agora + timedelta(hours=1))
    assert est.origem_sem_match_nova_hoje("BRXX1", agora + timedelta(days=1))
