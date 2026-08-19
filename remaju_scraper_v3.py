import asyncio
from playwright.async_api import async_playwright
import pandas as pd
import random
import re
import unicodedata
from datetime import datetime
import pdfplumber
import pytesseract
from pdf2image import convert_from_path
import requests
import tempfile
import os    
import json
import fitz  # PyMuPDF
import io
from PIL import Image

# Ruta a tesseract.exe: SOLO hace falta en Windows si no está en el PATH
# del sistema. En Linux (GitHub Actions, cualquier servidor) NO se toca
# nada -- pytesseract encuentra solo el 'tesseract' que se instala con
# `apt-get install tesseract-ocr` (así está configurado el workflow).
# Si en tu Windows necesitas apuntarlo a mano, define la variable de
# entorno TESSERACT_CMD_PATH antes de correr el script.
_ruta_tesseract_manual = os.environ.get("TESSERACT_CMD_PATH")
if _ruta_tesseract_manual:
    pytesseract.pytesseract.tesseract_cmd = _ruta_tesseract_manual

# No usado actualmente (quedó de una versión anterior con pdf2image),
# se deja documentado por si se reactiva ese camino de OCR.
POPPLER_PATH = os.environ.get("POPPLER_PATH", "")
 
            
# ============================================================
# AYUDAS DE LIMPIEZA Y EXTRACCIÓN 
# ============================================================

def limpiar_texto(texto): 
    if texto is None:
        return ""
    texto = unicodedata.normalize("NFKC", str(texto))
    texto = texto.replace("\xa0", " ")
    lineas = []
    for linea in texto.splitlines():      
        linea = re.sub(r"\s+", " ", linea).strip()
        if linea:
            lineas.append(linea)
    return " | ".join(lineas)







def leer_pdf_inteligente(ruta_pdf):
    texto_acumulado = []

    # 1. Extracción con Ordenamiento Espacial por Bounding Boxes (y0, x0)
    try:
        doc = fitz.open(ruta_pdf)
        for pagina in doc:
            # Obtener bloques con coordenadas: (x0, y0, x1, y1, texto, block_no, block_type)
            bloques = pagina.get_text("blocks")
            # Filtrar solo bloques de texto (block_type == 0) y ordenar de arriba a abajo, izquierda a derecha
            bloques_texto = [b for b in bloques if b[6] == 0]
            bloques_ordenados = sorted(bloques_texto, key=lambda b: (round(b[1], -1), b[0]))
            
            texto_pagina = "\n".join([b[4].strip() for b in bloques_ordenados if b[4].strip()])
            if texto_pagina:
                texto_acumulado.append(texto_pagina)
    except Exception as e:
        print(f"Error en lectura espacial PDF: {e}")

    texto_limpio = limpiar_texto("\n".join(texto_acumulado))
    texto_upper = texto_limpio.upper()

    tiene_dte = any(k in texto_upper for k in ["DEMANDANTE", "EJECUTANTE"])
    tiene_ddo = any(k in texto_upper for k in ["DEMANDADO", "EJECUTADO"])

    # 2. Fallback OCR a 300 DPI con PSM 6 (Aislamiento de bloques) para escaneados
    if len(texto_limpio) < 300 or not (tiene_dte and tiene_ddo):
        try:
            doc = fitz.open(ruta_pdf)
            texto_ocr_acumulado = []
            for pagina in doc:
                pix = pagina.get_pixmap(dpi=300)
                img = Image.open(io.BytesIO(pix.tobytes()))
                
                # Configuración PSM 6: Asume un único bloque de texto uniforme (evita saltos por sellos)
                config_ocr = r'--psm 6 -l spa'
                raw_ocr = pytesseract.image_to_string(img, config=config_ocr)
                if raw_ocr.strip():
                    texto_ocr_acumulado.append(raw_ocr)
            
            texto_limpio = limpiar_texto("\n".join(texto_ocr_acumulado))
        except Exception as e:
            print(f"OCR espacial falló: {e}")

    return texto_limpio                       








def normalizar_busqueda(valor):
    if valor is None:
        return ""
    valor = unicodedata.normalize("NFKD", str(valor))
    valor = "".join(c for c in valor if not unicodedata.combining(c))
    valor = valor.replace("\xa0", " ")
    valor = re.sub(r"\s+", " ", valor).strip().lower()
    return valor


def segmentar_texto(texto):
    if not texto:
        return []
    return [s.strip() for s in re.split(r"\s*\|\s*|\n+", str(texto)) if s.strip()]


def extraer_campo(texto, nombres):
    try:
        if texto is None:
            return None

        if isinstance(nombres, str):
            nombres = [nombres]

        texto = str(texto)
        if not texto.strip():
            return None

        segmentos = segmentar_texto(texto)
        if not segmentos:
            return None

        segmentos_norm = [normalizar_busqueda(s) for s in segmentos]

        for nombre in nombres:
            nombre_norm = normalizar_busqueda(nombre)
            if not nombre_norm:
                continue

            for i, seg in enumerate(segmentos):
                seg_norm = segmentos_norm[i]

                # etiqueta sola y valor en el siguiente segmento
                if seg_norm == nombre_norm:
                    if i + 1 < len(segmentos):
                        valor = segmentos[i + 1].strip()
                        if valor and normalizar_busqueda(valor) != nombre_norm:
                            return valor
                    continue

                # etiqueta y valor en el mismo segmento
                m = re.match(r"^(.+?)\s*[:\-—–|]\s*(.+)$", seg)
                if m and normalizar_busqueda(m.group(1)) == nombre_norm:
                    valor = m.group(2).strip()
                    if valor:
                        return valor

                # etiqueta y valor separados por múltiples espacios
                m = re.match(r"^(.+?)\s{2,}(.+)$", seg)
                if m and normalizar_busqueda(m.group(1)) == nombre_norm:
                    valor = m.group(2).strip()
                    if valor:
                        return valor

        return None

    except Exception:
        return None


   
                               
def extraer_partes_procesales_resolucion(texto):
    """
    Extracción directa basada en intervalos entre etiquetas adyacentes.
    Ignora por completo la estructura del documento, sellos SINOE y formatos de resolución.
    """
    resultado = {"Demandante": None, "Demandado": None}
    if not texto or len(texto.strip()) < 10:
        return resultado

    # 1. Eliminar líneas de sellos de notificación (SINOE / Firma Digital)
    lineas = texto.splitlines()
    lineas_limpias = [
        l for l in lineas 
        if not re.search(r'(?:SINOE|FIRMA\s+DIGITAL|CORTE\s+SUPERIOR|NOTIFICACIONES|Validez\s+desconocida|Razon:\s*RESOLUCION)', l, re.IGNORECASE)
    ]
    texto_limpio = "\n".join(lineas_limpias)

    # 2. Definir los patrones de TODAS las posibles etiquetas de una carátula
    patrones_etiquetas = {
        'EXPEDIENTE': r'(?:EXPEDIENTE|EXP\.|EXP|DIENTE)\s*[:：]?',
        'MATERIA': r'(?:MATERIA|ERIA)\s*[:：]?',
        'JUEZ': r'JUEZ\s*[:：]?',
        'ESPECIALISTA': r'(?:ESPECIALISTA|CIALISTA)(\s+LEGAL)?\s*[:：]?',
        'CURADOR': r'CURADOR(\s+PROCESAL)?\s*[:：]?',
        'PERITO': r'PERITO\s*[:：]?',
        'MARTILLERO': r'MARTILLERO\s*[:：]?',
        'TERCERO': r'TERCERO\s*[:：]?',
        'SUCESOR': r'SUCESOR(\s+PROCESAL)?\s*[:：]?',
        'DEMANDANTE': r'(?:DEMANDANTE|DEMANDANTES|EJECUTANTE|EJECUTANTES|DEMANDANTE\(S\))\s*[:：]?',
        'DEMANDADO': r'(?:DEMANDADO|DEMANDADOS|EJECUTADO|EJECUTADOS|DEMANDADO\(S\))\s*[:：]?',
        'FIN_CARATULA': r'(?:RESOLUCI[ÓO]N|RESOLUCION|AUTO|AUTOS\s+Y\s+VISTOS|DADO\s+CUENTA|ATENDIENDO)'
    }

    # 3. Encontrar todas las apariciones de etiquetas y registrar sus posiciones
    marcamedida = []
    for tipo, patron in patrones_etiquetas.items():
        for m in re.finditer(patron, texto_limpio, re.IGNORECASE):
            marcamedida.append((m.start(), m.end(), tipo))

    if not marcamedida:
        return resultado

    marcamedida.sort(key=lambda x: x[0])

    # 4. Función de extracción entre la etiqueta encontrada y la INMEDIATAMENTE SIGUIENTE
    def capturar_contenido_etiqueta(tipo_objetivo):
        bloques = []
        for i in range(len(marcamedida)):
            pos_ini_etiq, pos_fin_etiq, tipo = marcamedida[i]
            
            if tipo == tipo_objetivo:
                if i + 1 < len(marcamedida):
                    pos_limite = marcamedida[i + 1][0]
                else:
                    pos_limite = pos_fin_etiq + 300

                chunk = texto_limpio[pos_fin_etiq:pos_limite]
                
                lineas_chunk = [s.strip(" :;,-|.") for s in chunk.splitlines() if s.strip(" :;,-|.")]
                texto_final = ", ".join(lineas_chunk)
                texto_final = re.sub(r'\s+', ' ', texto_final).strip(" :;,-|.")
                
                if len(texto_final) > 1:
                    bloques.append(texto_final)

        return " / ".join(bloques) if bloques else None

    resultado["Demandante"] = capturar_contenido_etiqueta('DEMANDANTE')
    resultado["Demandado"] = capturar_contenido_etiqueta('DEMANDADO')

    return resultado                                                              

        





