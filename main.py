import time
import math
import json
import random
import logging
import datetime
import argparse
import os
import html
from threading import Semaphore
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURACIÓN Y LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "es-ES,es;q=0.9"
}

# Control de Tasa Global (RPS)
SEMAFORO_RED = Semaphore(3)
CACHE_EVENTOS_FILE = "cache_eventos.json"
NOTIFICACIONES_FILE = "notificaciones_telegram.json"
CACHE_HISTORIAL_USUARIOS = {}
TTL_HISTORIAL_HORAS = 3
MINUTOS_ANTES_PARTIDO = 10
USUARIOS_OBJETIVO = (
    ("And.A.", "678767edb8435cc2d1bba515"),
    ("Guest623527", "6a50b0b53059489f16131c97"),
    ("António Coelho 19", "5f347976e3799696cf765618"),
    ("Ꮛ.Borges", "6a8c9d6dc2e1d8a5b3acc831"),
    ("tiagoleo4", "648a447c6df949167f3e146a"),
    ("Scuti.", "6758979fed09a67b595d5ba2"),
    ("XHA885", "5a2bac5469243973927b84db"),
    ("🪄 Mandrake's🪄", "64bc4a7b85b676738deab28f"),
    ("Ridel Yoka", "6a661619cfb654246ce4338f"),
)

# Configuración de Telegram (Usa variables de entorno o valores por defecto)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- 1. CAPA DE RED, CACHÉ Y TELEGRAM ---

