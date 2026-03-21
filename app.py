"""
Hechizo Bijou - Servicio de Ventas v3
- GET /ventas  → trae ordenes de Tiendanube, agrega las nuevas a Ventas_diarias, devuelve hoy/ayer
- GET /trigger → escribe PENDIENTE en Trigger!A1
- GET /trigger/status → lee estado del trigger
- GET /ping    → health check

Ventas_diarias mantiene el mismo formato que escribe KNIME (CSV con todas las columnas).
Railway solo agrega filas nuevas — no sobreescribe.
"""

from flask import Flask, jsonify
from flask_cors import CORS
import requests
import os
import json
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

def get_orders_tn(days=8):
    desde = (datetime.now(TZ_AR) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00-03:00")
    orders, page = [], 1
    while True:
        try:
            r = requests.get(f"{BASE_URL}/orders", headers=TN_HEADERS,
                           params={"page": page, "per_page": 200, "updated_at_min": desde}, timeout=20)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"[ERROR] TN página {page}: {e}")
            break
        if not batch: break
        orders.extend(batch)
        if len(batch) < 200: break
        page += 1
    return orders

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

def calcular_resumen(orders_tn):
    """Calcula hoy/ayer a partir de las órdenes de Tiendanube (fuente de verdad)."""
    ahora_ar = datetime.now(TZ_AR)
    hoy_str  = ahora_ar.strftime("%Y-%m-%d")
    ayer_str = (ahora_ar - timedelta(days=1)).strftime("%Y-%m-%d")
    acum = {hoy_str: {"total": 0.0, "cantidad": 0}, ayer_str: {"total": 0.0, "cantidad": 0}}

    for o in orders_tn:
        if o.get("status") == "cancelled": continue
        if o.get("payment_status") in ("voided", "refunded"): continue
        dia = dia_ar(o.get("created_at", ""))
        if dia not in acum: continue
        try:
            acum[dia]["total"]    += float(o.get("total", 0))
            acum[dia]["cantidad"] += 1
        except: pass

    return acum, hoy_str, ayer_str


@app.route("/ventas")
def ventas():
    if not STORE_ID or not ACCESS_TOKEN:
        return jsonify({"ok": False, "error": "Credenciales TN no configuradas"}), 500
    if not SA_JSON_STR:
        return jsonify({"ok": False, "error": "Service account no configurada"}), 500

    ahora_ar       = datetime.now(TZ_AR)
    actualizado_en = ahora_ar.strftime("%Y-%m-%dT%H:%M:%S-03:00")

    # 1. Traer órdenes de Tiendanube (últimos 8 días)
    orders_tn = get_orders_tn(days=8)

    # 2. Leer órdenes existentes en Sheet
    try:
        svc = get_svc()
        _, ordenes_existentes = leer_ordenes_sheet(svc)
        agregar_ordenes_nuevas(svc, orders_tn, ordenes_existentes)
    except Exception as e:
        print(f"[WARN] No se pudo actualizar Sheet: {e}")

    # 3. Calcular resumen hoy/ayer
    acum, hoy_str, ayer_str = calcular_resumen(orders_tn)
    hoy_data  = acum[hoy_str]
    ayer_data = acum[ayer_str]

    variacion = None
    if ayer_data["total"] > 0:
        variacion = round((hoy_data["total"] - ayer_data["total"]) / ayer_data["total"] * 100, 1)

    return jsonify({
        "ok": True,
        "actualizadoEn": actualizado_en,
        "hoy":  {"fecha": hoy_str,  "total": round(hoy_data["total"], 2),  "cantidad": hoy_data["cantidad"]},
        "ayer": {"fecha": ayer_str, "total": round(ayer_data["total"], 2), "cantidad": ayer_data["cantidad"]},
        "variacion": variacion,
    })


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
