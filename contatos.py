# -*- coding: utf-8 -*-
"""Contatos da Shopee de cada rota: Field e Senior - Suporte da origem e do destino, e o
Reversa / Monitoramento (nacional), com quem está no turno agora em primeiro.

A base fica na aba Contatos_Shopee da planilha de resultados (a equipe mantém lá; telefone
de pessoas não vai para o repositório). Coluna "Estações" com as siglas da Shopee: SP8,
"SP6, SP25", "FBS MG5 / MG4 / FMH-MG", "SPI/MG". Como cada rota casa com a base:
- SOC pela sigla (SOC-SP8 -> SP8; SP09 = SP9); FBS com o prefixo (FBS-MG5 -> FBS MG5);
- FMH pela sigla da cidade (FMH-BNU-01 -> BNU) e pela região: em SP, FMH - SPM (Grande
  São Paulo) ou FMH SPI (interior); em MG, FMH-MG; em GO, BA e PE, o Field/FMH do SOC do estado;
- região da estação para quem cobre região inteira (SPI/MG, SUL).
HUB e demais estações de última milha não estão na base: ficam só com o nacional."""

import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta

import geo_tomtom

ABA = "Contatos_Shopee"
ATUALIZAR_MIN = int(os.getenv("CONTATOS_ATUALIZAR_MIN", "30"))
# A operação fala só com estas funções (comparadas sem acento/espaço, pelo começo).
FUNCOES_ESTACAO = ("FIELD", "SENIORSUPORTE")
FUNCAO_NACIONAL = "REVERSAMONITORAMENTO"
# Field que acompanha as FMH do estado (na base, "Field/ FMH" no SOC do estado).
FMH_POR_UF = {"GO": "GO2", "BA": "BA2", "PE": "PE2"}
UF_SUL = {"PR", "SC", "RS"}
GRANDE_SP = {
    "ARUJA", "BARUERI", "BIRITIBAMIRIM", "CAIEIRAS", "CAJAMAR", "CARAPICUIBA", "COTIA", "DIADEMA",
    "EMBUDASARTES", "EMBUGUACU", "FERRAZDEVASCONCELOS", "FRANCISCOMORATO", "FRANCODAROCHA", "GUARAREMA",
    "GUARULHOS", "ITAPECERICADASERRA", "ITAPEVI", "ITAQUAQUECETUBA", "JANDIRA", "JUQUITIBA", "MAIRIPORA",
    "MAUA", "MOGIDASCRUZES", "OSASCO", "PIRAPORADOBOMJESUS", "POA", "RIBEIRAOPIRES", "RIOGRANDEDASERRA",
    "SALESOPOLIS", "SANTAISABEL", "SANTANADEPARNAIBA", "SANTOANDRE", "SAOBERNARDODOCAMPO",
    "SAOCAETANODOSUL", "SAOLOURENCODASERRA", "SAOPAULO", "SUZANO", "TABOAODASERRA", "VARGEMGRANDEPAULISTA"}
DIAS = {"SEG": 0, "TER": 1, "QUA": 2, "QUI": 3, "SEX": 4, "SAB": 5, "DOM": 6}


def _norm(texto) -> str:
    t = str(texto or "").replace("–", "-").replace("—", "-")
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().upper()


def _chave(texto) -> str:
    return re.sub(r"[^A-Z0-9]", "", _norm(texto))


def _sigla(texto) -> str:
    """'SP09' -> 'SP9'; 'FBS MG5' -> 'FBSMG5'; 'FMH - SPM' -> 'FMHSPM'."""
    return re.sub(r"([A-Z])0+(\d)", r"\1\2", _chave(texto))


def siglas_do_contato(texto: str) -> set:
    t = re.sub(r"FMH\s*-\s*", "FMH ", str(texto or ""), flags=re.I)
    return {_sigla(p) for p in re.split(r"\s*[/,]\s*|\s+-\s+", t) if _sigla(p)}


def siglas_da_estacao(codigo: str) -> set:
    codigo = str(codigo or "").strip().upper()
    tipo, _, resto = codigo.partition("-")
    _nome, cidade, uf = geo_tomtom.cadastro_estacoes().get(codigo, ("", "", ""))
    siglas = set()
    if tipo == "SOC":
        siglas.add(_sigla(resto))
    elif tipo == "FBS":
        siglas.add("FBS" + _sigla(resto))
    elif tipo == "FMH":
        siglas.add(_sigla(resto.split("-")[0]))
        if uf in FMH_POR_UF:
            siglas.add(FMH_POR_UF[uf])
    else:
        return set()
    if uf == "SP":
        regioes = {"SPM", "SPI"} if not cidade else {"SPM" if _chave(cidade) in GRANDE_SP else "SPI"}
        siglas |= regioes | ({"FMH" + r for r in regioes} if tipo == "FMH" else set())
    elif uf == "MG":
        siglas |= {"MG"} | ({"FMHMG"} if tipo == "FMH" else set())
    elif uf in UF_SUL:
        siglas.add("SUL")
    return siglas