def enviar_alerta_telegram(mensaje):
    """Envía alertas formateadas en HTML a Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram token o Chat ID no configurados.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        respuesta = resp.json()
        if resp.status_code == 200 and respuesta.get("ok"):
            logging.info("Alerta de Telegram enviada exitosamente.")
            return True
        else:
            logging.error(
                "Error al enviar a Telegram (%s): %s",
                resp.status_code,
                respuesta.get("description", resp.text)
            )
            return False
    except Exception:
        logging.exception("Excepción en envío a Telegram")
        return False

def cargar_cache_disco():
    try:
        with open(CACHE_EVENTOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def guardar_cache_disco(cache):
    try:
        with open(CACHE_EVENTOS_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        logging.exception("Error al guardar caché en disco")

def realizar_peticion(url, reintentos=3):
    backoff = 1.5
    for intento in range(1, reintentos + 1):
        with SEMAFORO_RED:
            time.sleep(random.uniform(0.1, 0.4))
            try:
                resp = requests.get(url, headers=HEADERS, impersonate="chrome", timeout=10)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 403:
                    logging.error("[HTTP 403] SofaScore bloqueó esta IP: %s", url)
                    return None
                elif resp.status_code == 429:
                    logging.warning(f" [HTTP 429] Rate-limit en {url}. Intento {intento}/{reintentos}")
                else:
                    logging.warning(f" [HTTP {resp.status_code}] Error en respuesta para {url}")
            except Exception:
                logging.exception("Excepción en petición (%s)", url)

        if intento < reintentos:
            time.sleep(backoff)
            backoff *= 2
    return None

def obtener_detalle_evento(event_id, cache_memoria):
    if str(event_id) in cache_memoria:
        return cache_memoria[str(event_id)]
    
    data = realizar_peticion(f"https://www.sofascore.com/api/v1/event/{event_id}")
    evento = data.get('event', {}) if data else {}
    if evento:
        cache_memoria[str(event_id)] = evento
    return evento

# --- 2. ALGORITMOS Y MAPPING DE MERCADOS ---

def estadisticas_vacias():
    return {"racha_activa": 0, "aciertos_3d": 0, "aciertos_7d": 0, "peso_usuario": 1.0}

def prediccion_ganadora(prediccion):
    return (
        prediccion.get('isWinning') is True
        or prediccion.get('status') in [1, 'won', 'WINNING', 'CORRECT']
        or prediccion.get('correct') is True
    )

def calcular_estadisticas(predicciones, ahora):
    limite_3d = ahora - (3 * 86400)
    limite_7d = ahora - (7 * 86400)
    racha_activa = 0
    corte_racha = False
    aciertos_3d = 0
    aciertos_7d = 0
    score_7d_decaimiento = 0.0

    for prediccion in predicciones:
        if not prediccion_ganadora(prediccion):
            corte_racha = True
            continue
        if not corte_racha:
            racha_activa += 1

        timestamp = prediccion.get('event', {}).get('startTimestamp', 0)
        if timestamp < limite_7d:
            continue
        aciertos_7d += 1
        dias_transcurridos = (ahora - timestamp) / 86400.0
        score_7d_decaimiento += math.exp(-dias_transcurridos / 3.5)
        if timestamp >= limite_3d:
            aciertos_3d += 1

    return {
        "racha_activa": racha_activa,
        "aciertos_3d": aciertos_3d,
        "aciertos_7d": aciertos_7d,
        "peso_usuario": round(1.0 + (racha_activa * 0.3) + (score_7d_decaimiento * 0.15), 2)
    }

def analizar_historial_usuario(user_id):
    ahora = datetime.datetime.now().timestamp()

    if user_id in CACHE_HISTORIAL_USUARIOS:
        cached = CACHE_HISTORIAL_USUARIOS[user_id]
        if ahora - cached["timestamp"] < (TTL_HISTORIAL_HORAS * 3600):
            return cached["data"]

    data = realizar_peticion(f"https://www.sofascore.com/api/v1/user-account/{user_id}/predictions/ended/0")
    if not data:
        return estadisticas_vacias()

    predicciones = data.get('predictions', [])
    predicciones_ordenadas = sorted(predicciones, key=lambda x: x.get('event', {}).get('startTimestamp', 0), reverse=True)
    resultado = calcular_estadisticas(predicciones_ordenadas, ahora)
    
    CACHE_HISTORIAL_USUARIOS[user_id] = {"timestamp": ahora, "data": resultado}
    return resultado

def normalizar_mercado_voto(voto_raw, pred_raw):
    str_voto = str(voto_raw).upper().strip()
    market_type = pred_raw.get('marketType', '').lower()
    
    if 'both' in market_type or 'btts' in market_type:
        return f"BTTS: {str_voto}"
    if 'first_goal' in market_type or 'first' in market_type:
        return f"1ST GOAL: {str_voto}"
    
    return str_voto

def resolver_datos_evento(pred, cache_eventos):
    evento = pred.get('event') or {}
    timestamp = evento.get('startTimestamp') or pred.get('startDateTimestamp')
    eq_local = evento.get('homeTeam', {}).get('name') or pred.get('homeTeamName')
    eq_visit = evento.get('awayTeam', {}).get('name') or pred.get('awayTeamName')
    if eq_local and eq_visit:
        return evento, timestamp, eq_local, eq_visit

    event_id = evento.get('id') or pred.get('eventId')
    if event_id:
        evento = obtener_detalle_evento(event_id, cache_eventos)
        eq_local = evento.get('homeTeam', {}).get('name') or pred.get('homeTeamName')
        eq_visit = evento.get('awayTeam', {}).get('name') or pred.get('awayTeamName')
        timestamp = evento.get('startTimestamp') or timestamp
    return evento, timestamp, eq_local, eq_visit

def obtener_cuota(pred):
    cuotas = pred.get('odds') or {}
    for campo in ('decimalValue', 'fractionalValue', 'americanValue'):
        valor = cuotas.get(campo)
        if valor not in (None, '', '-'):
            return str(valor)
    return "No disponible"

def preparar_prediccion(pred, posicion, ah_ts, cache_eventos, user_id, username, stats_racha):
    evento, timestamp, eq_local, eq_visit = resolver_datos_evento(pred, cache_eventos)
    if not timestamp or timestamp <= ah_ts:
        return None

    return {
        "partido": f"{eq_local} vs {eq_visit}" if eq_local and eq_visit else "Partido Desconocido",
        "info": {
            "torneo": evento.get('tournament', {}).get('name') or "Competición",
            "fecha": datetime.datetime.fromtimestamp(timestamp).strftime('%d/%m %H:%M'),
            "timestamp": timestamp
        },
        "prediccion": {
            "top": posicion,
            "usuario": username,
            "voto": normalizar_mercado_voto(pred.get('vote', '?'), pred),
            "cuota": obtener_cuota(pred),
            "user_id": user_id,
            "event_id": evento.get('id') or pred.get('eventId'),
            "stats": stats_racha
        }
    }

def procesar_usuario(item, posicion, ah_ts, cache_eventos):
    user_data = item.get('user', {})
    user_id = item.get('id') or user_data.get('id')
    username = (
        item.get('username')
        or item.get('nickname')
        or user_data.get('username')
        or user_data.get('nickname')
        or f"Usuario {posicion}"
    )
    
    if not user_id:
        return None, [], estadisticas_vacias()

    stats_racha = analizar_historial_usuario(user_id)

    urls = [
        f"https://www.sofascore.com/api/v1/user-account/{user_id}/predictions/next/0",
        f"https://www.sofascore.com/api/v1/user-account/{user_id}/predictions/active/0"
    ]
    
    predicciones_validas = []
    for url in urls:
        data = realizar_peticion(url)
        if not data:
            continue
        for pred in data.get('predictions', []):
            prediccion = preparar_prediccion(
                pred, posicion, ah_ts, cache_eventos, user_id, username, stats_racha
            )
            if prediccion:
                predicciones_validas.append(prediccion)
            
    return user_id, predicciones_validas, stats_racha

# --- 3. GENERADOR DE HTML Y DISEÑO UI ---

CSS_COMMON = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Inter', sans-serif; background-color: #0b0f19; color: #f3f4f6; padding: 30px 20px; -webkit-font-smoothing: antialiased; }
.header-container { max-width: 1200px; margin: 0 auto 30px auto; text-align: center; }
.nav-bar { margin-bottom: 25px; }
.nav-button { background: #1f2937; color: #d1d5db; padding: 10px 20px; text-decoration: none; font-size: 0.88em; font-weight: 500; border-radius: 99px; border: 1px solid #4b5563; transition: all 0.2s ease; display: inline-block; }
.nav-button:hover { background: #374151; color: #ffffff; border-color: #6b7280; }
h1 { font-size: 1.8em; font-weight: 700; letter-spacing: -0.02em; color: #ffffff; margin-bottom: 8px; }
.subtitle { font-size: 0.9em; color: #6b7280; font-weight: 400; }
.container { max-width: 1200px; margin: 0 auto; display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 20px; }
.match-card { background: #111827; border-radius: 16px; padding: 20px; border: 1px solid #1f2937; display: flex; flex-direction: column; transition: transform 0.2s ease, border-color 0.2s ease; }
.match-card:hover { border-color: #374151; transform: translateY(-2px); }
.match-header { display: flex; justify-content: space-between; font-size: 0.75em; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; margin-bottom: 12px; }
.match-title { font-size: 1.1em; font-weight: 600; color: #f9fafb; margin-bottom: 16px; text-align: center; line-height: 1.4; }
.consensus-badge { text-align: center; font-weight: 600; padding: 8px; border-radius: 8px; margin-bottom: 16px; font-size: 0.85em; letter-spacing: 0.02em; }
.cons-active { background: #064e3b; color: #6ee7b7; border: 1px solid #10b981; }
.cons-none { background: #374151; color: #d1d5db; border: 1px solid #6b7280; }
.prediction-list { display: flex; flex-direction: column; gap: 8px; margin-top: auto; }
.prediction-badge { background: #1f2937; padding: 10px 14px; border-radius: 10px; font-size: 0.85em; display: flex; align-items: center; justify-content: space-between; border: 1px solid rgba(255, 255, 255, 0.03); }
.user-info { display: flex; align-items: center; gap: 8px; }
.top-rank { font-size: 0.7em; color: #f3f4f6; background: #374151; padding: 2px 6px; border-radius: 4px; font-weight: 600; }
.user-name { font-weight: 500; color: #e5e7eb; max-width: 110px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stats-group { display: flex; gap: 6px; align-items: center; }
.badge-item { font-size: 0.7em; padding: 3px 7px; border-radius: 6px; font-weight: 600; }
.badge-racha { background: #065f46; color: #a7f3d0; }
.badge-3d { background: #78350f; color: #fde68a; }
.badge-7d { background: #581c87; color: #e9d5ff; }
.vote { font-weight: 700; padding: 4px 10px; border-radius: 6px; font-size: 0.8em; text-align: center; background: #374151; color: #f3f4f6; }
.empty-state { grid-column: 1 / -1; text-align: center; color: #4b5563; padding: 60px 0; font-size: 0.95em; }
"""

