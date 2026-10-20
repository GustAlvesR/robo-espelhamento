"""Gera dados/estacoes_shopee.csv (sigla, tipo, nome, cidade, UF) a partir da
planilha de endereços da Shopee (aba "Locations Atualizada": SIGLA | Tipo | Nome |
Endereço | CEP | UF | CNPJ). Endereço e CNPJ não vão para o CSV.
Uso: pip install openpyxl && python importar_estacoes.py "Endereços e CNPJ.xlsx"
Depois, faça commit do CSV gerado."""

import csv
import os
import re
import sys

import openpyxl

UFS = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR",
       "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}
SAIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados", "estacoes_shopee.csv")


def cidade_do_nome(nome, uf):
    """'SoC_MG_Betim' -> Betim; 'LM HUB-SAO-10_Mogi das Cruzes_SP' -> Mogi das Cruzes;
    'SoC_GO_Goiânia_02' -> Goiânia."""
    texto = re.split(r"\s+/\s+", str(nome or ""))[0].strip()
    texto = re.sub(r"^[A-Z]+-[A-Z]+-?\d+\w*\s+[A-Z]?\s*-\s+", "", texto)  # 'SOC-MG-03 - Contagem_MG'
    partes = [p.strip() for p in texto.split("_") if p.strip()]
    if partes and ("-" in partes[0] or re.match(r"^(SOC|FBS|FM|LM|XPT|MINI|HUB|OWN|CB)\b", partes[0], re.I)):
        partes = partes[1:]
    i = next((k for k, p in enumerate(partes) if p.upper() in UFS), None)
    if i is not None:
        uf = partes[i].upper()
        partes = partes[i + 1:] if i == 0 else partes[:i] + partes[i + 1:]
    partes = [p for p in partes if not re.fullmatch(r"V?\d+[A-Z]?|EX|BTS", p, re.I)]
    return re.sub(r"\s+(BTS|\d+)$", "", " ".join(partes)).strip(), uf


def cidade_do_endereco(endereco, uf):
    """Último 'Cidade/UF', 'Cidade - UF' ou ', Cidade, UF' do endereço."""
    texto = " ".join(str(endereco or "").split())
    ufs = "|".join(sorted(UFS))
    achados = re.findall(rf"(?:^|[,\-–]\s*)([A-Za-zÀ-ÿ'][A-Za-zÀ-ÿ' .]{{1,40}}?)\s*(?:/|-|–|,)\s*({ufs})\b",
                         texto, re.I)
    if not achados:
        return "", uf
    cidade, uf = achados[-1]
    cidade = re.sub(r"^(na cidade de|cidade de|cidade:)\s+", "", cidade.strip(), flags=re.I).title()
    for minuscula in ("do", "da", "de", "dos", "das"):
        cidade = cidade.replace(f" {minuscula.title()} ", f" {minuscula} ")
    return cidade, uf.upper()


def main():
    if len(sys.argv) < 2:
        sys.exit('Uso: python importar_estacoes.py "Endereços e CNPJ.xlsx"')
    ws = openpyxl.load_workbook(sys.argv[1], read_only=True, data_only=True).worksheets[0]
    linhas, vistos = [], set()
    for r in list(ws.iter_rows(values_only=True))[1:]:
        if not r or not r[0]:
            continue
        codigo = str(r[0]).strip().upper()
        if codigo in vistos:
            continue
        vistos.add(codigo)
        uf = str(r[5] or "").strip().upper()
        cidade, uf = cidade_do_nome(r[2], uf if uf in UFS else "")
        if not cidade:
            cidade, uf = cidade_do_endereco(r[3], uf)
        linhas.append([codigo, str(r[1] or "").strip(), " ".join(str(r[2] or "").split()), cidade, uf])
    linhas.sort()
    os.makedirs(os.path.dirname(SAIDA), exist_ok=True)
    with open(SAIDA, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["codigo", "tipo", "nome", "cidade", "uf"])
        w.writerows(linhas)
    print(f"{len(linhas)} estações em {SAIDA}; {sum(1 for l in linhas if not l[3])} sem cidade.")


if __name__ == "__main__":
    main()
