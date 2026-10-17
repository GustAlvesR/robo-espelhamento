"""Ponto de entrada: loop de monitoramento, resiliência e modos de diagnóstico."""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from datetime import time as dtime
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, Playwright, sync_playwright

from . import address_lookup, notifier, parser, regras, scraper
from .config import Config, ErroConfig, carregar_config
from .notifier import Mensagem, Notificador
from .state import Estado

log = logging.getLogger("rotas_extras")

_PADRAO = object()


def configurar_log(cfg: Config, verbose: bool = False) -> None:
    """Log em arquivo com rotação + console."""
    nivel = logging.DEBUG if verbose else getattr(logging, cfg.log.nivel.upper(), logging.INFO)
    arquivo = cfg.caminho(cfg.log.arquivo)
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    # Console do Windows em cp1252 quebra com emoji dos títulos; troca por '?' em vez de gerar erro.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    arq = RotatingFileHandler(arquivo, maxBytes=cfg.log.max_mb * 1024 * 1024, backupCount=cfg.log.backups,
                              encoding="utf-8")
    con = logging.StreamHandler(sys.stdout)
    raiz = logging.getLogger()
    raiz.handlers.clear()
    raiz.setLevel(nivel)
    for h in (arq, con):
        h.setFormatter(fmt)
        raiz.addHandler(h)


