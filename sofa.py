import time
import math
import json
import random
import logging
import datetime
import argparse
from threading import Semaphore
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests

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
SEMAFORO_RED = Semaphore(3)  # Máximo 3 peticiones concurrentes simultáneas
CACHE_EVENTOS_FILE = "cache_eventos.json"
CACHE_HISTORIAL_USUARIOS = {}  # {user_id: {"timestamp": ts, "data": stats}}
TTL_HISTORIAL_HORAS = 3

# --- 1. CAPA DE RED RESILIENTE Y CACHÉ PERSISTENTE ---

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
    except Exception as e:
        logging.error(f"Error al guardar caché en disco: {e}")

def realizar_peticion(url, reintentos=3):
    """Realiza peticiones HTTP con reintentos, backoff exponencial y rate limiting."""
    backoff = 1.5
    for intento in range(1, reintentos + 1):
        with SEMAFORO_RED:
            time.sleep(random.uniform(0.1, 0.4))  # Jitter aleatorio
            try:
                resp = requests.get(url, headers=HEADERS, impersonate="chrome", timeout=10)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code in [403, 429]:
                    logging.warning(f" [HTTP {resp.status_code}] Rate-limited o Bloqueado en {url}. Intento {intento}/{reintentos}")
                else:
                    logging.warning(f" [HTTP {resp.status_code}] Error en respuesta para {url}")
            except Exception as e:
                logging.error(f"Excepción en petición ({url}): {type(e).__name__} - {e}. Intento {intento}/{reintentos}")

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

# --- 2. ALGORITMOS DE RENDIMIENTO, DECAIMIENTO Y RACHA ---

def analizar_historial_usuario(user_id):
    """Calcula racha cronológica y aciertos ponderados por decaimiento exponencial."""
    ahora = datetime.datetime.now().timestamp()
    
    # Verificar Caché TTL en memoria para historial de usuario
    if user_id in CACHE_HISTORIAL_USUARIOS:
        cached = CACHE_HISTORIAL_USUARIOS[user_id]
        if ahora - cached["timestamp"] < (TTL_HISTORIAL_HORAS * 3600):
            return cached["data"]

    data = realizar_peticion(f"https://www.sofascore.com/api/v1/user-account/{user_id}/predictions/ended/0")
    if not data:
        stats_vacias = {"racha_activa": 0, "aciertos_3d": 0, "aciertos_7d": 0, "peso_usuario": 1.0}
        return stats_vacias

    predicciones = data.get('predictions', [])
    
    # Ordenamiento cronológico garantizado (Descendente: más reciente primero)
    predicciones_ordenadas = sorted(
        predicciones, 
        key=lambda x: x.get('event', {}).get('startTimestamp', 0), 
        reverse=True
    )
    
    limite_3d = ahora - (3 * 86400)
    limite_7d = ahora - (7 * 86400)
    tau = 3.5  # Constante de decaimiento en días
    
    racha_activa = 0
    corte_racha = False
    aciertos_3d = 0
    aciertos_7d = 0
    score_7d_decaimiento = 0.0

    for pred in predicciones_ordenadas:
        win_flag = pred.get('isWinning')
        status = pred.get('status')
        correct = pred.get('correct')
        
        es_verde = (
            win_flag is True or 
            status in [1, 'won', 'WINNING', 'CORRECT'] or 
            correct is True
        )
        
        ts_evento = pred.get('event', {}).get('startTimestamp', 0)
        
        if es_verde:
            if not corte_racha:
                racha_activa += 1
            
            if ts_evento >= limite_7d:
                aciertos_7d += 1
                dias_transcurridos = (ahora - ts_evento) / 86400.0
                peso_tiempo = math.exp(-dias_transcurridos / tau)
                score_7d_decaimiento += peso_tiempo
                
                if ts_evento >= limite_3d:
                    aciertos_3d += 1
        else:
            corte_racha = True

    # Ponderación algorítmica del usuario
    peso_usuario = 1.0 + (racha_activa * 0.3) + (score_7d_decaimiento * 0.15)
    
    resultado = {
        "racha_activa": racha_activa,
        "aciertos_3d": aciertos_3d,
        "aciertos_7d": aciertos_7d,
        "peso_usuario": round(peso_usuario, 2)
    }
    
    CACHE_HISTORIAL_USUARIOS[user_id] = {"timestamp": ahora, "data": resultado}
    return resultado

