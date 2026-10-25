"""
Hechizo Bijou - Servicio de Ventas v3
- GET /ventas     → rápido: devuelve hoy/ayer con una consulta liviana a Tiendanube y,
                     en background, upsertea los últimos días a Supabase
                     (tabla 'ventas'/'ventas_detalle'). Sin pendientes_mes (ver /ventas/mes).
- GET /ventas/mes → lento: trae todo el mes de Tiendanube y devuelve pendientes_mes.
                     Pensado para llamarse en paralelo con /ventas, sin bloquearlo.
- GET /ping    → health check

El upsert a Supabase usa la misma tabla/esquema que hechizo-reporte-nuevo (que la
llena vía su reporte completo). Es una escritura idempotente (ON CONFLICT DO UPDATE
por orden_id) tomada siempre de TiendaNube en vivo, así que no compite con el reporte
completo — el que corrió más reciente gana, y ambos reflejan la misma verdad. Existe
para que "ayer" no dependa de que alguien corra el reporte completo después de la
última venta del día.
"""

from flask import Flask, jsonify, request
import hmac
import time
from flask_cors import CORS
import requests
import os
import re
import threading
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
CORS(app)


# /ventas y /ventas/mes exigen la misma clave que el reporte (header X-Web-Key = WEB_PASSWORD).
# /ping queda abierta (despierta el servidor).
@app.before_request
def _auth():
    if request.method == "OPTIONS" or request.endpoint == "ping":
        return None
    password = os.environ.get("WEB_PASSWORD", "")
    if not password:
        return jsonify({"ok": False, "error": "WEB_PASSWORD no configurada"}), 503
    key = request.headers.get("X-Web-Key", "")
    if not hmac.compare_digest(key.encode(), password.encode()):
        time.sleep(0.5)
        return jsonify({"ok": False, "error": "Clave incorrecta"}), 401
    return None

STORE_ID     = os.environ.get("TIENDANUBE_STORE_ID")
ACCESS_TOKEN = os.environ.get("TIENDANUBE_ACCESS_TOKEN")
BASE_URL     = f"https://api.tiendanube.com/v1/{STORE_ID}"
TN_HEADERS   = {
    "Authentication": f"bearer {ACCESS_TOKEN}",
    "User-Agent": "HechizoBijou/1.0 (hechizobijou@gmail.com)"
}

TZ_AR        = timezone(timedelta(hours=-3))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def dia_ar(dt_utc_str):
    """Retorna YYYY-MM-DD en hora Argentina"""
    if not dt_utc_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_utc_str.replace("Z", "+00:00")).astimezone(TZ_AR)
        return dt.strftime("%Y-%m-%d")
    except:
        return ""

def safe_float(v):
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    is_neg = s.startswith('-')
    s = re.sub(r'[A-Za-z\$\s]', '', s)
    if is_neg and not s.startswith('-'):
        s = '-' + s
    s = s.replace('.', '').replace(',', '.') if s.count(',') == 1 and s.rfind(',') > s.rfind('.') else s.replace(',', '')
    try:
        return float(s)
    except:
        return 0.0


# Mayoristas: hasta 2025 entraban con cupón MAYORISTA*. Desde 2026 la tienda usa
# una "tabla de precios" de TN (25% off, mín. $200k y 15 u.) que la API no expone:
# el pedido llega sin cupón y con cada línea por debajo de compare_at_price.
# Se detecta por eso; el umbral de fecha evita confundir liquidaciones viejas
# (2022-24 tenían precios tachados en casi todo el catálogo).
MAY_TABLA_DESDE = "2026-01-01"

def es_mayorista(o):
    coupons = o.get("coupon") or []
    if any("mayo" in (c.get("code", "") or "").lower() for c in coupons if isinstance(c, dict)):
        return True
    if (o.get("created_at") or "") < MAY_TABLA_DESDE:
        return False
    prods = o.get("products") or []
    unidades = rebajadas = 0
    for p in prods:
        q = int(safe_float(p.get("quantity", 0)))
        unidades += q
        if safe_float(p.get("compare_at_price")) > safe_float(p.get("price")) + 0.5:
            rebajadas += q
    return unidades >= 15 and safe_float(o.get("subtotal")) >= 200000 and rebajadas >= 0.9 * unidades

