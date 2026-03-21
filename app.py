"""
Hechizo Bijou - Servicio de Ventas
Corre en Railway, expone GET /ventas
Consulta Tiendanube, escribe en Google Sheets y devuelve JSON a la PWA.

Variables de entorno necesarias:
  TIENDANUBE_STORE_ID
  TIENDANUBE_ACCESS_TOKEN
  GOOGLE_SERVICE_ACCOUNT_JSON   (contenido del JSON de service account, en una sola linea)
  SHEET_ID                      (ID del Google Sheet)
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

# ── Tiendanube ──
STORE_ID     = os.environ.get("TIENDANUBE_STORE_ID")
ACCESS_TOKEN = os.environ.get("TIENDANUBE_ACCESS_TOKEN")
BASE_URL     = f"https://api.tiendanube.com/v1/{STORE_ID}"
TN_HEADERS   = {
    "Authentication": f"bearer {ACCESS_TOKEN}",
    "User-Agent": "HechizoBijou/1.0 (hechizobijou@gmail.com)"
}

# ── Google Sheets ──
SHEET_ID         = os.environ.get("SHEET_ID", "1nUWfj9u0y7M7n2fNG6v55WnIxAedPpBxQZFUXk28nlI")
VENTAS_SHEET     = "Ventas_diarias"
SA_JSON_STR      = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# Zona horaria Argentina UTC-3
TZ_AR = timezone(timedelta(hours=-3))


def get_sheets_service():
    sa_info = json.loads(SA_JSON_STR)
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_orders_tiendanube(updated_at_min: str) -> list:
    orders = []
    page = 1
    while True:
        params = {"page": page, "per_page": 200, "updated_at_min": updated_at_min}
        try:
            r = requests.get(f"{BASE_URL}/orders", headers=TN_HEADERS, params=params, timeout=20)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"[ERROR] Tiendanube página {page}: {e}")
            break
        if not batch:
            break
        orders.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return orders


def agrupar_por_dia(orders: list, dias: int = 7) -> list:
    """Agrupa pedidos por día, excluye cancelados, devuelve lista ordenada desc."""
    ahora_ar = datetime.now(TZ_AR)
    pad = lambda n: str(n).padStart if False else str(n).zfill(2)

    acum = {}
    for i in range(dias):
        d = ahora_ar - timedelta(days=i)
        key = f"{d.year}-{str(d.month).zfill(2)}-{str(d.day).zfill(2)}"
        acum[key] = {"total": 0.0, "cantidad": 0}

    for o in orders:
        if o.get("status") == "cancelled":
            continue
        payment = o.get("payment_status", "")
        if payment in ("voided", "refunded"):
            continue

        raw = o.get("created_at", "")
        if not raw:
            continue
        try:
            dt_utc = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            dt_ar  = dt_utc.astimezone(TZ_AR)
            dia    = f"{dt_ar.year}-{str(dt_ar.month).zfill(2)}-{str(dt_ar.day).zfill(2)}"
        except Exception:
            continue

        if dia not in acum:
            continue
        try:
            acum[dia]["total"]    += float(o.get("total", 0))
            acum[dia]["cantidad"] += 1
        except (ValueError, TypeError):
            pass

    return sorted(
        [{"fecha": k, "total": round(v["total"], 2), "cantidad": v["cantidad"]} for k, v in acum.items()],
        key=lambda x: x["fecha"],
        reverse=True
    )


def escribir_en_sheet(dias_data: list, actualizado_en: str):
    """Sobreescribe la hoja Ventas_diarias con los datos frescos."""
    svc = get_sheets_service()
    sheet = svc.spreadsheets()

    # Header + filas
    rows = [["Fecha", "Total", "Cantidad", "Actualizado"]]
    for i, d in enumerate(dias_data):
        rows.append([
            d["fecha"],
            d["total"],
            d["cantidad"],
            actualizado_en if i == 0 else ""
        ])

    # Limpiar rango y escribir
    rng = f"{VENTAS_SHEET}!A1:D{len(rows) + 1}"
    sheet.values().clear(
        spreadsheetId=SHEET_ID,
        range=rng
    ).execute()

    sheet.values().update(
        spreadsheetId=SHEET_ID,
        range=f"{VENTAS_SHEET}!A1",
        valueInputOption="RAW",
        body={"values": rows}
    ).execute()


@app.route("/ventas")
def ventas():
    if not STORE_ID or not ACCESS_TOKEN:
        return jsonify({"ok": False, "error": "Credenciales Tiendanube no configuradas"}), 500
    if not SA_JSON_STR:
        return jsonify({"ok": False, "error": "Service account no configurada"}), 500

    ahora_ar = datetime.now(TZ_AR)
    actualizado_en = ahora_ar.strftime("%Y-%m-%dT%H:%M:%S-03:00")

    # Traer últimos 8 días de Tiendanube
    desde = (ahora_ar - timedelta(days=8)).strftime("%Y-%m-%dT00:00:00-03:00")
    orders = get_orders_tiendanube(desde)

    # Agrupar por día
    dias_data = agrupar_por_dia(orders, dias=7)

    # Escribir en el Sheet
    try:
        escribir_en_sheet(dias_data, actualizado_en)
    except Exception as e:
        print(f"[WARN] No se pudo escribir en Sheet: {e}")

    # Preparar respuesta hoy/ayer
    hoy_str  = ahora_ar.strftime("%Y-%m-%d")
    ayer_str = (ahora_ar - timedelta(days=1)).strftime("%Y-%m-%d")

    hoy_data  = next((d for d in dias_data if d["fecha"] == hoy_str),  {"total": 0, "cantidad": 0})
    ayer_data = next((d for d in dias_data if d["fecha"] == ayer_str), {"total": 0, "cantidad": 0})

    variacion = None
    if ayer_data["total"] > 0:
        variacion = round((hoy_data["total"] - ayer_data["total"]) / ayer_data["total"] * 100, 1)

    return jsonify({
        "ok": True,
        "actualizadoEn": actualizado_en,
        "hoy":  {"fecha": hoy_str,  "total": hoy_data["total"],  "cantidad": hoy_data["cantidad"]},
        "ayer": {"fecha": ayer_str, "total": ayer_data["total"], "cantidad": ayer_data["cantidad"]},
        "variacion": variacion,
        "ultimos7dias": dias_data
    })


@app.route("/ping")
def ping():
    return jsonify({"ok": True, "msg": "Hechizo ventas service running"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
