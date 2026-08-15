"""
Hechizo Bijou - Servicio de Ventas v3
- GET /ventas     → rápido: trae los últimos días de Tiendanube, agrega las nuevas a
                     Ventas_diarias, upsertea a Supabase (tabla 'ventas'/'ventas_detalle')
                     y devuelve hoy/ayer. Sin pendientes_mes (ver /ventas/mes).
- GET /ventas/mes → lento: trae todo el mes de Tiendanube y devuelve pendientes_mes.
                     Pensado para llamarse en paralelo con /ventas, sin bloquearlo.
- GET /trigger → escribe PENDIENTE en Trigger!A1
- GET /trigger/status → lee estado del trigger
- GET /ping    → health check

Ventas_diarias mantiene el mismo formato que escribe KNIME (CSV con todas las columnas).
Railway solo agrega filas nuevas — no sobreescribe.

El upsert a Supabase usa la misma tabla/esquema que hechizo-reporte-nuevo (que la
llena vía su reporte completo). Es una escritura idempotente (ON CONFLICT DO UPDATE
por orden_id) tomada siempre de TiendaNube en vivo, así que no compite con el reporte
completo — el que corrió más reciente gana, y ambos reflejan la misma verdad. Existe
para que "ayer" no dependa de que alguien corra el reporte completo después de la
última venta del día.
"""

from flask import Flask, jsonify
from flask_cors import CORS
import requests
import os
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)

STORE_ID     = os.environ.get("TIENDANUBE_STORE_ID")
ACCESS_TOKEN = os.environ.get("TIENDANUBE_ACCESS_TOKEN")
BASE_URL     = f"https://api.tiendanube.com/v1/{STORE_ID}"
TN_HEADERS   = {
    "Authentication": f"bearer {ACCESS_TOKEN}",
    "User-Agent": "HechizoBijou/1.0 (hechizobijou@gmail.com)"
}

SHEET_ID     = os.environ.get("SHEET_ID", "1nUWfj9u0y7M7n2fNG6v55WnIxAedPpBxQZFUXk28nlI")
VENTAS_SHEET = "Ventas_diarias"
SA_JSON_STR  = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
TZ_AR        = timezone(timedelta(hours=-3))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Columnas que escribe KNIME — Railway usa el mismo orden
COLUMNAS = [
    "Número de orden", "Email", "Fecha", "Estado de la orden", "Estado del pago",
    "Estado del envío", "Moneda", "Subtotal de productos", "Descuento", "Costo de envío",
    "Total", "Nombre del comprador", "DNI / CUIT", "Teléfono", "Nombre para el envío",
    "Teléfono para el envío", "Dirección", "Número", "Piso", "Localidad", "Ciudad",
    "Código postal", "Provincia o estado", "País", "Medio de envío", "Medio de pago",
    "Cupón de descuento", "Notas del comprador", "Notas del vendedor", "Fecha de pago",
    "Fecha de envío", "Nombre del producto", "Precio del producto", "Cantidad del producto",
    "SKU", "Canal", "Código de tracking del envío",
    "Identificador de la transacción en el medio de pago",
    "Identificador de la orden", "Producto Físico", "Persona que registró la venta",
    "Sucursal de venta", "Vendedor", "Fecha y hora de cancelación", "Motivo de cancelación"
]

TRADUCCION_ESTADOS = {
    'open': 'Abierta', 'closed': 'Cerrada', 'cancelled': 'Cancelada',
    'pending': 'Pendiente', 'authorized': 'Autorizado', 'paid': 'Recibido',
    'voided': 'Anulado', 'refunded': 'Reembolsado', 'abandoned': 'Abandonado',
    'unpaid': 'No pago', 'unpacked': 'No está empaquetado', 'unfulfilled': 'Sin cumplir',
    'packed': 'Empaquetado', 'ready_for_pickup': 'Listo para retirar',
    'shipped': 'Enviado', 'delivered': 'Entregado', 'unshipped': 'Sin enviar',
}

def traducir(v):
    return TRADUCCION_ESTADOS.get(str(v).lower(), v) if v else ""

