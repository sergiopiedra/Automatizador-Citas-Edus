# Project notes for Copilot

- This repo is a single macOS-only script, [edus_local.py](../edus_local.py), that watches EDUS Citas Web (CCSS, Costa Rica) for appointment slots, books the first one, and alarms the user. Dependencies: `playwright`, `pillow`, `keyring` in a local `.venv`.
- For install / configure / run / troubleshoot requests, follow the workflow in [the EDUS Citas agent](agents/edus-citas.agent.md) (or suggest the `/configurar-edus` prompt).
- Talk to the user in Spanish. Code, comments and commits in English; user-facing strings in the script stay in Spanish.
- Never ask for or handle the EDUS password in chat or command arguments; it is typed by the user in the terminal (`--setup`) and stored in the macOS Keychain (service `edus-citas`).
- Booking flow facts (verified against the live site): CAPTCHA image is `#formInicioSesion:captcha` (screenshot it, do not re-fetch `/captcha`); PrimeFaces selects need real clicks; the confirm dialog is `formSIAC:dlgCitasAsignar` with a "Confirmar Cita" button; success = the dialog's N° de Cita appears in `formSIAC:tablaCitas`; EDUS refuses with "ya posee citas en los próximos 3 días".