class Monitor:
    """Executa os ciclos de leitura, avaliação e notificação. Só lê a tela: nunca aceita nem recusa rotas."""

    def __init__(self, cfg: Config, pw: Playwright, notificador: Notificador, estado: Estado,
                 dry_run: bool = False, dump_html: bool = False):
        self.cfg = cfg
        self.pw = pw
        self.notificador = notificador
        self.estado = estado
        self.dry_run = dry_run
        self.dump_html = dump_html
        self.ctx = None
        self.page: Page | None = None
        self.cache = address_lookup.CacheTabela(cfg.caminho(cfg.hub.cache_arquivo))
        self.cache.carregar()
        self._proxima_tentativa_tabela: datetime | None = None
        self._envio_falhou_no_ciclo = False

    def agora(self) -> datetime:
        return datetime.now(self.cfg.tz)

    # ---------------- navegador ----------------

    def pagina(self) -> Page:
        if self.page is not None and not self.page.is_closed():
            return self.page
        self.encerrar()
        self.ctx = scraper.abrir_contexto(self.pw, self.cfg)
        self.page = scraper.pagina_principal(self.ctx)
        return self.page

    def encerrar(self) -> None:
        if self.ctx is not None:
            try:
                self.ctx.close()
            except PWError as e:
                log.debug("Erro ao fechar o navegador (já estava fechado?): %s", e)
        self.ctx = self.page = None

    # ---------------- loop ----------------

    def executar(self, once: bool = False) -> None:
        if not self.cache.linhas:
            self.atualizar_tabela()
        while True:
            inicio = time.monotonic()
            self.ciclo_protegido()
            self._salvar_estado()
            if once:
                break
            self._tarefas_periodicas()
            espera = self.cfg.monitor.intervalo_s - (time.monotonic() - inicio)
            if espera > 0:
                time.sleep(espera)

    def ciclo_protegido(self) -> None:
        self.estado.stats["ciclos"] += 1
        try:
            self.ciclo()
        except scraper.ErroSessao as e:
            self._registrar_falha(e, alertar=False)
            self._alertar("sessao", "sessão expirada no AppSheet",
                          f"{e}. Pare o robô e rode iniciar_monitor.bat --login (o Google bloqueia login na janela "
                          "controlada pelo robô).")
        except scraper.ErroCarregamento as e:
            self._registrar_falha(e)
        except PWError as e:
            self._registrar_falha(e)
            if "closed" in str(e).lower() or self.estado.contadores["falhas_seguidas"] >= 2:
                log.warning("Reiniciando o navegador no próximo ciclo")
                self.encerrar()
        except Exception as e:
            log.exception("Erro inesperado no ciclo")
            self._registrar_falha(e)
        else:
            self.estado.contadores["falhas_seguidas"] = 0
            self.estado.limpar_alerta("falhas")
            self.estado.limpar_alerta("sessao")

    def ciclo(self) -> None:
        cfg = self.cfg
        agora = self.agora()
        page = self.pagina()
        scraper.carregar_tela_rotas(page, cfg)
        ext = scraper.extrair_cards(page, cfg)
        if self.dump_html:
            scraper.salvar_dump(page, cfg.caminho(cfg.dumps_dir), ext)

        rotas = [parser.parse_card(c, cfg.tz) for c in ext.cards]
        self._checar_layout(ext, rotas)
        unicas: dict[str, parser.Rota] = {}
        for r in rotas:
            unicas.setdefault(r.chave, r)
        novas = self.estado.registrar_presentes(unicas.values(), agora)
        log.info("Ciclo: %d card(s), %d rota(s) única(s), %d nova(s)", len(ext.cards), len(unicas), len(novas))

        self._envio_falhou_no_ciclo = False
        for chave, rota in unicas.items():
            if self.estado.precisa_processar(chave):
                self._processar_rota(rota)

        removidas = self.estado.esquecer_ausentes(set(unicas), agora,
                                                  timedelta(minutes=cfg.monitor.esquecer_apos_min))
        if removidas:
            log.info("Rotas que sumiram da tela e foram esquecidas: %s", removidas)

    # ---------------- rotas ----------------

    def _processar_rota(self, rota: parser.Rota) -> None:
        cfg, chave = self.cfg, rota.chave
        av = regras.avaliar(rota, self.agora(), cfg, self.cache.linhas)
        if av.motivo_filtro:
            self.estado.marcar_filtrada(chave, av.motivo_filtro)
            log.info("Rota filtrada (%s): %s", av.motivo_filtro, chave)
            return
        if av.hub and av.hub.status == "SEM_MATCH" and rota.origem:
            self._registrar_sem_match(rota)

        if self._envio_falhou_no_ciclo:
            # Canais fora do ar: não gasta o ciclo com mais retries; a rota segue pendente.
            log.warning("Envio de %s adiado para o próximo ciclo (canais falharam neste ciclo)", chave)
            return
        msg = notifier.mensagem_rota(rota, av, self.estado.primeira_vez(chave), self.agora(), cfg)
        if self.notificador.enviar(msg):
            self.estado.marcar_notificada(chave, self.agora())
        else:
            self._envio_falhou_no_ciclo = True
            n = self.estado.registrar_falha_notificacao(chave)
            log.error("Notificação de %s falhou (%dª vez); continua pendente para o próximo ciclo", chave, n)

    def _registrar_sem_match(self, rota: parser.Rota) -> None:
        agora = self.agora()
        if not self.estado.origem_sem_match_nova_hoje(rota.origem, agora):
            return
        caminho = self.cfg.caminho(self.cfg.hub.arquivo_sem_match)
        caminho.parent.mkdir(parents=True, exist_ok=True)
        with caminho.open("a", encoding="utf-8") as f:
            f.write(f"{agora:%Y-%m-%d %H:%M:%S}\t{rota.origem}\t{rota.nome}\n")
        log.warning("Origem %s sem correspondência na tabela de endereço (registrada em %s)", rota.origem, caminho)

    def _checar_layout(self, ext: scraper.Extracao, rotas: list[parser.Rota]) -> None:
        c, m = self.estado.contadores, self.cfg.monitor
        if not ext.cards:
            c["ciclos_vazios"] += 1
            if ext.labels_total > 0 and ext.total_botoes == 0:
                self._alertar("layout_aceitar", "possível mudança de layout",
                              f"Há {ext.labels_total} elemento(s) de rota mas nenhum botão ACEITAR reconhecido. "
                              "Rode com --dump-html --once e confira os seletores.", cooldown=None)
            elif c["ciclos_vazios"] == m.ciclos_vazios_alerta:
                minutos = c["ciclos_vazios"] * m.intervalo_s // 60
                self._alertar("vazio", "nenhum card há muito tempo",
                              f"0 cards em {c['ciclos_vazios']} ciclos seguidos (~{minutos} min). Pode ser só falta "
                              "de oferta; se houver rotas na tela, o layout mudou (rode --dump-html --once).",
                              cooldown=None, nivel="alerta")
            return
        c["ciclos_vazios"] = 0
        self.estado.limpar_alerta("vazio")
        self.estado.limpar_alerta("layout_aceitar")
        sem_nome = sum(1 for r in rotas if not r.nome)
        if sem_nome or ext.botoes_sem_card:
            self._alertar("layout_parse", "cards com leitura incompleta",
                          f"{sem_nome} card(s) sem nome de rota e {ext.botoes_sem_card} botão(ões) ACEITAR sem card. "
                          "O layout pode ter mudado.")

    # ---------------- alertas, heartbeat e tabela ----------------

    def _registrar_falha(self, erro: Exception, alertar: bool = True) -> None:
        c = self.estado.contadores
        c["falhas_seguidas"] += 1
        self.estado.stats["falhas"] += 1
        self.estado.stats["ultimo_erro"] = f"{type(erro).__name__}: {str(erro)[:300]}"
        log.error("Ciclo falhou (%d seguida(s)): %s: %s", c["falhas_seguidas"], type(erro).__name__, erro)
        if alertar and c["falhas_seguidas"] >= self.cfg.monitor.falhas_seguidas_alerta:
            self._alertar("falhas", "ciclos falhando",
                          f"{c['falhas_seguidas']} ciclos seguidos falharam (rede, AppSheet fora do ar ou layout). "
                          f"Último erro: {self.estado.stats['ultimo_erro']}")

    def _alertar(self, tipo: str, titulo: str, texto: str, cooldown: object = _PADRAO, nivel: str = "erro") -> None:
        agora = self.agora()
        cd = timedelta(minutes=self.cfg.notificacao.cooldown_alertas_min) if cooldown is _PADRAO else cooldown
        if not self.estado.pode_alertar(tipo, agora, cd):
            log.debug("Alerta '%s' suprimido pelo cooldown", tipo)
            return
        log.warning("ALERTA %s: %s", tipo, texto)
        msg = Mensagem(titulo=f"⚙ Monitor de rotas extras: {titulo}", resumo=texto, nivel=nivel,
                       link=self.cfg.app.url_rotas or None)
        if self.notificador.enviar(msg):
            self.estado.marcar_alerta(tipo, agora)

    def _tarefas_periodicas(self) -> None:
        try:
            self._heartbeat()
            agora = self.agora()
            if (self.cache.desatualizada(agora, self.cfg.hub.atualizar_cada_h)
                    and (self._proxima_tentativa_tabela is None or agora >= self._proxima_tentativa_tabela)):
                self.atualizar_tabela()
        except PWError as e:
            log.error("Erro do navegador em tarefa periódica: %s", e)

    def _heartbeat(self) -> None:
        agora = self.agora()
        h, m = map(int, self.cfg.monitor.heartbeat_hora.split(":"))
        if agora.time() < dtime(h, m) or self.estado.heartbeat_data == agora.date().isoformat():
            return
        s = self.estado.stats
        tabela = (f"{len(self.cache.linhas)} linhas, lida em {self.cache.atualizada_em:%d/%m %H:%M}"
                  if self.cache.atualizada_em else "não carregada")
        msg = Mensagem(
            titulo="💓 Monitor de rotas extras ativo",
            resumo="Resumo desde o último heartbeat.", nivel="info", link=self.cfg.app.url_rotas or None,
            secoes=[(None, [("Ciclos", str(s["ciclos"])), ("Rotas novas", str(s["rotas_novas"])),
                            ("Notificadas", str(s["notificadas"])), ("Filtradas", str(s["filtradas"])),
                            ("Ciclos com falha", str(s["falhas"])), ("Último erro", s["ultimo_erro"] or "-"),
                            ("Última rota nova", s["ultima_rota_em"] or "-"), ("Tabela de endereço", tabela),
                            ("Modo", "dry-run" if self.dry_run else "normal")])])
        if self.notificador.enviar(msg):
            self.estado.heartbeat_data = agora.date().isoformat()
            self.estado.reiniciar_stats()

    def atualizar_tabela(self) -> bool:
        """Lê a TABELA DE ENDEREÇO em outra aba e atualiza o cache. Falha não interrompe o monitor."""
        agora = self.agora()
        inicio = time.monotonic()
        log.info("Lendo a tabela de endereço...")
        try:
            self.pagina()
            aba = self.ctx.new_page()
        except PWError as e:
            log.error("Não consegui abrir o navegador para ler a tabela: %s", e)
            self._proxima_tentativa_tabela = agora + timedelta(minutes=30)
            return False
        try:
            address_lookup.abrir_tabela(aba, self.cfg)
            linhas = address_lookup.ler_tabela(aba, self.cfg)
            self.cache.salvar(linhas, self.agora())
            self.estado.limpar_alerta("tabela")
            self._proxima_tentativa_tabela = None
            log.info("Tabela de endereço atualizada: %d linhas em %.0fs", len(linhas), time.monotonic() - inicio)
            return True
        except (PWError, RuntimeError) as e:
            login = scraper.detectar_login(aba, self.cfg) if not aba.is_closed() else False
            log.exception("Falha ao ler a tabela de endereço%s", " (tela de login)" if login else "")
            self._proxima_tentativa_tabela = agora + timedelta(minutes=30)
            origem = (f"cache de {self.cache.atualizada_em:%d/%m %H:%M}" if self.cache.atualizada_em
                      else "nenhum cache: avisos sairão sem dados do hub")
            self._alertar("tabela", "falha ao ler a tabela de endereço", f"{e}. Usando {origem}.",
                          cooldown=timedelta(hours=6), nivel="alerta")
            return False
        finally:
            if not aba.is_closed():
                aba.close()

    def _salvar_estado(self) -> None:
        try:
            self.estado.salvar()
        except OSError:
            log.exception("Não consegui gravar o estado")