def limpiar_valor_parte_ocr(valor):

    if not valor:
        return None

    valor = unicodedata.normalize("NFKC", valor)

    valor = valor.replace("\xa0", " ")

    # Separadores PDF/OCR
    partes = [
        p.strip()
        for p in valor.split("|")
        if p.strip()
    ]

    texto = " ".join(partes)


    # Eliminar basura judicial frecuente
    basura = [
        r"JUDICIAL.*",
        r"D\.?JUDICIAL.*",
        r"SECRETARIO.*",
        r"ESPECIALISTA.*",
        r"FIRMA DIGITAL.*",
        r"FECHA.*",
        r"EXPEDIENTE.*",
        r"MATERIA.*",
        r"RESOLUCIÓN.*",
        r"RESOLUCION.*",
        r"JUZGADO.*",
        r"ÓRGANO.*",
        r"ORGANO.*"
    ]


    for patron in basura:
        texto = re.split(
            patron,
            texto,
            flags=re.I
        )[0]


    texto = re.sub(
        r"\s+",
        " ",
        texto
    ).strip()


    # eliminar duplicados separados
    bloques = [
        x.strip()
        for x in texto.split("|")
        if x.strip()
    ]


    vistos = []
    resultado = []

    for b in bloques:

        normal = normalizar_busqueda(b)

        if normal not in vistos:
            vistos.append(normal)
            resultado.append(b)


    texto = " | ".join(resultado)


    # Limpieza final
    texto = texto.strip(" :-|,.")


    return texto if texto else None                                





def extraer_partes_procesales_respaldo(texto):
    """
    Respaldo que usa la misma lógica V6 de segmentación por etiquetas.
    """
    return extraer_partes_procesales_resolucion(texto)    


# ============================================================
# EXTRACCIÓN DESDE "AVISO DE REMATE" (FUENTE PRIMARIA)
# ============================================================
# El Aviso de Remate lo genera el propio sistema REM@JU con una plantilla
# fija idéntica en todos los remates:
#   "...En los seguidos por [DEMANDANTE], contra [DEMANDADO],
#    sobre proceso de [MATERIA], en el Expediente Judicial N° [EXP]..."
# A diferencia de "resolucion.pdf" (redactado libremente por cada juzgado),
# este es un documento estructurado por diseño. Debe ser SIEMPRE el primer
# intento de extracción de partes procesales.

def limpiar_lista_partes(bruto):
    """
    Convierte un bloque de texto con una o varias partes separadas por
    coma en una lista limpia, descartando huecos vacíos (campos que el
    propio REMAJU dejó sin llenar, ej: "..., , contra...").
    """
    if not bruto:
        return None
    partes = [p.strip(" .-") for p in bruto.split(",")]
    partes = [p for p in partes if p and len(p) > 1]
    if not partes:
        return None
    return " / ".join(partes)


def extraer_partes_desde_aviso(texto):
    """
    Extractor PRIMARIO. Usa la plantilla fija del Aviso de Remate.
    Si el Demandante viene vacío en la fuente (ocurre con frecuencia:
    el juzgado no cargó el dato), devuelve None para ese campo en vez
    de capturar basura — nunca hay que inventar un dato que no existe.
    """
    resultado = {"Demandante": None, "Demandado": None, "Materia_Aviso": None}
    if not texto:
        return resultado

    # IMPORTANTE: limpiar_texto() une líneas del PDF con " | " (pensado
    # para fichas tipo campo-valor). El Aviso es una sola oración
    # continua ("En los seguidos por X, contra Y, sobre proceso de Z...")
    # así que un salto de línea a mitad de frase deja un "|" literal
    # incrustado (ej. "sobre proceso | de EJECUCION...") que rompe el
    # ancla del regex. Aquí se trata como lo que es: un espacio.
    t = texto.replace("|", " ")
    t = re.sub(r"\s+", " ", t).strip()

    m = re.search(
        r"seguidos?\s+por\s+(.*?)\s*,?\s*(?:en\s+)?contra\s+(?:de\s+)?(.*?)\s*,?\s*sobre\s+proceso\s+de\s+(.*?)\s*,?\s*en\s+el\s+Expediente",
        t, re.IGNORECASE
    )
    if not m:
        return resultado

    resultado["Demandante"] = limpiar_lista_partes(m.group(1))
    resultado["Demandado"] = limpiar_lista_partes(m.group(2))
    resultado["Materia_Aviso"] = m.group(3).strip(" .,-") or None

    return resultado


# ============================================================
# FORMATO "PARTE" PARA CONSULTA AUTOMÁTICA EN EL CEJ
# ============================================================
# El buscador del CEJ (cej.pj.gob.pe) exige el campo "Parte" en un
# formato específico: SOLO apellido paterno + apellido materno para
# personas naturales, o la razón social completa para personas
# jurídicas (bancos, cajas, cooperativas, empresas). No acepta el
# nombre completo con nombres de pila.

INDICADORES_PERSONA_JURIDICA = [
    "S.A.C", "SAC", "S.A.A", "SAA", "S.A", " SA ", "S.R.L", "SRL",
    "E.I.R.L", "EIRL", "BANCO", "CAJA ", "COOPERATIVA", "FINANCIERA",
    "EMPRESA", "CORPORACION", "CORPORACIÓN", "INMOBILIARIA",
    "CONSTRUCTORA", "INVERSIONES", "FONDO ", "MUNICIPALIDAD",
    "MINISTERIO", "SUNAT", "AFP ", "ASOCIACION", "ASOCIACIÓN",
    "FUNDACION", "FUNDACIÓN", "SOCIEDAD", "SCOTIABANK", "INTERBANK",
    "BBVA", "BCP", "MIBANCO", "CREDISCOTIA", "COFIDE"
]


def es_persona_juridica(nombre):
    if not nombre:
        return False
    n = f" {nombre.upper()} "
    return any(ind in n for ind in INDICADORES_PERSONA_JURIDICA)


