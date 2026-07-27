#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Competencia de alquiler en las zonas de mis pisos.

Lee los anuncios de alquiler publicados en idealista en La Fortuna (Leganes) y
Los Angeles (Villaverde, Madrid), con los filtros que ya vienen en cada enlace,
y envia un email con foto, precio, metros y habitaciones de cada anuncio.

Uso:
    python alquileres.py            # extrae y envia el email
    python alquileres.py --dry-run  # extrae y guarda preview_alquileres.html, sin enviar
"""

import gzip
import html as htmllib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from calendar import monthrange
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- config

EMAIL_FROM = "Alquileres <onboarding@resend.dev>"
EMAIL_TO = os.environ.get("EMAIL_TO", "igarcia@dazia.com")

# Las zonas donde tengo piso. El enlace ya lleva dentro los filtros de idealista
# (aqui, de dos dormitorios en adelante): para cambiar el filtro basta con hacer
# la busqueda en idealista y pegar aqui la URL que salga en el navegador.
ZONAS = [
    {
        "id": "la-fortuna",
        "nombre": "La Fortuna · Leganés",
        "url": "https://www.idealista.com/alquiler-viviendas/leganes/la-fortuna/la-fortuna/"
               "con-de-dos-dormitorios,de-tres-dormitorios,de-cuatro-cinco-habitaciones-o-mas/",
    },
    {
        "id": "los-angeles",
        "nombre": "Los Ángeles · Villaverde, Madrid",
        "url": "https://www.idealista.com/alquiler-viviendas/madrid/villaverde/los-angeles/"
               "con-de-dos-dormitorios,de-tres-dormitorios,de-cuatro-cinco-habitaciones-o-mas/",
    },
]

# Dias de envio en hora de Madrid: 0 = lunes ... 6 = domingo.
DIAS_ENVIO = (2, 5)  # miercoles y sabado

# Ventana (hora de Madrid) dentro de la cual se acepta enviar.
#
# El workflow lanza VARIOS intentos dentro de esta ventana. El primero que consiga
# ejecutarse manda el email y deja escrita la marca "ya enviado hoy"; los demas la
# leen y se retiran solos. Es lo que protege del punto debil de verdad:
#
#   GitHub NO garantiza las tareas programadas. Las mete en una cola compartida y,
#   si hay atasco, las DESCARTA -- no las retrasa: las pierde, sin aviso, sin email
#   y sin dejar rastro en la pestana Actions. Con un unico disparo al dia, un
#   descarte son 3 o 4 dias sin email y sin que nadie se entere.
#
# Por eso tampoco se mira que cron disparo la ejecucion, sino el reloj: da igual
# cual de los intentos llegue, mientras caiga dentro de la ventana.
VENTANA_ENVIO = ((7, 40), (14, 0))

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

# Tope de anuncios por zona en el email. Hoy hay 2 y 10; sobra de largo.
MAX_POR_ZONA = 40

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

BASE = "https://www.idealista.com"

# ---------------------------------------------------------------- helpers

try:  # la consola de Windows suele ser cp1252 y revienta con acentos
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

AQUI = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(AQUI, "alquileres.log")
ESTADO_PATH = os.path.join(AQUI, "estado_alquileres.json")


def log(msg):
    # ASCII puro: asi el log se lee igual con cualquier codificacion (el visor de
    # Windows y PowerShell no asumen UTF-8 y destrozarian los acentos).
    msg = msg.replace("…", "...").replace("·", "-").replace("→", "->").replace("²", "2")
    msg = msg.encode("ascii", "replace").decode("ascii")
    linea = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except Exception:
        pass
    try:
        print(linea, flush=True)
    except Exception:
        pass


def ahora_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def es_horario_de_verano(utc=None):
    """Reglas europeas: verano entre el ultimo domingo de marzo y el de octubre,
    ambos a las 01:00 UTC. Se calculan a mano porque Windows no trae la base de
    datos de zonas horarias y zoneinfo falla ahi."""
    utc = utc or ahora_utc()

    def ultimo_domingo(mes):
        ultimo = datetime(utc.year, mes, monthrange(utc.year, mes)[1])
        return (ultimo - timedelta(days=(ultimo.weekday() + 1) % 7)).replace(hour=1)

    return ultimo_domingo(3) <= utc < ultimo_domingo(10)


def ahora_en_madrid(utc=None):
    """Hora peninsular, sin depender de la base de datos de zonas horarias."""
    utc = utc or ahora_utc()
    return utc + timedelta(hours=2 if es_horario_de_verano(utc) else 1)


def toca_enviar(estado, ahora=None):
    """(si_toca, motivo_por_el_que_no). Mira dia, hora de Madrid y si ya se envio.

    Cualquiera de los intentos del dia sirve: el primero que llegue hasta aqui
    envia, y los siguientes se encuentran la marca puesta y no hacen nada.
    """
    ahora = ahora or ahora_en_madrid()

    if ahora.weekday() not in DIAS_ENVIO:
        return False, f"hoy es {DIAS[ahora.weekday()].lower()} y no toca envío"

    minutos = ahora.hour * 60 + ahora.minute
    inicio = VENTANA_ENVIO[0][0] * 60 + VENTANA_ENVIO[0][1]
    fin = VENTANA_ENVIO[1][0] * 60 + VENTANA_ENVIO[1][1]
    if not inicio <= minutos <= fin:
        return False, (f"en Madrid son las {ahora:%H:%M}, fuera de la ventana de envío "
                       f"({VENTANA_ENVIO[0][0]:02d}:{VENTANA_ENVIO[0][1]:02d}"
                       f"-{VENTANA_ENVIO[1][0]:02d}:{VENTANA_ENVIO[1][1]:02d})")

    if estado.get("ultimo_envio") == f"{ahora:%Y-%m-%d}":
        return False, "el email de hoy ya salió en un intento anterior"

    return True, ""


def limpiar(texto):
    """Quita etiquetas HTML, decodifica entidades y normaliza espacios."""
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = htmllib.unescape(texto).replace("\xa0", " ")
    return re.sub(r"\s+", " ", texto).strip()


def a_numero(texto):
    """'1.100' -> 1100. Devuelve None si no hay un numero limpio."""
    m = re.search(r"\d[\d.\s]*", texto or "")
    if not m:
        return None
    try:
        return int(re.sub(r"[.\s]", "", m.group(0)))
    except ValueError:
        return None


# ---------------------------------------------------------------- descarga
#
# idealista esta detras de DataDome. No basta con imitar las cabeceras de un
# navegador: reconoce al cliente por su huella (TLS y JavaScript) y responde 403.
# Medido el 27/07/2026 desde esta conexion:
#
#   urllib con cabeceras de Chrome ....... 403 (a la 4a peticion, y ya para siempre)
#   Chromium de Playwright, oculto ....... 403
#   Chromium de Playwright, con ventana .. OK
#   Chrome instalado, oculto ............. OK   <- lo que usamos
#
# Por eso la via principal es abrir un Chrome de verdad. El servicio de scraping
# y la descarga directa quedan como alternativas por si algun dia hicieran falta.

CABECERAS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

_tarro = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_tarro))


def _clave(nombre):
    """Lee una clave del entorno (GitHub) o de clave.txt / clave_scraper.txt (este PC)."""
    valor = os.environ.get(nombre, "").strip()
    if valor:
        return valor
    fichero = os.path.join(AQUI, "clave_scraper.txt" if "SCRAP" in nombre or "ZENROWS" in nombre
                           else "clave.txt")
    if os.path.exists(fichero):
        with open(fichero, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def servicio_de_scraping():
    """Devuelve (nombre, funcion_que_construye_la_url) del servicio configurado."""
    if _clave("SCRAPERAPI_KEY"):
        k = _clave("SCRAPERAPI_KEY")
        # Por defecto, proxies "premium". Si aun asi DataDome bloqueara, basta con
        # crear el secret SCRAPERAPI_NIVEL con el valor "ultra": es el modo que
        # ScraperAPI reserva para las webs mas protegidas. Gasta mas creditos.
        ultra = os.environ.get("SCRAPERAPI_NIVEL", "").strip().lower().startswith("ultra")
        nivel = {"ultra_premium": "true"} if ultra else {"premium": "true"}
        return f"ScraperAPI ({'ultra' if ultra else 'premium'})", lambda u: (
            "https://api.scraperapi.com/?" + urllib.parse.urlencode(
                {"api_key": k, "url": u, "country_code": "es", **nivel}))
    if _clave("SCRAPINGBEE_KEY"):
        k = _clave("SCRAPINGBEE_KEY")
        return "ScrapingBee", lambda u: (
            "https://app.scrapingbee.com/api/v1/?" + urllib.parse.urlencode(
                {"api_key": k, "url": u, "premium_proxy": "true",
                 "country_code": "es", "render_js": "false"}))
    if _clave("ZENROWS_KEY"):
        k = _clave("ZENROWS_KEY")
        return "ZenRows", lambda u: (
            "https://api.zenrows.com/v1/?" + urllib.parse.urlencode(
                {"apikey": k, "url": u, "premium_proxy": "true", "proxy_country": "es"}))
    return None, None


def _leer(url, timeout):
    req = urllib.request.Request(url, headers=CABECERAS)
    with _opener.open(req, timeout=timeout) as r:
        crudo = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        crudo = gzip.decompress(crudo)
    elif "deflate" in enc:
        crudo = zlib.decompress(crudo, -zlib.MAX_WBITS)
    return crudo.decode("utf-8", "replace")


def descargar(url, reintentos=3):
    """Devuelve (html, error). Usa el servicio de scraping si hay clave; si no, directo."""
    nombre, construir = servicio_de_scraping()
    destino = construir(url) if construir else url
    via = nombre or "directo"
    ultimo = ""

    for intento in range(1, reintentos + 1):
        try:
            pagina = _leer(destino, timeout=90 if nombre else 45)
            # DataDome a veces responde 200 con su pagina de bloqueo en lugar de 403.
            if "geo.captcha-delivery.com" in pagina or "Please enable JS" in pagina:
                ultimo = f"{via}: DataDome devolvio su pagina de bloqueo"
            else:
                return pagina, None
        except urllib.error.HTTPError as e:
            detalle = ""
            if e.code == 401:
                detalle = " (clave del servicio incorrecta o sin credito)"
            elif e.code == 403:
                detalle = " (DataDome ha bloqueado la peticion)"
            ultimo = f"{via}: HTTP {e.code}{detalle}"
        except Exception as e:
            ultimo = f"{via}: {type(e).__name__}"

        if intento < reintentos:
            espera = 15 * intento
            log(f"  reintento {intento}/{reintentos - 1} tras {espera}s - {ultimo}")
            time.sleep(espera)

    return None, ultimo


# --- via principal: un navegador de verdad ------------------------------------

INIT_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


# Se prueban por orden hasta que una pase el antibot. Medido en local: el Chrome
# instalado pasa incluso oculto, y el Chromium de Playwright solo con ventana.
# Desde los servidores de GitHub la IP tambien cuenta, asi que conviene tener
# varias balas: si una configuracion es bloqueada, se intenta con la siguiente.
# "Con ventana" en Linux necesita xvfb, que el workflow ya deja instalado.
CONFIGS_NAVEGADOR = [
    ("Chrome oculto",        {"channel": "chrome", "headless": True}),
    ("Chrome con ventana",   {"channel": "chrome", "headless": False}),
    ("Chromium con ventana", {"headless": False}),
    ("Chromium oculto",      {"headless": True}),
]


def _una_pagina(ctx, url, reintentos=1):
    ultimo = ""
    for intento in range(1, reintentos + 1):
        pag = ctx.new_page()
        try:
            resp = pag.goto(url, wait_until="domcontentloaded", timeout=60000)
            pag.wait_for_timeout(2500)
            html = pag.content()
            if "geo.captcha-delivery.com" in html or "Please enable JS" in html:
                ultimo = "navegador: DataDome ha bloqueado la peticion"
            elif resp and resp.status >= 400:
                ultimo = f"navegador: HTTP {resp.status}"
            else:
                return html, None
        except Exception as e:
            ultimo = f"navegador: {type(e).__name__}"
        finally:
            pag.close()
        if intento < reintentos:
            time.sleep(20)
    return None, ultimo


def _paginas_con_navegador(urls):
    """Devuelve ({url: (html, error)}, nota). El diccionario es None si ninguna
    configuracion de navegador consiguio pasar."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "Playwright no está instalado"

    nota = "ningún navegador logró pasar el antibot"

    with sync_playwright() as p:
        for como, opciones in CONFIGS_NAVEGADOR:
            try:
                nav = p.chromium.launch(
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                    **opciones)
            except Exception:
                continue  # ese navegador no esta disponible en esta maquina

            try:
                ctx = nav.new_context(
                    user_agent=UA, locale="es-ES", timezone_id="Europe/Madrid",
                    viewport={"width": 1366, "height": 900},
                    extra_http_headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"})
                ctx.add_init_script(INIT_JS)

                salida = {}
                for i, url in enumerate(urls):
                    if i:
                        time.sleep(5)  # sin prisa: son dos paginas dos dias por semana
                    salida[url] = _una_pagina(ctx, url)

                if any(html for html, _ in salida.values()):
                    log(f"Descarga vía: navegador real ({como})")
                    return salida, None

                nota = salida[urls[0]][1] or nota
                log(f"  {como}: sin suerte ({nota}); probando otra configuración")
            finally:
                try:
                    nav.close()
                except Exception:
                    pass

    return None, nota


def descargar_todas(urls):
    """Devuelve {url: (html, error)} por la mejor via disponible."""
    salida, nota = _paginas_con_navegador(urls)
    if salida is not None:
        return salida

    # El navegador no ha podido: queda el servicio de scraping, si hay clave.
    nombre, _ = servicio_de_scraping()
    if nombre:
        log(f"Descarga vía: {nombre} (el navegador no pasó: {nota})")
        salida = {}
        for i, url in enumerate(urls):
            if i:
                time.sleep(5)
            salida[url] = descargar(url)
        return salida

    return {u: (None, nota) for u in urls}


# ---------------------------------------------------------------- extraccion

ANUNCIO = re.compile(
    r'<article class="item[^"]*"[^>]*data-element-id="(\d+)"[^>]*>(.*?)</article>', re.S)


def parse_anuncios(pagina):
    """Saca de la pagina de resultados los datos de cada anuncio."""
    anuncios = []
    vistos = set()

    for aid, bloque in ANUNCIO.findall(pagina):
        if aid in vistos:
            continue
        vistos.add(aid)

        enlace = re.search(r'<a href="(/inmueble/\d+/?)"[^>]*class="item-link[^"]*"'
                           r'[^>]*title="([^"]*)"', bloque)
        precio = re.search(r'<span class="item-price[^"]*">([^<]*)<', bloque)
        chars = re.search(r'<div class="item-detail-char[^"]*">(.*?)</div>', bloque, re.S)
        detalles = [limpiar(d) for d in
                    re.findall(r'<span class="item-detail[^"]*">(.*?)</span>',
                               chars.group(1), re.S)] if chars else []

        # La primera foto de la galeria. El mapa estatico vive en otro dominio
        # (st3.idealista.com), asi que exigir img<numero> ya lo descarta.
        foto = re.search(r'<img[^>]+src="(https://img\d+\.idealista\.com[^"]+)"', bloque)

        habitaciones = next((a_numero(d) for d in detalles if re.search(r"\bhab", d)), None)
        metros = next((a_numero(d) for d in detalles if re.search(r"m[²2]\b", d)), None)
        planta = next((d for d in detalles if "planta" in d.lower()), "")

        anuncios.append({
            "id": aid,
            "titulo": limpiar(htmllib.unescape(enlace.group(2))) if enlace else "Anuncio",
            "url": BASE + enlace.group(1) if enlace else f"{BASE}/inmueble/{aid}/",
            "precio": a_numero(precio.group(1)) if precio else None,
            "habitaciones": habitaciones,
            "metros": metros,
            "planta": planta,
            "foto": foto.group(1) if foto else "",
            "garaje": any("garaje" in d.lower() for d in detalles),
        })

    return anuncios


def recoger(zona, pagina, error):
    """Devuelve (anuncios, error). error es None si fue bien."""
    if error:
        return [], f"no se pudo leer idealista - {error}"

    try:
        anuncios = parse_anuncios(pagina)
    except Exception as e:
        return [], f"error al analizar el HTML ({type(e).__name__})"

    if not anuncios:
        # Puede ser legitimo (cero resultados) o que idealista haya cambiado el HTML.
        cero = re.search(r"<h1[^>]*>\s*0\s+casas", pagina, re.I)
        if cero:
            return [], None
        return [], ("la pagina cargo pero no se reconocio ningun anuncio "
                    "(idealista puede haber cambiado su estructura)")

    return anuncios[:MAX_POR_ZONA], None


# ---------------------------------------------------------------- novedades


def leer_estado():
    """El fichero de estado entero: anuncios ya enviados y fecha del ultimo envio."""
    try:
        with open(ESTADO_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def vistos_de(estado):
    """IDs de anuncios que ya salieron en el email anterior, por zona."""
    return {k: set(v) for k, v in (estado.get("vistos") or {}).items()}


def guardar_estado(vistos, ahora=None):
    """Guarda que anuncios se han enviado y, sobre todo, que HOY ya se ha enviado.

    Esa fecha es la marca que hace que los intentos posteriores del mismo dia se
    retiren en lugar de mandar un segundo email.
    """
    ahora = ahora or ahora_en_madrid()
    try:
        with open(ESTADO_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "actualizado": f"{ahora:%Y-%m-%d %H:%M} (Madrid)",
                "ultimo_envio": f"{ahora:%Y-%m-%d}",
                "vistos": {k: sorted(v) for k, v in vistos.items()},
            }, f, indent=2)
        return True
    except Exception:
        log("AVISO: no se pudo guardar el estado. El proximo email marcara todo como "
            "nuevo, y si hoy queda algun intento por delante podria repetirse.")
        return False


# ---------------------------------------------------------------- email


def tarjeta(a, es_nuevo):
    """Una ficha de anuncio: foto a la izquierda, datos a la derecha."""
    precio = f"{a['precio']:,}".replace(",", ".") + " €" if a["precio"] else "s/precio"
    eur_m2 = ""
    if a["precio"] and a["metros"]:
        eur_m2 = f"{a['precio'] / a['metros']:.1f}".replace(".", ",") + " €/m²"

    datos = []
    if a["habitaciones"]:
        datos.append(f"{a['habitaciones']} hab.")
    if a["metros"]:
        datos.append(f"{a['metros']} m²")
    if eur_m2:
        datos.append(eur_m2)

    if a["foto"]:
        foto_html = (
            f'<a href="{htmllib.escape(a["url"], quote=True)}">'
            f'<img src="{htmllib.escape(a["foto"], quote=True)}" width="180" alt=""'
            f' style="display:block;width:180px;height:135px;object-fit:cover;'
            f'border-radius:3px;border:1px solid #e3e8ed;"></a>')
    else:
        foto_html = ('<div style="width:180px;height:135px;background:#f2f5f8;'
                     'border:1px solid #e3e8ed;border-radius:3px;color:#93a1b0;'
                     'font-size:12px;line-height:135px;text-align:center;'
                     'font-family:Arial,Helvetica,sans-serif;">Sin fotos</div>')

    etiquetas = ""
    if es_nuevo:
        etiquetas += ('<span style="display:inline-block;background:#1f7a5c;color:#ffffff;'
                      'font-size:10px;font-weight:700;letter-spacing:.08em;padding:3px 7px;'
                      'border-radius:2px;margin-right:6px;'
                      'font-family:Arial,Helvetica,sans-serif;">NUEVO</span>')
    if a["garaje"]:
        etiquetas += ('<span style="display:inline-block;background:#eef1f4;color:#5b6b7c;'
                      'font-size:10px;font-weight:700;letter-spacing:.08em;padding:3px 7px;'
                      'border-radius:2px;font-family:Arial,Helvetica,sans-serif;">GARAJE</span>')

    return f"""
    <tr>
      <td style="padding:0 0 20px 0;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td width="180" valign="top" style="width:180px;padding-right:16px;">{foto_html}</td>
            <td valign="top">
              {f'<div style="margin-bottom:6px;">{etiquetas}</div>' if etiquetas else ''}
              <div style="color:#12263a;font-size:22px;font-weight:700;line-height:1.1;
                          font-family:Georgia,'Times New Roman',serif;">{precio}<span
                   style="font-size:13px;font-weight:400;color:#5b6b7c;">/mes</span></div>
              <div style="margin-top:7px;color:#12263a;font-size:14px;font-weight:600;
                          font-family:Arial,Helvetica,sans-serif;">{htmllib.escape(' · '.join(datos))}</div>
              <a href="{htmllib.escape(a['url'], quote=True)}"
                 style="display:block;margin-top:7px;color:#5b6b7c;font-size:13px;line-height:1.4;
                        text-decoration:none;font-family:Arial,Helvetica,sans-serif;">
                {htmllib.escape(a['titulo'])}
              </a>
              {f'<div style="margin-top:4px;color:#93a1b0;font-size:12px;font-family:Arial,Helvetica,sans-serif;">{htmllib.escape(a["planta"])}</div>' if a['planta'] else ''}
            </td>
          </tr>
        </table>
      </td>
    </tr>"""


def construir_html(resultados, fecha_texto):
    partes = []

    for zona, anuncios, error, nuevos in resultados:
        filas = [tarjeta(a, a["id"] in nuevos) for a in anuncios]

        if error:
            filas.append(f"""
            <tr>
              <td style="padding:14px 16px;background:#fdf4f0;border-left:3px solid #d9694a;
                         color:#8a4530;font-size:13px;font-family:Arial,Helvetica,sans-serif;">
                No se pudieron recuperar los anuncios de esta zona: {htmllib.escape(error)}.
              </td>
            </tr>""")
        elif not anuncios:
            filas.append("""
            <tr>
              <td style="padding:14px 16px;background:#f2f5f8;color:#5b6b7c;font-size:13px;
                         font-family:Arial,Helvetica,sans-serif;">
                Ahora mismo no hay ningún anuncio publicado con estos filtros.
              </td>
            </tr>""")

        # Resumen de la zona: cuantos hay y en que horquilla de precio se mueven.
        precios = [a["precio"] for a in anuncios if a["precio"]]
        resumen = f"{len(anuncios)} anuncio{'s' if len(anuncios) != 1 else ''}"
        if precios:
            barato = f"{min(precios):,}".replace(",", ".")
            caro = f"{max(precios):,}".replace(",", ".")
            resumen += f" · {barato}–{caro} €" if len(precios) > 1 else f" · {barato} €"
        if nuevos:
            resumen += f" · {len(nuevos)} nuevo{'s' if len(nuevos) != 1 else ''}"

        partes.append(f"""
        <tr>
          <td style="padding:34px 32px 0 32px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="border-bottom:2px solid #12263a;padding-bottom:8px;">
                  <span style="color:#12263a;font-size:13px;font-weight:700;letter-spacing:.10em;
                               text-transform:uppercase;font-family:Arial,Helvetica,sans-serif;">
                    {htmllib.escape(zona['nombre'])}
                  </span>
                  <span style="color:#93a1b0;font-size:12px;font-family:Arial,Helvetica,sans-serif;">
                    &nbsp;· {htmllib.escape(resumen)}
                  </span>
                </td>
              </tr>
            </table>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="margin-top:20px;">
              {''.join(filas)}
            </table>
          </td>
        </tr>""")

    total = sum(len(a) for _, a, _, _ in resultados)

    return f"""<!DOCTYPE html>
<html lang="es">
<body style="margin:0;padding:0;background:#eef1f4;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background:#eef1f4;padding:28px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
               style="width:600px;max-width:100%;background:#ffffff;border-radius:4px;
                      overflow:hidden;box-shadow:0 1px 3px rgba(18,38,58,.10);">

          <tr>
            <td style="background:#12263a;padding:26px 32px;">
              <div style="color:#ffffff;font-size:19px;font-weight:700;letter-spacing:-.01em;
                          font-family:Georgia,'Times New Roman',serif;">
                Alquileres en mis zonas
              </div>
              <div style="color:#8fa3b8;font-size:12px;margin-top:5px;letter-spacing:.05em;
                          font-family:Arial,Helvetica,sans-serif;">
                {htmllib.escape(fecha_texto)} · {total} anuncio{'s' if total != 1 else ''} en alquiler
              </div>
            </td>
          </tr>

          {''.join(partes)}

          <tr>
            <td style="padding:34px 32px 26px 32px;">
              <div style="border-top:1px solid #e3e8ed;padding-top:16px;color:#93a1b0;
                          font-size:11px;line-height:1.6;font-family:Arial,Helvetica,sans-serif;">
                Anuncios de alquiler publicados en idealista.com con los filtros guardados
                para cada zona (dos dormitorios en adelante). Las fotos y los datos pertenecen
                a idealista y a cada anunciante. «NUEVO» marca lo que no aparecía en el email anterior.
              </div>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def enviar(asunto, cuerpo_html):
    clave = _clave("RESEND_API_KEY")
    if not clave:
        return False, ("no hay clave de Resend: define la variable de entorno "
                       "RESEND_API_KEY o crea el fichero clave.txt junto al script")

    payload = json.dumps({
        "from": EMAIL_FROM,
        "to": EMAIL_TO,
        "subject": asunto,
        "html": cuerpo_html,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {clave}",
            "Content-Type": "application/json",
            # Sin un User-Agent normal, Cloudflare bloquea la peticion con un 403 (error 1010)
            "User-Agent": UA,
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return True, f"{r.status} {r.read().decode('utf-8', 'replace')}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- main


def main():
    dry_run = "--dry-run" in sys.argv

    estado = leer_estado()

    if "--solo-por-la-manana" in sys.argv:
        toca, motivo = toca_enviar(estado)
        if not toca:
            log(f"No envía nada: {motivo}.")
            return 0

    estado_previo = vistos_de(estado)
    primera_vez = not estado_previo
    resultados = []
    estado_nuevo = dict(estado_previo)

    # Las dos zonas se piden en la misma sesion de navegador: abrirlo cuesta
    # varios segundos y no tiene sentido hacerlo dos veces.
    paginas = descargar_todas([z["url"] for z in ZONAS])

    for zona in ZONAS:
        pagina, fallo = paginas.get(zona["url"], (None, "no se llegó a pedir"))
        log(f"Leyendo {zona['nombre']}…")
        anuncios, error = recoger(zona, pagina, fallo)

        ids = {a["id"] for a in anuncios}
        # La primera ejecucion no marca nada como nuevo: aun no hay con que comparar.
        nuevos = set() if primera_vez else ids - estado_previo.get(zona["id"], set())
        if not error:
            estado_nuevo[zona["id"]] = ids

        log(f"  → {len(anuncios)} anuncios"
            + (f", {len(nuevos)} nuevos" if nuevos else "")
            + (f" · AVISO: {error}" if error else ""))

        # Primero lo nuevo, y dentro de cada grupo del mas barato al mas caro.
        anuncios.sort(key=lambda a: (a["id"] not in nuevos, a["precio"] or 10**9))
        resultados.append((zona, anuncios, error, nuevos))

    if all(error for _, _, error, _ in resultados):
        log("ERROR: ninguna zona se pudo leer. Se envia el email igualmente para que se note.")

    ahora = ahora_en_madrid()
    fecha_corta = f"{ahora:%d/%m/%Y}"
    fecha_texto = f"{DIAS[ahora.weekday()]} {fecha_corta}"

    total_nuevos = sum(len(n) for _, _, _, n in resultados)
    cuerpo = construir_html(resultados, fecha_texto)
    asunto = f"Alquileres La Fortuna y Los Ángeles - {fecha_corta}"
    if total_nuevos:
        asunto += f" ({total_nuevos} nuevo{'s' if total_nuevos != 1 else ''})"

    if dry_run:
        destino = os.path.join(AQUI, "preview_alquileres.html")
        with open(destino, "w", encoding="utf-8") as f:
            f.write(cuerpo)
        log(f"DRY RUN · preview guardado en {destino} · asunto: {asunto}")
        return 0

    log(f"Enviando a {EMAIL_TO}…")
    ok, detalle = enviar(asunto, cuerpo)
    log(("OK · " if ok else "FALLO · ") + detalle)

    if ok:
        # Lo primero que hay que dejar escrito es que hoy ya se ha enviado: es lo
        # que impide que los intentos que queden por delante repitan el email.
        guardar_estado(estado_nuevo, ahora)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        log("ERROR INESPERADO:\n" + traceback.format_exc())
        sys.exit(1)
