"""Converte um rotograma da Angellira (PDF) em dados/rotogramas/<ORIGEM>__<DESTINO>.json.
Guarda só a rota: SM, origem, destino, pontos autorizados, áreas de risco e a rota
detalhada. Motorista, CPF, placas e telefones não vão para o arquivo.
Uso: pip install pdfplumber && python importar_rotograma.py SM39635748.PDF [...]
     (--origem SOC-SP8 --destino SOC-MG4 se o nome da estação não for reconhecido)
Um rotograma de SM mais nova substitui o do mesmo trecho; um de SM mais antiga é ignorado
(--forcar grava assim mesmo)."""

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from datetime import datetime

import pdfplumber

PASTA = os.path.dirname(os.path.abspath(__file__))
SAIDA = os.path.join(PASTA, "dados", "rotogramas")
CADASTRO = os.path.join(PASTA, "dados", "estacoes_shopee.csv")
TIPOS_RISCO = ("LOCAL DE ROUBO GERAL", "LOCAL DE ROUBO", "LOCAL PROIBIDO", "FRIGORIFICO", "TODOS")


def norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", t).split())


def codigo_da_estacao(nome: str):
    """'SOC_SP_SÃO BERNARDO DO CAMPO' -> 'SOC-SP8', pelo nome no cadastro da Shopee."""
    alvo = norm(nome)
    with open(CADASTRO, encoding="utf-8", newline="") as f:
        for linha in csv.DictReader(f):
            if norm(linha["nome"]) == alvo:
                return linha["codigo"]
    return None


def separar_cidade(cidade_uf: str):
    m = re.match(r"^(.*?)\s*/\s*([A-Z]{2})\s*$", " ".join(str(cidade_uf or "").split()))
    return (m.group(1).strip(), m.group(2)) if m else (" ".join(str(cidade_uf or "").split()), "")


def nome_do_local(texto: str, cidade: str = "") -> str:
    """'POSTO GRILL - BRAGANCA PAULISTA/SP' -> 'POSTO GRILL';
    'AUTO POSTO GIGIO ATIBAIA/SP' -> 'AUTO POSTO GIGIO'; '22 - ATIBAIA/SP' fica como está."""
    t = " ".join(str(texto or "").split())
    sem_cidade = re.sub(r"\s*-\s*[^-]+/\s*[A-Z]{2}\s*\d*\s*$", "", t).strip()
    if cidade and sem_cidade == t:
        palavras = t.split(" ")
        for k in range(1, len(palavras)):
            if re.fullmatch(rf"{re.escape(norm(cidade))} [A-Z]{{2}}( \d+)?", norm(" ".join(palavras[-k:]))):
                sem_cidade = " ".join(palavras[:-k]).rstrip(" -")
                break
    return t if not sem_cidade or sem_cidade.isdigit() else sem_cidade


def ler_cabecalho(texto: str) -> dict:
    def campo(rotulo, ate=r"\s+(?:Origem|Destino|Transportador|Veículo|Previsão)\b|\s{2,}|\n|$"):
        m = re.search(rf"{rotulo}:\s*(.+?)(?:{ate})", texto)
        return m.group(1).strip() if m else ""
    dist = re.search(r"Distância Planejada:\s*([\d.,]+)\s*km", texto)
    return {
        "sm": campo("Solicitação de Monitoramento"),
        "origem_nome": campo("Origem"),
        "destino_nome": campo("Destino"),
        "distancia_km": float(dist.group(1).replace(",", ".")) if dist else None,
    }


COLUNA_DISTANCIA = 55  # no texto com layout, a distância fica à direita desta coluna


def ler_passos(texto: str) -> list:
    """Rota detalhada, do texto com o layout preservado: '12.Vire à direita ... 0,46 km'.
    A instrução pode quebrar a linha, e o número da distância às vezes sai uma linha
    acima ou abaixo do 'km'. Por isso as distâncias são lidas como uma coluna à parte,
    na ordem, e casadas com os passos."""
    trecho = texto.split("Rota Detalhada", 1)[-1]
    passos, distancias, numero, atual = [], [], None, None
    for linha in trecho.splitlines():
        palavras = []
        for m_tok in re.finditer(r"\S+", linha):
            tok, coluna_dist = m_tok.group(), m_tok.start() >= COLUNA_DISTANCIA
            if coluna_dist and re.fullmatch(r"\d+(?:,\d+)?", tok):
                numero = float(tok.replace(",", "."))
            elif coluna_dist and tok in ("km", "m") and numero is not None:
                distancias.append(round(numero / 1000 if tok == "m" else numero, 3))
                numero = None
            else:
                palavras.append(tok)
        texto_linha = " ".join(palavras)
        m = re.match(r"^(\d+)\.(.*)$", texto_linha)
        if m:
            atual = {"instrucao": m.group(2).strip(), "km": None}
            passos.append(atual)
        elif atual is not None and texto_linha:
            if re.fullmatch(r"[A-Z0-9_ÀÁÂÃÉÊÍÓÔÕÚÇ ().-]+", texto_linha) and "_" in texto_linha:
                atual = None  # nome da estação de destino: fim da rota detalhada
            else:
                atual["instrucao"] += " " + texto_linha
    if len(distancias) == len(passos):
        for p, km in zip(passos, distancias):
            p["km"] = km
    return passos


