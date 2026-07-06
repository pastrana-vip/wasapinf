"""
main.py — VERSIÓN CORREGIDA v3
================================
Fix aplicado:
- La detección automática de ngrok fallaba en Windows porque el proceso
  ngrok bloquea la API local (localhost:4040) para uso externo en algunas versiones.
- Solución: leer PUBLIC_BASE_URL desde archivo .env con python-dotenv,
  con fallback a detección por API de ngrok, con fallback a localhost.
- Se añade ruta de inicio rápido /setup que muestra cómo configurar la URL.
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, HTMLResponse
from fastapi import Query, HTTPException
import uvicorn
import os
import mimetypes
import json

# ── Cargar .env si existe ─────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv no instalado, usar solo variables de entorno del sistema

from models.database import init_db
from routers.api import router
from routers.invoices import router as invoices_router
from routers.agents import router as agents_router


# ══════════════════════════════════════════════════════════════════════════════
# RESOLVER URL PÚBLICA — 3 métodos en orden de prioridad
# ══════════════════════════════════════════════════════════════════════════════

def resolve_public_url() -> str:
    """
    Resuelve la URL pública del servidor en este orden:
    1. Variable de entorno PUBLIC_BASE_URL (definida en .env o sistema)
    2. Detección automática via API de ngrok (localhost:4040)
    3. Fallback a localhost (solo para desarrollo local sin PDFs)
    """

    # Prioridad 1: variable de entorno explícita
    env_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if env_url and env_url != "http://localhost:8000":
        print(f"[STARTUP] ✅ URL pública desde .env: {env_url}")
        return env_url

    # Prioridad 2: auto-detectar ngrok via su API local
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:4040/api/tunnels",
            headers={"User-Agent": "WaSapinf/1.0"}
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            tunnels = data.get("tunnels", [])
            # Preferir el túnel HTTPS
            for tunnel in tunnels:
                if tunnel.get("proto") == "https":
                    url = tunnel["public_url"].rstrip("/")
                    print(f"[STARTUP] ✅ ngrok detectado automáticamente: {url}")
                    return url
            # Si no hay HTTPS, usar el primero disponible
            if tunnels:
                url = tunnels[0]["public_url"].rstrip("/")
                print(f"[STARTUP] ⚠️  ngrok detectado (sin HTTPS): {url}")
                return url
    except Exception as e:
        print(f"[STARTUP] ℹ️  ngrok no detectado en 127.0.0.1:4040: {e}")

    # Prioridad 3: fallback a localhost (PDFs no accesibles por Meta)
    print("[STARTUP] ⚠️  PUBLIC_BASE_URL no configurada.")
    print("[STARTUP]    Los PDFs no llegarán a WhatsApp hasta que configures la URL.")
    print("[STARTUP]    → Crea un archivo .env con: PUBLIC_BASE_URL=https://tu-ngrok.ngrok-free.app")
    print("[STARTUP]    → O define la variable de entorno antes de arrancar.")
    return "http://localhost:8000"


PUBLIC_BASE_URL = resolve_public_url()
os.environ["PUBLIC_BASE_URL"] = PUBLIC_BASE_URL
print(f"[Config] PUBLIC_BASE_URL en uso: {PUBLIC_BASE_URL}")

# ── Crear carpetas ────────────────────────────────────────────────────────────
UPLOADS_DIR = os.path.abspath("uploads")
os.makedirs(f"{UPLOADS_DIR}/invoices", exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="WaSapinf - WhatsApp unlimited", version="1.0.0")
app.include_router(agents_router, prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Type", "Content-Length"],
)


# ══════════════════════════════════════════════════════════════════════════════
# SERVIR ARCHIVOS DE /uploads/ CON HEADERS CORRECTOS PARA NGROK + META
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/uploads/{subpath:path}")
async def serve_upload(subpath: str, request: Request):
    """
    Sirve archivos con los headers necesarios para que:
    1. ngrok NO muestre la página de advertencia (ngrok-skip-browser-warning)
    2. Meta WhatsApp API pueda descargar el PDF correctamente
    3. CORS permita acceso externo
    """
    file_path = os.path.join(UPLOADS_DIR, subpath)

    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return Response(content=f"Archivo no encontrado: {subpath}", status_code=404)

    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "application/octet-stream"

    with open(file_path, "rb") as f:
        content = f.read()

    filename = os.path.basename(file_path)

    return Response(
        content=content,
        media_type=mime_type,
        headers={
            # Ngrok: salta página de advertencia
            "ngrok-skip-browser-warning": "true",
            "bypass-tunnel-reminder":     "true",
            # Descarga correcta
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length":      str(len(content)),
            # CORS
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            # Sin caché
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma":        "no-cache",
            "X-Content-Type-Options": "nosniff",
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# RUTA DE CONFIGURACIÓN RÁPIDA
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    """Página de diagnóstico y configuración."""
    url_ok    = not PUBLIC_BASE_URL.startswith("http://localhost")
    status_color = "#25d366" if url_ok else "#ff5c5c"
    status_text  = "✅ Correcta — Meta puede descargar PDFs" if url_ok else \
                   "❌ Incorrecta — PDFs NO llegarán a WhatsApp"

    # Contar archivos en uploads
    total_files = sum(len(files) for _, _, files in os.walk(UPLOADS_DIR))

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>WaSapinf — Configuración</title>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; background: #0a0f0d; color: #e8f5ee; margin: 0; padding: 32px; }}
  h1 {{ color: #25d366; font-size: 24px; margin-bottom: 24px; }}
  .card {{ background: #1a2820; border: 1px solid #253d30; border-radius: 14px; padding: 24px; margin-bottom: 16px; }}
  .label {{ font-size: 12px; color: #8fada0; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }}
  .value {{ font-size: 16px; font-weight: 600; }}
  .status {{ color: {status_color}; font-size: 15px; margin-top: 8px; }}
  code {{ background: #253d30; padding: 2px 8px; border-radius: 6px; font-size: 13px; color: #25d366; }}
  .step {{ background: #111a15; border-radius: 10px; padding: 16px; margin-top: 12px; font-size: 14px; line-height: 1.6; }}
  .step strong {{ color: #25d366; }}
  a {{ color: #25d366; }}
  pre {{ background: #0a0f0d; padding: 14px; border-radius: 8px; overflow-x: auto; font-size: 13px; color: #e8f5ee; }}
</style>
</head>
<body>
<h1>⚙️ WaSapinf — Estado del servidor</h1>

<div class="card">
  <div class="label">URL Pública actual</div>
  <div class="value">{PUBLIC_BASE_URL}</div>
  <div class="status">{status_text}</div>
</div>

<div class="card">
  <div class="label">Archivos en uploads/</div>
  <div class="value">{total_files} archivos</div>
  <div style="margin-top:10px">
    <a href="/debug/uploads">→ Ver lista completa de archivos</a>
  </div>
</div>

{"" if url_ok else '''
<div class="card">
  <div class="label" style="color:#ff5c5c">⚠️  Acción requerida — Configura la URL pública</div>
  <div class="step">
    <strong>Paso 1:</strong> Asegúrate de que ngrok está corriendo:<br>
    <pre>ngrok http 8000</pre>
    Copia la URL HTTPS que aparece, ejemplo:<br>
    <code>https://wasapinf.onrender.com</code>
  </div>
  <div class="step">
    <strong>Paso 2:</strong> Crea el archivo <code>.env</code> en la misma carpeta que <code>main.py</code>:<br>
    <pre>https://wasapinf.onrender.com</pre>
    (Reemplaza con tu URL real de ngrok)
  </div>
  <div class="step">
    <strong>Paso 3:</strong> Reinicia el servidor. Verás:<br>
    <pre>[STARTUP] ✅ URL pública desde .env: https://wasapinf.onrender.com</pre>
  </div>
  <div class="step">
    <strong>Sobre los PDFs que ya enviaste:</strong> El wamid se generó (WhatsApp aceptó el mensaje)
    pero Meta no pudo descargar el PDF desde localhost. Debes reenviar el lote con la URL correcta.
  </div>
</div>
'''}

<div class="card">
  <div class="label">Rutas de debug disponibles</div>
  <ul style="font-size:14px; line-height:2;">
    <li><a href="/debug/uploads">/debug/uploads</a> — Lista todos los archivos con URLs públicas</li>
    <li><a href="/debug/config">/debug/config</a> — Configuración actual del servidor</li>
    <li><a href="/setup">/setup</a> — Esta página</li>
  </ul>
</div>
</body></html>"""
    return html