def parte_para_cej(nombre_extraido):
    """
    Traduce el valor de Demandante/Demandado (tal como sale del Aviso)
    al formato exacto que pide el buscador del CEJ.
    Si hay varias partes (demandados múltiples separados por " / "),
    usa la primera, porque el CEJ solo admite un valor por búsqueda.

    ADVERTENCIA: la heurística "últimas 2 palabras = apellidos" asume
    el orden Nombre(s) + Apellido Paterno + Apellido Materno, que es el
    estándar en documentos judiciales peruanos, pero no es infalible
    (nombres compuestos, apellidos con "DE LA", etc.). Tratar esta
    columna como un borrador que conviene revisar en los primeros
    lotes, no como verdad absoluta desde el día uno.
    """
    if not nombre_extraido:
        return None

    primera_parte = nombre_extraido.split(" / ")[0].strip()

    if es_persona_juridica(primera_parte):
        return primera_parte

    palabras = primera_parte.split()
    if len(palabras) < 2:
        return primera_parte

    # Toma las últimas 2 palabras como apellidos, pero si la palabra
    # justo antes es una partícula de apellido compuesto (DE, DEL, LA,
    # LOS, SAN, MC, VON...), la incluye. Ej: "SAIDA LUZ LA ROSA AVILA"
    # -> últimas 2 = "ROSA AVILA", pero hay que arrastrar el "LA" ->
    # "LA ROSA AVILA".
    particulas = {"DE", "DEL", "LA", "LAS", "LOS", "SAN", "SANTA", "MC", "VON", "VAN"}
    indice_inicio = len(palabras) - 2
    while indice_inicio > 0 and palabras[indice_inicio - 1].upper() in particulas:
        indice_inicio -= 1

    apellidos = " ".join(palabras[indice_inicio:])
    # Salvaguarda: no dejar que un nombre corto (2-3 palabras) se
    # devore por completo si la "partícula" en realidad es un nombre
    # de pila (ej. "DE LA CRUZ" tiene 3 apellidos-palabras válidas,
    # pero "ANA MARIA DE LOS SANTOS" no debería perder "MARIA").
    if len(apellidos.split()) >= len(palabras):
        apellidos = " ".join(palabras[-2:])

    return apellidos


def seleccionar_parte_para_busqueda(demandante, demandado):
    """
    Decide qué parte usar para consultar el CEJ cuando solo se necesita
    una. Prioriza el DEMANDADO: es el dueño del bien rematado, el dato
    de mayor valor comercial, y en la práctica el Demandante suele
    faltar en el Aviso con más frecuencia que el Demandado.
    """
    if demandado:
        return parte_para_cej(demandado), "Demandado"
    if demandante:
        return parte_para_cej(demandante), "Demandante"
    return None, None


def extraer_partes_narrativa_generica(texto):
    """
    Respaldo NARRATIVO para resoluciones judiciales que no usan carátula
    tipo "DEMANDADO:" (eso ya lo cubre extraer_partes_procesales_resolucion)
    sino redacción libre dentro del cuerpo de la resolución, ej.:
    "...SEGUIDO POR KENYI DENIS GÓMEZ CASTILLO, EN CONTRA DE COUNTRY CLUB
    LAS PRADERAS DEL SUR S.A.C. 2) DATOS DEL BIEN..."

    A diferencia del extractor del Aviso (que exige que la frase cierre
    en "sobre proceso de ... Expediente"), aquí esa continuación NO es
    constante -- las resoluciones siguen con numerales (ej. "2)"),
    "DATOS DEL BIEN", un punto, o cualquier otra cosa. Por eso el cierre
    es más permisivo: corta en el primer punto, numeral, "sobre" o
    "DATOS", lo que aparezca primero.
    """
    resultado = {"Demandante": None, "Demandado": None}
    if not texto:
        return resultado

    t = texto.replace("|", " ")
    t = re.sub(r"\s+", " ", t).strip()

    m = re.search(
        r"seguidos?\s+por\s+(.*?)\s*,\s*(?:en\s+)?contra\s+(?:de\s+)?(.*?)"
        r"(?=\s*\.\s|\s*\d\)|\s*\bsobre\b|\s*\bDATOS\b|$)",
        t, re.IGNORECASE
    )
    if not m:
        return resultado

    resultado["Demandante"] = limpiar_lista_partes(m.group(1))
    resultado["Demandado"] = limpiar_lista_partes(m.group(2))
    return resultado





def extraer_numero_remate(texto_tarjeta):
    if not texto_tarjeta:
        return None
    m = re.search(r"Remate\s*N[°ºo.]\s*(\d+)", texto_tarjeta, re.IGNORECASE)
    if m:
        return f"Remate N° {m.group(1).strip()}"
    return None


def extraer_convocatoria(texto):
    if not texto:
        return None
    texto_u = texto.upper()
    convocatorias = [
        "PRIMERA CONVOCATORIA",
        "SEGUNDA CONVOCATORIA",
        "TERCERA CONVOCATORIA",
        "CUARTA CONVOCATORIA",
        "QUINTA CONVOCATORIA",
    ]
    for c in convocatorias:
        if c in texto_u:
            return c
    return None


def extraer_fecha_hora_presentacion(texto_tarjeta):
    """
    Busca la fecha y hora donde aparece:
    Presentación de Ofertas | 04/08/2026 | 11:59 AM
    """
    if not texto_tarjeta:
        return None, None

    segmentos = segmentar_texto(texto_tarjeta)

    for i, seg in enumerate(segmentos):
        if normalizar_busqueda(seg) in [
            "presentacion de ofertas",
            "presentacion ofertas",
            "fecha presentacion ofertas",
        ]:
            fecha = None
            hora = None

            if i + 1 < len(segmentos):
                fecha_raw = segmentos[i + 1].strip()
                fecha = fecha_raw

            if i + 2 < len(segmentos):
                hora_raw = segmentos[i + 2].strip()
                if re.search(r"\d{1,2}:\d{2}", hora_raw):
                    hora = hora_raw

            return fecha, hora

    return None, None


def calcular_dias_restantes(fecha_texto):
    if not fecha_texto:
        return None, "inexistente"

    fecha_texto = unicodedata.normalize("NFKC", str(fecha_texto)).replace("\xa0", " ").strip()
    if not fecha_texto:
        return None, "inexistente"

    fecha_texto = re.sub(r"\s+", " ", fecha_texto)
    fecha_texto = re.sub(r"\bA\.?\s*M\.?\b", "AM", fecha_texto, flags=re.IGNORECASE)
    fecha_texto = re.sub(r"\bP\.?\s*M\.?\b", "PM", fecha_texto, flags=re.IGNORECASE)

    formatos = [
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y %I:%M %p",
        "%d-%m-%Y",
        "%d-%m-%Y %H:%M",
        "%d-%m-%Y %I:%M %p",
    ]

    for fmt in formatos:
        try:
            fecha_obj = datetime.strptime(fecha_texto, fmt)
            dias = (fecha_obj.date() - datetime.now().date()).days
            if dias >= 0:
                return dias, "Próximo" if dias > 0 else "Hoy"
            return dias, "Vencido"
        except ValueError:
            continue

    return None, "error"


def extraer_partida_registral(texto):
    if not texto:
        return None

    texto = unicodedata.normalize("NFKC", str(texto)).replace("\xa0", " ")
    texto = re.sub(r"\s+", " ", texto)

    patrones = [
        r"PARTIDA\s+ELECTR[ÓO]NICA\s*N[°ºo.]?\s*([A-Z0-9\-\/\.]+)",
        r"PARTIDA\s+REGISTRAL\s*N[°ºo.]?\s*([A-Z0-9\-\/\.]+)",
        r"PARTIDA\s*N[°ºo.]?\s*([A-Z0-9\-\/\.]+)",
        r"PARTIDA\s+N[°ºo.]?\s*([A-Z0-9\-\/\.]+)",
    ]

    for patron in patrones:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            valor = m.group(1).strip()
            valor = valor.rstrip(".,;:) ")
            return valor

    return None


def extraer_ubicacion_bien(texto):
    salida = {
        "Distrito Bien": None,
        "Provincia Bien": None,
        "Departamento Bien": None
    }

    if not texto:
        return salida

    texto = unicodedata.normalize("NFKC", str(texto)).replace("\xa0", " ")
    texto = re.sub(r"\s+", " ", texto)

    m_dist = re.search(r"\bDISTRITO\s+(?:DE\s+)?([A-ZÁÉÍÓÚÑ0-9\s\-.()]+?)(?:,|\s+PROVINCIA|\s+DEPARTAMENTO|;|\.|\|)", texto, re.IGNORECASE)
    m_prov = re.search(r"\bPROVINCIA\s+(?:DE\s+)?([A-ZÁÉÍÓÚÑ0-9\s\-.()]+?)(?:,|\s+DEPARTAMENTO|;|\.|\|)", texto, re.IGNORECASE)
    m_dep = re.search(r"\bDEPARTAMENTO\s+(?:DE\s+)?([A-ZÁÉÍÓÚÑ0-9\s\-.()]+?)(?:,|;|\.|\|)", texto, re.IGNORECASE)

    if m_dist:
        salida["Distrito Bien"] = limpiar_texto(m_dist.group(1)).replace(" | ", " ").strip(" ,;.")
    if m_prov:
        salida["Provincia Bien"] = limpiar_texto(m_prov.group(1)).replace(" | ", " ").strip(" ,;.")
    if m_dep:
        salida["Departamento Bien"] = limpiar_texto(m_dep.group(1)).replace(" | ", " ").strip(" ,;.")

    return salida

   