def es_moto(medio_env):
    m = (medio_env or "").lower()
    return any(x in m for x in (
        "mot", "mensajer", "mile",
        "entrega rapida", "entrega rápida", "epick", "e-pick",
    ))

def guardar_ventas_supabase(orders):
    """Upsertea a Supabase (tablas 'ventas'/'ventas_detalle'), mismo esquema que usa
    hechizo-reporte-nuevo. Escritura idempotente — no rompe nada si se llama seguido
    ni si corre en paralelo con el reporte completo (mismos datos, misma clave)."""
    if not DATABASE_URL or not orders:
        return
    try:
        import psycopg2
        import psycopg2.extras
    except Exception as e:
        print(f"[WARN] psycopg2 no disponible: {e}")
        return

    rows_ventas, rows_detalle = [], []
    for o in orders:
        if o.get("status") == "cancelled": continue
        if o.get("payment_status") not in ("paid", "authorized"): continue
        orden_id = o.get("id")
        if not orden_id: continue
        try:
            dt = datetime.fromisoformat(
                o.get("created_at", "").replace("Z", "+00:00")
            ).astimezone(TZ_AR)
        except:
            continue

        shipping_cust = safe_float(o.get("shipping_cost_customer", 0))
        tracking  = str(o.get("shipping_tracking_number", "") or "").lower()
        medio_env = str(o.get("shipping_option", "") or "").lower()
        if "36000" in tracking:       carrier = "andreani"
        elif es_moto(medio_env):      carrier = "moto"
        elif "1978" in tracking:      carrier = "correo"
        else:                         carrier = "otro"
        is_may = es_mayorista(o)
        customer = o.get("customer") or {}
        rows_ventas.append((
            orden_id, dt.date(), dt.year, dt.month,
            customer.get("name", ""), customer.get("email", ""),
            safe_float(o.get("subtotal", 0)), safe_float(o.get("discount", 0)),
            shipping_cust, safe_float(o.get("total", 0)),
            "mayorista" if is_may else "minorista", carrier,
            o.get("payment_status", ""), o.get("shipping_status", ""),
            str(o.get("gateway_name", "") or o.get("gateway", "")),
            str(o.get("shipping_tracking_number", "") or ""),
        ))

        for p in (o.get("products") or []):
            variante_id = p.get("variant_id") or p.get("id")
            if not variante_id: continue
            variant_values  = p.get("variant_values") or []
            variante_nombre = " / ".join(
                str(v.get("es") or v.get("en") or v.get("pt") or (list(v.values())[0] if v else ""))
                for v in variant_values if isinstance(v, dict)
            )
            rows_detalle.append((
                orden_id, dt.date(), dt.year, dt.month,
                p.get("product_id"), variante_id,
                p.get("name", "") or "", variante_nombre,
                p.get("sku", "") or "", int(p.get("quantity") or 0),
                float(p.get("price") or 0),
            ))

    if not rows_ventas:
        return

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, """
                INSERT INTO ventas
                    (orden_id, fecha, anio, mes, cliente, email,
                     subtotal, descuento, envio_cobrado, total,
                     tipo, carrier, estado_pago, estado_envio, medio_pago, tracking)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (orden_id) DO UPDATE SET
                    subtotal=EXCLUDED.subtotal, descuento=EXCLUDED.descuento,
                    envio_cobrado=EXCLUDED.envio_cobrado, total=EXCLUDED.total,
                    tipo=EXCLUDED.tipo, carrier=EXCLUDED.carrier,
                    estado_pago=EXCLUDED.estado_pago, estado_envio=EXCLUDED.estado_envio,
                    tracking=EXCLUDED.tracking
            """, rows_ventas, page_size=200)
            if rows_detalle:
                psycopg2.extras.execute_batch(cur, """
                    INSERT INTO ventas_detalle
                        (orden_id, fecha, anio, mes, producto_id, variante_id,
                         producto_nombre, variante_nombre, sku, cantidad, precio_unitario)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (orden_id, variante_id) DO NOTHING
                """, rows_detalle, page_size=200)
        conn.commit()
        print(f"[INFO] Supabase: {len(rows_ventas)} ventas, {len(rows_detalle)} detalle upserteadas")
    except Exception as e:
        print(f"[WARN] guardar_ventas_supabase: {e}")
        if conn:
            try: conn.rollback()
            except: pass
    finally:
        if conn:
            try: conn.close()
            except: pass