def ler_turno(horario: str):
    """(dias da semana ou None, paridade do dia do ano ou None, início_min, fim_min) ou None."""
    t = _norm(horario)
    horas = re.findall(r"(?<![\dX])(\d{1,2})(?::(\d{2})|H(\d{2})?)", t)
    if len(horas) < 2:
        return None
    (h1, m1a, m1b), (h2, m2a, m2b) = horas[:2]
    ini, fim = int(h1) * 60 + int(m1a or m1b or 0), int(h2) * 60 + int(m2a or m2b or 0)
    dias = None
    nome = r"(SEG|TER|QUA|QUI|SEX|SAB|DOM)[A-Z]*"
    faixa = re.search(nome + r"\s*-\s*" + nome, t)
    par = re.search(nome + r"\s+E\s+" + nome, t)
    if faixa:
        a, b = DIAS[faixa.group(1)], DIAS[faixa.group(2)]
        dias = {(a + i) % 7 for i in range((b - a) % 7 + 1)}
    elif par:
        dias = {DIAS[par.group(1)], DIAS[par.group(2)]}
    paridade = 1 if "IMPAR" in t else 0 if re.search(r"\bPAR", t) else None
    return dias, paridade, ini, fim


def no_turno(horario: str, now: datetime):
    """True/False; None quando o horário não dá para ler. Turno que vira a noite conta
    pelo dia em que começou (12x36 das 18h às 06h do 'dia par' começa no dia par)."""
    turno = ler_turno(horario)
    if not turno:
        return None
    dias, paridade, ini, fim = turno
    for atras in (0, 1):
        dia = (now - timedelta(days=atras)).replace(hour=0, minute=0, second=0, microsecond=0)
        if dias is not None and dia.weekday() not in dias:
            continue
        if paridade is not None and dia.timetuple().tm_yday % 2 != paridade:
            continue
        comeco = dia + timedelta(minutes=ini)
        termino = dia + timedelta(minutes=fim if fim > ini else fim + 24 * 60)
        if comeco <= now < termino:
            return True
    return False


class Contatos:
    def __init__(self):
        self.base = []
        self._proxima = None

    def atualizar(self, planilha, now: datetime) -> None:
        """Relê a aba a cada ATUALIZAR_MIN; sem a aba, segue sem contatos."""
        if self._proxima and now < self._proxima:
            return
        self._proxima = now + timedelta(minutes=ATUALIZAR_MIN)
        try:
            linhas = planilha.worksheet(ABA).get_all_values()
        except Exception as e:
            logging.warning("Contatos: aba '%s' não lida (%s); seguindo com a leitura anterior.", ABA, e)
            return
        cab = {_chave(c): i for i, c in enumerate(linhas[0])} if linhas else {}
        cel = lambda l, nome: l[cab[nome]].strip() if nome in cab and cab[nome] < len(l) else ""
        base = []
        for l in linhas[1:]:
            nome, funcao = cel(l, "NOME"), cel(l, "FUNCAO")
            if not nome:
                continue
            base.append({"nome": nome, "funcao": funcao, "turno": cel(l, "TURNO"), "horario": cel(l, "HORARIO"),
                         "telefone": cel(l, "TELEFONE"), "siglas": siglas_do_contato(cel(l, "ESTACOES")),
                         "nacional": _chave(funcao).startswith(FUNCAO_NACIONAL)})
        self.base = base
        logging.info("Contatos: %s contato(s) na aba %s.", len(base), ABA)

    @staticmethod
    def _saida(contatos: list, now: datetime) -> list:
        """Uma linha por pessoa (a base repete a pessoa por escala), quem está no turno primeiro."""
        pessoas = {}
        for c in contatos:
            chave = (c["nome"], c["telefone"])
            agora = no_turno(c["horario"], now)
            p = pessoas.setdefault(chave, {"nome": c["nome"], "funcao": c["funcao"], "telefone": c["telefone"],
                                           "horarios": [], "no_turno": False})
            if c["horario"] and c["horario"] not in p["horarios"]:
                p["horarios"].append(c["horario"])
            p["no_turno"] = p["no_turno"] or bool(agora)
        saida = [{"nome": p["nome"], "funcao": p["funcao"], "telefone": p["telefone"],
                  "horario": " / ".join(p["horarios"]), "no_turno": p["no_turno"]} for p in pessoas.values()]
        return sorted(saida, key=lambda p: (not p["no_turno"], not _chave(p["funcao"]).startswith("SENIOR"),
                                            p["nome"]))

    def da_estacao(self, codigo: str, now: datetime) -> dict:
        siglas = siglas_da_estacao(codigo)
        achados = [c for c in self.base if not c["nacional"] and c["siglas"] & siglas
                   and _chave(c["funcao"]).startswith(FUNCOES_ESTACAO)]
        return {"estacao": codigo, "contatos": self._saida(achados, now)}

    def nacional(self, now: datetime) -> list:
        return self._saida([c for c in self.base if c["nacional"]], now)