def calcular_consenso_ponderado(predicciones):
    if len(predicciones) < 3:
        return "MUESTRA INSUFICIENTE (<3 VOTOS)", "cons-none", 0, ""
        
    pesos_votos = {}
    peso_total = 0.0
    
    for p in predicciones:
        voto = p['voto']
        peso = p['stats']['peso_usuario']
        pesos_votos[voto] = pesos_votos.get(voto, 0.0) + peso
        peso_total += peso

    if peso_total == 0:
        return "SIN CONSENSO", "cons-none", 0, ""

    voto_ganador = max(pesos_votos, key=pesos_votos.get)
    porcentaje = int((pesos_votos[voto_ganador] / peso_total) * 100)

    if porcentaje >= 55:
        return f"🔥 {porcentaje}% CONSENSO: {voto_ganador} (N={len(predicciones)})", "cons-active", porcentaje, voto_ganador
    
    return "SIN CONSENSO CLARO", "cons-none", porcentaje, voto_ganador

def generar_vistas_html(partidos_top, partidos_rachas):
    # DASHBOARD TOP
    html_top = f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="/favicon.svg" type="image/svg+xml"><title>Radar Top - SofaScore</title><style>{CSS_COMMON}</style></head><body>
    <div class="header-container"><div class="nav-bar"><a href="rachas.html" class="nav-button">🔥 Ver Tipsters en Racha →</a></div><h1>Radar Top Mundial</h1><p class="subtitle">Consenso Ponderado por Confiabilidad</p></div><div class="container">"""

    if not partidos_top:
        html_top += "<div class='empty-state'>No hay predicciones futuras disponibles.</div>"
    else:
        for partido, datos in sorted(partidos_top.items(), key=lambda x: x[1]['info']['timestamp']):
            texto_cons, clase_cons, _, _ = calcular_consenso_ponderado(datos['predicciones'])
            html_top += f"""<div class="match-card"><div class="match-header"><span>{datos['info']['torneo']}</span><span>{datos['info']['fecha']}</span></div><div class="match-title">{partido}</div><div class="consensus-badge {clase_cons}">{texto_cons}</div><div class="prediction-list">"""
            for p in datos['predicciones']:
                html_top += f"""<div class="prediction-badge"><div class="user-info"><span class="top-rank">T{p['top']}</span><span class="user-name" title="{p['usuario']}">{p['usuario']}</span></div><span class="vote">{p['voto']}</span></div>"""
            html_top += "</div></div>"
    html_top += "</div></body></html>"

    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(html_top)

    # DASHBOARD RACHAS
    html_rachas = f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="/favicon.svg" type="image/svg+xml"><title>Radar Rachas - SofaScore</title><style>{CSS_COMMON}</style></head><body>
    <div class="header-container"><div class="nav-bar"><a href="dashboard.html" class="nav-button">← Ver Radar Top General</a></div><h1>Tipsters en Racha</h1><p class="subtitle">Filtrados por algoritmo de decaimiento y rachas activas</p></div><div class="container">"""

    if not partidos_rachas:
        html_rachas += "<div class='empty-state'>No hay tipsters con racha destacada actualmente.</div>"
    else:
        for partido, datos in sorted(partidos_rachas.items(), key=lambda x: x[1]['info']['timestamp']):
            html_rachas += f"""<div class="match-card"><div class="match-header"><span>{datos['info']['torneo']}</span><span>{datos['info']['fecha']}</span></div><div class="match-title">{partido}</div><div class="prediction-list">"""
            for p in datos['predicciones']:
                st = p['stats']
                html_rachas += f"""<div class="prediction-badge"><span class="user-name" title="{p['usuario']}">{p['usuario']}</span><div class="stats-group"><span class="badge-item badge-racha" title="Racha activa">🔥 {st['racha_activa']}</span><span class="badge-item badge-3d" title="Aciertos 3d">3D: {st['aciertos_3d']}</span><span class="badge-item badge-7d" title="Aciertos 7d">7D: {st['aciertos_7d']}</span></div><span class="vote">{p['voto']}</span></div>"""
            html_rachas += "</div></div>"
    html_rachas += "</div></body></html>"

    with open("rachas.html", "w", encoding="utf-8") as f:
        f.write(html_rachas)

    logging.info("Archivos HTML generados exitosamente.")

