import logging

from rotas_extras.address_lookup import (Cabecalho, Celula, LinhaTabela, agrupar_linhas, atribuir_colunas,
                                         escolher_cabecalhos, mesclar_linhas, processar_snapshot, validar_linhas)
from rotas_extras.config import COLUNAS_TABELA_PADRAO, TabelaCfg

COLS = COLUNAS_TABELA_PADRAO
LARG = 100
CAB_Y = 100


def cabecalhos(colunas=COLS, desloc=0, y=CAB_Y, indices=None):
    idx = range(len(colunas)) if indices is None else indices
    return [{"titulo": colunas[i], "x": i * LARG - desloc, "x2": (i + 1) * LARG - desloc, "vy": y} for i in idx]


def celula(col_idx, texto, y, desloc=0, jitter=0.0, ordem=0, linha_y=None, scroll_top=0):
    x = col_idx * LARG + 5 - desloc
    return {"texto": texto, "x": x, "x2": x + 40, "vy": y + jitter, "y": y + jitter + scroll_top, "h": 16,
            "ordem": ordem, "linha_y": linha_y}


def linha_completa(y, valores: dict[int, str], **kw):
    return [celula(i, v, y, ordem=i, **kw) for i, v in valores.items()]


def test_escolhe_faixa_do_cabecalho_ignorando_iscas():
    cands = [Cabecalho(**c) for c in cabecalhos()]
    cands += [Cabecalho("Endereço", 900, 1000, 500), Cabecalho("ID", 0, 20, 20)]
    escolhidos = escolher_cabecalhos(cands, 10)
    assert len(escolhidos) == 16
    assert escolhidos["Endereço"].vy == CAB_Y and escolhidos["ID"].x == 100


def test_snapshot_com_celulas_vazias_e_nao_aplicavel():
    celulas = (linha_completa(150, {0: "MELI", 1: "10", 2: "SUDESTE", 3: "SP21", 4: "Rua A, 1", 5: "BRSP21",
                                    7: "Sim", 8: "CARRETA", 11: "Não Aplicável"}, jitter=2)
               + linha_completa(190, {0: "MELI", 2: "SUDESTE", 4: "Av. Vitória, 2", 5: "BRES01", 7: "Não"})
               + linha_completa(230, {0: "MELI", 4: "Rua C", 5: "BRMG03", 9: "Em validação"}, jitter=-3))
    snap = {"celulas": celulas, "cabecalhos": cabecalhos()}
    linhas, cab = processar_snapshot(snap, TabelaCfg())
    assert len(cab) == 16
    assert len(linhas) == 3
    l1, l2, l3 = linhas
    assert l1.valores["Localização Fixa"] == "BRSP21"
    assert l1.valores["PERFIL RESTRIÇÃO"] == "CARRETA"
    assert l1.valores["Qual?"] is None
    assert l1.valores["ID"] == "10"
    assert l2.valores["ID"] is None and l2.valores["Endereço"] == "Av. Vitória, 2"
    assert l3.valores["MOTIVO RESTRIÇÃO"] == "Em validação"
    assert all(not ln.problemas for ln in linhas)


def test_titulo_dentro_de_span_de_celula_e_descartado():
    celulas = [celula(i, t, CAB_Y) for i, t in enumerate(COLS)] + linha_completa(150, {0: "MELI", 5: "BRSP21",
                                                                                      4: "Rua"})
    linhas, _ = processar_snapshot({"celulas": celulas, "cabecalhos": cabecalhos()}, TabelaCfg())
    assert len(linhas) == 1 and linhas[0].valores["Operação"] == "MELI"


def test_estrategia_ancestral_agrupa_celulas_multilinha():
    cels = [Celula(**celula(0, "MELI", 150, linha_y=140)), Celula(**celula(4, "Endereço longo", 165, linha_y=140)),
            Celula(**celula(5, "BRSP21", 150, linha_y=140)), Celula(**celula(0, "MELI", 200, linha_y=190))]
    assert [len(g) for g in agrupar_linhas(cels, "auto", 10, 16)] == [3, 1]
    # por coordenadas, a célula de 2 linhas (y=165) cairia em outra linha
    assert [len(g) for g in agrupar_linhas(cels, "coordenadas", 10, 16)] == [2, 1, 1]


