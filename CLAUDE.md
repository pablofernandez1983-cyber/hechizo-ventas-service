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
| `GET /ventas` | Rápido (últimos 3 días): sincroniza TN → Sheets/Supabase en background y devuelve hoy/ayer |
| `GET /ventas/mes` | Lento (todo el mes): devuelve `pendientes_mes`. Se llama en paralelo con `/ventas`, no lo bloquea |
| `GET /trigger` | Escribe estado en celda Trigger |
| `GET /trigger/status` | Lee estado del Trigger |
| `GET /ping` | Health check |

## Conexión con el ecosistema Hechizo

Escribe en el mismo Sheet que usa todo el ecosistema (`SHEET_ID_GASTOS`):
- Hoja `Ventas_diarias` → auditoría cruda de órdenes (formato KNIME)
- Celda `Trigger!A1` → escribe `PENDIENTE` para disparar KNIME (legacy, ya no se usa activamente)

**No se solapa con `hechizo-reporte-nuevo`**: ambos leen Tiendanube directamente, pero para propósitos distintos. Este es dashboard tiempo real (<1s), el reporte es contabilidad mensual (minutos).

## Variables de entorno

```
TIENDANUBE_STORE_ID
TIENDANUBE_ACCESS_TOKEN
GOOGLE_SERVICE_ACCOUNT_JSON   ← mismo service account que el resto de Hechizo
SHEET_ID
```