# ---------------- modos auxiliares ----------------

def _modo_login(pw: Playwright, cfg: Config) -> int:
    """Login num navegador comum (o Google bloqueia login em navegador automatizado) e verificação."""
    try:
        proc = scraper.abrir_navegador_manual(cfg)
    except FileNotFoundError as e:
        print(e)
        return 1
    print("\nAbri um Edge NORMAL usando o perfil do robô.\n"
          "1. Faça o login do AppSheet/Google nessa janela, como faria no dia a dia.\n"
          "2. Confira que a tela ROTA EXTRA abriu.\n"
          "3. FECHE essa janela do Edge e volte aqui.")
    input("\nPressione ENTER depois de fechar a janela... ")
    if proc.poll() is None:
        print("A janela do Edge ainda está aberta; aguardando você fechá-la (até 2 min)...")
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            print("Ela continua aberta. Feche todas as janelas desse Edge e rode --login de novo.")
            return 1
    print("Verificando se o robô enxerga a sessão...")
    return _verificar_sessao(pw, cfg)


def _verificar_sessao(pw: Playwright, cfg: Config) -> int:
    try:
        ctx = scraper.abrir_contexto(pw, cfg)
    except PWError as e:
        print(f"Não consegui abrir o navegador do robô (alguma janela com o perfil ainda aberta?): {e}")
        return 1
    try:
        page = scraper.pagina_principal(ctx)
        scraper.carregar_tela_rotas(page, cfg)
        ext = scraper.extrair_cards(page, cfg)
        print(f"Login OK: a tela de rotas carregou ({len(ext.cards)} card(s) no momento).\n"
              "Próximo passo: iniciar_monitor.bat --once --dry-run")
        return 0
    except scraper.ErroSessao:
        print("O robô ainda cai na tela de login. Rode --login de novo e confirme que entrou no app antes "
              "de fechar a janela.")
        return 1
    except (scraper.ErroCarregamento, PWError) as e:
        print(f"Login parece feito, mas a tela de rotas não carregou: {e}\n"
              "Confira app.url_rotas no config.yaml e rode --once --dry-run --dump-html.")
        return 1
    finally:
        ctx.close()