def ler_pdf(caminho: str) -> dict:
    pontos, riscos, vistos_p, vistos_r = [], [], set(), set()
    texto, texto_layout = "", ""
    with pdfplumber.open(caminho) as pdf:
        for pagina in pdf.pages:
            texto += (pagina.extract_text() or "") + "\n"
            texto_layout += (pagina.extract_text(layout=True) or "") + "\n"
            for tabela in pagina.extract_tables():
                for linha in tabela:
                    celulas = [" ".join(str(c or "").split()) for c in linha]
                    if not celulas or not celulas[0].isdigit():
                        continue
                    cidade, uf = separar_cidade(celulas[2] if len(celulas) > 2 else "")
                    rodovia = celulas[3] if len(celulas) > 3 else ""
                    if len(celulas) == 6:
                        nome = nome_do_local(celulas[1], cidade)
                        chave = (norm(nome), norm(cidade))
                        if chave not in vistos_p:
                            vistos_p.add(chave)
                            pontos.append({"nome": nome, "cidade": cidade, "uf": uf, "rodovia": rodovia,
                                           "motivo": celulas[5]})
                    elif len(celulas) == 5:
                        completo = celulas[1]
                        tipo = next((t for t in TIPOS_RISCO if norm(completo).startswith(norm(t))), "ÁREA DE RISCO")
                        resto = re.sub(rf"^{re.escape(tipo)}\s*-\s*", "", completo, flags=re.I) if tipo != "ÁREA DE RISCO" else completo
                        nome = nome_do_local(resto, cidade)
                        chave = (norm(tipo), norm(nome), norm(cidade))
                        if chave not in vistos_r:
                            vistos_r.add(chave)
                            # A coluna "Rodovia" das áreas de risco traz anotações ("GERAL", "QUADRANTE").
                            riscos.append({"tipo": tipo, "nome": nome, "cidade": cidade, "uf": uf, "obs": rodovia})
    return {**ler_cabecalho(texto), "pontos": pontos, "riscos": riscos, "passos": ler_passos(texto_layout)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--origem")
    ap.add_argument("--destino")
    ap.add_argument("--forcar", action="store_true", help="grava mesmo se o trecho já tiver SM mais nova")
    args = ap.parse_args()
    os.makedirs(SAIDA, exist_ok=True)
    for caminho in args.pdfs:
        dados = ler_pdf(caminho)
        origem = args.origem or codigo_da_estacao(dados["origem_nome"])
        destino = args.destino or codigo_da_estacao(dados["destino_nome"])
        if not origem or not destino:
            sys.exit(f"{caminho}: estação não reconhecida ({dados['origem_nome']!r} -> {dados['destino_nome']!r}). "
                     "Informe --origem e --destino.")
        dados = {"origem": origem, "destino": destino, **dados,
                 "importado_em": datetime.now().strftime("%d/%m/%Y %H:%M")}
        arquivo = os.path.join(SAIDA, f"{origem}__{destino}.json")
        if os.path.exists(arquivo) and not args.forcar:
            with open(arquivo, encoding="utf-8") as f:
                atual = json.load(f).get("sm", "")
            if atual.isdigit() and str(dados["sm"]).isdigit() and int(atual) > int(dados["sm"]):
                print(f"SM {dados['sm']}: {origem} -> {destino} ignorado; o trecho já tem a SM {atual}, mais nova.")
                continue
        with open(arquivo, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=1)
        print(f"SM {dados['sm']}: {origem} -> {destino}, {len(dados['pontos'])} pontos autorizados, "
              f"{len(dados['riscos'])} áreas de risco, {len(dados['passos'])} passos -> {os.path.relpath(arquivo, PASTA)}")


if __name__ == "__main__":
    main()