# --- 4. PIPELINE Y LIMPIEZA DE DATOS ---

def agregar_partido(partidos, partido, info, pred):
    if partido not in partidos:
        partidos[partido] = {"info": info, "predicciones": []}
    partidos[partido]["predicciones"].append(pred)

def incorporar_predicciones(partidos_top, partidos_rachas, posicion, preds, stats):
    for entry in preds:
        partido = entry["partido"]
        if posicion <= 10:
            agregar_partido(partidos_top, partido, entry["info"], entry["prediccion"])
        if stats["racha_activa"] >= 2 or stats["aciertos_7d"] >= 3:
            agregar_partido(partidos_rachas, partido, entry["info"], entry["prediccion"])

def seleccionar_usuarios_por_nickname():
    seleccionados = [
        (indice, nickname, {"id": user_id, "nickname": nickname})
        for indice, (nickname, user_id) in enumerate(USUARIOS_OBJETIVO)
    ]
    logging.info("Nicknames consultados: %s", ", ".join(item[1] for item in seleccionados))
    return seleccionados

def recopilar_partidos(usuarios_seleccionados, ahora_ts, cache_eventos):
    partidos_top = {}
    partidos_rachas = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(procesar_usuario, item, indice + 1, ahora_ts, cache_eventos): indice
            for indice, _, item in usuarios_seleccionados
        }
        for future in as_completed(futures):
            posicion = futures[future]
            user_id, preds, stats = future.result()
            if user_id and preds:
                incorporar_predicciones(partidos_top, partidos_rachas, posicion, preds, stats)
    partidos_top = dict(sorted(
        partidos_top.items(),
        key=lambda item: item[1]['info']['timestamp']
    ))
    partidos_rachas = dict(sorted(
        partidos_rachas.items(),
        key=lambda item: item[1]['info']['timestamp']
    ))
    return partidos_top, partidos_rachas

