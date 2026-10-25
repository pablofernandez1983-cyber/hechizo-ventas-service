# Hechizo Ventas Service

API Flask que devuelve las ventas de hoy/ayer de Tiendanube en tiempo real para la app de Hechizo, y sincroniza los pedidos de los últimos días a Supabase.

## Stack

- Flask, Gunicorn (puerto 5000)
- Tiendanube API
- Supabase (Postgres, `DATABASE_URL`)
- Deploy: Railway (proyecto `hechizo-ventas-tn`), **siempre prendido** (`sleepApplication=false`) para que "ventas de hoy" cargue al instante

## Endpoints

| Ruta | Descripción |
|------|-------------|
| `GET /ventas` | Rápido (~1 s): hoy/ayer con una consulta liviana a TN (creados desde ayer, solo 6 campos); en background baja los últimos 3 días completos y los upsertea a Supabase |
| `GET /ventas/mes` | Lento (todo el mes): devuelve `pendientes_mes`. Se llama en paralelo con `/ventas`, no lo bloquea |
| `GET /pendientes` | Pedidos abiertos con pago pendiente de los últimos 30 días (TN filtra `payment_status=pending`), con teléfono normalizado para wa.me (549…) y el estado de la tabla `pendientes_pago` |
| `POST /pendientes/<orden_id>` | `{accion: descartar\|restaurar\|reclamado, numero}` → upsert en `pendientes_pago` (Supabase). Lo usa la pantalla "Pendientes de pago" de la app |
| `POST /pendientes/<orden_id>/cancelar` | Cancela en TN (`reason=customer`, `restock=true`, sin mail) **solo si sigue open + pending** (si no, 409); guarda `cancelado_en`. La app pide confirmar dos veces |
| `GET /ping` | Health check (única ruta abierta; el resto exige header `X-Web-Key` = `WEB_PASSWORD`) |

## Actualizaciones automáticas (`_horario_loop`, hora Argentina)

Como es el único servicio siempre prendido, hace de reloj de todo Hechizo (hilo dentro del proceso, solo en Railway):

| Hora | Qué hace |
|------|----------|
| 8, 11, 13, 18, 22 | Sincroniza los pedidos de TN de los últimos 3 días a Supabase |
| 10 | `POST /ejecutar` en el reporte (resultados completos) |
| 19 | `GET /stock-data?refresh=1` (recalcula y guarda el stock en S3) y después `POST /ejecutar` |

El reporte se duerme por inactividad; la llamada lo despierta (3 intentos con 30 s entre medio). Además, la app pide `/ventas` cada vez que se abre.

## Conexión con el ecosistema Hechizo

- Escribe en Supabase las tablas `ventas`/`ventas_detalle`, igual que `hechizo-reporte-nuevo` (upsert idempotente por `orden_id`).
- Ya no escribe en Google Sheets: la hoja `Ventas_diarias` (auditoría del formato KNIME, con datos personales de clientes) se dejó de llenar el 14/10/2026 porque nada la leía. El `index.html` viejo (dashboard con fallback a esa hoja) también se eliminó.
- `/trigger` y `/trigger/status` (escribían `PENDIENTE` en `Trigger!A1` para KNIME) se eliminaron el 3/10/2026: eran públicos y nadie los usaba

**No se solapa con `hechizo-reporte-nuevo`**: ambos leen Tiendanube directamente, pero para propósitos distintos. Este es dashboard tiempo real, el reporte es contabilidad mensual (minutos).

## Variables de entorno

```
TIENDANUBE_STORE_ID
TIENDANUBE_ACCESS_TOKEN
DATABASE_URL
WEB_PASSWORD
```
