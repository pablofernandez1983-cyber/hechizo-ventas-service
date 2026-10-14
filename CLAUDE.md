# Hechizo Ventas Service

API Flask que sincroniza órdenes de Tiendanube con Google Sheets para dashboard de ventas en tiempo real y como disparador del flujo KNIME (legacy).

## Stack

- Flask, Gunicorn (puerto 5000)
- Tiendanube API (últimos 8 días de órdenes)
- Google Sheets API (OAuth2, service account)
- Deploy: Railway

## Endpoints

| Ruta | Descripción |
|------|-------------|
| `GET /ventas` | Rápido (~1 s): hoy/ayer con una consulta liviana a TN (creados desde ayer, solo 6 campos); en background baja los últimos 3 días completos y sincroniza Sheets/Supabase |
| `GET /ventas/mes` | Lento (todo el mes): devuelve `pendientes_mes`. Se llama en paralelo con `/ventas`, no lo bloquea |
| `GET /ping` | Health check (única ruta abierta; el resto exige header `X-Web-Key` = `WEB_PASSWORD`) |

## Conexión con el ecosistema Hechizo

Escribe en el mismo Sheet que usa todo el ecosistema (`SHEET_ID_GASTOS`):
- Hoja `Ventas_diarias` → auditoría cruda de órdenes (formato KNIME)
- `/trigger` y `/trigger/status` (escribían `PENDIENTE` en `Trigger!A1` para KNIME) se eliminaron el 3/10/2026: eran públicos y nadie los usaba

**No se solapa con `hechizo-reporte-nuevo`**: ambos leen Tiendanube directamente, pero para propósitos distintos. Este es dashboard tiempo real (<1s), el reporte es contabilidad mensual (minutos).

## Variables de entorno

```
TIENDANUBE_STORE_ID
TIENDANUBE_ACCESS_TOKEN
GOOGLE_SERVICE_ACCOUNT_JSON   ← mismo service account que el resto de Hechizo
SHEET_ID
```
