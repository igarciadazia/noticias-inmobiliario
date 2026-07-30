#!/usr/bin/env python3
"""
Resumen diario de noticias inmobiliarias.

Lee la portada de EjePrime y la seccion de mercado inmobiliario de
El Confidencial, y envia un email maquetado con los titulares via Resend.

Uso:
    python noticias.py            # extrae y envia el email
    python noticias.py --dry-run  # extrae y guarda preview.html, sin enviar
"""

import html as htmllib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- config

EMAIL_FROM = "Resumen Inmobiliario <onboarding@resend.dev>"
EMAIL_TO = os.environ.get("EMAIL_TO", "igarcia@dazia.com")


def clave_resend():
    """La clave nunca va en el codigo: asi el script se puede subir a GitHub.

    En GitHub Actions llega como variable de entorno (secret del repositorio).
    En este ordenador se lee de clave.txt, que esta excluido del repositorio.
    """
    valor = os.environ.get("RESEND_API_KEY", "").strip()
    if valor:
        return valor
    fichero = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clave.txt")
    if os.path.exists(fichero):
        with open(fichero, encoding="utf-8") as f:
            return f.read().strip()
    return ""

# Franja (hora de Madrid) dentro de la cual se acepta el envio de la manana.
# El workflow lanza VARIOS intentos aqui dentro porque el programador de GitHub
# es de "mejor esfuerzo" y descarta ejecuciones sin avisar. El primero que salga
# adelante envia; los demas ven la marca del dia y se retiran solos.
FRANJA_ENVIO = ((6, 0), (8, 59))

MAX_POR_FUENTE = 20
# Cuantas fichas se pueden abrir para averiguar la fecha de las noticias que no
# la muestran en portada. Limita lo que puede tardar el script.
MAX_FICHAS_EXTRA = 20

FUENTES = [
    {
        "id": "ejeprime",
        "nombre": "EjePrime",
        "url": "https://www.ejeprime.com/",
        "base": "https://www.ejeprime.com",
    },
    {
        "id": "confidencial",
        "nombre": "El Confidencial · Mercado inmobiliario",
        "url": "https://www.elconfidencial.com/tags/temas/mercado-inmobiliario-5324/",
        "base": "https://www.elconfidencial.com",
    },
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MESES = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}

# ---------------------------------------------------------------- helpers


try:  # la consola de Windows suele ser cp1252 y revienta con acentos
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "noticias.log")


def ahora_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def es_horario_de_verano(utc=None):
    """Reglas europeas: verano entre el ultimo domingo de marzo y el de octubre,
    ambos a las 01:00 UTC. Se calculan a mano porque Windows no trae la base de
    datos de zonas horarias y zoneinfo falla ahi."""
    from calendar import monthrange

    utc = utc or ahora_utc()

    def ultimo_domingo(mes):
        ultimo = datetime(utc.year, mes, monthrange(utc.year, mes)[1])
        return (ultimo - timedelta(days=(ultimo.weekday() + 1) % 7)).replace(hour=1)

    return ultimo_domingo(3) <= utc < ultimo_domingo(10)


def ahora_en_madrid(utc=None):
    """Hora peninsular, sin depender de la base de datos de zonas horarias."""
    utc = utc or ahora_utc()
    return utc + timedelta(hours=2 if es_horario_de_verano(utc) else 1)


def hora_programada_utc():
    """(hora, minuto) UTC del cron que disparo esta ejecucion, segun el evento de GitHub.

    Importa usar esto y no el reloj: GitHub retrasa las tareas programadas con
    frecuencia, y si una arrancase mas de una hora tarde, mirar la hora real
    descartaria el envio y el fallo pasaria inadvertido (la ejecucion sale verde).
    Devuelve None si no se ejecuta desde una programacion de GitHub.
    """
    ruta = os.environ.get("GITHUB_EVENT_PATH", "")
    if not ruta or not os.path.exists(ruta):
        return None
    try:
        with open(ruta, encoding="utf-8") as f:
            cron = json.load(f).get("schedule") or ""
    except Exception:
        return None
    m = re.match(r"\s*(\d{1,2})\s+(\d{1,2})\s", cron)
    return (int(m.group(2)), int(m.group(1))) if m else None


MARCA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ultimo-envio.txt")


def hoy_en_madrid():
    return f"{ahora_en_madrid():%Y-%m-%d}"


def ya_enviado_hoy():
    """La marca vive en el repositorio y la escribe la ejecucion que consigue enviar.

    Es lo que permite programar varios intentos por la manana sin duplicar correos:
    el primero que funciona deja la fecha escrita y los siguientes se retiran.
    """
    try:
        with open(MARCA_PATH, encoding="utf-8") as f:
            return f.read().strip() == hoy_en_madrid()
    except FileNotFoundError:
        return False
    except Exception:
        return False  # ante la duda, mejor enviar que quedarse callado


