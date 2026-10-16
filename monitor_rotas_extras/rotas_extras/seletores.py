"""Todos os seletores e textos de tela do AppSheet, em um só lugar.

IDs e classes do AppSheet são gerados (sufixos dinâmicos, classes cd-jss*/b-jss*). Por isso só
usamos aria-label, role, data-testid, prefixos de id e texto. Listas são tentadas em ordem (fallback).
O nome do arquivo não é "selectors.py" para não colidir com o módulo selectors da biblioteca padrão.
"""
from __future__ import annotations

SELETORES: dict[str, object] = {
    # Tela ROTA EXTRA (cards)
    "aceitar": ['[role="button"][aria-label="ACEITAR" i]'],
    "aceitar_texto": "ACEITAR",  # fallback: role=button cujo texto normalizado é exatamente este
    "abrir_endereco": [
        '[role="button"][aria-label="ABRIR ENDEREÇO" i]',
        '[role="button"][aria-label="ABRIR ENDERECO" i]',
        '[role="button"]:has(i.fa-map-marker)',
    ],
    "recusar_icone": "i.fa-times-circle",
    "card_label": '[id^="label"]',
    "card_descricao": '[id^="description"]',
    # Elementos que costumam existir dentro de um card (ajudam a delimitá-lo quando falta a descrição)
    "card_acoes": ["i.fa-times-circle", '[aria-label="ABRIR ENDEREÇO" i]', "i.fa-map-marker"],

    # Tela TABELA DE ENDEREÇO
    "celula_tabela": 'span[data-testid="text-type-display-span"]',
    "celula_tabela_fallback": "span.TextTypeDisplay__text",

    # Navegação / estrutura
    "item_navegacao": '[role="button"]',
    "breadcrumb_atual": '[aria-current="page"]',
    "filtro_drawer": '[data-testid="filter-drawer"]',
    "filtro_indice": "nav.filtering-index",
    "voltar": '[aria-label="back"]',
    "dialogo": '[role="dialog"], [role="alertdialog"]',
    # Só senha e o campo de conta do Google: input de e-mail comum pode existir em formulários do app.
    "campos_login": 'input[type="password"], input[name="identifier"]',
}

TEXTOS: dict[str, list[str]] = {
    "menu": ["MENU"],
    "rota_extra": ["ROTA EXTRA"],
    # Breadcrumb vem sem cedilha; a barra de busca vem com. A comparação é feita sem acentos.
    "tabela_endereco": ["TABELA DE ENDERECO", "TABELA DE ENDEREÇO"],
    "filtro_feito": ["FEITO"],
    "filtro_limpar": ["LIMPAR"],
}
