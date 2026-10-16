"""Carregamento e validação da configuração (config.yaml + variáveis de ambiente/.env)."""
from __future__ import annotations

import dataclasses
import os
import re
import typing
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ErroConfig(Exception):
    """Configuração inválida ou ausente."""


COLUNAS_TABELA_PADRAO = [
    "Operação", "ID", "MACRO REGIÃO", "MICRO REGIÃO", "Endereço", "Localização Fixa", "CNPJ",
    "POSSUI RESTRIÇÃO", "PERFIL RESTRIÇÃO", "MOTIVO RESTRIÇÃO", "Possuí Aplicativo ou Forms de Acesso?",
    "Qual?", "LINK", "Local de Bipagem do YMS", "DEMANDA", "Restrição APP Sheet",
]


@dataclass
class AppCfg:
    url_rotas: str = ""
    url_tabela_endereco: str = ""


@dataclass
class NavegadorCfg:
    canal: str = "msedge"
    perfil_dir: str = "perfil_edge"
    headless: bool = False
    largura: int = 1600
    altura: int = 1000
    proxy: str = ""
    timeout_navegacao_s: int = 60
    timeout_carregamento_s: int = 30
    espera_cards_s: int = 5
    espera_lazy_ms: int = 1500
    espera_rede_s: int = 10
    max_rolagens: int = 60


@dataclass
class MonitorCfg:
    intervalo_s: int = 45
    fuso: str = "America/Sao_Paulo"
    janela_oferta_min: int = 30
    esquecer_apos_min: int = 10
    ciclos_vazios_alerta: int = 80
    falhas_seguidas_alerta: int = 3
    heartbeat_hora: str = "08:00"
    indicadores_login_url: list[str] = field(default_factory=lambda: [
        "accounts.google.com", "login.microsoftonline.com", "/account/login", "signin", "oauth",
    ])
    indicadores_login_texto: list[str] = field(default_factory=lambda: [
        "Sign in with Google", "Fazer login com o Google", "Sign in with Microsoft",
        "Entrar com a Microsoft", "Faça login para continuar", "Sign in to continue",
    ])


@dataclass
class ValidacaoCfg:
    regex_nome: str = r"^BR[A-Z]{2}\d+(-[A-Z0-9]+)+(\.[A-Z]+)+$"


@dataclass
class FiltrosCfg:
    veiculos: list[str] = field(default_factory=list)
    origens: list[str] = field(default_factory=list)
    eta_minima_min: int = 0


@dataclass
class TabelaCfg:
    colunas: list[str] = field(default_factory=lambda: list(COLUNAS_TABELA_PADRAO))
    estrategia_linhas: str = "auto"
    seletor_linha: str = '[role="row"], tr'
    tolerancia_y_px: float = 10.0
    min_celulas_linha: int = 3
    valores_nulos: list[str] = field(default_factory=lambda: ["", "Não Aplicável", "N/A", "-"])
    max_rolagens: int = 300


@dataclass
class HubCfg:
    cache_arquivo: str = "dados/tabela_endereco.json"
    atualizar_cada_h: float = 12.0
    colunas_busca: list[str] = field(default_factory=lambda: ["Localização Fixa", "MICRO REGIÃO", "Operação"])
    mapeamento_manual: dict[str, str] = field(default_factory=dict)
    termos_alerta: list[str] = field(default_factory=lambda: ["SEM OPERAÇÃO MELI", "EM VALIDAÇÃO"])
    veiculos_conhecidos: list[str] = field(default_factory=lambda: [
        "VUC", "VAN", "FIORINO", "3/4", "TOCO", "TRUCK", "CARRETA", "BITREM", "RODOTREM",
    ])
    perfil_indica: str = "proibidos"
    arquivo_sem_match: str = "logs/origens_sem_match.log"
    tabela: TabelaCfg = field(default_factory=TabelaCfg)


@dataclass
class TeamsCfg:
    ativo: bool = True
    webhook_url: str = ""
    timeout_s: int = 15