def _modo_descobrir(pw: Playwright, cfg: Config, alvos: list[str]) -> int:
    ctx = scraper.abrir_contexto(pw, cfg)
    try:
        page = scraper.pagina_principal(ctx)
        address_lookup.abrir_tabela(page, cfg)
        if scraper.detectar_login(page, cfg):
            print("Tela de login detectada. Rode --login primeiro.")
            return 1
        pasta = cfg.caminho(cfg.dumps_dir) / f"discover_{datetime.now():%Y%m%d_%H%M%S}"
        res = address_lookup.descobrir_tabela(page, cfg, alvos, pasta)
        print(address_lookup.formatar_descoberta(res))
        try:
            linhas = address_lookup.ler_tabela(page, cfg)
            (pasta / "linhas_lidas.json").write_text(json.dumps(linhas, ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
            print(f"\nLeitura completa: {len(linhas)} linhas (veja linhas_lidas.json)")
        except (PWError, RuntimeError) as e:
            print(f"\nLeitura completa falhou: {e}")
        print(f"\nArquivos salvos em: {pasta}\nEnvie discover_table.json (e, se puder, o tabela.png).")
        return 0
    finally:
        ctx.close()


def _mensagem_teste(cfg: Config) -> Mensagem:
    return Mensagem(titulo="🧪 Teste do monitor de rotas extras",
                    resumo="Se você está lendo isto, o canal de notificação está funcionando.",
                    secoes=[(None, [("Enviado em", datetime.now(cfg.tz).strftime("%d/%m/%Y %H:%M:%S"))])],
                    alertas=["Exemplo de alerta (pode ignorar)"], link=cfg.app.url_rotas or None)


def _args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rotas_extras", description="Monitor de rotas extras do AppSheet")
    p.add_argument("--config", default="config.yaml", help="caminho do config.yaml")
    p.add_argument("--dry-run", action="store_true", help="não notifica e não grava o estado")
    p.add_argument("--once", action="store_true", help="executa um único ciclo")
    p.add_argument("--dump-html", action="store_true", help="salva HTML, screenshot e cards de cada ciclo")
    p.add_argument("--discover-table", action="store_true", help="diagnóstico da estrutura da tabela de endereço")
    p.add_argument("--discover-text", nargs="+", default=["Vitória", "MACRO REGIÃO"],
                   help="textos-alvo do --discover-table")
    p.add_argument("--login", action="store_true", help="abre um Edge comum no perfil do robô para o login e verifica a sessão")
    p.add_argument("--refresh-table", action="store_true", help="relê a tabela de endereço e sai")
    p.add_argument("--test-notify", action="store_true", help="envia uma notificação de teste e sai")
    p.add_argument("--verbose", action="store_true", help="log em nível DEBUG")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    try:
        cfg = carregar_config(Path(args.config), exigir_url=not args.test_notify)
    except ErroConfig as e:
        print(f"Erro de configuração: {e}", file=sys.stderr)
        return 2
    configurar_log(cfg, args.verbose)

    if args.test_notify:
        return 0 if Notificador(cfg.notificacao).enviar(_mensagem_teste(cfg)) else 1

    estado = Estado.carregar(None if args.dry_run else cfg.caminho(cfg.estado_arquivo))
    notificador = Notificador(cfg.notificacao, dry_run=args.dry_run)
    log.info("Iniciando (dry_run=%s, once=%s)", args.dry_run, args.once)
    with sync_playwright() as pw:
        if args.login:
            return _modo_login(pw, cfg)
        if args.discover_table:
            return _modo_descobrir(pw, cfg, args.discover_text)
        monitor = Monitor(cfg, pw, notificador, estado, dry_run=args.dry_run, dump_html=args.dump_html)
        try:
            if args.refresh_table:
                return 0 if monitor.atualizar_tabela() else 1
            monitor.executar(once=args.once)
        except KeyboardInterrupt:
            log.info("Interrompido pelo usuário")
        finally:
            monitor._salvar_estado()
            monitor.encerrar()
    return 0