def get_orders_tn(days=8):
    desde = (datetime.now(TZ_AR) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00-03:00")
    orders, page = [], 1
    tn_error = False
    while True:
        try:
            r = requests.get(f"{BASE_URL}/orders", headers=TN_HEADERS,
                           params={"page": page, "per_page": 200, "updated_at_min": desde}, timeout=20)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"[ERROR] TN página {page}: {e}")
            if page == 1:
                tn_error = True  # Sin datos en absoluto
            break
        if not batch: break
        orders.extend(batch)
        if len(batch) < 200: break
        page += 1
    return orders, tn_error

# Para hoy/ayer alcanza con lo creado desde ayer y estos campos: ~20 KB contra
# ~1,3 MB de los pedidos completos (que traen productos, direcciones, etc.).
CAMPOS_RESUMEN = "id,number,status,payment_status,created_at,total"

def get_orders_resumen_tn(dias=1):
    desde = (datetime.now(TZ_AR) - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00-03:00")
    orders, page = [], 1
    while True:
        try:
            r = requests.get(f"{BASE_URL}/orders", headers=TN_HEADERS,
                             params={"page": page, "per_page": 200, "created_at_min": desde,
                                     "fields": CAMPOS_RESUMEN}, timeout=20)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"[ERROR] TN resumen página {page}: {e}")
            return orders, page == 1
        if not batch: break
        orders.extend(batch)
        if len(batch) < 200: break
        page += 1
    return orders, False

def _sincronizar_tn_background():
    """Baja los pedidos completos de los últimos días y los persiste en Supabase."""
    orders_tn, tn_error = get_orders_tn(days=3)
    if not tn_error:
        _persistir_orden_tn(orders_tn)

def _persistir_orden_tn(orders_tn):
    """Guarda en Supabase. Corre en background, después de responder /ventas."""
    try:
        guardar_ventas_supabase(orders_tn)
    except Exception as e:
        print(f"[WARN] No se pudo sincronizar Supabase: {e}")

def calcular_pendientes(orders_tn):
    """Calcula órdenes pendientes de pago (pending/unpaid) para hoy, ayer y el mes actual."""
    ahora_ar   = datetime.now(TZ_AR)
    hoy_str    = ahora_ar.strftime("%Y-%m-%d")
    ayer_str   = (ahora_ar - timedelta(days=1)).strftime("%Y-%m-%d")
    mes_prefix = ahora_ar.strftime("%Y-%m")

    pend_hoy  = {"total": 0.0, "cantidad": 0}
    pend_ayer = {"total": 0.0, "cantidad": 0}
    pend_mes  = {"cantidad": 0}

    for o in orders_tn:
        if o.get("status") == "cancelled": continue
        if o.get("payment_status") not in ("pending", "unpaid"): continue
        dia = dia_ar(o.get("created_at", ""))
        if not dia: continue
        try:
            total = float(o.get("total", 0))
        except:
            total = 0.0
        if dia == hoy_str:
            pend_hoy["total"]    += total
            pend_hoy["cantidad"] += 1
        if dia == ayer_str:
            pend_ayer["total"]    += total
            pend_ayer["cantidad"] += 1
        if dia.startswith(mes_prefix):
            pend_mes["cantidad"] += 1

    return pend_hoy, pend_ayer, pend_mes


def calcular_resumen(orders_tn):
    """Calcula hoy/ayer a partir de las órdenes de Tiendanube (fuente de verdad).
    Solo cuenta órdenes con payment_status paid o authorized, igual que el reporte general."""
    ahora_ar = datetime.now(TZ_AR)
    hoy_str  = ahora_ar.strftime("%Y-%m-%d")
    ayer_str = (ahora_ar - timedelta(days=1)).strftime("%Y-%m-%d")
    acum = {hoy_str: {"total": 0.0, "cantidad": 0}, ayer_str: {"total": 0.0, "cantidad": 0}}

    for o in orders_tn:
        if o.get("payment_status") not in ("paid", "authorized"): continue
        dia = dia_ar(o.get("created_at", ""))
        if dia not in acum: continue
        try:
            acum[dia]["total"]    += float(o.get("total", 0))
            acum[dia]["cantidad"] += 1
        except: pass

    return acum, hoy_str, ayer_str