def marcar_enviado():
    try:
        with open(MARCA_PATH, "w", encoding="utf-8") as f:
            f.write(hoy_en_madrid() + "\n")
    except Exception as e:
        log(f"AVISO: no se pudo escribir la marca del dia ({type(e).__name__}). "
            f"Podria llegar un correo repetido.")


def le_toca_enviar(hora_utc, minuto_utc, utc=None):
    """Decide si el cron indicado es el que corresponde al envio de la manana.

    Hay dos crons separados una hora exacta (uno para horario de verano y otro
    para invierno). Se convierte la hora programada a hora de Madrid y solo envia
    el que cae dentro de FRANJA_ENVIO, asi que siempre entra uno y solo uno.
    """
    desfase = 2 if es_horario_de_verano(utc) else 1
    minutos_madrid = (hora_utc + desfase) * 60 + minuto_utc
    inicio = FRANJA_ENVIO[0][0] * 60 + FRANJA_ENVIO[0][1]
    fin = FRANJA_ENVIO[1][0] * 60 + FRANJA_ENVIO[1][1]
    return inicio <= minutos_madrid % (24 * 60) <= fin, minutos_madrid % (24 * 60)


def log(msg):
    # ASCII puro: asi el log se lee igual con cualquier codificacion (el visor de
    # Windows y PowerShell no asumen UTF-8 y destrozarian los acentos).
    msg = msg.replace("…", "...").replace("·", "-").replace("→", "->")
    msg = msg.encode("ascii", "replace").decode("ascii")
    linea = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    # El fichero primero: con pythonw.exe no hay consola y sys.stdout es None,
    # asi que un print() sin proteger abortaria el script en silencio.
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except Exception:
        pass
    try:
        print(linea, flush=True)
    except Exception:
        pass


def limpiar(texto):
    """Quita etiquetas HTML, decodifica entidades y normaliza espacios."""
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = htmllib.unescape(texto)
    texto = texto.replace("\xa0", " ")
    return re.sub(r"\s+", " ", texto).strip()