def normalizar_mercado_voto(voto_raw, pred_raw):
    """Mapea predicciones de 1X2, Ambos Anotan (BTTS) y Primer Gol."""
    str_voto = str(voto_raw).upper().strip()
    market_type = pred_raw.get('marketType', '').lower()
    
    if 'both' in market_type or 'btts' in market_type:
        return f"BTTS: {str_voto}"
    if 'first_goal' in market_type or 'first' in market_type:
        return f"1ST GOAL: {str_voto}"
    
    return str_voto

def procesar_usuario(item, posicion, ah_ts, cache_eventos):
    """Worker para extraer predicciones de usuario de forma segura."""
    user_data = item.get('user', {})
    user_id = item.get('id') or user_data.get('id')
    username = item.get('username') or user_data.get('username') or f"Usuario {posicion}"
    
    if not user_id:
        return None, [], {"racha_activa": 0, "aciertos_3d": 0, "aciertos_7d": 0, "peso_usuario": 1.0}

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
            evento = pred.get('event', {})
            timestamp = evento.get('startTimestamp')
            
            if timestamp and timestamp < ah_ts:
                continue

            eq_local = evento.get('homeTeam', {}).get('name')
            eq_visit = evento.get('awayTeam', {}).get('name')
            
            if not eq_local or not eq_visit:
                e_id = evento.get('id') or pred.get('eventId')
                if e_id:
                    evento = obtener_detalle_evento(e_id, cache_eventos)
                    eq_local = evento.get('homeTeam', {}).get('name')
                    eq_visit = evento.get('awayTeam', {}).get('name')
                    timestamp = evento.get('startTimestamp') or timestamp

            if timestamp and timestamp < ah_ts:
                continue

            partido = f"{eq_local} vs {eq_visit}" if eq_local and eq_visit else "Partido Desconocido"
            torneo = evento.get('tournament', {}).get('name') or "Competición"
            fecha_str = datetime.datetime.fromtimestamp(timestamp).strftime('%d/%m %H:%M') if timestamp else "Próximamente"

            voto_normalizado = normalizar_mercado_voto(pred.get('vote', '?'), pred)

            predicciones_validas.append({
                "partido": partido,
                "info": {
                    "torneo": torneo, 
                    "fecha": fecha_str,
                    "timestamp": timestamp or 9999999999
                },
                "prediccion": {
                    "top": posicion,
                    "usuario": username,
                    "voto": voto_normalizado,
                    "user_id": user_id,
                    "stats": stats_racha
                }
            })
            
    return user_id, predicciones_validas, stats_racha

# --- 3. GENERADOR DE HTML Y DISEÑO UI ---