# ══════════════════════════════════════════════════════════════════════════════
# RUTAS DE DEBUG
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/debug/uploads")
async def debug_list_uploads():
    files = []
    for root, dirs, filenames in os.walk(UPLOADS_DIR):
        for fname in filenames:
            full_path = os.path.join(root, fname)
            rel_path  = os.path.relpath(full_path, UPLOADS_DIR).replace("\\", "/")
            public_url = f"{PUBLIC_BASE_URL}/uploads/{rel_path}"
            files.append({
                "filename":   fname,
                "path":       rel_path,
                "size_kb":    round(os.path.getsize(full_path) / 1024, 1),
                "public_url": public_url,
            })
    return {
        "public_base_url": PUBLIC_BASE_URL,
        "url_is_public":   not PUBLIC_BASE_URL.startswith("http://localhost"),
        "uploads_dir":     UPLOADS_DIR,
        "total_files":     len(files),
        "files":           files,
    }


@app.get("/debug/uploads/{subpath:path}")
async def debug_check_file(subpath: str):
    file_path  = os.path.join(UPLOADS_DIR, subpath)
    public_url = f"{PUBLIC_BASE_URL}/uploads/{subpath}"
    exists     = os.path.exists(file_path)
    result = {
        "subpath":    subpath,
        "full_path":  file_path,
        "public_url": public_url,
        "exists":     exists,
    }
    if exists:
        result["size_kb"]   = round(os.path.getsize(file_path) / 1024, 1)
        mime, _             = mimetypes.guess_type(file_path)
        result["mime_type"] = mime or "application/octet-stream"
        result["status"]    = "✅ Accesible"
    else:
        result["status"]    = "❌ No encontrado"
    return result