def descargar(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        crudo = r.read()
    charset = "utf-8"
    m = re.search(rb'charset=["\']?([\w-]+)', crudo[:3000], re.I)
    if m:
        charset = m.group(1).decode("ascii", "ignore")
    return crudo.decode(charset, "replace")


def absoluta(href, base):
    if href.startswith("http"):
        return href
    if href.startswith("//"):  # enlace sin protocolo: //dominio.com/ruta
        return "https:" + href
    return base + href if href.startswith("/") else f"{base}/{href}"


# ---------------------------------------------------------------- parsers


def fecha_es(texto):
    """'24 jul 2026 - 17:00' -> datetime. Devuelve (dt, tiene_hora)."""
    m = re.search(r"(\d{1,2})\s+([a-zA-Zñ]{3})[a-zA-Zñ.]*\s+(\d{4})(?:\s*-\s*(\d{1,2}):(\d{2}))?", texto)
    if not m:
        return None, False
    mes = MESES.get(m.group(2).lower()[:3])
    if not mes:
        return None, False
    hora, minuto = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
    try:
        return datetime(int(m.group(3)), mes, int(m.group(1)), hora, minuto), bool(m.group(4))
    except ValueError:
        return None, False


def fecha_del_articulo(url):
    """Lee 'datePublished' del JSON-LD de la ficha, para las que no la muestran en portada."""
    try:
        pagina = descargar(url, timeout=20)
    except Exception:
        return None, False
    m = re.search(r'"datePublished"\s*:\s*"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})', pagina)
    if not m:
        return None, False
    try:
        return datetime(*(int(g) for g in m.groups())), True
    except ValueError:
        return None, False


def parse_ejeprime(pagina, base):
    """Solo <article class="news_list_item">: es el flujo real de portada.

    Los <article class="featured_news_item"> son carruseles laterales (entrevistas,
    opinion, especiales) que no forman parte de la portada, y /content/ es
    publirreportaje, asi que ambos se descartan.
    """
    noticias = []
    vistos = set()

    patron = re.compile(r'<article class="news_list_item[^"]*"[^>]*>(.*?)</article>', re.S)

    for bloque in patron.findall(pagina):
        titulo_m = re.search(r"<h2[^>]*>(.*?)</h2>", bloque, re.S)
        href_m = re.search(r'<a[^>]+href="([^"]+)"', bloque)
        if not titulo_m or not href_m:
            continue

        titulo = limpiar(titulo_m.group(1))
        href = href_m.group(1)
        if len(titulo) < 20 or href in vistos or href.startswith("/content/"):
            continue
        vistos.add(href)

        fecha_m = re.search(r'<p class="date"[^>]*>(.*?)</p>', bloque, re.S)
        entrada_m = re.search(r'<div class="text[^"]*"[^>]*>(.*?)</div>', bloque, re.S)

        dt, con_hora = fecha_es(limpiar(fecha_m.group(1))) if fecha_m else (None, False)

        noticias.append({
            "titulo": titulo,
            "url": absoluta(href, base),
            "dt": dt,
            "con_hora": con_hora,
            "entradilla": limpiar(entrada_m.group(1)) if entrada_m else "",
        })

    # Varios layouts de portada no pintan la fecha: hay que ir a la ficha a buscarla.
    sin_fecha = [n for n in noticias if n["dt"] is None][:MAX_FICHAS_EXTRA]
    for n in sin_fecha:
        n["dt"], n["con_hora"] = fecha_del_articulo(n["url"])

    return noticias


def parse_confidencial(pagina, base):
    """El Confidencial: <a class="...titleLink" href><hN class="...title">Titular</hN>."""
    noticias = []
    vistos = set()

    patron = re.compile(
        r'<a[^>]+class="[^"]*titleLink[^"]*"[^>]+href="([^"]+)"[^>]*>\s*'
        r"<h\d[^>]*>(.*?)</h\d>",
        re.S | re.I,
    )

    for href, titulo_raw in patron.findall(pagina):
        titulo = limpiar(titulo_raw)
        if len(titulo) < 20 or href in vistos:
            continue
        vistos.add(href)

        # La fecha viene en la propia URL: /2026-07-26/
        dt = None
        fm = re.search(r"/(\d{4})-(\d{2})-(\d{2})/", href)
        if fm:
            try:
                dt = datetime(int(fm.group(1)), int(fm.group(2)), int(fm.group(3)))
            except ValueError:
                dt = None

        noticias.append({
            "titulo": titulo,
            "url": absoluta(href, base),
            "dt": dt,
            "con_hora": False,
            "entradilla": "",
        })

    return noticias


PARSERS = {"ejeprime": parse_ejeprime, "confidencial": parse_confidencial}


def recoger(fuente):
    """Devuelve (noticias, error). error es None si fue bien."""
    try:
        pagina = descargar(fuente["url"])
    except Exception as e:
        return [], f"no se pudo descargar la web ({type(e).__name__})"

    try:
        noticias = PARSERS[fuente["id"]](pagina, fuente["base"])
    except Exception as e:
        return [], f"error al analizar el HTML ({type(e).__name__})"

    if len(noticias) < 3:
        return noticias, "la web cargo pero no se reconocieron titulares (puede haber cambiado su estructura)"

    # Orden cronologico, lo mas reciente primero. Las que no tengan fecha quedan
    # al final conservando el orden en que aparecen en la portada.
    posicion = {id(n): i for i, n in enumerate(noticias)}
    noticias.sort(
        key=lambda n: (n["dt"] is not None, n["dt"] or datetime.min, -posicion[id(n)]),
        reverse=True,
    )

    return noticias[:MAX_POR_FUENTE], None


# ---------------------------------------------------------------- email


def construir_html(resultados, fecha_texto):
    partes = []

    for fuente, noticias, error in resultados:
        filas = []
        for n in noticias:
            if n["dt"] is None:
                meta = ""
            elif n["con_hora"]:
                meta = f"{n['dt']:%d/%m/%Y · %H:%M}"
            else:
                meta = f"{n['dt']:%d/%m/%Y}"
            entradilla = n["entradilla"]
            if len(entradilla) > 180:
                entradilla = entradilla[:180].rsplit(" ", 1)[0] + "…"

            filas.append(f"""
            <tr>
              <td style="padding:0 0 22px 0;">
                <a href="{htmllib.escape(n['url'], quote=True)}"
                   style="color:#12263a;font-size:16px;line-height:1.4;font-weight:600;
                          text-decoration:none;font-family:Georgia,'Times New Roman',serif;">
                  {htmllib.escape(n['titulo'])}
                </a>
                {f'<div style="margin-top:6px;color:#5b6b7c;font-size:13px;line-height:1.5;font-family:Arial,Helvetica,sans-serif;">{htmllib.escape(entradilla)}</div>' if entradilla else ''}
                {f'<div style="margin-top:6px;color:#93a1b0;font-size:11px;letter-spacing:.04em;text-transform:uppercase;font-family:Arial,Helvetica,sans-serif;">{htmllib.escape(meta)}</div>' if meta else ''}
              </td>
            </tr>""")

        if error:
            filas.append(f"""
            <tr>
              <td style="padding:14px 16px;background:#fdf4f0;border-left:3px solid #d9694a;
                         color:#8a4530;font-size:13px;font-family:Arial,Helvetica,sans-serif;">
                No se pudieron recuperar titulares de esta fuente: {htmllib.escape(error)}.
              </td>
            </tr>""")

        partes.append(f"""
        <tr>
          <td style="padding:34px 32px 0 32px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="border-bottom:2px solid #12263a;padding-bottom:8px;">
                  <span style="color:#12263a;font-size:13px;font-weight:700;letter-spacing:.10em;
                               text-transform:uppercase;font-family:Arial,Helvetica,sans-serif;">
                    {htmllib.escape(fuente['nombre'])}
                  </span>
                  <span style="color:#93a1b0;font-size:12px;font-family:Arial,Helvetica,sans-serif;">
                    &nbsp;· {len(noticias)}
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

    total = sum(len(n) for _, n, _ in resultados)

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
                Resumen inmobiliario
              </div>
              <div style="color:#8fa3b8;font-size:12px;margin-top:5px;letter-spacing:.05em;
                          font-family:Arial,Helvetica,sans-serif;">
                {htmllib.escape(fecha_texto)} · {total} titulares
              </div>
            </td>
          </tr>

          {''.join(partes)}

          <tr>
            <td style="padding:34px 32px 26px 32px;">
              <div style="border-top:1px solid #e3e8ed;padding-top:16px;color:#93a1b0;
                          font-size:11px;line-height:1.6;font-family:Arial,Helvetica,sans-serif;">
                Resumen automatico generado a partir de las portadas de EjePrime y
                El Confidencial. Los titulares y enlaces pertenecen a sus respectivos medios.
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
    clave = clave_resend()
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

    # GitHub Actions solo programa en UTC, asi que el workflow se lanza a las 6:00
    # y a las 7:00 UTC y aqui se descarta la que no corresponda: una u otra son las
    # 8:00 en Madrid segun sea horario de verano o de invierno.
    if "--programado" in sys.argv:
        programada = hora_programada_utc()

        if programada is not None:
            # Caso normal en GitHub: se mira que cron disparo la ejecucion, asi que
            # un retraso en el arranque no altera la decision.
            toca, minutos = le_toca_enviar(*programada)
            if not toca:
                log(f"Intento programado para las {minutos // 60:02d}:{minutos % 60:02d} "
                    f"de Madrid, fuera de la franja de envio. No envia nada.")
                return 0
        else:
            # Fuera de GitHub no hay evento que consultar: se mira el reloj.
            utc = ahora_utc()
            toca, _ = le_toca_enviar(utc.hour, utc.minute)
            if not toca:
                log(f"En Madrid son las {ahora_en_madrid():%H:%M}, fuera de la franja "
                    f"de envio. No envia nada.")
                return 0

        if ya_enviado_hoy():
            log(f"El resumen del {hoy_en_madrid()} ya se envio en un intento anterior. "
                f"Este se retira para no duplicar.")
            return 0

    resultados = []
    for fuente in FUENTES:
        log(f"Leyendo {fuente['nombre']}…")
        noticias, error = recoger(fuente)
        log(f"  → {len(noticias)} titulares" + (f" · AVISO: {error}" if error else ""))
        resultados.append((fuente, noticias, error))

    total = sum(len(n) for _, n, _ in resultados)
    if total == 0:
        log("ERROR: ninguna fuente devolvio titulares. No se envia email.")
        return 1

    # Madrid, no el reloj de la maquina: los servidores de GitHub van en UTC.
    ahora = ahora_en_madrid()
    fecha_corta = f"{ahora:%d/%m/%Y}"
    dias = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
    fecha_texto = f"{dias[ahora.weekday()]} {fecha_corta}"

    cuerpo = construir_html(resultados, fecha_texto)
    asunto = f"Resumen inmobiliario diario - {fecha_corta}"

    if dry_run:
        destino = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview.html")
        with open(destino, "w", encoding="utf-8") as f:
            f.write(cuerpo)
        log(f"DRY RUN · preview guardado en {destino} · asunto: {asunto}")
        return 0

    log(f"Enviando a {EMAIL_TO}…")
    ok, detalle = enviar(asunto, cuerpo)
    log(("OK · " if ok else "FALLO · ") + detalle)

    # Solo se marca el dia si el envio salio bien: si fallo, el siguiente intento
    # de la manana debe volver a probarlo.
    if ok and "--programado" in sys.argv:
        marcar_enviado()

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
