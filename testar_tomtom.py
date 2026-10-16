"""Confere se a sua chave TomTom funciona em cada API que o robô usa.
Uso (PowerShell):  $env:TOMTOM_API_KEY="SUA_CHAVE"; python testar_tomtom.py"""

import os
import sys
import urllib.error
import urllib.request

KEY = os.getenv("TOMTOM_API_KEY") or (sys.argv[1] if len(sys.argv) > 1 else "")
if not KEY:
    sys.exit("Defina TOMTOM_API_KEY ou passe a chave como argumento.")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from site_web import TOMTOM_TILE_URL  # noqa: E402

TESTES = {
    "Geocodificação (estações e cidades)":
        "https://api.tomtom.com/search/2/geocode/BETIM%2C%20MG.json?countrySet=BR&limit=1&key={k}",
    "Busca de ponto de referência":
        "https://api.tomtom.com/search/2/search/GESTAMP.json?countrySet=BR&limit=1&lat=-19.97&lon=-44.20&radius=30000&key={k}",
    "Rota de caminhão com trânsito":
        "https://api.tomtom.com/routing/1/calculateRoute/-19.97,-44.20:-23.19,-46.88/json"
        "?travelMode=truck&traffic=true&routeRepresentation=summaryOnly&key={k}",
    "Tiles do mapa": TOMTOM_TILE_URL.format(z=5, x=11, y=17, key="{k}"),
}

falhou = False
for nome, url in TESTES.items():
    try:
        with urllib.request.urlopen(url.format(k=KEY), timeout=20) as r:
            print(f"OK    {nome} (HTTP {r.status})")
    except urllib.error.HTTPError as e:
        falhou = True
        print(f"FALHA {nome}: HTTP {e.code} {e.read()[:200].decode(errors='ignore')}")
    except Exception as e:
        falhou = True
        print(f"FALHA {nome}: {e}")

sys.exit(1 if falhou else 0)
