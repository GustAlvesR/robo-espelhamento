# -*- coding: utf-8 -*-
"""Medição e liberação de memória. O Render derruba o serviço acima de 512 MB, somando o
robô e o navegador da Angellira; estes números vão para o log para achar o pico real."""

import ctypes
import gc

_CGROUP = [("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
           ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes")]


def _ler_int(caminho):
    try:
        with open(caminho, encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def container_mb():
    """(uso, limite) do container em MB, quando o sistema informa; limite None se não houver."""
    for atual, maximo in _CGROUP:
        uso = _ler_int(atual)
        if uso is not None:
            limite = _ler_int(maximo)
            return round(uso / 2**20), round(limite / 2**20) if limite and limite < 2**50 else None
    return None, None


def processo_mb(pid="self") -> int:
    """Memória do processo em MB (PSS: a parte compartilhada é dividida entre quem a usa)."""
    for arquivo, campo in (("smaps_rollup", "Pss:"), ("status", "VmRSS:")):
        try:
            with open(f"/proc/{pid}/{arquivo}", encoding="ascii") as f:
                for linha in f:
                    if linha.startswith(campo):
                        return int(linha.split()[1]) // 1024
        except (OSError, ValueError):
            continue
    return 0


def limitar_arenas() -> None:
    """Com muitas threads, o malloc do glibc cria uma reserva de memória por thread e segura o
    que foi solto (equivale a MALLOC_ARENA_MAX=2). Chamar antes de abrir as threads."""
    try:
        ctypes.CDLL("libc.so.6").mallopt(-8, 2)  # -8 = M_ARENA_MAX
    except (OSError, AttributeError):
        pass


def liberar() -> None:
    """Devolve ao sistema a memória que o Python já soltou (antes de abrir o navegador)."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def resumo() -> str:
    uso, limite = container_mb()
    texto = f"robô {processo_mb()} MB"
    if uso is not None:
        texto += f", container {uso} MB" + (f" de {limite} MB" if limite else "")
    return texto