def test_estrategia_blocos():
    cels = [Celula(**celula(i % 16, f"v{i}", 0, ordem=i)) for i in range(32)]
    grupos = agrupar_linhas(cels, "blocos", 10, 16)
    assert [len(g) for g in grupos] == [16, 16]
    assert grupos[1][0].texto == "v16"


def test_sem_cabecalho_mapeia_por_ordem_so_com_16_celulas():
    nulos = {"", "NAO APLICAVEL"}
    completa = [Celula(**celula(i, f"v{i}", 150)) for i in range(16)]
    ln = atribuir_colunas(completa, {}, COLS, nulos)
    assert ln.valores["Operação"] == "v0" and ln.valores["Restrição APP Sheet"] == "v15"
    incompleta = completa[:15]
    ln = atribuir_colunas(incompleta, {}, COLS, nulos)
    assert ln.problemas and all(v is None for v in ln.valores.values())


def test_celula_sem_coluna_e_sinalizada_quando_todos_os_titulos_existem():
    cab = {c.titulo: c for c in (Cabecalho(**h) for h in cabecalhos())}
    perdida = Celula(**celula(30, "???", 150))
    ln = atribuir_colunas([perdida], cab, COLS, set())
    assert any("célula sem coluna" in p for p in ln.problemas)


def test_rolagem_horizontal_mescla_colunas_virtualizadas():
    cfg = TabelaCfg()
    esquerda = {"cabecalhos": cabecalhos(indices=range(12)),
                "celulas": linha_completa(150, {0: "MELI", 5: "BRSP21", 7: "Sim"})}
    desloc = 800
    direita = {"cabecalhos": cabecalhos(desloc=desloc, indices=range(8, 16)),
               "celulas": linha_completa(150, {9: "Falta doca", 12: "http://x", 15: "Bloqueado"}, desloc=desloc)}
    acumulado: list[LinhaTabela] = []
    for snap in (esquerda, direita):
        linhas, _ = processar_snapshot(snap, cfg)
        mesclar_linhas(acumulado, linhas, cfg.tolerancia_y_px)
    assert len(acumulado) == 1
    v = acumulado[0].valores
    assert v["Operação"] == "MELI" and v["LINK"] == "http://x" and v["Restrição APP Sheet"] == "Bloqueado"
    assert v["MOTIVO RESTRIÇÃO"] == "Falta doca"
    assert not acumulado[0].problemas


def test_rolagem_vertical_nao_duplica_linhas():
    cfg = TabelaCfg()
    topo = {"cabecalhos": cabecalhos(), "celulas": linha_completa(150, {0: "A", 5: "BR1", 4: "x"})
            + linha_completa(190, {0: "B", 5: "BR2", 4: "y"})}
    # rolou 40px: a linha B aparece em vy=150 mas com o mesmo y de conteúdo (190)
    rolado = {"cabecalhos": cabecalhos(), "celulas": linha_completa(150, {0: "B", 5: "BR2", 4: "y"}, scroll_top=40)
              + linha_completa(190, {0: "C", 5: "BR3", 4: "z"}, scroll_top=40)}
    acumulado: list[LinhaTabela] = []
    for snap in (topo, rolado):
        mesclar_linhas(acumulado, processar_snapshot(snap, cfg)[0], cfg.tolerancia_y_px)
    assert [ln.valores["Operação"] for ln in sorted(acumulado, key=lambda l: l.y)] == ["A", "B", "C"]


def test_conflito_na_mescla_e_registrado():
    a = LinhaTabela(y=100, valores={"Operação": "A"})
    b = LinhaTabela(y=103, valores={"Operação": "B"})
    acumulado = [a]
    mesclar_linhas(acumulado, [b], 10)
    assert len(acumulado) == 1 and any("conflito" in p for p in a.problemas)


def test_validar_linhas_descarta_curtas_e_loga_suspeitas(caplog):
    cfg = TabelaCfg()
    boa = LinhaTabela(y=1, valores={c: "x" for c in COLS})
    curta = LinhaTabela(y=2, valores={**{c: None for c in COLS}, "Operação": "x"})
    suspeita = LinhaTabela(y=3, valores={c: "y" for c in COLS}, problemas=["múltiplos valores em 'ID'"])
    with caplog.at_level(logging.WARNING):
        validas = validar_linhas([boa, curta, suspeita], cfg)
    assert validas == [boa, suspeita]
    assert "Linha suspeita" in caplog.text