@dataclass
class EmailCfg:
    ativo: bool = True
    destinatarios: list[str] = field(default_factory=list)
    assunto_prefixo: str = "[Rotas Extras]"


@dataclass
class NotificacaoCfg:
    teams: TeamsCfg = field(default_factory=TeamsCfg)
    email: EmailCfg = field(default_factory=EmailCfg)
    tentativas: int = 3
    backoff_inicial_s: float = 2.0
    cooldown_alertas_min: int = 30
    proxy: str = ""


@dataclass
class AutoAceiteCfg:
    ativo: bool = False
    max_por_hora: int = 2
    exigir_hub_ok: bool = True
    confirmar_dialogo: bool = False
    textos_confirmacao: list[str] = field(default_factory=lambda: ["OK", "CONFIRMAR", "SIM"])
    timeout_confirmacao_s: int = 10
    arquivo_auditoria: str = "logs/auto_aceite_auditoria.jsonl"


@dataclass
class LogCfg:
    nivel: str = "INFO"
    arquivo: str = "logs/monitor.log"
    max_mb: int = 5
    backups: int = 5


@dataclass
class Config:
    app: AppCfg = field(default_factory=AppCfg)
    navegador: NavegadorCfg = field(default_factory=NavegadorCfg)
    monitor: MonitorCfg = field(default_factory=MonitorCfg)
    validacao: ValidacaoCfg = field(default_factory=ValidacaoCfg)
    filtros: FiltrosCfg = field(default_factory=FiltrosCfg)
    hub: HubCfg = field(default_factory=HubCfg)
    notificacao: NotificacaoCfg = field(default_factory=NotificacaoCfg)
    auto_aceite: AutoAceiteCfg = field(default_factory=AutoAceiteCfg)
    log: LogCfg = field(default_factory=LogCfg)
    estado_arquivo: str = "dados/estado.json"
    dumps_dir: str = "dumps"
    base_dir: Path = field(default=Path("."), init=False)

    def caminho(self, relativo: str) -> Path:
        """Resolve um caminho relativo à pasta do config.yaml."""
        p = Path(os.path.expandvars(relativo))
        return p if p.is_absolute() else self.base_dir / p

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.monitor.fuso)

    @property
    def regex_nome(self) -> re.Pattern[str]:
        return re.compile(self.validacao.regex_nome)


def _coagir(tipo: typing.Any, valor: typing.Any, caminho: str) -> typing.Any:
    origem = typing.get_origin(tipo)
    if origem is list:
        if valor is None:
            return []
        if not isinstance(valor, list):
            raise ErroConfig(f"'{caminho}' deve ser uma lista")
        return [str(v) for v in valor]
    if origem is dict:
        if valor is None:
            return {}
        if not isinstance(valor, dict):
            raise ErroConfig(f"'{caminho}' deve ser um mapa chave: valor")
        return {str(k): str(v) for k, v in valor.items()}
    if tipo is bool:
        if not isinstance(valor, bool):
            raise ErroConfig(f"'{caminho}' deve ser true/false")
        return valor
    if tipo in (int, float):
        try:
            return tipo(valor)
        except (TypeError, ValueError) as e:
            raise ErroConfig(f"'{caminho}' deve ser numérico") from e
    if tipo is str:
        return "" if valor is None else str(valor)
    return valor


def _construir(cls: type, dados: typing.Any, caminho: str = "") -> typing.Any:
    if dados is None:
        dados = {}
    if not isinstance(dados, dict):
        raise ErroConfig(f"Seção '{caminho or 'raiz'}' deve ser um mapa")
    campos = {f.name: f for f in dataclasses.fields(cls) if f.init}
    # Chave desconhecida costuma ser erro de digitação (ex.: "ativ0"), e em auto_aceite isso é perigoso.
    desconhecidas = sorted(set(dados) - set(campos))
    if desconhecidas:
        raise ErroConfig(f"Chave(s) desconhecida(s) em '{caminho or 'raiz'}': {', '.join(desconhecidas)}")
    tipos = typing.get_type_hints(cls)
    kwargs = {}
    for nome, valor in dados.items():
        tipo = tipos[nome]
        sub = f"{caminho}.{nome}" if caminho else nome
        kwargs[nome] = _construir(tipo, valor, sub) if dataclasses.is_dataclass(tipo) else _coagir(tipo, valor, sub)
    return cls(**kwargs)