def get_svc():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(SA_JSON_STR),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def fmt_ar(dt_utc_str):
    """Convierte ISO UTC a string Argentina DD/MM/YYYY HH:MM:SS"""
    if not dt_utc_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_utc_str.replace("Z", "+00:00")).astimezone(TZ_AR)
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except:
        return str(dt_utc_str)

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
        coupons = o.get("coupon") or []
        coupon_codes = [c.get("code", "") or "" for c in coupons if isinstance(c, dict)]
        is_may = any("mayo" in c.lower() for c in coupon_codes)
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

def leer_ordenes_sheet(svc):
    """Lee Ventas_diarias y retorna (headers, set de números de orden existentes)"""
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{VENTAS_SHEET}!A1:A"
        ).execute()
        rows = res.get("values", [])
        if not rows:
            return [], set()
        # Primera fila es header
        ordenes = set()
        for row in rows[1:]:
            if row and str(row[0]).strip():
                ordenes.add(str(row[0]).strip())
        return rows[0], ordenes
    except Exception as e:
        print(f"[ERROR] leer_ordenes_sheet: {e}")
        return [], set()

def orden_to_rows(o):
    """Convierte una orden de Tiendanube a lista de filas (una por producto)"""
    products = o.get("products", [])
    if not products:
        products = [{}]

    numero_orden  = str(o.get("number", ""))
    email         = o.get("contact_email", "") or ""
    fecha         = fmt_ar(o.get("created_at", ""))
    estado_orden  = traducir(o.get("status", ""))
    estado_pago   = traducir(o.get("payment_status", ""))
    estado_envio  = traducir(o.get("shipping_status", ""))
    moneda        = o.get("currency", "ARS")
    subtotal      = str(o.get("subtotal", ""))
    descuento     = str(o.get("discount", "0"))
    costo_envio   = str(o.get("shipping_cost_owner", "0"))
    total         = str(o.get("total", ""))
    comprador     = (o.get("customer", {}) or {}).get("name", "")
    dni           = ""
    telefono      = o.get("contact_phone", "") or ""
    shipping      = o.get("shipping_address", {}) or {}
    nombre_envio  = shipping.get("name", "")
    tel_envio     = shipping.get("phone", "") or ""
    direccion     = shipping.get("address", "")
    numero_dir    = shipping.get("number", "")
    piso          = shipping.get("floor", "")
    localidad     = shipping.get("locality", "")
    ciudad        = shipping.get("city", "")
    cp            = shipping.get("zipcode", "")
    provincia     = shipping.get("province", "")
    pais          = shipping.get("country", "")
    medio_envio   = (o.get("shipping_option", "") or "")
    pagos         = o.get("payment_details", []) or []
    medio_pago    = pagos[0].get("payment_method_id", "") if pagos else ""
    cupon         = (o.get("coupon", []) or [{}])[0].get("code", "") if o.get("coupon") else ""
    notas_c       = o.get("note", "") or ""
    notas_v       = o.get("owner_note", "") or ""
    fecha_pago    = fmt_ar(o.get("paid_at", ""))
    fecha_envio   = fmt_ar(o.get("shipped_at", ""))
    canal         = o.get("storefront", "")
    tracking      = o.get("tracking_number", "") or ""
    transaction   = pagos[0].get("transaction_id", "") if pagos else ""
    order_id      = str(o.get("id", ""))
    fecha_cancel  = fmt_ar(o.get("cancelled_at", ""))
    motivo_cancel = o.get("cancel_reason", "") or ""

    filas = []
    for idx, prod in enumerate(products):
        if idx == 0:
            base = [
                numero_orden, email, fecha, estado_orden, estado_pago, estado_envio,
                moneda, subtotal, descuento, costo_envio, total, comprador, dni,
                telefono, nombre_envio, tel_envio, direccion, numero_dir, piso,
                localidad, ciudad, cp, provincia, pais, medio_envio, medio_pago,
                cupon, notas_c, notas_v, fecha_pago, fecha_envio
            ]
        else:
            base = [numero_orden, email] + [""] * 29

        nombre_prod   = prod.get("name", "") or ""
        precio_prod   = str(prod.get("price", ""))
        cantidad_prod = str(prod.get("quantity", ""))
        sku           = prod.get("sku", "") or ""

        fila = base + [
            nombre_prod, precio_prod, cantidad_prod, sku,
            canal if idx == 0 else "",
            tracking if idx == 0 else "",
            transaction if idx == 0 else "",
            order_id if idx == 0 else "",
            "", "", "", "",
            fecha_cancel if idx == 0 else "",
            motivo_cancel if idx == 0 else ""
        ]
        filas.append(fila)

    return filas

