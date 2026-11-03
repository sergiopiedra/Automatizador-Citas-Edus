#!/usr/bin/env python3
"""
Local EDUS appointment watcher for macOS (no Telegram, no cloud services).

- Credentials are stored in the macOS Keychain, never in files or env vars.
- The CAPTCHA pops up in a native dialog; you type it.
- Logs in once and keeps polling for slots in the same session, so the
  CAPTCHA is only requested again when EDUS expires the session.
- Books the first available slot, notifies you, and stops.

Usage:
  python edus_local.py --setup     # store cedula/clave in the Keychain
  python edus_local.py             # watch until a slot is booked (Ctrl+C to stop)
"""
import argparse
import getpass
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import keyring
from PIL import Image
from playwright.sync_api import sync_playwright

KEYCHAIN_SERVICE = "edus-citas"
BASE = "https://edus.ccss.sa.cr"
LOGIN_URL = f"{BASE}/eduscitasweb/"
DATA_DIR = Path.home() / "Library" / "Application Support" / "EdusCitas"
CAPTCHA_TIMEOUT = 110
MAX_LOGIN = 5

SUCCESS_WORDS = ("confirmada", "exitosa", "registrada", "agendada", "reservada", "comprobante")
TAKEN_WORDS = ("asignada a otro", "no se encontraron", "no disponible", "no fue posible", "sin éxito")
CAPTCHA_WORDS = ("captcha", "código", "codigo", "imagen")
CREDENTIAL_WORDS = ("clave", "contraseña", "credencial", "incorrect")


class CredencialesInvalidas(Exception):
    pass


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ── macOS integration ─────────────────────────────────────────

def _osascript(lines: list[str], *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    # Values go through argv, never interpolated into the script.
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    return subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=timeout)


def notificar(titulo: str, mensaje: str) -> None:
    _osascript([
        "on run argv",
        'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
        "end run",
    ], titulo, mensaje)


def alerta(titulo: str, mensaje: str) -> None:
    subprocess.Popen(["afplay", "/System/Library/Sounds/Hero.aiff"])
    _osascript([
        "on run argv",
        "display alert (item 1 of argv) message (item 2 of argv)",
        "end run",
    ], titulo, mensaje)