@app.get("/debug/config")
async def debug_config():
    return {
        "public_base_url":    PUBLIC_BASE_URL,
        "url_is_public":      not PUBLIC_BASE_URL.startswith("http://localhost"),
        "uploads_dir":        UPLOADS_DIR,
        "uploads_dir_exists": os.path.exists(UPLOADS_DIR),
        "cwd":                os.getcwd(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP Y ROUTERS
# ══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    await init_db()
    url_ok = not PUBLIC_BASE_URL.startswith("http://localhost")
    print(f"\n{'='*60}")
    print(f"  WaSapinf iniciado")
    print(f"  URL pública: {PUBLIC_BASE_URL}")
    if not url_ok:
        print(f"  ⚠️  ATENCIÓN: URL es localhost — PDFs no llegarán a WhatsApp")
        print(f"  → Crea .env con PUBLIC_BASE_URL=https://tu-ngrok.ngrok-free.app")
    else:
        print(f"  ✅ URL pública configurada correctamente")
    print(f"  Debug:       {PUBLIC_BASE_URL}/debug/uploads")
    print(f"  Setup:       http://localhost:8000/setup")
    print(f"{'='*60}\n")


# ── IMPORTANTE: routers ANTES del mount de /static ───────────────────────────
app.include_router(router, prefix="/api")
app.include_router(invoices_router, prefix="/api")

# Esta ruta es para que Meta verifique tu URL
@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge")
):
    # El token debe coincidir con lo que pusiste en Meta Developer Portal
    VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "wablast_webhook_secret_2025")
    
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return int(hub_challenge)
    
    raise HTTPException(status_code=403, detail="Token de verificación inválido")

# Esta ruta es para que Meta te envíe los mensajes
@app.post("/webhook")
async def receive_webhook(request: Request):
    data = await request.json()
    # Aquí deberías procesar los mensajes que llegan de WhatsApp
    print(f"Mensaje recibido: {data}")
    return {"status": "ok"}

# ── Frontend ──────────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_frontend():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
