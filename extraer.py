#!/usr/bin/env python3
"""Trae TODAS las filas de una API de datos.gov.co, página por página,
comprueba que no falte ninguna y guarda el bruto con fecha.

Uso (en la carpeta de tu repositorio):
    python3 extraer.py

La configuración va en el archivo .env, que nunca se sube al repositorio:
    API_URL=https://www.datos.gov.co/resource/nfrm-mmfe.json
    API_FILTRO=municipio=Medellín        (opcional; varios se unen con &)
    SOCRATA_APP_TOKEN=                   (opcional)
    TAM_PAGINA=1000                      (opcional)
    MAX_FILAS=100000                     (opcional: tope de seguridad)
    SIMULAR_FALLA=                       (opcional: pagina | error)

Resultado:
    data/raw/<conjunto>_<fecha>.json  el bruto, solo si la extracción está completa
    conteos.csv                       una fila por corrida, completa o no

Solo usa la biblioteca estándar de Python: no hay nada que instalar.
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

COLOMBIA = timezone(timedelta(hours=-5))
REINTENTOS = 3


def cargar_env(ruta=".env"):
    """Lee KEY=VALOR de .env sin pisar variables que ya existan."""
    if not os.path.exists(ruta):
        return
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def miles(n):
    return f"{n:,}".replace(",", ".")


class FallaAPI(Exception):
    pass


def pedir(base, params, token, simular_error=False):
    """GET con reintento prudente: espera lo que pida la API ante un 429 y
    reintenta pocas veces ante errores del servidor. Otros errores no se reintentan."""
    url = base + "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "estudia-extraer/1.0"}
    if token:
        headers["X-App-Token"] = token
    ultimo = ""
    for intento in range(1, REINTENTOS + 1):
        try:
            if simular_error:
                raise urllib.error.HTTPError(url, 503, "Service Unavailable (simulado)", None, None)
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            ultimo = f"HTTP {e.code} {e.reason}"
            if e.code == 429:
                espera = min(int(e.headers.get("Retry-After", "10")) if e.headers else 10, 60)
            elif e.code >= 500:
                espera = 2 ** intento
            else:
                cuerpo = ""
                try:
                    cuerpo = e.read().decode("utf-8")[:200]
                except Exception:
                    pass
                raise FallaAPI(f"{ultimo}. {cuerpo}".strip())
        except (urllib.error.URLError, TimeoutError) as e:
            ultimo = f"sin respuesta ({e})"
            espera = 2 ** intento
        if intento < REINTENTOS:
            print(f"  {ultimo}: espero {espera} s y reintento ({intento}/{REINTENTOS - 1})")
            time.sleep(espera)
    raise FallaAPI(f"{ultimo} después de {REINTENTOS} intentos")


def registrar(fila, ruta="conteos.csv"):
    nuevo = not os.path.exists(ruta)
    with open(ruta, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["fecha", "conjunto", "filtro", "esperadas", "traidas", "ids_unicos",
                        "paginas", "estado", "archivo", "detalle"])
        w.writerow(fila)


def revisar_gitignore():
    if os.path.exists(".gitignore"):
        with open(".gitignore", encoding="utf-8") as f:
            lineas = [l.strip() for l in f]
        if "data/" not in lineas and "data" not in lineas:
            print("OJO: agrega la línea data/ a .gitignore. El bruto no se sube al repositorio.")


def main():
    cargar_env()
    base = os.environ.get("API_URL", "").strip()
    if not base:
        sys.exit("Falta API_URL en .env. Copia .env.example como .env y llénalo.")
    filtro = os.environ.get("API_FILTRO", "").strip()
    token = os.environ.get("SOCRATA_APP_TOKEN", "").strip()
    tam = int(os.environ.get("TAM_PAGINA", "1000") or 1000)
    max_filas = int(os.environ.get("MAX_FILAS", "100000") or 100000)
    simular = os.environ.get("SIMULAR_FALLA", "").strip().lower()

    conjunto = base.rstrip("/").split("/")[-1].replace(".json", "")
    filtros = urllib.parse.parse_qsl(filtro, keep_blank_values=True)
    ahora = datetime.now(COLOMBIA)
    fecha = ahora.strftime("%Y-%m-%d %H:%M")
    revisar_gitignore()
    print(f"Fuente: {conjunto} · filtro: {filtro or '(ninguno)'}")
    if simular:
        print(f"SIMULACRO: falla «{simular}» activada")

    def cerrar(esperadas, traidas, unicos, paginas, estado, archivo, detalle):
        registrar([fecha, conjunto, filtro, esperadas, traidas, unicos, paginas, estado, archivo, detalle])
        print(f"Registro agregado a conteos.csv ({estado}).")
        sys.exit(0 if estado == "completa" else 1)

    # 1. Cifra de control: cuántas filas dice la API que hay.
    try:
        resp = pedir(base, filtros + [("$select", "count(*)")], token)
        esperadas = int(list(resp[0].values())[0])
    except (FallaAPI, IndexError, ValueError) as e:
        print(f"ALTO: no pude pedir el conteo: {e}")
        cerrar("", 0, 0, 0, "error", "", f"conteo: {e}")
    print(f"Esperadas según la API: {miles(esperadas)}")
    if esperadas == 0:
        print("ALTO: la API dice que hay 0 filas. Revisa API_FILTRO.")
        cerrar(0, 0, 0, 0, "vacia", "", "0 filas con ese filtro")
    if esperadas > max_filas:
        print(f"ALTO: {miles(esperadas)} filas pasan el tope de {miles(max_filas)}. "
              "Agrega un filtro o habla con el profe antes de traerlas.")
        cerrar(esperadas, 0, 0, 0, "detenida", "", f"más de {max_filas} filas")

    # 2. Páginas: mismo orden siempre (:id) para no repetir ni saltar filas.
    filas, pagina, offset = [], 0, 0
    while True:
        pagina += 1
        if simular == "pagina" and pagina == 2:
            print(f"Página {pagina}: SIMULACRO, me la salto")
            offset += tam
            continue
        params = filtros + [("$select", ":*,*"), ("$order", ":id"),
                            ("$limit", str(tam)), ("$offset", str(offset))]
        try:
            lote = pedir(base, params, token, simular_error=(simular == "error" and pagina == 2))
        except FallaAPI as e:
            print(f"ALTO: la página {pagina} falló: {e}")
            print("No guardo nada: una extracción a medias no es la cifra total.")
            cerrar(esperadas, len(filas), len({r.get(':id') for r in filas}), pagina,
                   "incompleta", "", f"página {pagina}: {e}")
        filas.extend(lote)
        print(f"Página {pagina}: {miles(len(lote))} filas (llevo {miles(len(filas))})")
        if len(lote) < tam or len(filas) > esperadas + tam:
            break
        offset += tam

    # 3. Comprobación: traídas = esperadas y sin filas repetidas.
    unicos = len({r.get(":id") for r in filas})
    print(f"Traídas: {miles(len(filas))} · id únicos: {miles(unicos)} · esperadas: {miles(esperadas)}")
    if len(filas) != esperadas or unicos != len(filas):
        print("ALTO: no cuadra. No guardo el bruto: vuelve a correr o revisa qué falta.")
        cerrar(esperadas, len(filas), unicos, pagina, "incompleta", "",
               "traídas distintas de esperadas" if len(filas) != esperadas else "filas repetidas")

    # 4. Bruto con fecha, fuera del repositorio.
    os.makedirs(os.path.join("data", "raw"), exist_ok=True)
    archivo = os.path.join("data", "raw", f"{conjunto}_{ahora.strftime('%Y-%m-%d_%H%M%S')}.json")
    with open(archivo, "w", encoding="utf-8") as f:
        json.dump(filas, f, ensure_ascii=False)
    print(f"COMPLETA. Bruto guardado en {archivo}")
    cerrar(esperadas, len(filas), unicos, pagina, "completa", archivo, "")


if __name__ == "__main__":
    main()
