# Resumen inmobiliario diario

Cada día a las 8:00 (hora de Madrid) lee la portada de EjePrime y la sección de
mercado inmobiliario de El Confidencial y envía un email con los titulares,
ordenados del más reciente al más antiguo.

Se ejecuta en los servidores de GitHub, así que no depende de ningún ordenador
encendido.

## Cómo funciona

- `noticias.py` — extrae los titulares, maqueta el email y lo envía con Resend.
- `.github/workflows/resumen-diario.yml` — la programación diaria.

GitHub solo permite programar en horario UTC, así que el workflow se lanza a las
6:00 y a las 7:00 UTC y el script descarta la ejecución que no corresponda a las
8:00 de Madrid. Así el horario se mantiene al cambiar de estación.

## La clave de Resend

No está en el código. Se guarda en **Settings → Secrets and variables → Actions**
del repositorio, con el nombre `RESEND_API_KEY`.

## Lanzarlo a mano

Pestaña **Actions** → *Resumen inmobiliario diario* → **Run workflow**.
Las ejecuciones manuales envían el email siempre, sin comprobar la hora.

## Si algún día no llega el email

Entra en la pestaña **Actions** y abre la última ejecución: ahí se ve el registro
completo. GitHub además avisa por email cuando una ejecución falla.
