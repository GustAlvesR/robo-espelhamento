from rotas_extras.address_lookup import avaliar_restricao, buscar_hub, veiculos_no_texto
from rotas_extras.config import HubCfg


def linha(**kw):
    base = {"Operação": "MELI", "ID": "1", "MICRO REGIÃO": "SP21", "Endereço": "Rua A, 100",
            "Localização Fixa": "BRSP21 - Cajamar", "POSSUI RESTRIÇÃO": "Não", "PERFIL RESTRIÇÃO": None,
            "MOTIVO RESTRIÇÃO": None, "Restrição APP Sheet": None}
    base.update(kw)
    return base


def test_match_por_localizacao_fixa():
    res = buscar_hub("BRSP21", "TOCO", [linha(), linha(**{"Localização Fixa": "BRSP22", "ID": "2"})], HubCfg())
    assert res.status == "OK" and res.coluna_match == "Localização Fixa"
    assert res.linha["Endereço"] == "Rua A, 100"
    assert "Hub sem restrição" in res.mensagens


def test_token_inteiro_nao_casa_prefixo():
    res = buscar_hub("BRSP2", "TOCO", [linha()], HubCfg())
    assert res.status == "SEM_MATCH"


def test_cai_para_micro_regiao():
    ln = linha(**{"Localização Fixa": "-23.3, -46.8", "MICRO REGIÃO": "BRSP21"})
    res = buscar_hub("BRSP21", "TOCO", [ln], HubCfg())
    assert res.status == "OK" and res.coluna_match == "MICRO REGIÃO"


def test_restrito_a_carreta_e_rota_toco_ou_truck_ok():
    ln = linha(**{"POSSUI RESTRIÇÃO": "Sim", "PERFIL RESTRIÇÃO": "CARRETA"})
    for v in ("TOCO", "TRUCK"):
        res = buscar_hub("BRSP21", v, [ln], HubCfg())
        assert res.status == "OK", res.mensagens
        assert "compatível" in res.mensagens[0]


def test_perfil_cita_o_veiculo_da_rota_incompativel():
    ln = linha(**{"POSSUI RESTRIÇÃO": "Sim", "PERFIL RESTRIÇÃO": "TRUCK / CARRETA"})
    res = buscar_hub("BRSP21", "TRUCK", [ln], HubCfg())
    assert res.status == "INCOMPATIVEL"


def test_perfil_permitidos():
    cfg = HubCfg(perfil_indica="permitidos")
    ln = linha(**{"POSSUI RESTRIÇÃO": "Sim", "PERFIL RESTRIÇÃO": "SOMENTE TOCO"})
    assert buscar_hub("BRSP21", "TOCO", [ln], cfg).status == "OK"
    assert buscar_hub("BRSP21", "TRUCK", [ln], cfg).status == "INCOMPATIVEL"


def test_sem_operacao_meli_e_alerta():
    ln = linha(**{"POSSUI RESTRIÇÃO": "Sim", "PERFIL RESTRIÇÃO": "SEM OPERAÇÃO MELI"})
    res = buscar_hub("BRSP21", "TOCO", [ln], HubCfg())
    assert res.status == "ALERTA"
    assert res.mensagens == ["Hub marcado como 'SEM OPERAÇÃO MELI'"]


def test_em_validacao_e_alerta():
    ln = linha(**{"POSSUI RESTRIÇÃO": "Em validação"})
    assert buscar_hub("BRSP21", "TOCO", [ln], HubCfg()).status == "ALERTA"


def test_restricao_sem_veiculo_identificavel_pede_verificacao():
    ln = linha(**{"POSSUI RESTRIÇÃO": "Sim", "PERFIL RESTRIÇÃO": "Horário restrito"})
    res = buscar_hub("BRSP21", "TOCO", [ln], HubCfg())
    assert res.status == "VERIFICAR"


def test_veiculo_da_rota_desconhecido_pede_verificacao():
    ln = linha(**{"POSSUI RESTRIÇÃO": "Sim", "PERFIL RESTRIÇÃO": "CARRETA"})
    assert buscar_hub("BRSP21", None, [ln], HubCfg()).status == "VERIFICAR"


def test_ambiguo_quando_ha_hubs_diferentes():
    linhas = [linha(), linha(ID="2", **{"Endereço": "Outro endereço", "Localização Fixa": "BRSP21"})]
    res = buscar_hub("BRSP21", "TOCO", linhas, HubCfg())
    assert res.status == "AMBIGUO" and len(res.candidatos) == 2


def test_linhas_identicas_nao_sao_ambiguas():
    res = buscar_hub("BRSP21", "TOCO", [linha(), linha(ID="2")], HubCfg())
    assert res.status == "OK"


def test_sem_match_e_sem_tabela():
    assert buscar_hub("BRXX99", "TOCO", [linha()], HubCfg()).status == "SEM_MATCH"
    assert buscar_hub("BRSP21", "TOCO", [], HubCfg()).status == "SEM_TABELA"
    assert buscar_hub(None, "TOCO", [linha()], HubCfg()).status == "SEM_MATCH"


def test_mapeamento_manual_por_id():
    cfg = HubCfg(mapeamento_manual={"BRXX99": "7"})
    res = buscar_hub("BRXX99", "TOCO", [linha(), linha(ID="7", **{"Endereço": "Hub manual"})], cfg)
    assert res.status == "OK" and res.linha["Endereço"] == "Hub manual"
    assert buscar_hub("BRXX99", "TOCO", [linha()], cfg).status == "SEM_MATCH"


def test_titulo_de_coluna_com_grafia_diferente():
    ln = {"Localizacao fixa": "BRSP21", "ENDERECO": "Rua Z", "possui restricao": "Sim", "Perfil Restricao": "CARRETA"}
    res = buscar_hub("BRSP21", "TOCO", [ln], HubCfg())
    assert res.status == "OK"


def test_veiculos_no_texto():
    conhecidos = HubCfg().veiculos_conhecidos
    assert veiculos_no_texto("Proibido carreta e 3/4", conhecidos) == ["3/4", "CARRETA"]
    assert veiculos_no_texto("BITRUCK", conhecidos) == []


def test_avaliar_restricao_direto():
    status, msgs = avaliar_restricao(linha(), "TOCO", HubCfg())
    assert status == "OK"