@app.route("/ventas")
def ventas():
    """Rápido: solo hoy/ayer (pedidos creados desde ayer, campos mínimos). Sin pendientes_mes
    — eso requiere traer todo el mes, que es lo lento. Ver /ventas/mes."""
    if not STORE_ID or not ACCESS_TOKEN:
        return jsonify({"ok": False, "error": "Credenciales TN no configuradas"}), 500

    ahora_ar       = datetime.now(TZ_AR)
    actualizado_en = ahora_ar.strftime("%Y-%m-%dT%H:%M:%S-03:00")

    # 1. Traer solo lo creado desde ayer, con los campos mínimos — alcanza para hoy/ayer
    orders_tn, tn_error = get_orders_resumen_tn()
    if tn_error:
        return jsonify({"ok": False, "error": "Error al conectar con TiendaNube"}), 502

    # 2. Bajar los pedidos completos y persistir en Supabase en background.
    threading.Thread(target=_sincronizar_tn_background, daemon=True).start()

    # 3. Calcular resumen hoy/ayer y pendientes de esos mismos días
    acum, hoy_str, ayer_str = calcular_resumen(orders_tn)
    hoy_data  = acum[hoy_str]
    ayer_data = acum[ayer_str]
    pend_hoy, pend_ayer, _ = calcular_pendientes(orders_tn)  # pend_mes acá sería incompleto

    variacion = None
    if ayer_data["total"] > 0:
        variacion = round((hoy_data["total"] - ayer_data["total"]) / ayer_data["total"] * 100, 1)

    return jsonify({
        "ok": True,
        "actualizadoEn": actualizado_en,
        "hoy":  {"fecha": hoy_str,  "total": round(hoy_data["total"], 2),  "cantidad": hoy_data["cantidad"]},
        "ayer": {"fecha": ayer_str, "total": round(ayer_data["total"], 2), "cantidad": ayer_data["cantidad"]},
        "pendientes_hoy":  {"total": round(pend_hoy["total"],  2), "cantidad": pend_hoy["cantidad"]},
        "pendientes_ayer": {"total": round(pend_ayer["total"], 2), "cantidad": pend_ayer["cantidad"]},
        "variacion": variacion,
    })


@app.route("/ventas/mes")
def ventas_mes():
    """pendientes_mes: pedidos creados este mes, solo los campos mínimos (antes bajaba
    los pedidos completos y tardaba 6-11 s). No persiste nada: /ventas ya sincroniza
    a Supabase todo lo que cambió en los últimos 3 días."""
    if not STORE_ID or not ACCESS_TOKEN:
        return jsonify({"ok": False, "error": "Credenciales TN no configuradas"}), 500

    orders_tn, tn_error = get_orders_resumen_tn(dias=datetime.now(TZ_AR).day - 1)
    if tn_error:
        return jsonify({"ok": False, "error": "Error al conectar con TiendaNube"}), 502

    _, _, pend_mes = calcular_pendientes(orders_tn)
    return jsonify({"ok": True, "pendientes_mes": {"cantidad": pend_mes["cantidad"]}})


# ── Pendientes de pago (pantalla para reclamarlos por WhatsApp) ──────────────
# Lo que se descartó o ya se reclamó vive en Supabase (tabla pendientes_pago) para
# que se vea igual desde cualquier celu.

def whatsapp_ar(tel):
    """Teléfono de TN → número para wa.me. Los celulares de Argentina necesitan el 9
    después del 54 (TN suele guardarlos como +54 11 ...)."""
    d = re.sub(r"\D", "", tel or "")
    if not d:
        return ""
    if d.startswith("0"):
        d = d.lstrip("0")
    if not d.startswith("54"):
        d = "54" + d
    if not d.startswith("549"):
        d = "549" + d[2:]
    return d

def medio_pendiente(o):
    nombre = (o.get("gateway_name") or "").strip()
    n = nombre.lower()
    if "transfer" in n:
        tipo = "transferencia"
    elif "efectivo" in n:
        tipo = "efectivo"
    else:
        tipo = "otro"
    return tipo, nombre

def _db():
    import psycopg2
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)

