# -*- coding: utf-8 -*-
"""Site do monitoramento: login, mapa e API. Roda no mesmo processo do robô
(o Render free só sustenta um serviço ligado 24/7)."""

import hmac
import json
import math
import logging
import os
import re
import secrets
import time
import urllib.request
from collections import OrderedDict, defaultdict
from datetime import timedelta
from functools import wraps

from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import geo_tomtom

USUARIOS_WORKSHEET_NAME = os.getenv("USUARIOS_WORKSHEET_NAME", "Usuarios_Site")
TOMTOM_TILE_URL = os.getenv(
    "TOMTOM_TILE_URL",
    "https://api.tomtom.com/maps/orbis/display/raster/tile/{z}/{x}/{y}"
    "?apiVersion=2&style={estilo}&key={key}")
ESTILOS_MAPA = {"claro": "street-light", "escuro": "street-dark"}
SESSAO_HORAS = int(os.getenv("SESSAO_HORAS", "12"))
MAX_TILES_CACHE = 800
MAX_FALHAS_LOGIN, JANELA_FALHAS_S = 5, 15 * 60


def _limpar(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _limpar(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_limpar(x) for x in v]
    return v


def create_app(get_snapshot, get_client, results_spreadsheet_id, tomtom_key):
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    secret = os.getenv("SECRET_KEY")
    if not secret:
        logging.warning("SECRET_KEY não definida: as sessões caem a cada reinício.")
        secret = secrets.token_hex(32)
    app.config.update(
        SECRET_KEY=secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSAO_HORAS),
    )

    usuarios_cache = {"em": 0.0, "dados": {}}
    estacoes_cache = {"em": 0.0, "dados": []}
    falhas = defaultdict(list)
    tiles = OrderedDict()

    def carregar_usuarios(forcar=False):
        if not forcar and time.time() - usuarios_cache["em"] < 300:
            return usuarios_cache["dados"]
        try:
            ws = get_client().open_by_key(results_spreadsheet_id).worksheet(USUARIOS_WORKSHEET_NAME)
            dados = {}
            for l in ws.get_all_values()[1:]:
                l = (l + [""] * 4)[:4]
                email = l[0].strip().lower()
                ativo = l[3].strip().lower() not in ("não", "nao", "n", "false", "0")
                if email and l[2].strip() and ativo:
                    dados[email] = {"nome": l[1].strip() or email, "hash": l[2].strip()}
            usuarios_cache.update(em=time.time(), dados=dados)
        except Exception:
            logging.exception("Não foi possível ler a aba %s.", USUARIOS_WORKSHEET_NAME)
        return usuarios_cache["dados"]

    def carregar_estacoes():
        # Lidas direto da aba, e não do ciclo do robô: depois de um reinício o
        # primeiro ciclo (com a TomTom) leva minutos para terminar.
        if estacoes_cache["dados"] and time.time() - estacoes_cache["em"] < 600:
            return estacoes_cache["dados"]
        try:
            dados = geo_tomtom.ler_estacoes_mapa(get_client().open_by_key(results_spreadsheet_id))
            estacoes_cache.update(em=time.time(), dados=dados)
        except Exception:
            logging.exception("Não foi possível ler a aba %s.", geo_tomtom.ESTACOES_WORKSHEET_NAME)
        return estacoes_cache["dados"]

    def exige_login(f):
        @wraps(f)
        def wrapper(*a, **kw):
            email = session.get("email")
            if not email or email not in carregar_usuarios():
                session.clear()
                if request.path.startswith(("/api/", "/tiles/")):
                    abort(401)
                return redirect(url_for("login"))
            return f(*a, **kw)
        return wrapper

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def csrf_ok():
        return hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", ""))

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.after_request
    def cabecalhos(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    @app.get("/health")
    def health():
        return "ok"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        erro = ""
        if request.method == "POST":
            ip = request.remote_addr or "?"
            agora = time.time()
            falhas[ip] = [t for t in falhas[ip] if agora - t < JANELA_FALHAS_S]
            if len(falhas[ip]) >= MAX_FALHAS_LOGIN:
                erro = "Muitas tentativas. Aguarde 15 minutos."
            elif not csrf_ok():
                erro = "Sessão expirada. Tente de novo."
            else:
                email = request.form.get("email", "").strip().lower()
                senha = request.form.get("senha", "")
                u = carregar_usuarios(forcar=email not in usuarios_cache["dados"]).get(email)
                if u and check_password_hash(u["hash"], senha):
                    session.clear()
                    session.permanent = True
                    session["email"], session["nome"] = email, u["nome"]
                    falhas.pop(ip, None)
                    return redirect(url_for("index"))
                falhas[ip].append(agora)
                erro = "E-mail ou senha inválidos."
        return render_template("login.html", erro=erro)

    @app.post("/logout")
    def logout():
        if csrf_ok():
            session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @exige_login
    def index():
        # Os dados já vão dentro da página: em redes que bloqueiam as chamadas em
        # segundo plano (fetch), o site funciona recarregando a página inteira.
        dados = dados_json().replace("<", "\\u003c")
        estacoes = json.dumps(carregar_estacoes(), ensure_ascii=False).replace("<", "\\u003c")
        return render_template("index.html", nome=session.get("nome", ""), dados_iniciais=dados,
                               estacoes_iniciais=estacoes)

    @app.get("/api/rotas")
    @exige_login
    def api_rotas():
        return Response(dados_json(), mimetype="application/json")

    def dados_json() -> str:
        snap = get_snapshot() or {}
        geo = snap.get("geo", {})
        rotas = [{**r, "_geo": geo.get(r["Trip Number"], {})} for r in snap.get("resultados", [])]
        corpo = {"atualizado_em": snap.get("atualizado_em", ""),
                 "angellira_em": snap.get("angellira_em", ""),
                 "tomtom": snap.get("tomtom", {}), "rotas": rotas}
        # allow_nan=False + _limpar: um NaN vindo da planilha geraria JSON inválido
        # e quebraria a página inteira.
        return json.dumps(_limpar(corpo), ensure_ascii=False, allow_nan=False, default=str)

    @app.get("/tiles/<int:z>/<int:x>/<int:y>.png")
    @exige_login
    def tile(z, x, y):
        estilo = ESTILOS_MAPA.get(request.args.get("tema", "claro"))
        if (not tomtom_key or not estilo or not 0 <= z <= 20
                or not (0 <= x < 2 ** z and 0 <= y < 2 ** z)):
            abort(404)
        chave = (estilo, z, x, y)
        if chave in tiles:
            tiles.move_to_end(chave)
            corpo, tipo = tiles[chave]
        else:
            url = TOMTOM_TILE_URL.format(z=z, x=x, y=y, key=tomtom_key, estilo=estilo)
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    corpo, tipo = r.read(), r.headers.get("Content-Type", "image/png")
            except Exception as e:
                logging.warning("Tile %s/%s/%s falhou (%s).", z, x, y, re.sub(r"key=\w+", "key=***", str(e)))
                abort(502)
            tiles[chave] = (corpo, tipo)
            if len(tiles) > MAX_TILES_CACHE:
                tiles.popitem(last=False)
        resp = Response(corpo, mimetype=tipo)
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    return app