def agregar_ordenes_nuevas(svc, orders_tn, ordenes_existentes):
    """Agrega al Sheet solo las órdenes que no están todavía."""
    nuevas_filas = []
    for o in orders_tn:
        num = str(o.get("number", "")).strip()
        if not num or num in ordenes_existentes:
            continue
        nuevas_filas.extend(orden_to_rows(o))

    if not nuevas_filas:
        print("[INFO] No hay órdenes nuevas para agregar")
        return 0

    # Append al final del sheet
    svc.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{VENTAS_SHEET}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": nuevas_filas}
    ).execute()

    print(f"[INFO] Agregadas {len(nuevas_filas)} filas ({len(set(f[0] for f in nuevas_filas if f[0]))} órdenes nuevas)")
    return len(nuevas_filas)

def _persistir_orden_tn(orders_tn):
    """Guarda en Sheet y Supabase. Corre en background, después de responder /ventas."""
    try:
        svc = get_svc()
        _, ordenes_existentes = leer_ordenes_sheet(svc)
        agregar_ordenes_nuevas(svc, orders_tn, ordenes_existentes)
    except Exception as e:
        print(f"[WARN] No se pudo actualizar Sheet: {e}")

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
    """Rápido: solo hoy/ayer (3 días de margen por huso horario). Sin pendientes_mes
    — eso requiere traer todo el mes, que es lo lento. Ver /ventas/mes."""
    if not STORE_ID or not ACCESS_TOKEN:
        return jsonify({"ok": False, "error": "Credenciales TN no configuradas"}), 500
    if not SA_JSON_STR:
        return jsonify({"ok": False, "error": "Service account no configurada"}), 500

    ahora_ar       = datetime.now(TZ_AR)
    actualizado_en = ahora_ar.strftime("%Y-%m-%dT%H:%M:%S-03:00")

    # 1. Traer solo los últimos días de Tiendanube — alcanza para hoy/ayer
    orders_tn, tn_error = get_orders_tn(days=3)
    if tn_error:
        return jsonify({"ok": False, "error": "Error al conectar con TiendaNube"}), 502

    # 2. Persistir en Sheet y Supabase en background — no bloquea la respuesta.
    threading.Thread(target=_persistir_orden_tn, args=(orders_tn,), daemon=True).start()

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
    """Lento: trae todo el mes de Tiendanube para calcular pendientes_mes.
    Se llama en paralelo con /ventas pero no bloquea su respuesta."""
    if not STORE_ID or not ACCESS_TOKEN:
        return jsonify({"ok": False, "error": "Credenciales TN no configuradas"}), 500

    ahora_ar      = datetime.now(TZ_AR)
    dias_mes      = ahora_ar.day  # día del mes actual (1-31)
    orders_tn, tn_error = get_orders_tn(days=dias_mes + 1)
    if tn_error:
        return jsonify({"ok": False, "error": "Error al conectar con TiendaNube"}), 502

    threading.Thread(target=_persistir_orden_tn, args=(orders_tn,), daemon=True).start()

    _, _, pend_mes = calcular_pendientes(orders_tn)
    return jsonify({"ok": True, "pendientes_mes": {"cantidad": pend_mes["cantidad"]}})


@app.route("/trigger", methods=["POST"])
def trigger():
    try:
        svc = get_svc()
        svc.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range="Trigger!A1",
            valueInputOption="RAW", body={"values": [["PENDIENTE"]]}
        ).execute()
        return jsonify({"ok": True, "msg": "PENDIENTE escrito en Sheet"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/trigger/status")
def trigger_status():
    try:
        svc = get_svc()
        res = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range="Trigger!A1:A2"
        ).execute()
        rows = res.get("values", [])
        estado  = rows[0][0].strip() if rows and rows[0] else ""
        detalle = rows[1][0].strip() if len(rows) > 1 and rows[1] else ""
        return jsonify({"ok": True, "estado": estado, "detalle": detalle})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/ping")
def ping():
    return jsonify({"ok": True, "msg": "Hechizo ventas service v3 running"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