@app.route("/pendientes")
def pendientes():
    """Pedidos abiertos con pago pendiente de los últimos 30 días, con lo necesario para
    escribirles por WhatsApp y el estado guardado (descartado / reclamado_en)."""
    if not STORE_ID or not ACCESS_TOKEN:
        return jsonify({"ok": False, "error": "Credenciales TN no configuradas"}), 500

    desde = (datetime.now(TZ_AR) - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00-03:00")
    orders, page = [], 1
    while True:
        try:
            r = requests.get(f"{BASE_URL}/orders", headers=TN_HEADERS,
                             params={"page": page, "per_page": 200, "status": "open",
                                     "payment_status": "pending", "created_at_min": desde}, timeout=20)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"[ERROR] TN pendientes página {page}: {e}")
            return jsonify({"ok": False, "error": "Error al conectar con TiendaNube"}), 502
        if not batch: break
        orders.extend(batch)
        if len(batch) < 200: break
        page += 1

    estado = {}
    if DATABASE_URL and orders:
        conn = None
        try:
            conn = _db()
            with conn.cursor() as cur:
                cur.execute("SELECT orden_id, descartado, reclamado_en FROM pendientes_pago WHERE orden_id = ANY(%s)",
                            ([int(o["id"]) for o in orders],))
                for oid, desc, recl in cur.fetchall():
                    estado[oid] = (desc, recl.isoformat() if recl else None)
        except Exception as e:
            print(f"[WARN] pendientes_pago: {e}")
        finally:
            if conn: conn.close()

    lista = []
    for o in orders:
        cliente = o.get("customer") or {}
        tel = o.get("contact_phone") or cliente.get("phone") or ""
        tipo, medio = medio_pendiente(o)
        desc, recl = estado.get(int(o["id"]), (False, None))
        lista.append({
            "id": o["id"],
            "numero": o.get("number"),
            "creado": o.get("created_at"),
            "total": safe_float(o.get("total")),
            "nombre": (o.get("contact_name") or cliente.get("name") or "").strip(),
            "telefono": tel,
            "whatsapp": whatsapp_ar(tel),
            "medio": medio,
            "medio_tipo": tipo,
            # unpacked = sin empaquetar; unshipped = empaquetado (en efectivo: listo para retirar)
            "preparacion": o.get("shipping_status"),
            "productos": [p.get("name") for p in o.get("products") or []],
            "descartado": desc,
            "reclamado_en": recl,
        })
    lista.sort(key=lambda x: x["creado"] or "", reverse=True)
    return jsonify({"ok": True, "pendientes": lista})

@app.route("/pendientes/<int:orden_id>", methods=["POST"])
def pendiente_accion(orden_id):
    """accion: descartar | restaurar | reclamado | no_reclamado"""
    body = request.get_json(silent=True) or {}
    accion = body.get("accion")
    sets = {
        "descartar": "descartado = true",
        "restaurar": "descartado = false",
        "reclamado": "reclamado_en = now()",
        "no_reclamado": "reclamado_en = null",
    }.get(accion)
    if not sets:
        return jsonify({"ok": False, "error": "accion inválida"}), 400
    if not DATABASE_URL:
        return jsonify({"ok": False, "error": "Sin base de datos"}), 500
    conn = None
    try:
        conn = _db()
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pendientes_pago (orden_id, numero) VALUES (%s, %s) "
                "ON CONFLICT (orden_id) DO NOTHING", (orden_id, body.get("numero")))
            cur.execute(f"UPDATE pendientes_pago SET {sets}, actualizado = now() WHERE orden_id = %s",
                        (orden_id,))
    except Exception as e:
        print(f"[ERROR] pendientes_pago {orden_id}: {e}")
        return jsonify({"ok": False, "error": "No se pudo guardar"}), 500
    finally:
        if conn: conn.close()
    return jsonify({"ok": True})

