"""
enviar_alertas.py

Compara el checkpoint de ANTES de esta corrida del scraper
(checkpoint_remaju_anterior.jsonl, una foto tomada al inicio del
workflow, antes de que remaju_scraper_v3.py lo sobreescriba) contra
el checkpoint de DESPUÉS (checkpoint_remaju.jsonl, ya actualizado).

Cualquier "Código de Remate" que aparezca en el de después pero NO
en el de antes es un remate genuinamente nuevo desde la última
corrida. Para cada suscriptor en alertas_config.json, si el remate
nuevo cumple sus criterios, le manda un WhatsApp vía Twilio.

Modo de prueba (dry-run): si no hay credenciales de Twilio en el
entorno (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM),
el script NO falla -- simplemente imprime en consola qué mensaje le
habría mandado a quién, para poder revisar la lógica sin gastar
saldo ni tener las credenciales todavía configuradas.
"""

import json
import os
import sys

import requests

RUTA_CHECKPOINT_ANTERIOR = "checkpoint_remaju_anterior.jsonl"
RUTA_CHECKPOINT_ACTUAL = "checkpoint_remaju.jsonl"
RUTA_CONFIG = "alertas_config.json"

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")  # ej: "whatsapp:+14155238886"

DRY_RUN = not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM)


def cargar_jsonl(ruta):
    registros = {}
    if not os.path.exists(ruta):
        return registros
    with open(ruta, "r", encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                reg = json.loads(linea)
            except json.JSONDecodeError:
                continue
            codigo = reg.get("Código de Remate")
            if codigo:
                registros[codigo] = reg
    return registros


def precio_en_pen(remate):
    """Convierte Precio Base a soles usando el Tipo Cambio real de ESE remate."""
    precio = remate.get("Precio Base")
    moneda = remate.get("Moneda Precio Base")
    if precio is None:
        return None
    try:
        precio = float(precio)
    except (TypeError, ValueError):
        return None
    if moneda == "PEN":
        return precio
    if moneda == "USD":
        tc = remate.get("Tipo Cambio")
        try:
            tc = float(tc)
        except (TypeError, ValueError):
            tc = 3.8  # respaldo razonable si el remate no trae su propio tipo de cambio
        return precio * tc
    return precio  # moneda desconocida: se deja tal cual, mejor que perder el dato


def descuento_pct(remate):
    tasacion = remate.get("Tasación")
    precio = remate.get("Precio Base")
    try:
        tasacion = float(tasacion)
        precio = float(precio)
        if tasacion <= 0:
            return None
        return round((1 - precio / tasacion) * 100)
    except (TypeError, ValueError):
        return None


def cumple_criterios(remate, criterios):
    if remate.get("Estado REMAJU") != "Activo":
        return False

    dept = criterios.get("departamento", "").strip().upper()
    if dept and (remate.get("Departamento Inmueble") or "").strip().upper() != dept:
        return False

    tipo = criterios.get("tipo_inmueble", "").strip().upper()
    if tipo and (remate.get("Tipo Inmueble 1") or "").strip().upper() != tipo:
        return False

    precio_max = criterios.get("precio_max_pen")
    if precio_max:
        p = precio_en_pen(remate)
        if p is None or p > precio_max:
            return False

    desc_min = criterios.get("descuento_min_pct")
    if desc_min:
        d = descuento_pct(remate)
        if d is None or d < desc_min:
            return False

    return True


def armar_mensaje(nombre, remates):
    lineas = [f"🔔 {nombre}, {len(remates)} remate(s) nuevo(s) que coinciden con tus criterios en Remaju Ledger:\n"]
    for r in remates[:5]:  # tope de 5 por mensaje para no hacerlo gigante
        codigo = r.get("Código de Remate", "")
        tipo = r.get("Tipo Inmueble 1", "Inmueble")
        distrito = r.get("Distrito Inmueble") or r.get("Distrito Judicial") or ""
        precio = r.get("Precio Base")
        moneda = r.get("Moneda Precio Base", "")
        desc = descuento_pct(r)
        fecha = r.get("Fecha del remate", "")
        linea = f"• {tipo} en {distrito} — {moneda} {precio:,.0f}" if isinstance(precio, (int, float)) else f"• {tipo} en {distrito}"
        if desc is not None:
            linea += f" (-{desc}% vs. tasación)"
        linea += f" — remate {fecha} — {codigo}"
        lineas.append(linea)
    if len(remates) > 5:
        lineas.append(f"... y {len(remates) - 5} más.")
    return "\n".join(lineas)


def enviar_whatsapp(telefono, mensaje):
    if DRY_RUN:
        print(f"\n[DRY-RUN -- no se envió de verdad, faltan credenciales de Twilio]")
        print(f"Para: whatsapp:+{telefono}")
        print(f"Mensaje:\n{mensaje}\n")
        return True

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        resp = requests.post(
            url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={
                "From": TWILIO_WHATSAPP_FROM,
                "To": f"whatsapp:+{telefono}",
                "Body": mensaje,
            },
            timeout=30,
        )
        if resp.status_code >= 300:
            print(f"⚠️ Twilio devolvió error {resp.status_code} para +{telefono}: {resp.text}")
            return False
        print(f"✅ WhatsApp enviado a +{telefono}")
        return True
    except Exception as e:
        print(f"⚠️ Falló el envío a +{telefono}: {e}")
        return False


def main():
    if DRY_RUN:
        print("⚠️ Modo DRY-RUN: no hay credenciales de Twilio en el entorno.")
        print("   Se va a mostrar en consola qué se habría enviado, sin mandar nada real.\n")

    if not os.path.exists(RUTA_CONFIG):
        print(f"❌ No se encontró {RUTA_CONFIG}. Nada que hacer.")
        sys.exit(0)

    with open(RUTA_CONFIG, "r", encoding="utf-8") as f:
        config = json.load(f)
    suscriptores = config.get("suscriptores", [])

    if not suscriptores:
        print("ℹ️ No hay suscriptores configurados en alertas_config.json.")
        sys.exit(0)

    anteriores = cargar_jsonl(RUTA_CHECKPOINT_ANTERIOR)
    actuales = cargar_jsonl(RUTA_CHECKPOINT_ACTUAL)

    codigos_nuevos = set(actuales.keys()) - set(anteriores.keys())
    remates_nuevos = [actuales[c] for c in codigos_nuevos]

    print(f"📋 Remates en checkpoint anterior: {len(anteriores)}")
    print(f"📋 Remates en checkpoint actual:   {len(actuales)}")
    print(f"🆕 Remates nuevos esta corrida:    {len(remates_nuevos)}")

    if not remates_nuevos:
        print("\nNada nuevo desde la última corrida -- no se manda ninguna alerta.")
        return

    total_alertas_enviadas = 0
    for sus in suscriptores:
        nombre = sus.get("nombre", "suscriptor")
        telefono = sus.get("telefono", "").strip()
        if not telefono:
            print(f"⚠️ Suscriptor '{nombre}' no tiene teléfono configurado, se salta.")
            continue

        coincidencias = [r for r in remates_nuevos if cumple_criterios(r, sus)]
        if not coincidencias:
            print(f"— {nombre}: 0 remates nuevos coinciden con sus criterios.")
            continue

        mensaje = armar_mensaje(nombre, coincidencias)
        if enviar_whatsapp(telefono, mensaje):
            total_alertas_enviadas += 1

    print(f"\n✅ Proceso de alertas terminado. {total_alertas_enviadas} mensaje(s) procesado(s).")


if __name__ == "__main__":
    main()
