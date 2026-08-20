"""
subir_a_sheets.py
==================
Sube el contenido de Bloomberg_Remates_ULTIMO.xlsx a una Google Sheet,
para que los clientes puedan verlo sin entrar a GitHub.

Usa una CUENTA DE SERVICIO de Google (no tu cuenta personal, no pide
login interactivo) -es la forma correcta de autenticar algo que corre
solo, sin humano presente, como este workflow.

Variables de entorno requeridas (se configuran como Secrets en GitHub):
  GOOGLE_SERVICE_ACCOUNT_JSON  -> contenido completo del archivo JSON
                                   de la cuenta de servicio
  GOOGLE_SHEET_ID               -> el ID de la hoja de cálculo destino
                                   (el string largo en la URL, entre
                                   /d/ y /edit)
"""

import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import os
import json
from datetime import datetime, timezone

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def conectar():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError(
            "Falta la variable de entorno GOOGLE_SERVICE_ACCOUNT_JSON "
            "(el contenido del JSON de la cuenta de servicio)."
        )
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def subir_excel_a_sheets(ruta_excel, id_hoja_calculo, nombre_pestana="Remates"):
    if not os.path.exists(ruta_excel):
        print(f"⚠️ No existe {ruta_excel} -nada que subir esta corrida-.")
        return

    cliente = conectar()
    hoja = cliente.open_by_key(id_hoja_calculo)

    df = pd.read_excel(ruta_excel)

    # Se convierte todo a texto para evitar errores de serialización con
    # NaN, fechas y tipos mixtos al mandarlo a la API de Sheets. Es una
    # simplificación consciente para esta primera versión: los números
    # llegan como texto (no se pueden sumar directamente en la hoja).
    # Si más adelante hace falta que Sheets trate columnas específicas
    # como números reales (para gráficos, sumas, etc.), se ajusta acá.
    df = df.fillna("")
    df = df.astype(str)

    try:
        ws = hoja.worksheet(nombre_pestana)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = hoja.add_worksheet(
            title=nombre_pestana, rows=len(df) + 10, cols=len(df.columns) + 5
        )

    valores = [df.columns.tolist()] + df.values.tolist()
    ws.update(valores, value_input_option="USER_ENTERED")

    # Pestaña aparte con la hora de la última actualización, para que
    # el cliente sepa qué tan fresca es la data que está viendo.
    try:
        ws_meta = hoja.worksheet("_meta")
    except gspread.exceptions.WorksheetNotFound:
        ws_meta = hoja.add_worksheet(title="_meta", rows=10, cols=5)

    ahora_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ws_meta.update(
        [
            ["Última actualización", ahora_utc],
            ["Total de remates", str(len(df))],
        ]
    )

    print(f"✅ Subidos {len(df)} remates a Google Sheets (pestaña '{nombre_pestana}').")
    print(f"   Última actualización marcada: {ahora_utc}")


if __name__ == "__main__":
    ruta = os.environ.get("RUTA_EXCEL", "Bloomberg_Remates_ULTIMO.xlsx")
    id_hoja = os.environ.get("GOOGLE_SHEET_ID")
    if not id_hoja:
        raise RuntimeError("Falta la variable de entorno GOOGLE_SHEET_ID.")
    subir_excel_a_sheets(ruta, id_hoja)