CSS_COMMON = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Inter', sans-serif; background-color: #0b0f19; color: #f3f4f6; padding: 30px 20px; -webkit-font-smoothing: antialiased; }
.header-container { max-width: 1200px; margin: 0 auto 30px auto; text-align: center; }
.nav-bar { margin-bottom: 25px; }
.nav-button { background: rgba(255, 255, 255, 0.05); color: #9ca3af; padding: 10px 20px; text-decoration: none; font-size: 0.88em; font-weight: 500; border-radius: 99px; border: 1px solid rgba(255, 255, 255, 0.1); transition: all 0.2s ease; display: inline-block; }
.nav-button:hover { background: rgba(255, 255, 255, 0.1); color: #ffffff; border-color: rgba(255, 255, 255, 0.2); }
h1 { font-size: 1.8em; font-weight: 700; letter-spacing: -0.02em; color: #ffffff; margin-bottom: 8px; }
.subtitle { font-size: 0.9em; color: #6b7280; font-weight: 400; }
.container { max-width: 1200px; margin: 0 auto; display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 20px; }
.match-card { background: #111827; border-radius: 16px; padding: 20px; border: 1px solid #1f2937; display: flex; flex-direction: column; transition: transform 0.2s ease, border-color 0.2s ease; }
.match-card:hover { border-color: #374151; transform: translateY(-2px); }
.match-header { display: flex; justify-content: space-between; font-size: 0.75em; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; margin-bottom: 12px; }
.match-title { font-size: 1.1em; font-weight: 600; color: #f9fafb; margin-bottom: 16px; text-align: center; line-height: 1.4; }
.consensus-badge { text-align: center; font-weight: 600; padding: 8px; border-radius: 8px; margin-bottom: 16px; font-size: 0.85em; letter-spacing: 0.02em; }
.cons-active { background: rgba(16, 185, 129, 0.1); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.2); }
.cons-none { background: rgba(107, 114, 128, 0.1); color: #9ca3af; border: 1px solid rgba(107, 114, 128, 0.2); }
.prediction-list { display: flex; flex-direction: column; gap: 8px; margin-top: auto; }
.prediction-badge { background: #1f2937; padding: 10px 14px; border-radius: 10px; font-size: 0.85em; display: flex; align-items: center; justify-content: space-between; border: 1px solid rgba(255, 255, 255, 0.03); }
.user-info { display: flex; align-items: center; gap: 8px; }
.top-rank { font-size: 0.7em; color: #9ca3af; background: #374151; padding: 2px 6px; border-radius: 4px; font-weight: 600; }
.user-name { font-weight: 500; color: #e5e7eb; max-width: 110px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stats-group { display: flex; gap: 6px; align-items: center; }
.badge-item { font-size: 0.7em; padding: 3px 7px; border-radius: 6px; font-weight: 600; }
.badge-racha { background: rgba(16, 185, 129, 0.15); color: #34d399; }
.badge-3d { background: rgba(245, 158, 11, 0.15); color: #fbbf24; }
.badge-7d { background: rgba(168, 85, 247, 0.15); color: #c084fc; }
.vote { font-weight: 700; padding: 4px 10px; border-radius: 6px; font-size: 0.8em; text-align: center; background: #374151; color: #f3f4f6; }
.empty-state { grid-column: 1 / -1; text-align: center; color: #4b5563; padding: 60px 0; font-size: 0.95em; }
"""

def calcular_consenso_ponderado(predicciones):
    """Calcula consenso por suma de pesos de usuarios y filtra muestras pequeñas."""
    if len(predicciones) < 3:
        return "MUESTRA INSUFICIENTE (<3 VOTOS)", "cons-none"
        
    pesos_votos = {}
    peso_total = 0.0
    
    for p in predicciones:
        voto = p['voto']
        peso = p['stats']['peso_usuario']
        pesos_votos[voto] = pesos_votos.get(voto, 0.0) + peso
        peso_total += peso

    if peso_total == 0:
        return "SIN CONSENSO", "cons-none"

    voto_ganador = max(pesos_votos, key=pesos_votos.get)
    porcentaje = int((pesos_votos[voto_ganador] / peso_total) * 100)

    if porcentaje >= 55:
        return f"🔥 {porcentaje}% CONSENSO: {voto_ganador} (N={len(predicciones)})", "cons-active"
    
    return "SIN CONSENSO CLARO", "cons-none"

def generar_vistas_html(partidos_top, partidos_rachas):
    """Genera vistas HTML a partir de datos procesados sin hacer llamadas extra a red."""
    
    # --- DASHBOARD TOP ---
    html_top = f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta http-equiv="refresh" content="300"><title>Radar Top - SofaScore</title><style>{CSS_COMMON}</style></head><body>
    <div class="header-container"><div class="nav-bar"><a href="rachas.html" class="nav-button">🔥 Ver Tipsters en Racha →</a></div><h1>Radar Top Mundial</h1><p class="subtitle">Consenso Ponderado por Confiabilidad</p></div><div class="container">"""

    if not partidos_top:
        html_top += "<div class='empty-state'>No hay predicciones futuras disponibles.</div>"
    else:
        for partido, datos in sorted(partidos_top.items(), key=lambda x: x[1]['info']['timestamp']):
            texto_cons, clase_cons = calcular_consenso_ponderado(datos['predicciones'])
            html_top += f"""<div class="match-card"><div class="match-header"><span>{datos['info']['torneo']}</span><span>{datos['info']['fecha']}</span></div><div class="match-title">{partido}</div><div class="consensus-badge {clase_cons}">{texto_cons}</div><div class="prediction-list">"""
            for p in datos['predicciones']:
                html_top += f"""<div class="prediction-badge"><div class="user-info"><span class="top-rank">T{p['top']}</span><span class="user-name" title="{p['usuario']}">{p['usuario']}</span></div><span class="vote">{p['voto']}</span></div>"""
            html_top += "</div></div>"
    html_top += "</div></body></html>"

    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(html_top)

    # --- DASHBOARD RACHAS ---
    html_rachas = f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta http-equiv="refresh" content="300"><title>Radar Rachas - SofaScore</title><style>{CSS_COMMON}</style></head><body>
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

    logging.info("Vistas HTML generadas exitosamente.")

# --- 4. PIPELINE Y PERSISTENCIA DE ESTADO ---

def ejecutar_pipeline():
    logging.info("Iniciando pipeline de actualización...")
    
    cache_eventos = cargar_cache_disco()
    data_ranking = realizar_peticion("https://www.sofascore.com/api/v1/user-account/vote-ranking")
    
    if not data_ranking:
        logging.error("No se obtuvo el ranking general de SofaScore. Abortando ciclo.")
        return

    top_usuarios = data_ranking.get('ranking', [])[:30]
    ahora_ts = datetime.datetime.now().timestamp()
    
    partidos_top = {}
    partidos_rachas = {}

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(procesar_usuario, item, posicion, ahora_ts, cache_eventos): (posicion, item) 
            for posicion, item in enumerate(top_usuarios, start=1)
        }
        
        for future in as_completed(futures):
            posicion, item = futures[future]
            user_id, preds, stats = future.result()
            
            if not user_id or not preds:
                continue

            for entry in preds:
                partido = entry["partido"]
                info = entry["info"]
                pred = entry["prediccion"]

                if posicion <= 10:
                    if partido not in partidos_top:
                        partidos_top[partido] = {"info": info, "predicciones": []}
                    partidos_top[partido]["predicciones"].append(pred)

                if stats["racha_activa"] >= 2 or stats["aciertos_7d"] >= 3:
                    if partido not in partidos_rachas:
                        partidos_rachas[partido] = {"info": info, "predicciones": []}
                    partidos_rachas[partido]["predicciones"].append(pred)

    # Persistir caché de eventos en disco
    guardar_cache_disco(cache_eventos)

    # Persistir estado intermedio en JSON (Separación Datos / Vista)
    estado = {"top": partidos_top, "rachas": partidos_rachas, "actualizado": ahora_ts}
    try:
        with open("state.json", "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Error al guardar state.json: {e}")

    # Renderizar HTML
    generar_vistas_html(partidos_top, partidos_rachas)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Actualiza los dashboards de SofaScore.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Ejecuta un ciclo y termina; útil para GitHub Actions.",
    )
    args = parser.parse_args()

    INTERVALO_MINUTOS = 240
    if args.once:
        ejecutar_pipeline()
    else:
        while True:
            ejecutar_pipeline()
            logging.info(f"Ciclo finalizado. Esperando {INTERVALO_MINUTOS} minutos...")
            time.sleep(INTERVALO_MINUTOS * 60)