@app.route("/pendientes/<int:orden_id>/cancelar", methods=["POST"])
def pendiente_cancelar(orden_id):
    """Cancela en Tiendanube un pedido que sigue abierto y sin pagar (vuelve el stock, sin
    mail a la clienta). La app pide confirmar dos veces antes de llamar acá."""
    if not STORE_ID or not ACCESS_TOKEN:
        return jsonify({"ok": False, "error": "Credenciales TN no configuradas"}), 500
    try:
        r = requests.get(f"{BASE_URL}/orders/{orden_id}", headers=TN_HEADERS, timeout=20)
        r.raise_for_status()
        o = r.json()
    except Exception as e:
        print(f"[ERROR] TN orden {orden_id}: {e}")
        return jsonify({"ok": False, "error": "No se pudo leer el pedido en Tiendanube"}), 502
    # Por seguridad solo se cancela lo que sigue pendiente: si mientras tanto pagó, no se toca
    if o.get("status") != "open" or o.get("payment_status") != "pending":
        return jsonify({"ok": False, "error": f"El pedido ya no está pendiente ({o.get('status')}/{o.get('payment_status')})"}), 409

    r = requests.post(f"{BASE_URL}/orders/{orden_id}/cancel", headers=TN_HEADERS,
                      json={"reason": "customer", "email": False, "restock": True}, timeout=30)
    if r.status_code >= 400:
        print(f"[ERROR] TN cancelar {orden_id}: {r.status_code} {r.text[:300]}")
        if r.status_code in (401, 403):
            return jsonify({"ok": False, "error": "La clave de Tiendanube no tiene permiso para cancelar pedidos"}), 502
        return jsonify({"ok": False, "error": f"Tiendanube rechazó la cancelación ({r.status_code})"}), 502
    print(f"[PENDIENTES] Cancelado pedido #{o.get('number')} ({orden_id})")

    if DATABASE_URL:
        conn = None
        try:
            conn = _db()
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pendientes_pago (orden_id, numero, cancelado_en) VALUES (%s, %s, now()) "
                    "ON CONFLICT (orden_id) DO UPDATE SET cancelado_en = now(), actualizado = now()",
                    (orden_id, o.get("number")))
        except Exception as e:
            print(f"[WARN] pendientes_pago cancelado {orden_id}: {e}")
        finally:
            if conn: conn.close()
    return jsonify({"ok": True})


@app.route("/ping")
def ping():
    return jsonify({"ok": True, "msg": "Hechizo ventas service v3 running"})


# ── Actualizaciones automáticas (hora Argentina) ─────────────────────────────
# Este servicio está siempre prendido, así que hace de reloj para todo Hechizo.
# El reporte (hechizo-reporte-nuevo) se duerme por inactividad: la llamada lo despierta.
REPORTE_URL = os.environ.get("REPORTE_URL", "https://hechizo-reporte-nuevo-production.up.railway.app")
HORARIO = {
    8:  ["ventas"],
    10: ["resultados"],
    11: ["ventas"],
    13: ["ventas"],
    18: ["ventas"],
    19: ["stock", "resultados"],   # stock primero: tarda ~25 s y de paso despierta al reporte
    22: ["ventas"],
}
VENTANA_MIN = 15  # si el servicio reinicia a las 8:05, igual corre lo de las 8

def _llamar_reporte(metodo, ruta, timeout):
    headers = {"X-Web-Key": os.environ.get("WEB_PASSWORD", "")}
    for intento in range(3):  # el primer intento puede cortarse mientras despierta
        try:
            r = requests.request(metodo, REPORTE_URL + ruta, headers=headers, timeout=timeout)
            print(f"[HORARIO] {metodo} {ruta} -> {r.status_code}")
            if r.status_code < 500:
                return
        except Exception as e:
            print(f"[HORARIO] {metodo} {ruta} intento {intento + 1}: {e}")
        time.sleep(30)

TAREAS = {
    "ventas":     _sincronizar_tn_background,
    "stock":      lambda: _llamar_reporte("GET", "/stock-data?refresh=1", 240),
    "resultados": lambda: _llamar_reporte("POST", "/ejecutar", 120),  # 409 = ya estaba corriendo
}

def _horario_loop():
    hechos = set()
    while True:
        ahora = datetime.now(TZ_AR)
        clave = (ahora.date(), ahora.hour)
        if ahora.hour in HORARIO and ahora.minute < VENTANA_MIN and clave not in hechos:
            hechos.add(clave)
            for tarea in HORARIO[ahora.hour]:
                print(f"[HORARIO] {ahora:%d/%m %H:%M} {tarea}")
                try:
                    TAREAS[tarea]()
                except Exception as e:
                    print(f"[HORARIO] {tarea} falló: {e}")
        time.sleep(60)

# Solo en Railway (no al importar app.py para probar local). Gunicorn corre 1 worker.
if os.environ.get("RAILWAY_ENVIRONMENT"):
    threading.Thread(target=_horario_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