def parsear_monto(texto):
    """
    Devuelve dict con moneda, valor original y valor numérico.
    """
    resultado = {
        "original": None,
        "moneda": None,
        "numerico": None
    }

    if not texto:
        return resultado

    texto = unicodedata.normalize("NFKC", str(texto)).replace("\xa0", " ").strip()
    if not texto:
        return resultado

    resultado["original"] = texto

    texto_u = texto.upper()
    if any(x in texto_u for x in ["US$", "USD", "$"]):
        resultado["moneda"] = "USD"
    elif any(x in texto_u for x in ["S/.", "S/", "PEN"]):
        resultado["moneda"] = "PEN"

    numero = re.sub(r"[^\d,.\-]", "", texto_u)
    # Elimina puntos iniciales provenientes de "S/."
    numero = numero.lstrip(".")
    if not numero:
        return resultado

    # normalización simple
    if "," in numero and "." in numero:
        if numero.rfind(",") > numero.rfind("."):
            numero = numero.replace(".", "").replace(",", ".")
        else:
            numero = numero.replace(",", "")
    elif "," in numero:
        partes = numero.split(",")
        if len(partes) == 2 and len(partes[1]) in (1, 2):
            numero = partes[0].replace(".", "") + "." + partes[1]
        else:
            numero = numero.replace(",", "")
    elif numero.count(".") > 1:
        numero = numero.replace(".", "")

    try:
        resultado["numerico"] = float(numero)
    except Exception:
        resultado["numerico"] = None

    return resultado


def extraer_tipo_cambio(texto):
    """
    Extrae únicamente el valor numérico del tipo de cambio SBS.
    Ejemplo:
    'S/. 3.389 a la fecha 16/07/2026 según la SBS.'
    devuelve:
    3.389
    """
    if not texto:
        return None

    tipo = extraer_campo(texto, "Tipo Cambio")
    if not tipo:
        return None

    m = re.search(r"(\d+[.,]\d+)", tipo)

    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except:
            return None



async def descargar_pdf_primefaces(page, selector_descarga, nombre_archivo, indice=None):
    """
    Descarga PDFs generados por PrimeFaces mediante POST.
    REMAJU no tiene URL directa del PDF.

    selector_descarga puede ser:
      - un string de selector CSS/texto (comportamiento original)
      - un Locator ya resuelto (ej. page.locator(...).nth(i))
    indice: si se pasa un string y hay varios elementos que matchean
    (ej. varios botones "Aviso" en la lista), selecciona el i-ésimo.
    """

    try:

        elemento = (
            page.locator(selector_descarga)
            if isinstance(selector_descarga, str)
            else selector_descarga
        )
        if indice is not None:
            elemento = elemento.nth(indice)

        async with page.expect_download(timeout=15000) as descarga_info:

            await elemento.click()

        descarga = await descarga_info.value

        ruta_temporal = await descarga.path()

        if ruta_temporal:

            with open(ruta_temporal, "rb") as f:
                contenido = f.read()

            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".pdf"
            ) as temp:

                temp.write(contenido)
                archivo_temp = temp.name


            texto = leer_pdf_inteligente(archivo_temp)

            os.remove(archivo_temp)

            return (
                descarga.suggested_filename,
                texto
            )
         

    except Exception as e:

        print(
            f"   ⚠️ Error descargando PDF PrimeFaces: {e}"
        )

        return None, ""






def descargar_y_leer_pdf(url_pdf):

    if not url_pdf:
        return ""

    try:

        r = requests.get(url_pdf, timeout=40)

        if r.status_code != 200:
            return ""

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(r.content)
            nombre = f.name

        texto = leer_pdf_inteligente(nombre)

        os.remove(nombre)

        return limpiar_texto(texto)

    except Exception:

        return ""






def ordenar_campos_fila(fila):
    orden = [
        "Código de Remate",
        "Convocatoria",
        "Tipo Remate",
        "Fecha del remate",
        "Hora del remate", 
        "Días restantes",
        "Estado temporal",
        "Número de Expediente",
        "Distrito Judicial",
        "Órgano Jurisdisccional",
        "Instancia",
        "Juez",
        "Especialista",
        "Materia",
        "Resolución",
        "Fecha Resolución",
        "Descripción",
        "N° inscritos",
        "Tasación",
        "Moneda Tasación",

        "Precio Base",
        "Moneda Precio Base",
        "Tipo Cambio",

        "Incremento entre ofertas",
        "Moneda Incremento",

        "Arancel",
        "Moneda Arancel",

        "Oblaje",
        "Moneda Oblaje",                 
                    
        "SUNARP Estado",
        "SUNARP Cargas",
        "SUNARP Gravámenes",
        "SUNARP Propietario",
        "CEJ Estado Proceso",
        "CEJ Partes Procesales",
        "Score Riesgo Legal",
        "Score Oportunidad",
        "Demandante",
        "Demandado",
        "Parte para CEJ",
        "Origen Parte CEJ",
        "Método extracción partes",
    ]

    # primero los definidos, luego los demás
    salida = {}
    for c in orden:
        salida[c] = fila.get(c)

    for k, v in fila.items():
        if k not in salida:
            salida[k] = v

    return salida


# ============================================================
# CHECKPOINT / RESUME
# ============================================================
# Escribe cada registro exitoso a disco INMEDIATAMENTE (formato JSON
# Lines: un JSON por línea, se puede ir agregando sin reescribir el
# archivo entero). Si el script se corta a media corrida -internet,
# corte de luz, timeout de REMAJU, lo que sea- todo lo procesado hasta
# ese instante queda a salvo, y la siguiente corrida retoma desde ahí
# en vez de reprocesar todo desde el remate N° 1.

RUTA_CHECKPOINT = os.environ.get("REMAJU_CHECKPOINT_PATH", "checkpoint_remaju.jsonl")


