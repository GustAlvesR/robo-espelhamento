"""Parsing dos cards de rota extra e validações de nomenclatura e datas (funções puras)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

_DATA_HORA = r"(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2}(?::\d{2})?)"
RE_ETA = re.compile(r"\bETA\b\s*:?\s*" + _DATA_HORA, re.IGNORECASE)
RE_CPT = re.compile(r"\bCPT\b\s*:?\s*" + _DATA_HORA, re.IGNORECASE)
RE_TEMPO_LIMITE = re.compile(
    r"TEMPO\s+LIMITE\s+DE\s+RETORNO\s*:?\s*(\d{1,3}):(\d{2})(?::(\d{2}))?", re.IGNORECASE)
RE_VEICULO = re.compile(r"^(\d+)\s*-\s*([A-Z0-9][A-Z0-9/ ]{1,29}?)$")
RE_NOME_FALLBACK = re.compile(r"\bBR[A-Z]{2}\d+[-A-Z0-9.]*")
RE_TIPO = re.compile(r"^[A-Z][A-Z ]{2,29}$")
_LINHAS_IGNORADAS = {"ACEITAR", "RECUSAR", "ABRIR ENDERECO"}
_RE_LINHA_DATA = re.compile(r"\b(ETA|CPT)\b|TEMPO LIMITE")


def normalizar(texto: str | None) -> str:
    """Remove acentos, colapsa espaços e coloca em maiúsculas."""
    if not texto:
        return ""
    s = unicodedata.normalize("NFKD", texto)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().upper()


@dataclass
class Rota:
    nome: str
    origem: str | None
    veiculo: str | None
    qtd_veiculos: int | None
    veiculo_texto: str | None
    tipo: str | None
    eta: datetime | None
    cpt: datetime | None
    eta_texto: str | None
    cpt_texto: str | None
    tempo_limite: timedelta | None
    tempo_limite_texto: str | None
    secao: str | None
    descricao: str
    texto: str
    erros_parse: list[str] = field(default_factory=list)

    @property
    def chave(self) -> str:
        """Chave de deduplicação: nome + ETA + CPT (com fallback para o texto da descrição)."""
        eta = self.eta.strftime("%Y-%m-%d %H:%M") if self.eta else (self.eta_texto or "")
        cpt = self.cpt.strftime("%Y-%m-%d %H:%M") if self.cpt else (self.cpt_texto or "")
        chave = f"{normalizar(self.nome)}|{eta}|{cpt}"
        if not eta or not cpt:
            chave += f"|{normalizar(self.descricao)}"
        return chave


def parse_data_hora(data: str, hora: str, tz: tzinfo) -> datetime:
    """Converte 'dd/mm/aaaa' + 'hh:mm[:ss]' em datetime com fuso."""
    fmt = "%d/%m/%Y %H:%M:%S" if hora.count(":") == 2 else "%d/%m/%Y %H:%M"
    return datetime.strptime(f"{data} {hora}", fmt).replace(tzinfo=tz)


def parse_tempo_limite(texto: str) -> tuple[timedelta | None, str | None]:
    """Extrai 'TEMPO LIMITE DE RETORNO: hh:mm[:ss]'."""
    m = RE_TEMPO_LIMITE.search(texto)
    if not m:
        return None, None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    bruto = f"{m.group(1)}:{m.group(2)}" + (f":{m.group(3)}" if m.group(3) else "")
    return timedelta(hours=h, minutes=mi, seconds=s), bruto


def parse_veiculo(linhas: list[str]) -> tuple[int | None, str | None, str | None]:
    """Procura uma linha no formato '1 - TOCO'. Retorna (quantidade, tipo, texto original)."""
    for linha in linhas:
        m = RE_VEICULO.match(normalizar(linha))
        if m:
            return int(m.group(1)), m.group(2).strip(), linha.strip()
    return None, None, None


def extrair_origem(nome: str) -> str | None:
    """Primeiro token do nome da rota (ex.: BRSP21 em BRSP21-BRSP11-SSP34.SOW.VEN)."""
    m = re.match(r"[A-Z0-9]+", normalizar(nome))
    return m.group(0) if m else None


def _escolher_nome(labels: list[str], texto: str) -> str:
    limpos = [normalizar(lb) for lb in labels if lb and lb.strip()]
    for lb in limpos:
        if RE_NOME_FALLBACK.search(lb):
            return lb
    if limpos:
        return limpos[0]
    m = RE_NOME_FALLBACK.search(normalizar(texto))
    return m.group(0) if m else ""


def parse_card(bruto: dict, tz: tzinfo) -> Rota:
    """Converte os dados brutos extraídos do DOM (label, descricao, texto, secao) em uma Rota."""
    labels = bruto.get("labels") or [bruto.get("label") or ""]
    descricao = (bruto.get("descricao") or "").strip()
    texto = (bruto.get("texto") or "").strip()
    erros: list[str] = []

    nome = _escolher_nome(labels, texto)
    if not nome:
        erros.append("nome da rota não encontrado")

    fonte_datas = f"{descricao}\n{texto}"
    eta = cpt = None
    eta_txt = cpt_txt = None
    for regex, rotulo in ((RE_ETA, "ETA"), (RE_CPT, "CPT")):
        m = regex.search(fonte_datas)
        if not m:
            continue
        bruto_dt = f"{m.group(1)} {m.group(2)}"
        try:
            valor = parse_data_hora(m.group(1), m.group(2), tz)
        except ValueError:
            erros.append(f"{rotulo} com data inválida: {bruto_dt}")
            valor = None
        if rotulo == "ETA":
            eta, eta_txt = valor, bruto_dt
        else:
            cpt, cpt_txt = valor, bruto_dt

    tempo_limite, tempo_txt = parse_tempo_limite(fonte_datas)
    linhas = [ln.strip() for ln in texto.splitlines() if ln.strip()]
    qtd, veiculo, veiculo_txt = parse_veiculo(linhas)

    tipo = None
    nome_norm = normalizar(nome)
    secao_norm = normalizar(bruto.get("secao"))
    for ln in linhas:
        n = normalizar(ln)
        if (n in _LINHAS_IGNORADAS or n == nome_norm or n == secao_norm or RE_VEICULO.match(n)
                or _RE_LINHA_DATA.search(n)):
            continue
        if RE_TIPO.match(n):
            tipo = ln.strip()
            break

    return Rota(
        nome=nome, origem=extrair_origem(nome) if nome else None, veiculo=veiculo, qtd_veiculos=qtd,
        veiculo_texto=veiculo_txt, tipo=tipo, eta=eta, cpt=cpt, eta_texto=eta_txt, cpt_texto=cpt_txt,
        tempo_limite=tempo_limite, tempo_limite_texto=tempo_txt, secao=bruto.get("secao") or None,
        descricao=descricao, texto=texto, erros_parse=erros,
    )


def nome_valido(nome: str, padrao: re.Pattern[str]) -> bool:
    """True se o nome bate com o padrão de nomenclatura configurado."""
    return bool(nome) and bool(padrao.fullmatch(nome.strip().upper()))


def validar_datas(rota: Rota, agora: datetime) -> list[str]:
    """Retorna anomalias de data (vazio = ok)."""
    anomalias = []
    if rota.eta is None:
        anomalias.append("ETA ausente ou ilegível")
    if rota.cpt is None:
        anomalias.append("CPT ausente ou ilegível")
    if rota.eta and rota.cpt and rota.eta >= rota.cpt:
        anomalias.append(f"ETA ({rota.eta:%d/%m %H:%M}) não é anterior ao CPT ({rota.cpt:%d/%m %H:%M})")
    if rota.eta and rota.eta <= agora:
        anomalias.append(f"ETA ({rota.eta:%d/%m %H:%M}) já passou")
    return anomalias