def carregar_dotenv(caminho: Path) -> None:
    """Carrega KEY=VALUE de um .env sem sobrescrever variáveis já definidas."""
    if not caminho.is_file():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


def _aplicar_env(cfg: Config) -> None:
    if url := os.getenv("ROTAS_TEAMS_WEBHOOK_URL"):
        cfg.notificacao.teams.webhook_url = url
    if dest := os.getenv("ROTAS_EMAIL_DESTINATARIOS"):
        cfg.notificacao.email.destinatarios = [d.strip() for d in re.split(r"[;,]", dest) if d.strip()]
    if url := os.getenv("ROTAS_URL_ROTAS"):
        cfg.app.url_rotas = url
    if url := os.getenv("ROTAS_URL_TABELA_ENDERECO"):
        cfg.app.url_tabela_endereco = url


def validar(cfg: Config, exigir_url: bool = True) -> None:
    """Valida regras que dependem de mais de um campo."""
    if exigir_url and not cfg.app.url_rotas:
        raise ErroConfig("app.url_rotas não configurada (ou variável ROTAS_URL_ROTAS)")
    try:
        cfg.tz
    except ZoneInfoNotFoundError as e:
        raise ErroConfig(f"Fuso '{cfg.monitor.fuso}' não encontrado. No Windows, instale o pacote tzdata.") from e
    try:
        cfg.regex_nome
    except re.error as e:
        raise ErroConfig(f"validacao.regex_nome inválida: {e}") from e
    if not re.fullmatch(r"\d{2}:\d{2}", cfg.monitor.heartbeat_hora):
        raise ErroConfig("monitor.heartbeat_hora deve estar no formato HH:MM")
    if cfg.monitor.intervalo_s < 15:
        raise ErroConfig("monitor.intervalo_s mínimo é 15 (o AppSheet demora para recarregar)")
    if cfg.hub.tabela.estrategia_linhas not in {"auto", "coordenadas", "ancestral", "blocos"}:
        raise ErroConfig("hub.tabela.estrategia_linhas deve ser auto, coordenadas, ancestral ou blocos")
    if cfg.hub.perfil_indica not in {"proibidos", "permitidos"}:
        raise ErroConfig("hub.perfil_indica deve ser 'proibidos' ou 'permitidos'")
    if cfg.auto_aceite.ativo and (not cfg.filtros.veiculos or not cfg.filtros.origens):
        raise ErroConfig("auto_aceite.ativo exige filtros.veiculos e filtros.origens preenchidos "
                         "(sem filtro, o robô aceitaria qualquer rota)")
    if cfg.auto_aceite.ativo and cfg.auto_aceite.max_por_hora < 1:
        raise ErroConfig("auto_aceite.max_por_hora deve ser >= 1")


def carregar_config(caminho: str | Path, exigir_url: bool = True) -> Config:
    """Lê o YAML, aplica .env/variáveis de ambiente e valida."""
    caminho = Path(caminho).resolve()
    if not caminho.is_file():
        raise ErroConfig(f"Arquivo de configuração não encontrado: {caminho} (copie config.example.yaml)")
    carregar_dotenv(caminho.parent / ".env")
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ErroConfig(f"YAML inválido em {caminho}: {e}") from e
    cfg = _construir(Config, dados)
    cfg.base_dir = caminho.parent
    _aplicar_env(cfg)
    validar(cfg, exigir_url=exigir_url)
    return cfg
