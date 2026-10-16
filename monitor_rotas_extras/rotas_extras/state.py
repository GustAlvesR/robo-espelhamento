"""Estado persistente (JSON com gravação atômica): deduplicação, cooldown de alertas e contadores."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .parser import Rota

log = logging.getLogger(__name__)

PENDENTE, NOTIFICADA, FILTRADA = "pendente", "notificada", "filtrada"


class Estado:
    """Memória do robô entre ciclos. Com caminho=None fica só em memória (usado no --dry-run)."""

    def __init__(self, caminho: Path | None):
        self.caminho = caminho
        self.rotas: dict[str, dict] = {}
        self.alertas: dict[str, str] = {}
        self.origens_sem_match: dict[str, str] = {}
        self.contadores: dict[str, int] = {"ciclos_vazios": 0, "falhas_seguidas": 0}
        self.stats: dict = self._stats_zerados()
        self.heartbeat_data: str | None = None

    @staticmethod
    def _stats_zerados() -> dict:
        return {"ciclos": 0, "rotas_novas": 0, "notificadas": 0, "filtradas": 0, "falhas": 0,
                "ultimo_erro": None, "ultima_rota_em": None}

    # ---------------- persistência ----------------

    @classmethod
    def carregar(cls, caminho: Path | None) -> "Estado":
        est = cls(caminho)
        if caminho is None or not caminho.is_file():
            return est
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            backup = caminho.with_name(f"{caminho.stem}.corrompido-{int(time.time())}{caminho.suffix}")
            log.error("Estado ilegível (%s). Movido para %s; começando do zero (rotas atuais serão avisadas de novo)",
                      e, backup.name)
            os.replace(caminho, backup)
            return est
        est.rotas = dados.get("rotas", {})
        est.alertas = dados.get("alertas", {})
        est.origens_sem_match = dados.get("origens_sem_match", {})
        est.contadores.update(dados.get("contadores", {}))
        est.stats.update(dados.get("stats", {}))
        est.heartbeat_data = dados.get("heartbeat_data")
        return est

    def salvar(self) -> None:
        if self.caminho is None:
            return
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        dados = {"versao": 1, "rotas": self.rotas, "alertas": self.alertas,
                 "origens_sem_match": self.origens_sem_match, "contadores": self.contadores,
                 "stats": self.stats, "heartbeat_data": self.heartbeat_data}
        tmp = self.caminho.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
        # No Windows o antivírus/indexador pode segurar o arquivo por instantes.
        for tentativa in range(3):
            try:
                os.replace(tmp, self.caminho)
                return
            except PermissionError:
                if tentativa == 2:
                    raise
                time.sleep(0.5)

    # ---------------- rotas ----------------

    def registrar_presentes(self, rotas: Iterable[Rota], agora: datetime) -> list[str]:
        """Atualiza 'ultima_vez' das rotas presentes; retorna as chaves vistas pela primeira vez."""
        novas = []
        for r in rotas:
            reg = self.rotas.get(r.chave)
            if reg is None:
                self.rotas[r.chave] = {"nome": r.nome, "primeira_vez": agora.isoformat(),
                                       "ultima_vez": agora.isoformat(), "status": PENDENTE, "tentativas": 0}
                novas.append(r.chave)
            else:
                reg["ultima_vez"] = agora.isoformat()
        if novas:
            self.stats["rotas_novas"] += len(novas)
            self.stats["ultima_rota_em"] = agora.isoformat()
        return novas

    def esquecer_ausentes(self, presentes: set[str], agora: datetime, apos: timedelta) -> list[str]:
        """Remove rotas que não aparecem há mais de `apos` (tolera falhas pontuais de leitura)."""
        removidas = [k for k, reg in self.rotas.items()
                     if k not in presentes and agora - datetime.fromisoformat(reg["ultima_vez"]) > apos]
        for k in removidas:
            del self.rotas[k]
        return removidas

    def precisa_processar(self, chave: str) -> bool:
        return self.rotas.get(chave, {}).get("status") == PENDENTE

    def primeira_vez(self, chave: str) -> datetime:
        return datetime.fromisoformat(self.rotas[chave]["primeira_vez"])

    def marcar_notificada(self, chave: str, agora: datetime) -> None:
        self.rotas[chave].update(status=NOTIFICADA, notificada_em=agora.isoformat())
        self.stats["notificadas"] += 1

    def marcar_filtrada(self, chave: str, motivo: str) -> None:
        self.rotas[chave].update(status=FILTRADA, motivo=motivo)
        self.stats["filtradas"] += 1

    def registrar_falha_notificacao(self, chave: str) -> int:
        self.rotas[chave]["tentativas"] = self.rotas[chave].get("tentativas", 0) + 1
        return self.rotas[chave]["tentativas"]

    # ---------------- alertas de sistema ----------------

    def pode_alertar(self, tipo: str, agora: datetime, cooldown: timedelta | None) -> bool:
        """cooldown=None: alerta uma única vez até `limpar_alerta(tipo)`."""
        ultimo = self.alertas.get(tipo)
        if ultimo is None:
            return True
        if cooldown is None:
            return False
        return agora - datetime.fromisoformat(ultimo) >= cooldown

    def marcar_alerta(self, tipo: str, agora: datetime) -> None:
        self.alertas[tipo] = agora.isoformat()

    def limpar_alerta(self, tipo: str) -> None:
        self.alertas.pop(tipo, None)

    # ---------------- origens sem match ----------------

    def origem_sem_match_nova_hoje(self, origem: str, agora: datetime) -> bool:
        """True se a origem ainda não foi registrada hoje (evita poluir o log)."""
        hoje = agora.date().isoformat()
        if self.origens_sem_match.get(origem) == hoje:
            return False
        self.origens_sem_match[origem] = hoje
        return True

    # ---------------- heartbeat ----------------

    def reiniciar_stats(self) -> None:
        ultima = self.stats.get("ultima_rota_em")
        self.stats = self._stats_zerados()
        self.stats["ultima_rota_em"] = ultima