def guardar_estado(partidos_top, partidos_rachas, ahora_ts):
    try:
        with open("state.json", "w", encoding="utf-8") as archivo:
            json.dump({"top": partidos_top, "rachas": partidos_rachas, "actualizado": ahora_ts}, archivo, ensure_ascii=False)
        logging.info("state.json actualizado y depurado correctamente.")
    except Exception:
        logging.exception("Error al escribir state.json")

def cargar_notificaciones():
    try:
        with open(NOTIFICACIONES_FILE, "r", encoding="utf-8") as archivo:
            datos = json.load(archivo)
            return set(datos if isinstance(datos, list) else [])
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def guardar_notificaciones(notificaciones):
    try:
        with open(NOTIFICACIONES_FILE, "w", encoding="utf-8") as archivo:
            json.dump(sorted(notificaciones), archivo, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception("Error al guardar registro de notificaciones")

def clave_notificacion(prediccion, info, partido):
    event_id = prediccion.get("event_id") or f"{partido}|{info['timestamp']}"
    return f"{prediccion['user_id']}:{event_id}:{prediccion['voto']}"

def clave_recordatorio(prediccion, info, partido):
    return f"recordatorio:{clave_notificacion(prediccion, info, partido)}"

def construir_mensaje_predicciones(partido, datos, predicciones, recordatorio=False):
    nicknames = ", ".join(sorted({prediccion['usuario'] for prediccion in predicciones}))
    cuotas = sorted({prediccion.get('cuota', 'No disponible') for prediccion in predicciones})
    cuota = ", ".join(cuotas)
    titulo = "⏰ PARTIDO EN 10 MINUTOS" if recordatorio else "🔔 NUEVA PREDICCIÓN"
    return (
        f"<b>{titulo}</b>\n\n"
        f"👤 <b>Nicknames:</b> {html.escape(nicknames)}\n"
        f"⚽ <b>Partido:</b> {html.escape(partido)}\n"
        f"🏆 <b>Torneo:</b> {html.escape(datos['info']['torneo'])}\n"
        f"🕒 <b>Hora:</b> {html.escape(datos['info']['fecha'])}\n"
        f"🎯 <b>Predicción:</b> {html.escape(predicciones[0]['voto'])}\n"
        f"💰 <b>Cuota:</b> {html.escape(cuota)}"
    )

def agrupar_predicciones(partidos_top):
    grupos = {}
    for partido, datos in partidos_top.items():
        for prediccion in datos['predicciones']:
            evento = prediccion.get('event_id') or f"{partido}|{datos['info']['timestamp']}"
            clave_grupo = f"{evento}:{prediccion['voto']}"
            grupo = grupos.setdefault(clave_grupo, {"partido": partido, "datos": datos, "predicciones": []})
            grupo["predicciones"].append(prediccion)
    return grupos.values()

def procesar_grupo_predicciones(grupo, notificaciones, ahora_ts):
    partido = grupo["partido"]
    datos = grupo["datos"]
    predicciones = grupo["predicciones"]
    nuevas = [
        prediccion for prediccion in predicciones
        if clave_notificacion(prediccion, datos['info'], partido) not in notificaciones
    ]
    if nuevas:
        mensaje = construir_mensaje_predicciones(partido, datos, predicciones)
        if enviar_alerta_telegram(mensaje):
            for prediccion in nuevas:
                notificaciones.add(clave_notificacion(prediccion, datos['info'], partido))
            return True
        return False

    timestamp = datos['info']['timestamp']
    comienza_pronto = ahora_ts <= timestamp <= ahora_ts + (MINUTOS_ANTES_PARTIDO * 60)
    recordatorio_nuevo = any(
        clave_recordatorio(prediccion, datos['info'], partido) not in notificaciones
        for prediccion in predicciones
    )
    if not comienza_pronto or not recordatorio_nuevo:
        return False

    mensaje = construir_mensaje_predicciones(partido, datos, predicciones, recordatorio=True)
    if not enviar_alerta_telegram(mensaje):
        return False
    for prediccion in predicciones:
        notificaciones.add(clave_recordatorio(prediccion, datos['info'], partido))
    return True

def enviar_predicciones(partidos_top, notificaciones, ahora_ts):
    enviadas = sum(
        procesar_grupo_predicciones(grupo, notificaciones, ahora_ts)
        for grupo in agrupar_predicciones(partidos_top)
    )
    guardar_notificaciones(notificaciones)
    logging.info("Predicciones nuevas enviadas a Telegram: %s", enviadas)

def ejecutar_pipeline():
    logging.info("Iniciando pipeline de actualización...")
    cache_eventos = cargar_cache_disco()
    ahora_ts = datetime.datetime.now().timestamp()
    usuarios_seleccionados = seleccionar_usuarios_por_nickname()
    partidos_top, partidos_rachas = recopilar_partidos(usuarios_seleccionados, ahora_ts, cache_eventos)
    if not partidos_top and not partidos_rachas:
        logging.error("No se obtuvieron predicciones; se conservan los dashboards anteriores.")
        return
    guardar_cache_disco(cache_eventos)
    guardar_estado(partidos_top, partidos_rachas, ahora_ts)
    generar_vistas_html(partidos_top, partidos_rachas)
    enviar_predicciones(partidos_top, cargar_notificaciones(), ahora_ts)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Actualiza los dashboards de SofaScore.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Ejecuta un ciclo y termina (Ideal para GitHub Actions).",
    )
    args = parser.parse_args()

    INTERVALO_MINUTOS = 5
    if args.once:
        ejecutar_pipeline()
    else:
        while True:
            ejecutar_pipeline()
            logging.info(f"Ciclo finalizado. Esperando {INTERVALO_MINUTOS} minutos...")
            time.sleep(INTERVALO_MINUTOS * 60)