def pedir_captcha(img: Path) -> str:
    subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
    visor = subprocess.Popen(["qlmanage", "-p", str(img)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        r = _osascript([
            "on run argv",
            'set r to display dialog (item 1 of argv) default answer "" with title "EDUS Citas" '
            "giving up after ((item 2 of argv) as integer)",
            'if gave up of r then return ""',
            "return text returned of r",
            "end run",
        ], "Escribe el código de seguridad que ves en la imagen:", str(CAPTCHA_TIMEOUT),
            timeout=CAPTCHA_TIMEOUT + 15)
    except subprocess.TimeoutExpired:
        return ""
    finally:
        visor.terminate()
    if r.returncode != 0:
        return ""
    return "".join(ch for ch in r.stdout.strip() if ch.isalnum())


# ── Credentials ───────────────────────────────────────────────

def setup() -> None:
    cedula = input("Cédula (9 dígitos): ").strip()
    if not (cedula.isdigit() and len(cedula) == 9):
        sys.exit("La cédula debe tener 9 dígitos.")
    clave = getpass.getpass("Clave de EDUS (no se muestra): ")
    if not clave:
        sys.exit("La clave no puede estar vacía.")
    keyring.set_password(KEYCHAIN_SERVICE, "cedula", cedula)
    keyring.set_password(KEYCHAIN_SERVICE, "clave", clave)
    print("Credenciales guardadas en el Llavero de macOS.")


def cargar_credenciales() -> tuple[str, str]:
    cedula = keyring.get_password(KEYCHAIN_SERVICE, "cedula")
    clave = keyring.get_password(KEYCHAIN_SERVICE, "clave")
    if not cedula or not clave:
        sys.exit("No hay credenciales. Ejecuta primero: python edus_local.py --setup")
    return cedula, clave


# ── EDUS ──────────────────────────────────────────────────────

def esperar(cond, timeout: float, paso: float = 0.5) -> bool:
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(paso)
    return False


def en_menu(page) -> bool:
    return "Agregar una cita" in page.content()


def leer_errores(page) -> list[str]:
    return page.evaluate("""
        () => {
            const sel = '.ui-messages-error, .ui-message-error, .ui-growl-message-error, .messagesError, ' +
                        '.error, .ui-messages, [class*="error"], [class*="message"]';
            const out = [];
            for (const el of document.querySelectorAll(sel)) {
                if (!el.getClientRects().length || el.closest('[id^="formChatBot"]')) continue;
                const t = el.textContent.trim();
                if (t && !out.includes(t)) out.push(t);
            }
            return out;
        }
    """)


def cerrar_sesion(page) -> None:
    for path in (
        "/CitasWebPF/faces/xhtml/seguridad/Logout.xhtml",
        "/CitasWebPF/faces/xhtml/centroSalud/salir.xhtml",
        "/CitasWebPF/faces/xhtml/seguridad/cerrarSesion.xhtml",
    ):
        try:
            page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            continue


def guardar_captcha(page) -> Path:
    # Capture the image the page shows; fetching /captcha again would replace the session's code.
    img_id = "formInicioSesion:captcha"
    if not esperar(lambda: page.evaluate(
            "(id) => { const i = document.getElementById(id); return !!i && i.complete && i.naturalWidth > 0; }",
            img_id), 20):
        raise RuntimeError("No cargó la imagen del código de seguridad")
    raw = DATA_DIR / "captcha.png"
    page.locator(f'[id="{img_id}"]').screenshot(path=str(raw))
    img = Image.open(raw)
    img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    grande = DATA_DIR / "captcha_grande.png"
    img.save(grande)
    return grande


def login(page, cedula: str, clave: str) -> str | None:
    """Returns the menu URL on success, None if the user did not answer the CAPTCHA."""
    for intento in range(1, MAX_LOGIN + 1):
        log(f"Iniciando sesión (intento {intento}/{MAX_LOGIN})")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector('[id="formInicioSesion:usuario"]', state="attached", timeout=20000)

        captcha = pedir_captcha(guardar_captcha(page))
        if not captcha:
            log("No se ingresó el código de seguridad.")
            return None

        page.evaluate("""
            (v) => {
                const f = (id) => document.getElementById(id);
                f('formInicioSesion:tipIdentificacion_input').value = '0';
                f('formInicioSesion:usuario').value = v.usuario;
                f('formInicioSesion:clave').value = v.clave;
                f('formInicioSesion:captchaDigitado').value = v.captcha;
                f('formInicioSesion:ejecutarPaso1').click();
            }
        """, {"usuario": cedula, "clave": clave, "captcha": captcha})

        if esperar(lambda: en_menu(page), 15):
            log("Sesión iniciada.")
            return page.url

        errores = " | ".join(leer_errores(page))
        log(f"Login rechazado: {errores or 'sin mensaje'}")
        texto = errores.lower()
        if "ya existe un usuario" in texto:
            cerrar_sesion(page)
        elif any(w in texto for w in CAPTCHA_WORDS):
            continue
        elif any(w in texto for w in CREDENTIAL_WORDS):
            # Stop immediately: retrying a wrong password can lock the account.
            raise CredencialesInvalidas(errores)
    return None


def abrir_menu(page, menu_url: str) -> bool:
    try:
        page.goto(menu_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return False
    return esperar(lambda: en_menu(page), 10)


def _elegir(page, select_id: str, valor: str) -> bool:
    indice = {"v": -1}

    def _opcion() -> bool:
        indice["v"] = page.evaluate(
            "([id, v]) => { const s = document.getElementById(id);"
            " return s ? Array.from(s.options).findIndex(o => o.value === v) : -1; }",
            [select_id, valor])
        return indice["v"] >= 0

    if not esperar(_opcion, 15):
        return False
    # Real clicks on the PrimeFaces widget: setting the hidden <select> does not fire its AJAX.
    base = select_id.removesuffix("_input")
    page.locator(f'[id="{base}"]').click()
    page.locator(f'[id="{base}_items"] li').nth(indice["v"]).click()
    etiqueta = page.locator(f'[id="{base}_label"]').inner_text().strip()
    log(f"  {base.split(':')[-1]}: {etiqueta}")
    return True


def leer_cupos(page, servicio: str, especialidad: str) -> list[dict]:
    if not esperar(lambda: page.evaluate("typeof PrimeFaces !== 'undefined'"), 20):
        raise RuntimeError("La página del menú no terminó de cargar")
    page.evaluate("PrimeFaces.ab({s: 'formSIAC:btnMenuAdd', f: 'formSIAC'});")
    if not _elegir(page, "formSIAC:menuServicios_input", servicio):
        raise RuntimeError("No apareció el selector de servicio")
    if not _elegir(page, "formSIAC:menuEspecialidades_input", especialidad):
        raise RuntimeError("No apareció el selector de especialidad")
    time.sleep(4)
    return page.evaluate("""
        () => {
            const t = document.getElementById('formSIAC:cuposDisponibles');
            if (!t) return [];
            const out = [];
            for (const row of t.querySelectorAll('tbody tr')) {
                const c = row.querySelectorAll('td');
                if (c.length >= 6 && c[5].querySelector('a, button')) {
                    out.push({
                        fecha: c[0].textContent.trim(),
                        hora: c[1].textContent.trim(),
                        consultorio: c[3].textContent.trim(),
                        funcionario: c[4].textContent.trim(),
                    });
                }
            }
            return out;
        }
    """)


def reservar(page, cupo: dict) -> str:
    """Returns 'ok', 'tomado' or 'incierto'."""
    clic = page.evaluate("""
        (cupo) => {
            const t = document.getElementById('formSIAC:cuposDisponibles');
            if (!t) return false;
            for (const row of t.querySelectorAll('tbody tr')) {
                const c = row.querySelectorAll('td');
                if (c.length >= 6 && c[0].textContent.trim() === cupo.fecha
                        && c[1].textContent.trim() === cupo.hora) {
                    const btn = c[5].querySelector('a, button');
                    if (btn) { btn.click(); return true; }
                }
            }
            return false;
        }
    """, cupo)
    if not clic:
        return "tomado"

    confirmar = """
        () => {
            for (const b of document.querySelectorAll('button, a')) {
                if (b.textContent.trim() === 'Confirmar') { b.click(); return true; }
            }
            return false;
        }
    """
    if not esperar(lambda: page.evaluate(confirmar), 10):
        return "tomado"

    resultado = {"v": "incierto"}

    def _respuesta() -> bool:
        cuerpo = page.inner_text("body").lower()
        if any(w in cuerpo for w in SUCCESS_WORDS):
            resultado["v"] = "ok"
            return True
        if any(w in cuerpo for w in TAKEN_WORDS):
            resultado["v"] = "tomado"
            return True
        return False

    esperar(_respuesta, 15)
    return resultado["v"]


# ── Main loop ─────────────────────────────────────────────────

def vigilar(browser, cedula: str, clave: str, args) -> None:
    excluir = {f.strip() for f in args.excluir.split(",") if f.strip()}
    while True:
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="es-CR",
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        try:
            menu_url = None
            while True:
                if menu_url is None or not abrir_menu(page, menu_url):
                    menu_url = login(page, cedula, clave)
                    if menu_url is None:
                        time.sleep(args.intervalo)
                        continue

                cupos = [c for c in leer_cupos(page, args.servicio, args.especialidad)
                         if c["fecha"] not in excluir]
                if not cupos:
                    log(f"Sin cupos. Reviso de nuevo en {args.intervalo} s.")
                    time.sleep(args.intervalo)
                    continue

                cupo = cupos[0]
                detalle = f"{cupo['fecha']} {cupo['hora']} - {cupo['consultorio']} - {cupo['funcionario']}"
                log(f"{len(cupos)} cupo(s). Reservando: {detalle}")
                resultado = reservar(page, cupo)

                if resultado == "ok":
                    page.screenshot(path=str(DATA_DIR / "cita_reservada.png"), full_page=True)
                    log(f"CITA RESERVADA: {detalle}")
                    notificar("Cita EDUS reservada", detalle)
                    alerta("Cita EDUS reservada", detalle)
                    return
                if resultado == "incierto":
                    # Stop instead of trying another slot: it may already be booked.
                    page.screenshot(path=str(DATA_DIR / "estado_incierto.png"), full_page=True)
                    log("Estado incierto tras confirmar. Me detengo para no reservar dos veces.")
                    notificar("EDUS: revisa tus citas", "No pude confirmar si la cita quedó reservada.")
                    alerta("EDUS: revisa tus citas",
                           f"Intenté reservar {detalle} pero no pude confirmar el resultado. "
                           "Revisa en EDUS si quedó agendada.")
                    return
                log("Ese cupo lo tomó otra persona. Reviso de nuevo.")
                time.sleep(3)
        except CredencialesInvalidas as e:
            log(f"Credenciales rechazadas: {e}")
            notificar("EDUS: clave incorrecta", "Revisa tu cédula y clave con --setup.")
            alerta("EDUS: clave incorrecta",
                   "EDUS rechazó tus datos. Me detuve para no bloquear la cuenta. "
                   "Ejecuta: python edus_local.py --setup")
            return
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"Error: {e}. Reintento en {args.intervalo} s.")
            time.sleep(args.intervalo)
        finally:
            try:
                cerrar_sesion(page)
                context.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Busca y reserva citas EDUS desde tu Mac.")
    parser.add_argument("--setup", action="store_true", help="guardar cédula y clave en el Llavero")
    parser.add_argument("--intervalo", type=int, default=120, help="segundos entre revisiones (default 120)")
    parser.add_argument("--servicio", default="1", help="1 = Medicina")
    parser.add_argument("--especialidad", default="1033", help="1033 = Medicina General")
    parser.add_argument("--excluir", default="", help="fechas a excluir: DD/MM/AAAA,DD/MM/AAAA")
    parser.add_argument("--ver", action="store_true", help="mostrar el navegador en pantalla")
    args = parser.parse_args()

    if args.setup:
        setup()
        return

    cedula, clave = cargar_credenciales()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)

    # Keep the Mac awake (idle sleep) while this process runs.
    subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])

    log("Buscando cita. Deja esta ventana abierta; Ctrl+C para detener.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.ver)
        try:
            vigilar(browser, cedula, clave, args)
        except KeyboardInterrupt:
            log("Detenido.")
        finally:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