def cargar_checkpoint(ruta=RUTA_CHECKPOINT):
    """
    Carga los registros ya procesados en corridas anteriores (o en lo
    que va de esta corrida si se reinicia). Devuelve un diccionario
    {Código de Remate: registro} para lookup instantáneo y una lista
    en el orden original (para no reordenar el Excel final).
    """
    procesados = {}
    orden = []
    if not os.path.exists(ruta):
        return procesados, orden
    with open(ruta, "r", encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                registro = json.loads(linea)
            except json.JSONDecodeError:
                continue  # línea corrupta (ej. corte a media escritura) -> se ignora, no revienta el resume
            codigo = registro.get("Código de Remate")
            if not codigo:
                continue
            if codigo not in procesados:
                orden.append(codigo)
            procesados[codigo] = registro  # si se repite, la versión más reciente gana
    return procesados, orden


def guardar_checkpoint_incremental(registro, ruta=RUTA_CHECKPOINT):
    """Agrega UN registro al checkpoint en disco, sin tocar lo ya escrito."""
    try:
        # Salvaguarda: si la última escritura se cortó a medias (ej. corte
        # de luz) y el archivo no terminó en salto de línea, ese registro
        # roto absorbería el siguiente si simplemente le pegamos texto
        # detrás. Por eso primero se asegura un salto de línea limpio.
        necesita_salto_previo = False
        if os.path.exists(ruta) and os.path.getsize(ruta) > 0:
            with open(ruta, "rb") as f:
                f.seek(-1, os.SEEK_END)
                necesita_salto_previo = f.read(1) != b"\n"

        with open(ruta, "a", encoding="utf-8") as f:
            if necesita_salto_previo:
                f.write("\n")
            f.write(json.dumps(registro, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"   ⚠️ No se pudo escribir el checkpoint para este remate: {e}")


# ============================================================
# CAPTCHA
# ============================================================

async def resolver_captcha_manualmente(page, headless=False):
    """
    Detecta si saltó el bloqueo de reCAPTCHA.
    - En modo visual (local, headless=False): pausa y espera a que un
      humano lo resuelva con el mouse, igual que antes.
    - En modo headless (nube, sin pantalla): NO tiene sentido esperar
      a nadie. Se registra el bloqueo con claridad y se detiene el
      script de forma controlada -lo ya procesado queda a salvo en el
      checkpoint- para que alguien lo revise y relance manualmente si
      hace falta.
    """
    if await page.locator("iframe[src*='recaptcha']").count() > 0 or "validate" in page.url.lower():
        if headless:
            print("\n🚨 [BLOQUEO EN MODO NUBE]: REMAJU mostró un reCAPTCHA y no hay forma de")
            print("   resolverlo sin un humano frente a la pantalla. Deteniendo el proceso de")
            print("   forma segura -todo lo procesado hasta ahora ya está en el checkpoint-.")
            raise CaptchaBloqueoHeadless("reCAPTCHA detectado en modo headless")

        print("\n🚨 [ALERTA ANTIRADAR]: El sistema ha detectado un reCAPTCHA del Poder Judicial.")
        print("   -> Por favor, ve a la ventana del navegador y resuélvelo manualmente con el mouse.")

        for _ in range(3):
            print('\a')
            await asyncio.sleep(0.5)

        print("   Waiting: Tienes 40 segundos para resolver el captcha en pantalla...")
        try:
            await page.wait_for_url("**/mostrarDetalleRemate.xhtml", timeout=40000)
            print("   🔑 Captcha superado. Retomando control automatizado...")
            return True
        except Exception:
            print("   ❌ No se detectó la resolución del captcha en el tiempo límite.")
            return False
    return False


class CaptchaBloqueoHeadless(Exception):
    """Señal controlada: hubo un reCAPTCHA y no hay humano para resolverlo (modo nube)."""
    pass


# ============================================================
# SCRAPER
# ============================================================

async def ejecutar_scraper():
    # En tu laptop puedes forzar ventana visible con:
    #   set REMAJU_HEADLESS=false   (PowerShell: $env:REMAJU_HEADLESS="false")
    # Por defecto corre headless=True, que es lo que necesita GitHub
    # Actions / cualquier servidor sin pantalla.
    modo_headless = os.environ.get("REMAJU_HEADLESS", "true").strip().lower() != "false"
    print(f"🖥️  Modo navegador: {'headless (sin pantalla)' if modo_headless else 'visible'}")

    # --- CHECKPOINT: cargar lo ya procesado en corridas anteriores ---
    registros_previos, orden_previo = cargar_checkpoint()
    if registros_previos:
        print(f"📌 Checkpoint encontrado: {len(registros_previos)} remates ya procesados antes.")
        print("   Se van a OMITIR (no se vuelven a scrapear) y se reusan tal cual en el Excel final.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=modo_headless,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print("1. Conectando a la plataforma REMAJU con huella humanizada...")
        await page.goto("https://remaju.pj.gob.pe/remaju/", timeout=60000)
        await page.wait_for_load_state("networkidle")

        print("2. Entrando a la sección de Remates...")
        await page.locator("a:has-text('Remates')").first.click()
        await page.wait_for_timeout(6000)

        lista_maestra_bloomberg = []
        lista_fallidos = []
        detenido_por_captcha = False

        numero_pagina = 1

        while True:
            print(f"\n========================================================")
            print(f"📖 AUDITANDO MIGRACIÓN DE DATOS - PÁGINA N° {numero_pagina}")
            print(f"========================================================")

            await page.wait_for_selector("button:has-text('Detalle')", timeout=15000)
            botones_detalle = await page.locator("button:has-text('Detalle')").all()
            total_remates_pagina = len(botones_detalle)
            print(f"   Se detectaron {total_remates_pagina} remates en esta página.")

            for i in range(total_remates_pagina):
                # Asegurar re-captura de los botones por si PrimeFaces re-renderizó el DOM
                botones_detalle = await page.locator("button:has-text('Detalle')").all()
                if i >= len(botones_detalle):
                    print(f"   ⚠️ Desfase de elementos en el DOM (índice {i}). Re-evaluando página...")
                    break           
                espera_humana = random.uniform(3.0, 5.5)
                print(f"\n⏳ Pausa preventiva anti-radar de {espera_humana:.2f} segundos...")
                await asyncio.sleep(espera_humana)

                print(f"🚀 Extrayendo Información del Remate {i+1} de {total_remates_pagina} (Pág. {numero_pagina})...")
                try:
                    # --- CAPTURA COMERCIAL DE SUPERFICIE ---
                    tarjeta_titulo = page.locator("text=Remate N°").nth(i)
                    texto_tarjeta = await tarjeta_titulo.locator("xpath=../../..").inner_text()
                    texto_limpio_tarjeta = limpiar_texto(texto_tarjeta)

                    remate_match = re.search(r'(Remate N°\s*\d+)', texto_limpio_tarjeta, re.IGNORECASE)
                    num_remate = remate_match.group(1).strip() if remate_match else f"Remate_{i+1}_P{numero_pagina}"

                    # --- CHECKPOINT: si este remate ya se procesó en una
                    # corrida anterior, se reusa tal cual y NO se vuelve a
                    # pedir el Aviso/ficha/PDF a REMAJU. Esto es lo que
                    # hace que retomar después de un corte sea rápido.
                    if num_remate in registros_previos:
                        print(f"   ⏭️  {num_remate} ya estaba en el checkpoint — se omite, no se vuelve a scrapear.")
                        if num_remate not in [r.get("Código de Remate") for r in lista_maestra_bloomberg]:
                            lista_maestra_bloomberg.append(registros_previos[num_remate])
                        continue

                    convocatoria = extraer_convocatoria(texto_limpio_tarjeta)
                    distrito_localidad = None
                    segmentos_tarjeta = segmentar_texto(texto_limpio_tarjeta)
                    if "REMATE SIMPLE" in [s.upper() for s in segmentos_tarjeta]:
                        try:
                            idx_rs = [s.upper() for s in segmentos_tarjeta].index("REMATE SIMPLE")
                            if idx_rs + 1 < len(segmentos_tarjeta):
                                distrito_localidad = segmentos_tarjeta[idx_rs + 1].strip()
                        except Exception:
                            pass

                    fecha_remate, hora_remate = extraer_fecha_hora_presentacion(texto_limpio_tarjeta)

                    # ==========================================================
                    # PDF AVISO DE REMATE (FUENTE PRIMARIA DE PARTES PROCESALES)
                    # Vive solo en la tarjeta de la LISTA, no en el Detalle.
                    # Hay que descargarlo ANTES de navegar, con el mismo índice i.
                    # ==========================================================
                    texto_aviso = ""
                    try:
                        boton_aviso = page.locator("button:has-text('Aviso')").nth(i)
                        if await boton_aviso.count() > 0:
                            _, texto_aviso = await descargar_pdf_primefaces(
                                page, boton_aviso, "aviso.pdf"
                            )
                            if texto_aviso:
                                print(f"   📋 Aviso de Remate descargado ({len(texto_aviso)} caracteres)")
                            else:
                                print("   ⚠️ Aviso de Remate vacío o no descargable")
                    except Exception as e:
                        print(f"   ⚠️ No se pudo descargar Aviso de Remate: {e}")

                    # --- NAVEGACIÓN PROFUNDA ---
                    print("   -> Accediendo a la ficha técnica interna...")
                    boton_actual = page.locator("button:has-text('Detalle')").nth(i)
                    await boton_actual.scroll_into_view_if_needed()
                    reintentos = 0
                    max_reintentos = 3

                    while True:

                        await boton_actual.click()    

                        await page.wait_for_timeout(2500)

                        if not page.url.endswith("mostrarDetalleRemate.xhtml"):
                            await resolver_captcha_manualmente(page, headless=modo_headless)

                        try:
                            await page.wait_for_url(
                                "**/mostrarDetalleRemate.xhtml",
                                timeout=30000
                            )      

                            await page.wait_for_load_state("networkidle")

                            if "error500.xhtml" in page.url.lower():
                                raise Exception("ERROR500")

                            break

                        except Exception:

                            if "error500.xhtml" in page.url.lower():

                                reintentos += 1

                                print(f"   ⚠ Error 500 detectado. Reintento {reintentos}/{max_reintentos}...")

                                if reintentos >= max_reintentos:
                                    raise Exception("No fue posible abrir la ficha después de varios intentos.")

                                await page.goto(
                                    "https://remaju.pj.gob.pe/remaju/pages/publico/mostrarRemates.xhtml"
                                )

                                await page.wait_for_load_state("networkidle")
                                await page.wait_for_timeout(4000)

                                await page.wait_for_selector(
                                    "button:has-text('Detalle')"
                                )

                                boton_actual = page.locator(
                                    "button:has-text('Detalle')"
                                ).nth(i)

                                await boton_actual.scroll_into_view_if_needed()

                                continue

                            raise

                    # Absorción de datos
                    texto_profundo_pagina = await page.locator("body").inner_text()
                    texto_limpio_profundo = limpiar_texto(texto_profundo_pagina)
                   
                    # =====================================
                    # PDF RESOLUCIÓN PRIMEFACES     
                    # =====================================

                    url_pdf = None
                    texto_pdf = ""
                    nombre_pdf = None


                    try:

                        documentos = page.locator(
                            "a[title='Descargar']"
                        )

                        cantidad_documentos = await documentos.count()


                        if cantidad_documentos > 0:

                            print(
                                f"   📄 Documentos encontrados: {cantidad_documentos}"
                            )


                            nombre_pdf, texto_pdf = await descargar_pdf_primefaces(
                                page,
                                "a[title='Descargar']",
                                "resolucion.pdf"
                            )

                            print(f"Longitud texto PDF: {len(texto_pdf)}")

                            if len(texto_pdf) < 100:
                                print("⚠️ OCR casi vacío")

                            if "DEMANDANTE" not in texto_pdf.upper():
                                print("⚠️ No aparece la palabra DEMANDANTE")

                            if "DEMANDADO" not in texto_pdf.upper():
                                print("⚠️ No aparece la palabra DEMANDADO")


                            if nombre_pdf:

                                url_pdf = (
                                    "GENERADO_POR_REMAJU_PRIMEFACES"
                                )


                    except Exception as e:

                        print(
                            f"   ⚠️ No se pudo descargar resolución: {e}"
                        )                                   
            
                    # =====================================
                    # UNIFICACIÓN DE TEXTO
                    # =====================================

                    texto_total = texto_limpio_profundo + " | " + texto_pdf


                    # ==========================================================
                    # PESTAÑA INMUEBLES  
                    # ==========================================================

                    await page.get_by_role("link", name="Inmuebles", exact=True).click()
                    await page.wait_for_timeout(800)

                    # -----------------------------
                    # DATOS GENERALES DEL INMUEBLE
                    # -----------------------------

                    texto_inmuebles = limpiar_texto(
                        await page.locator("div.ui-tabs-panel:visible").inner_text()
                    )

                    distrito_judicial_inmueble = extraer_campo(
                        texto_inmuebles,
                        "Distrito Judicial"
                    )

                    departamento_inmueble = extraer_campo(
                        texto_inmuebles,
                        "Departamento"
                    )

                    provincia_inmueble = extraer_campo(
                        texto_inmuebles,
                        "Provincia"
                    )

                    distrito_inmueble = extraer_campo(
                        texto_inmuebles,
                        "Distrito"
                    )


                    # -----------------------------
                    # TABLA DE INMUEBLES
                    # -----------------------------

                    tabla_inmuebles = page.locator(
                        "div.ui-tabs-panel:visible table"
                    ).last

                    filas_inmuebles = await tabla_inmuebles.locator(
                        "tbody tr"
                    ).all()                       

                    inmuebles_dict = {}

                    for idx, fila in enumerate(filas_inmuebles, start=1):

                        celdas = await fila.locator("td").all_inner_texts()

                        inmuebles_dict[f"Partida Registral {idx}"] = limpiar_texto(celdas[0]) if len(celdas) > 0 else ""
                        inmuebles_dict[f"Tipo Inmueble {idx}"] = limpiar_texto(celdas[1]) if len(celdas) > 1 else ""
                        inmuebles_dict[f"Dirección {idx}"] = limpiar_texto(celdas[2]) if len(celdas) > 2 else ""
                        inmuebles_dict[f"Carga y/o Gravamen {idx}"] = limpiar_texto(celdas[3]) if len(celdas) > 3 else ""
                        inmuebles_dict[f"Porcentaje a Rematar {idx}"] = limpiar_texto(celdas[4]) if len(celdas) > 4 else ""
                        inmuebles_dict[f"Imágenes {idx}"] = limpiar_texto(celdas[5]) if len(celdas) > 5 else ""

                                         
                    



                    # ==========================================================
                    # PESTAÑA CRONOGRAMA
                    # ==========================================================

                    await page.get_by_role("link", name="Cronograma", exact=True).click()
                    await page.wait_for_timeout(800)

                    tabla_cronograma = page.locator("div.ui-tabs-panel:visible table").first

                    filas = await tabla_cronograma.locator("tbody tr").all()

                    cronograma_dict = {}

                    for fila in filas:        

                        celdas = [
                            limpiar_texto(x)
                            for x in await fila.locator("td").all_inner_texts()  
                        ]

                        if len(celdas) >= 4:

                            nombre_fase = celdas[1]

                            # Limpieza del nombre para usarlo como encabezado
                            nombre_fase = re.sub(r"\s+", " ", nombre_fase).strip()

                            cronograma_dict[f"{nombre_fase} - Inicio"] = celdas[2]
                            cronograma_dict[f"{nombre_fase} - Fin"] = celdas[3]

                    # ==========================================================
                    # REGRESAR A REMATE (opcional)
                    # ==========================================================

                    # Cerrar posible modal de imágenes del inmueble
                    try:
                        boton_cerrar_modal = page.locator(
                            "div.ui-dialog:visible button.ui-dialog-titlebar-close"
                        )

                        if await boton_cerrar_modal.count() > 0:
                            await boton_cerrar_modal.click()
                            await page.wait_for_timeout(500)

                    except Exception:
                        pass


                    await page.get_by_role("link", name="Remate", exact=True).click()
                    await page.wait_for_timeout(500)
                    
            
                    # -----------------------------
                    # EXTRACCIÓN ORDENADA DE CAMPOS                                                                  
                    # -----------------------------
                    expediente_judicial = extraer_campo(texto_total, "Expediente") or "No localizado"
                    distrito_judicial = extraer_campo(texto_total, "Distrito Judicial")
                    organo_jurisdiccional = extraer_campo(texto_total, ["Órgano Jurisdiccional", "Órgano Jurisdisccional"])
                    instancia = extraer_campo(texto_total, "Instancia")
                    juez = extraer_campo(texto_total, "Juez")
                    especialista = extraer_campo(texto_total, "Especialista")
                    materia = extraer_campo(texto_total, "Materia")
                    resolucion = extraer_campo(texto_total, "Resolución")
                    fecha_resolucion = extraer_campo(texto_total, "Fecha Resolución")

                    descripcion = extraer_campo(texto_total, "Descripción")

                    n_inscritos = extraer_campo(texto_total, "N° inscritos")

                    tasacion_raw = extraer_campo(texto_total, "Tasación")
                    precio_base_raw = extraer_campo(texto_total, "Precio Base")
                    incremento_raw = extraer_campo(texto_total, "Incremento entre ofertas")
                    arancel_raw = extraer_campo(texto_total, "Arancel")
                    oblaje_raw = extraer_campo(texto_total, "Oblaje")
          
                    tasacion_data = parsear_monto(tasacion_raw)                        
                    precio_base_data = parsear_monto(precio_base_raw)
                    incremento_data = parsear_monto(incremento_raw)
                    arancel_data = parsear_monto(arancel_raw)
                    oblaje_data = parsear_monto(oblaje_raw)

                    tipo_cambio = None

                    if precio_base_data["moneda"] == "USD":
                        tipo_cambio = extraer_tipo_cambio(texto_limpio_profundo)

                    # si no apareció en tarjeta, usar como apoyo
                    if not fecha_remate:
                        fecha_remate = extraer_campo(texto_total, ["Fecha del remate", "Fecha Remate", "Fecha de remate"])

                    dias_restantes, estado_temporal = calcular_dias_restantes(fecha_remate)

                    # Gravámenes / Cargas / Recursos impugnatorios                                  
                    gravamenes = extraer_campo(texto_total, ["Gravámenes", "Gravamenes"])
                    cargas = extraer_campo(texto_total, "Cargas")
                    recursos_imp = extraer_campo(texto_total, ["Recursos impugnatorios", "Recurso impugnatorio", "Impugnaciones"])
                    
                    
                    
                           
                    
                    
                    
                    # ==========================================================
                    # EXTRACCIÓN DE PARTES PROCESALES - ESTRATEGIA CASCADA (WEB + PDF)
                    # ==========================================================

                    demandante = None
                    demandado = None
                    metodo_partes = "NINGUNO"

                    # PASO 1 (PRIMARIO): Aviso de Remate — plantilla fija generada
                    # por el propio sistema REM@JU, la fuente más confiable.
                    if texto_aviso:
                        partes_aviso = extraer_partes_desde_aviso(texto_aviso)
                        demandante = partes_aviso["Demandante"]
                        demandado = partes_aviso["Demandado"]
                        if demandante or demandado:
                            metodo_partes = "AVISO-REMAJU"

                    # PASO 2: resolucion.pdf (respaldo — redacción libre por juzgado)
                    if (not demandante or not demandado) and texto_pdf:
                        partes_pdf = extraer_partes_procesales_resolucion(texto_pdf)
                        if not demandante and partes_pdf["Demandante"]:
                            demandante = partes_pdf["Demandante"]
                        if not demandado and partes_pdf["Demandado"]:
                            demandado = partes_pdf["Demandado"]
                        if demandante or demandado:
                            metodo_partes = "PDF-DIRECTO" if metodo_partes == "NINGUNO" else "HÍBRIDO-AVISO-PDF"

                    # PASO 2b: variante narrativa ("SEGUIDO POR X, EN CONTRA
                    # DE Y") — cubre resoluciones que no usan carátula tipo
                    # "DEMANDADO:" sino redacción libre dentro del texto.
                    if (not demandante or not demandado) and texto_pdf:
                        partes_narr = extraer_partes_narrativa_generica(texto_pdf)
                        if not demandante and partes_narr["Demandante"]:
                            demandante = partes_narr["Demandante"]
                        if not demandado and partes_narr["Demandado"]:
                            demandado = partes_narr["Demandado"]
                        if demandante or demandado:
                            metodo_partes = "NARRATIVA-PDF" if metodo_partes == "NINGUNO" else f"{metodo_partes}+NARR"

                    # PASO 3: Si falta alguna parte, buscar en la Ficha Técnica Web (HTML)
                    if not demandante or not demandado:
                        partes_web = extraer_partes_procesales_resolucion(texto_limpio_profundo)
                        if not demandante and partes_web["Demandante"]:
                            demandante = partes_web["Demandante"]
                        if not demandado and partes_web["Demandado"]:
                            demandado = partes_web["Demandado"]
                        if demandante or demandado:
                            metodo_partes = "WEB-FALLBACK" if metodo_partes == "NINGUNO" else f"{metodo_partes}+WEB"

                    # PASO 4: Si aún falta, intentar sobre el texto unificado total (Web + PDF)
                    if not demandante or not demandado:
                        partes_total = extraer_partes_procesales_resolucion(texto_total)
                        if not demandante:
                            demandante = partes_total["Demandante"]
                        if not demandado:
                            demandado = partes_total["Demandado"]
                        if demandante or demandado:
                            metodo_partes = "TEXTO-TOTAL" if metodo_partes == "NINGUNO" else f"{metodo_partes}+TOTAL"

                    # PASO 5: Limpieza profunda final contra ruido OCR y basura judicial
                    # (no aplica al que ya vino limpio del Aviso, solo a resolucion.pdf/HTML)
                    if demandante and metodo_partes not in ("AVISO-REMAJU",):
                        demandante = limpiar_valor_parte_ocr(demandante)
                    if demandado and metodo_partes not in ("AVISO-REMAJU",):
                        demandado = limpiar_valor_parte_ocr(demandado)

                    # ==========================================================
                    # CAMPO LISTO PARA CONSULTA AUTOMÁTICA AL CEJ
                    # El CEJ exige el campo "Parte" en formato apellido paterno +
                    # apellido materno (o razón social). Se prioriza Demandado
                    # porque casi nunca viene vacío en el Aviso y es el dato de
                    # mayor valor comercial (dueño del bien rematado).
                    # ==========================================================
                    parte_busqueda_cej, origen_parte_cej = seleccionar_parte_para_busqueda(
                        demandante, demandado
                    )

                    # AUDITORÍA EN CONSOLA EN TIEMPO REAL
                    if not demandante:
                        print(f"   ⚠️ Demandante NO encontrado en {num_remate} (puede estar vacío en la fuente)")
                    if not demandado:
                        print(f"   ⚠️ Demandado NO encontrado en {num_remate}")
                    if demandante and demandado:
                        print(f"   ✅ Partes completas ({metodo_partes}): Dte='{demandante[:25]}...' | Ddo='{demandado[:25]}...'")                  
                           


                    
                    # --- REGRESO SEGURO USANDO EL BOTÓN NATIVO "REGRESAR" ---
                    print("   -> Retornando usando el botón 'Regresar' de la ficha...")
                    # Asegurar que no exista ningún modal bloqueando
                    try:
                        modal_visible = page.locator("div.ui-dialog:visible")

                        if await modal_visible.count() > 0:                       
                            cerrar = modal_visible.locator(
                                "button.ui-dialog-titlebar-close"
                            ) 

                            if await cerrar.count() > 0:
                                await cerrar.click()
                                await page.wait_for_timeout(500)

                    except Exception:
                        pass


                    await page.locator("button:has-text('Regresar')").first.click()

                    # Aumentamos el timeout a 35000 ms y aseguramos la navegación
                    try:
                        await page.wait_for_selector("div.ui-paginator", timeout=35000)
                    except Exception:
                        await page.wait_for_load_state("networkidle")
                        await page.wait_for_selector("div.ui-paginator", timeout=20000)

                    await page.wait_for_load_state("networkidle")                        

                    # Si REMAJU nos devolvió a la página 1, re-navegamos a la página donde nos quedamos:
                    if numero_pagina > 1:
                        pagina_activa_elem = page.locator("a.ui-paginator-page.ui-state-active")
                        if await pagina_activa_elem.count() > 0:
                            pag_actual_dom = (await pagina_activa_elem.inner_text()).strip()
                            if pag_actual_dom != str(numero_pagina):
                                print(f"   🔄 Restaurando ubicación a la página {numero_pagina}...")
                                target_page = page.locator(f"a.ui-paginator-page:has-text('{numero_pagina}')")
                                if await target_page.count() > 0:
                                    await target_page.click()
                                    await page.wait_for_load_state("networkidle")
                                    await page.wait_for_timeout(3000)            

                    # Construcción del registro organizado
                    registro = {
                        "Código de Remate": num_remate,
                        "Convocatoria": convocatoria,
                        "Tipo Remate": "REMATE SIMPLE",
                        "Fecha del remate": fecha_remate,
                        "Hora del remate": hora_remate,
                        "Días restantes": dias_restantes,
                        "Estado temporal": estado_temporal,
                        "Número de Expediente": expediente_judicial,
                        "Distrito Judicial": distrito_judicial,
                        "Órgano Jurisdisccional": organo_jurisdiccional,
                        "Instancia": instancia,
                        "Juez": juez,
                        "Especialista": especialista,
                        "Materia": materia,
                        "Resolución": resolucion,
                        "Fecha Resolución": fecha_resolucion,
                        "Descripción": descripcion,
                        "N° inscritos": n_inscritos,
                        "Tasación": tasacion_data["numerico"],
                        "Moneda Tasación": tasacion_data["moneda"],

                        "Precio Base": precio_base_data["numerico"],
                        "Moneda Precio Base": precio_base_data["moneda"],
                        "Tipo Cambio": tipo_cambio,

                        "Incremento entre ofertas": incremento_data["numerico"],
                        "Moneda Incremento": incremento_data["moneda"],

                        "Arancel": arancel_data["numerico"],
                        "Moneda Arancel": arancel_data["moneda"],

                        "Oblaje": oblaje_data["numerico"],
                        "Moneda Oblaje": oblaje_data["moneda"],
                        "Gravámenes": gravamenes,
                        "Cargas": cargas,
                        "Recursos impugnatorios": recursos_imp,
                        "Archivo PDF": url_pdf,
                        "Nombre Archivo PDF": nombre_pdf,                                
                        "Demandante": demandante,
                        "Demandado": demandado,
                        "Método extracción partes": metodo_partes,
                        "Parte para CEJ": parte_busqueda_cej,
                        "Origen Parte CEJ": origen_parte_cej,
                        "Distrito Judicial Inmueble": distrito_judicial_inmueble,
                        "Departamento Inmueble": departamento_inmueble,
                        "Provincia Inmueble": provincia_inmueble,
                        "Distrito Inmueble": distrito_inmueble,
     
                        **inmuebles_dict,
                        **cronograma_dict,
                    }

                                                                                                                  
                                 
                    

                    campos_obligatorios = [
                        "Código de Remate",               
                        "Número de Expediente",
                        "Precio Base",
                        "Descripción"
                    ]


                    faltantes = [
                        campo 
                        for campo in campos_obligatorios
                        if not registro.get(campo)
                    ]


                    if faltantes:        

                        print(
                            f"⚠️ Registro incompleto {num_remate}: {faltantes}"
                        )

                        lista_fallidos.append(
                            {
                                "Código Remate": num_remate,
                                "Error": f"Campos faltantes {faltantes}",
                                "Fecha Error": datetime.now()
                            }
                        )

                    else:

                        fila_ordenada = ordenar_campos_fila(registro)
                        lista_maestra_bloomberg.append(fila_ordenada)
                        guardar_checkpoint_incremental(fila_ordenada)

                    print(f"   ✅ Indexado: {num_remate} | Exp. {expediente_judicial}")

                except CaptchaBloqueoHeadless:
                    print("   🛑 Deteniendo la corrida por bloqueo de reCAPTCHA en modo headless.")
                    print("   (Todo lo procesado hasta este punto ya quedó guardado en el checkpoint)")
                    detenido_por_captcha = True
                    break

                except Exception as e:

                    print(
                        f"   ⚠️ Falló extracción elemento {i+1}: {e}"
                    )

                    lista_fallidos.append(
                        {
                            "Página": numero_pagina,
                            "Índice": i,
                            "Código Remate": num_remate if "num_remate" in locals() else None,
                            "Error": str(e),
                            "Fecha Error": datetime.now()
                        }
                    )
                    try:
                        print("   🔄 Reestableciendo conexión con la lista principal de REMAJU...")
                        await page.goto("https://remaju.pj.gob.pe/remaju/pages/publico/remateExterno.xhtml", timeout=60000)
                        await page.wait_for_load_state("networkidle")
                        await page.wait_for_timeout(5000)

                        # Forzar la re-orientación a la página en la que se interrumpió el proceso
                        if numero_pagina > 1:
                            print(f"   🎯 Re-navegando hacia la página N° {numero_pagina}...")
                            await page.wait_for_selector("div.ui-paginator", timeout=20000)

                            # Si la página requerida no es visible en el paginador inmediato, avanzar linealmente
                            for p_step in range(2, numero_pagina + 1):
                                target_btn = page.locator(f"a.ui-paginator-page:has-text('{p_step}')")
                                if await target_btn.count() > 0:
                                    await target_btn.first.click()
                                else:
                                    next_btn = page.locator("a.ui-paginator-next")
                                    if await next_btn.count() > 0:
                                        await next_btn.first.click()
                                await page.wait_for_load_state("networkidle")
                                await page.wait_for_timeout(2500)
                    except Exception as err_recovery:
                        print(f"   ⚠️ Falló la recuperación de sesión: {err_recovery}")
                    continue   

            if detenido_por_captcha:
                break

            # --- EVALUACIÓN DE PAGINACIÓN GENERAL ---
            # --- EVALUACIÓN DE PAGINACIÓN GENERAL ---
            print(f"\n🔄 Evaluando si existe una página siguiente...")
            boton_siguiente = page.locator("a.ui-paginator-next")

            if await boton_siguiente.count() > 0:
                clases = await boton_siguiente.first.get_attribute("class")
                if "ui-state-disabled" in clases:
                    print("🏁 ¡Fin del mapa! Se ha alcanzado la última página disponible de REMAJU.")
                    break
                else:
                    siguiente_num = numero_pagina + 1
                    print(f"➡️ Avanzando hacia la página N° {siguiente_num}...")

                    # Intentar clic directo sobre el número de página si está visible en el paginador
                    boton_num_pag = page.locator(f"a.ui-paginator-page:has-text('{siguiente_num}')")
                    if await boton_num_pag.count() > 0:
                        await boton_num_pag.first.click()
                    else:
                        await boton_siguiente.first.click()

                    numero_pagina += 1

                    # Esperar la respuesta AJAX de PrimeFaces
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(4000)
            else:
                print("⚠️ Paginador no detectado de forma inmediata. Esperando refresco de PrimeFaces...")
                await page.wait_for_timeout(5000)
                if await page.locator("div.ui-paginator").count() == 0:
                    print("🏁 No se localizó el componente de paginación tras la espera. Proceso cerrado.")
                    break
                                                
        # --- EXPORTACIÓN GENERAL ---
        # Se recarga el checkpoint completo (puede tener registros de
        # corridas anteriores que ni siquiera se tocaron en esta
        # ejecución) y se combina con lo nuevo de esta corrida, así el
        # Excel final es siempre la unión completa, nunca un subconjunto.
        registros_checkpoint_final, _ = cargar_checkpoint()
        combinados = dict(registros_checkpoint_final)
        for fila in lista_maestra_bloomberg:
            codigo = fila.get("Código de Remate")
            if codigo:
                combinados[codigo] = fila
        lista_maestra_bloomberg = list(combinados.values())

        if detenido_por_captcha:
            print(f"\n⚠️ La corrida se detuvo antes de terminar (bloqueo de captcha en modo headless).")
            print(f"   Se exporta igual lo acumulado hasta ahora ({len(lista_maestra_bloomberg)} remates en total).")
            print(f"   Vuelve a correr el script más tarde: retomará automáticamente desde el checkpoint.")

        if lista_maestra_bloomberg:
            df = pd.DataFrame(lista_maestra_bloomberg)

            demandantes_vacios = (
                df["Demandante"].fillna("").str.strip() == ""
            ).sum()

            demandados_vacios = (
                df["Demandado"].fillna("").str.strip() == ""
            ).sum()

            print("\n==============================")
            print(f"Demandantes vacíos: {demandantes_vacios}")
            print(f"Demandados vacíos: {demandados_vacios}")
            print("==============================")

            # ===== Auditoría detallada =====
            vacios = df[
                (df["Demandante"].fillna("").str.strip() == "") |
                (df["Demandado"].fillna("").str.strip() == "")
            ]

            if len(vacios) > 0:
                print("\nRemates con partes procesales faltantes:")
                print(vacios[["Código de Remate", "Demandante", "Demandado"]])                   


            nombre_archivo = f"Bloomberg_Remates_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            df.to_excel(nombre_archivo, index=False)
            if lista_fallidos:

                df_error = pd.DataFrame(lista_fallidos)

                nombre_error = (
                    f"Errores_Remates_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
                )

                df_error.to_excel(
                    nombre_error,
                    index=False
                )

                print(
                    f"⚠️ Atención: {len(lista_fallidos)} remates requieren revisión"
                )







            print(f"Archivo guardado como: {nombre_archivo}")
            print(f"\n📊 ¡MATRIZ FINANCIERA COMPLETADA!")
            print(f"   Extracción masiva exitosa. {len(lista_maestra_bloomberg)} registros guardados.")
        else:
            print("\n⚠️ Alerta: Matriz de almacenamiento vacía.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(ejecutar_scraper()) 
