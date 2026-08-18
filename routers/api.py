from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime, timedelta
import httpx
import os
import csv
import io
import json
import uuid
import secrets
import export_service

from models.database import (
    User, Campaign, Contact, Message, CampaignStatus,
    Conversation, ConversationStatus, ChatMessage, Agent, AgentRole,
    CampaignTemplate, CustomFieldDef, MetaTemplate, ContactTag,
    ApiKey, WebhookSubscription, WEBHOOK_EVENTS, get_db, normalize_phone
)
from auth import (
    hash_password, verify_password, create_token, get_current_user,
    has_permission, require_superadmin, require_owner_or_superadmin,
    generate_api_key
)
from whatsapp_service import (
    run_campaign, recalc_campaign_counters, send_whatsapp_text,
    send_whatsapp_document, send_whatsapp_image, send_whatsapp_template,
    is_within_24h_window, get_public_base_url
)
from webhooks_service import dispatch_webhook_event


router = APIRouter()


@router.get("/config/public")
async def public_config():
    """
    Config sin autenticación que el frontend necesita antes de loguearse
    (ej. el App ID de Meta para inicializar el SDK de Facebook Login).
    Se lee de la variable de entorno META_APP_ID si existe; si no, cae
    al valor que ya tenías funcionando, para no romper nada en quien no
    configure la variable.
    """
    return {
        "meta_app_id": os.getenv("META_APP_ID", "1423948069499384"),
    }

# ────────────────────────────────────────────────────────────────
# CONFIGURACIÓN META  (reemplaza con tus valores reales)
# ────────────────────────────────────────────────────────────────
META_APP_ID          = os.getenv("META_APP_ID")          # developers.facebook.com
META_APP_SECRET      = os.getenv("META_APP_SECRET")
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "wablast_webhook_secret_2025")

# ── Token del System User de TU Business Manager (portafolio "WaSapinf") ──
# Este token es TUYO, no el del cliente. Como Tech Provider, en cuanto un
# cliente comparte/crea su WABA contigo, tu System User tiene acceso
# inmediato y estable a esa WABA — a diferencia del token efímero que
# devuelve el OAuth del cliente, que a veces tarda en propagar permisos.
# Se usa como preferencia para las llamadas de "lectura/gestión" del lado
# del proveedor (listar teléfonos, registrar número, suscribir webhooks).
SYSTEM_USER_TOKEN = os.getenv("SYSTEM_USER_TOKEN")

# ID de TU Business Manager (el portafolio que ves en Meta Business Suite,
# ej. "WaSapinf"). Se usa como fallback para ubicar WABAs de clientes que
# aún no llegan por el evento postMessage del Embedded Signup.
TECH_PROVIDER_BUSINESS_ID = os.getenv("TECH_PROVIDER_BUSINESS_ID")

# Usa SIEMPRE la misma versión de Graph API en todo el archivo.
GRAPH_VERSION = "v21.0"
GRAPH_BASE    = f"https://graph.facebook.com/{GRAPH_VERSION}"


# ── Baja / opt-out & auto-respuesta de horario ────────────────────
OPT_OUT_KEYWORDS = {"BAJA", "STOP", "CANCELAR", "UNSUBSCRIBE", "NO MOLESTAR"}
AUTO_REPLY_COOLDOWN_HOURS = 12
# Sin timezone por cliente todavía — se asume Centroamérica (UTC-6) como
# valor razonable por defecto para el cálculo de horario de atención.
DEFAULT_UTC_OFFSET_HOURS = -6
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def is_within_business_hours(user: "User") -> Optional[bool]:
    """
    True/False si hay horario configurado; None si el cliente no
    configuró horario de atención (en ese caso no se puede saber si
    "está fuera de horario", así que no se bloquea el auto-reply).
    """
    if not user.business_hours:
        return None
    try:
        hours = json.loads(user.business_hours)
    except (ValueError, TypeError):
        return None
    local_now = datetime.utcnow() + timedelta(hours=DEFAULT_UTC_OFFSET_HOURS)
    day_key = DAY_KEYS[local_now.weekday()]
    ranges = hours.get(day_key)
    if not ranges:
        return False  # día sin horario definido = cerrado
    try:
        start_s, end_s = ranges
        start_h, start_m = map(int, start_s.split(":"))
        end_h, end_m = map(int, end_s.split(":"))
        start = local_now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = local_now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        return start <= local_now <= end
    except (ValueError, TypeError):
        return None


def should_send_auto_reply(user: "User", conv) -> bool:
    if not user.auto_reply_enabled or not user.auto_reply_message:
        return False
    within_hours = is_within_business_hours(user)
    if within_hours is True:
        return False  # dentro de horario: asumimos que hay alguien atendiendo
    if conv.last_auto_reply_at and (datetime.utcnow() - conv.last_auto_reply_at) < timedelta(hours=AUTO_REPLY_COOLDOWN_HOURS):
        return False  # ya se mandó hace poco, no saturar la conversación
    return True


def provider_token(client_token: str) -> str:
    """
    Devuelve el mejor token disponible para operar sobre la WABA de un
    cliente: prioriza el SYSTEM_USER_TOKEN (tuyo, estable) y solo si no
    está configurado cae al token efímero obtenido del OAuth del cliente.
    """
    return SYSTEM_USER_TOKEN or client_token

# ── Schemas ──────────────────────────────────────────────────────

class RegisterIn(BaseModel):
    name: str
    email: EmailStr
    password: str

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class WhatsAppConfigIn(BaseModel):
    whatsapp_token: str
    whatsapp_phone_id: str
    profile_name: Optional[str] = None
    waba_id: Optional[str] = None
    client_id: Optional[int] = None   # superadmin: conectar la línea de ESTE cliente, no la propia

class CampaignIn(BaseModel):
    name: str
    message_template: Optional[str] = None
    template_id: Optional[int] = None
    contact_tag: Optional[str] = None

class ChatReplyIn(BaseModel):
    body: str
    # Opcional: si el contacto está fuera de la ventana de 24h, se puede
    # enviar una plantilla HSM en lugar de texto libre.
    template_name: Optional[str] = None
    template_language: Optional[str] = None
    template_variables: Optional[List[str]] = None

class ChatStartIn(BaseModel):
    """Inicia una conversación con un contacto que aún no ha escrito
    (o cuya ventana de 24h ya expiró) usando una plantilla aprobada por Meta.
    Ejemplo típico: plantilla marketing 'mensaje_inicial' en es_HN.
    """
    contact_id: Optional[int] = None
    phone: Optional[str] = None
    contact_name: Optional[str] = None
    template_name: str                          # nombre exacto en Meta, ej. mensaje_inicial
    template_language: str = "es"               # ej. es, es_HN, es_MX
    template_variables: Optional[List[str]] = None  # valores para {{1}}, {{2}}...

class ConvStatusIn(BaseModel):
    status: str   # open | waiting | closed

# ── Auth ─────────────────────────────────────────────────────────

@router.post("/auth/register")
async def register(data: RegisterIn, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == data.email))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Este email ya está registrado")
    user = User(name=data.name, email=data.email,
                hashed_password=hash_password(data.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"token": create_token(user.id, user.email, typ="owner"),
            "user": {"id": user.id, "name": user.name, "email": user.email,
                      "type": "owner", "is_superadmin": False}}


@router.post("/auth/login")
async def login(data: LoginIn, db: AsyncSession = Depends(get_db)):
    # === 1. Intentar login como Usuario Principal (Dueño) ===
    res = await db.execute(select(User).where(User.email == data.email))
    user = res.scalar_one_or_none()

    if user and verify_password(data.password, user.hashed_password):
        return {
            "token": create_token(user.id, user.email, typ="owner"),
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "type": "owner",
                "role": "admin",
                "is_superadmin": bool(user.is_superadmin),
                "company_name": user.name,
                "industry": user.industry,
                "has_whatsapp": bool(user.whatsapp_token),
                "whatsapp_phone_id": user.whatsapp_phone_id,
            }
        }

    # === 2. Intentar login como Agente ===
    res2 = await db.execute(
        select(Agent).where(Agent.email == data.email, Agent.is_active == True)
    )
    agent = res2.scalar_one_or_none()

    if agent and verify_password(data.password, agent.hashed_password):
        return {
            "token": create_token(agent.id, agent.email, typ="agent"),
            "user": {
                "id": agent.id,
                "name": agent.name,
                "email": agent.email,
                "type": "agent",
                "role": agent.role.value,
                "permissions": agent.get_permissions(),
                "owner_id": agent.owner_id,
                "whatsapp_phone_id": agent.whatsapp_phone_id,
                "is_superadmin": False
            }
        }

    raise HTTPException(status_code=401, detail="Credenciales incorrectas")


@router.get("/auth/me")
async def me(user: User = Depends(get_current_user)):
    actor_type = getattr(user, "_actor_type", "owner")
    agent = getattr(user, "_agent", None)
    return {
        "id": agent.id if agent else user.id,
        "name": agent.name if agent else user.name,
        "email": agent.email if agent else user.email,
        "type": actor_type,
        "is_superadmin": getattr(user, "_is_superadmin", False),
        "permissions": sorted(agent.get_permissions()) if agent else None,
        "company_name": user.name,
        "industry": user.industry,
        "has_whatsapp": bool(user.whatsapp_token),
        "whatsapp_phone_id": user.whatsapp_phone_id,
        "profile_name": user.profile_name,
    }

# ── WhatsApp Config (manual) ──────────────────────────────────────

@router.post("/config/whatsapp")
async def save_whatsapp_config(
    data: WhatsAppConfigIn,
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    # Las empresas/clientes ya NO pueden conectar su propia línea de
    # WhatsApp — solo el superadmin (PRIME) puede. Si viene client_id,
    # la conexión se guarda en ESE cliente (el caso normal: el admin
    # está registrando/gestionando una empresa desde el Panel Admin);
    # si no viene, se guarda en la propia cuenta del superadmin (uso
    # para pruebas).
    if data.client_id:
        res_client = await db.execute(select(User).where(User.id == data.client_id, User.is_superadmin == False))
        target = res_client.scalar_one_or_none()
        if not target:
            raise HTTPException(404, "Cliente no encontrado")
        user = target

    token    = data.whatsapp_token or None
    phone_id = data.whatsapp_phone_id or None
    waba_id  = data.waba_id or None

    if token and phone_id:
        op_token = provider_token(token)
        reg_result = await register_phone_number(phone_id, op_token)
        if "error" in reg_result:
            print(f"[Register] Aviso (puede ya estar registrado): {reg_result['error']}")
            user.last_whatsapp_error = str(reg_result["error"].get("message", reg_result["error"]))[:500]
            user.last_whatsapp_error_at = datetime.utcnow()
        else:
            print(f"[Register] ✅ OK para phone_id={phone_id}")
            user.last_whatsapp_error = None
            user.last_whatsapp_error_at = None
        if waba_id:
            sub_result = await subscribe_app_to_waba(waba_id, op_token)
            if "error" in sub_result:
                print(f"[Subscribe] Error al suscribir app al WABA {waba_id}: {sub_result['error']}")
            else:
                print(f"[Subscribe] ✅ App suscrita al WABA {waba_id}: {sub_result}")
        else:
            print(f"[Subscribe] ⚠ SALTADO — no se ingresó waba_id, no se pudo suscribir el webhook para phone_id={phone_id}")

    user.whatsapp_token    = token
    user.whatsapp_phone_id = phone_id
    user.profile_name      = data.profile_name or None
    user.waba_id           = waba_id
    await db.commit()
    return {"ok": True}

# ── Helpers Meta / Cloud API ──────────────────────────────────────

async def fetch_client_wabas(long_token: str) -> list:
    """
    Devuelve las WABA visibles para este token, ya sea que el negocio
    las posea directamente o que se las hayan compartido (caso Tech Provider,
    que es tu caso según tu app "Ya eres un proveedor de tecnología").

    Consulta primero client_whatsapp_business_accounts (WABAs de CLIENTES
    compartidas contigo) y, si no hay resultados, cae a
    owned_whatsapp_business_accounts (WABAs que tú mismo posees).
    """
    wabas = []
    async with httpx.AsyncClient(timeout=20) as client:
        r_biz = await client.get(
            f"{GRAPH_BASE}/me/businesses",
            headers={"Authorization": f"Bearer {long_token}"}
        )
        biz_data = r_biz.json()
        for biz in biz_data.get("data", []):
            biz_id = biz["id"]

            # 1) WABAs de CLIENTES compartidas contigo (rol de Tech Provider)
            r_client = await client.get(
                f"{GRAPH_BASE}/{biz_id}/client_whatsapp_business_accounts",
                headers={"Authorization": f"Bearer {long_token}"}
            )
            client_data = r_client.json()
            wabas.extend(client_data.get("data", []))

            # 2) WABAs propias (por si el negocio también tiene una directa)
            r_owned = await client.get(
                f"{GRAPH_BASE}/{biz_id}/owned_whatsapp_business_accounts",
                headers={"Authorization": f"Bearer {long_token}"}
            )
            owned_data = r_owned.json()
            wabas.extend(owned_data.get("data", []))

        # 3) FALLBACK con TU propio Business Manager + SYSTEM_USER_TOKEN.
        #    El token del cliente (long_token) a veces no tiene permisos
        #    propagados justo después del signup. Tu System User, como
        #    Tech Provider, ve la WABA del cliente de forma inmediata y
        #    estable en cuanto se completó el Embedded Signup.
        if not wabas and SYSTEM_USER_TOKEN and TECH_PROVIDER_BUSINESS_ID:
            try:
                r_fallback = await client.get(
                    f"{GRAPH_BASE}/{TECH_PROVIDER_BUSINESS_ID}/client_whatsapp_business_accounts",
                    headers={"Authorization": f"Bearer {SYSTEM_USER_TOKEN}"}
                )
                fallback_data = r_fallback.json()
                if "error" in fallback_data:
                    print(f"[fetch_client_wabas] client_whatsapp_business_accounts error: {fallback_data['error']}")
                wabas.extend(fallback_data.get("data", []))

                # También cuentas que TÚ posees directamente (no solo
                # compartidas por clientes) — cubre el caso donde la WABA
                # aparece en tu propio portafolio en vez de como "cliente".
                r_fallback_owned = await client.get(
                    f"{GRAPH_BASE}/{TECH_PROVIDER_BUSINESS_ID}/owned_whatsapp_business_accounts",
                    headers={"Authorization": f"Bearer {SYSTEM_USER_TOKEN}"}
                )
                fallback_owned_data = r_fallback_owned.json()
                if "error" in fallback_owned_data:
                    print(f"[fetch_client_wabas] owned_whatsapp_business_accounts error: {fallback_owned_data['error']}")
                wabas.extend(fallback_owned_data.get("data", []))

                print(f"[fetch_client_wabas] Fallback con SYSTEM_USER_TOKEN devolvió {len(wabas)} WABA(s)")
            except Exception as e:
                print(f"[fetch_client_wabas] Fallback con SYSTEM_USER_TOKEN falló: {e}")

    # Deduplicar por id
    seen = set()
    unique = []
    for w in wabas:
        if w["id"] not in seen:
            seen.add(w["id"])
            unique.append(w)
    return unique


async def fetch_phone_numbers(waba_id: str, long_token: str) -> list:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{GRAPH_BASE}/{waba_id}/phone_numbers",
            params={"fields": "id,display_phone_number,verified_name,quality_rating"},
            headers={"Authorization": f"Bearer {long_token}"}
        )
        return r.json().get("data", [])


async def register_phone_number(phone_id: str, token: str, pin: str = "000000") -> dict:
    """
    Registra el número para poder enviar/recibir por Cloud API.
    Si el número ya está registrado, Meta devuelve un error que ignoramos
    (no es crítico, significa que ya estaba listo).
    """
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.post(
                f"{GRAPH_BASE}/{phone_id}/register",
                headers={"Authorization": f"Bearer {token}"},
                json={"messaging_product": "whatsapp", "pin": pin},
            )
            return r.json()
        except Exception as e:
            # Red caída, token inválido, o Meta devolvió algo no-JSON.
            # Nunca dejamos que esto tumbe la request del admin/cliente.
            return {"error": {"message": f"No se pudo contactar la API de Meta: {e}"}}


async def subscribe_app_to_waba(waba_id: str, token: str) -> dict:
    """
    Suscribe TU app a los webhooks de la WABA del cliente.
    Sin este paso, tu backend no recibirá notificaciones de entrega/lectura
    ni mensajes entrantes de esa línea, aunque el envío en sí pueda funcionar.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.post(
                f"{GRAPH_BASE}/{waba_id}/subscribed_apps",
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.json()
        except Exception as e:
            return {"error": {"message": f"No se pudo contactar la API de Meta: {e}"}}


async def fetch_meta_templates(waba_id: str, token: str) -> list:
    """
    Trae el catálogo de plantillas (HSM) aprobadas/pendientes/rechazadas
    en Meta Business Manager para esta WABA. Necesarias para enviar
    mensajes fuera de la ventana de 24h (WhatsApp no permite texto libre
    ahí — solo plantillas pre-aprobadas).
    """
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{GRAPH_BASE}/{waba_id}/message_templates",
                params={"fields": "name,language,category,status,components", "limit": 200},
                headers={"Authorization": f"Bearer {token}"},
            )
            data = r.json()
            if "error" in data:
                return []
            out = []
            for t in data.get("data", []):
                body_comp = next((c for c in t.get("components", []) if c.get("type") == "BODY"), {})
                body_text = body_comp.get("text", "")
                var_count = body_text.count("{{")
                out.append({
                    "name": t.get("name"),
                    "language": t.get("language", "es"),
                    "category": t.get("category"),
                    "status": t.get("status"),
                    "body_text": body_text,
                    "variable_count": var_count,
                })
            return out
        except Exception as e:
            print(f"[fetch_meta_templates] error: {e}")
            return []


# ── Meta Embedded Signup OAuth ────────────────────────────────────
# Flujo: Frontend abre popup → facebook.com/dialog/oauth
#        Meta redirige a  /api/auth/facebook/callback?code=XXX
#        El callback intercambia el code por token, recupera Phone IDs
#        y cierra el popup enviando postMessage al padre.

@router.get("/auth/facebook/callback", response_class=HTMLResponse)
async def facebook_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    if error:
        return HTMLResponse(_popup_close_html("error", {"error": error}))

    if not code:
        return HTMLResponse(_popup_close_html("error", {"error": "No se recibió código de autorización"}))

    redirect_uri = str(request.url_for("facebook_callback"))
    token_url = (
        f"{GRAPH_BASE}/oauth/access_token"
        f"?client_id={META_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&client_secret={META_APP_SECRET}"
        f"&code={code}"
    )

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(token_url)
        token_data = r.json()

    if "access_token" not in token_data:
        err = token_data.get("error", {}).get("message", "Error al obtener token")
        return HTMLResponse(_popup_close_html("error", {"error": err}))

    short_token = token_data["access_token"]
    long_token = short_token
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r2 = await client.get(
                f"{GRAPH_BASE}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": META_APP_ID,
                    "client_secret": META_APP_SECRET,
                    "fb_exchange_token": short_token,
                }
            )
            ld = r2.json()
            if "access_token" in ld:
                long_token = ld["access_token"]
    except Exception:
        pass

    waba_id = None
    phone_numbers = []
    try:
        wabas = await fetch_client_wabas(long_token)
        if wabas:
            waba_id = wabas[0]["id"]
            # Preferimos SYSTEM_USER_TOKEN (tuyo, estable) para leer los
            # números; si no está configurado, caemos al token del cliente.
            phone_numbers = await fetch_phone_numbers(waba_id, provider_token(long_token))
    except Exception as e:
        print(f"[OAuth] Error al obtener WABA/phones: {e}")

    payload = {
        "token":    long_token,
        "waba_id":  waba_id or "",
        "phones":   phone_numbers,
    }
    return HTMLResponse(_popup_close_html("success", payload))


def _popup_close_html(status: str, data: dict) -> str:
    import json
    msg_type = "fb_oauth_success" if status == "success" else "fb_oauth_error"
    payload_js = json.dumps({**data, "type": msg_type})
    return f"""<!DOCTYPE html>
<html><head><title>Conectando...</title></head>
<body style="font-family:sans-serif;background:#0a0f0d;color:#e8f5ee;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">
  <div style="text-align:center;">
    <div style="font-size:48px;margin-bottom:16px;">{'✅' if status=='success' else '❌'}</div>
    <p>{'Conexión exitosa. Cerrando...' if status=='success' else 'Error en la conexión. Cerrando...'}</p>
  </div>
  <script>
    try {{
      window.opener.postMessage({payload_js}, window.location.origin);
    }} catch(e) {{
      console.error('postMessage failed:', e);
    }}
    setTimeout(() => window.close(), 1500);
  </script>
</body></html>"""


@router.post("/auth/facebook/exchange")
async def exchange_facebook_code(
    data: dict,
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Recibe el 'code' del FB.login() (Embedded Signup) y lo intercambia
    por un token de acceso. El Embedded Signup NO usa redirect_uri.
    """
    code = data.get("code")
    if not code:
        raise HTTPException(400, "Falta el código de autorización")

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "code": code,
            }
        )
        token_data = r.json()

    if "access_token" not in token_data:
        err = token_data.get("error", {}).get("message", "Error al obtener token")
        return {"ok": False, "detail": err}

    short_token = token_data["access_token"]
    long_token = short_token
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r2 = await client.get(
                f"{GRAPH_BASE}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": META_APP_ID,
                    "client_secret": META_APP_SECRET,
                    "fb_exchange_token": short_token,
                }
            )
            ld = r2.json()
            if "access_token" in ld:
                long_token = ld["access_token"]
    except Exception:
        pass

    waba_id = None
    phone_numbers = []
    try:
        wabas = await fetch_client_wabas(long_token)
        if wabas:
            waba_id = wabas[0]["id"]
            phone_numbers = await fetch_phone_numbers(waba_id, provider_token(long_token))
    except Exception as e:
        print(f"[OAuth Exchange] Error al obtener WABA/phones: {e}")

    # Bug corregido: antes se evaluaba esto ANTES de tener phone_numbers,
    # así que primary_phone_id siempre quedaba en None.
    primary_phone_id = phone_numbers[0].get("id") if phone_numbers else None

    return {
        "ok": True,
        "token": long_token,
        "waba_id": waba_id or "",
        "phones": phone_numbers,
        "phone_id": primary_phone_id,
    }


@router.post("/auth/facebook/save")
async def save_facebook_connection(
    data: dict,
    user: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Guarda el token OAuth de Facebook + Phone ID obtenidos vía Embedded
    Signup, y completa los dos pasos que Meta exige antes de poder
    enviar/recibir mensajes por Cloud API: registrar el número y
    suscribir la app al WABA.

    Solo el superadmin puede llamar esto. Si se pasa "client_id", la
    conexión se guarda en la empresa/cliente indicado (el superadmin
    conectando la línea EN NOMBRE del cliente); si no, se guarda en la
    propia cuenta del superadmin.
    """
    token    = data.get("token")
    phone_id = data.get("phone_id")
    profile  = data.get("profile_name")
    waba_id  = data.get("waba_id")
    client_id = data.get("client_id")

    if not token or not phone_id:
        raise HTTPException(400, "Token y Phone ID son obligatorios")

    if client_id:
        res_client = await db.execute(select(User).where(User.id == client_id))
        target = res_client.scalar_one_or_none()
        if not target:
            raise HTTPException(404, "Cliente no encontrado")
        user = target

    # Usamos SYSTEM_USER_TOKEN (tuyo) cuando esté disponible: el token del
    # cliente recién emitido a veces todavía no tiene los permisos de
    # negocio propagados y estas dos llamadas fallan intermitentemente.
    op_token = provider_token(token)

    # 1. Registrar el número en Cloud API (idempotente: si ya está
    #    registrado, Meta devuelve error y simplemente lo ignoramos).
    reg_result = await register_phone_number(phone_id, op_token)
    if "error" in reg_result:
        print(f"[Register] Aviso (puede ya estar registrado): {reg_result['error']}")
    else:
        print(f"[Register] ✅ OK para phone_id={phone_id}")

    # 2. Suscribir tu app a los webhooks de esa WABA (necesario para
    #    recibir estados de entrega/lectura y mensajes entrantes).
    sub_result = {}
    if waba_id:
        sub_result = await subscribe_app_to_waba(waba_id, op_token)
        if "error" in sub_result:
            print(f"[Subscribe] Error al suscribir app al WABA {waba_id}: {sub_result['error']}")
        else:
            print(f"[Subscribe] ✅ App suscrita al WABA {waba_id}: {sub_result}")
    else:
        print(f"[Subscribe] ⚠ SALTADO — no se recibió waba_id, no se pudo suscribir el webhook para phone_id={phone_id}")

    user.whatsapp_token = token
    user.whatsapp_phone_id = phone_id
    user.profile_name = profile or None
    user.waba_id = waba_id or None
    await db.commit()
    await db.refresh(user)

    return {
        "ok": True,
        "message": "Línea de WhatsApp conectada exitosamente",
        "phone_id": user.whatsapp_phone_id,
        "register_status": "error" if "error" in reg_result else "ok",
        "subscribe_status": "error" if "error" in sub_result else "ok",
    }


# ── Contacts ──────────────────────────────────────────────────────

class ContactIn(BaseModel):
    name: str
    phone: str
    tag: Optional[str] = None
    custom_fields: Optional[dict] = None


def _contact_out(c: Contact) -> dict:
    return {
        "id": c.id, "name": c.name, "phone": c.phone, "tag": c.tag,
        "opted_out": c.opted_out, "custom_fields": c.get_fields(),
    }


@router.post("/contacts")
async def add_contact(data: ContactIn,
                      user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "manage_contacts"):
        raise HTTPException(403, "Tu cuenta de agente no puede gestionar contactos")
    # Formato estándar único: solo dígitos, con código de país, sin '+'
    # — así el mismo contacto nunca queda guardado con dos formatos
    # distintos (ej. "+504 9990-9099" vs "50499909099").
    phone = normalize_phone(data.phone)
    if not phone:
        raise HTTPException(400, "Número de teléfono inválido. Usa solo dígitos con código de país. Ej: 50490908080")
    c = Contact(owner_id=user.id, name=data.name, phone=phone, tag=data.tag)
    c.set_fields(data.custom_fields or {})
    db.add(c)
    await db.commit()
    await db.refresh(c)
    await dispatch_webhook_event(user.id, "contact.created", {
        "id": c.id, "name": c.name, "phone": c.phone, "tag": c.tag,
    })
    return _contact_out(c)


@router.get("/contacts")
async def list_contacts(user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Contact).where(Contact.owner_id == user.id))
    return [_contact_out(c) for c in res.scalars().all()]


@router.patch("/contacts/{contact_id}")
async def update_contact(contact_id: int,
                         data: ContactIn,
                         user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "manage_contacts"):
        raise HTTPException(403, "Tu cuenta de agente no puede gestionar contactos")
    res = await db.execute(
        select(Contact).where(Contact.id == contact_id, Contact.owner_id == user.id))
    c = res.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Contacto no encontrado")
    phone = normalize_phone(data.phone)
    if not phone:
        raise HTTPException(400, "Número de teléfono inválido. Usa solo dígitos con código de país. Ej: 50490908080")
    c.name = data.name
    c.phone = phone
    c.tag = data.tag
    if data.custom_fields is not None:
        c.set_fields(data.custom_fields)
    await db.commit()
    return _contact_out(c)


@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: int,
                         user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "manage_contacts"):
        raise HTTPException(403, "Tu cuenta de agente no puede gestionar contactos")
    res = await db.execute(
        select(Contact).where(Contact.id == contact_id, Contact.owner_id == user.id))
    c = res.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Contacto no encontrado")
    await db.delete(c)
    await db.commit()
    return {"ok": True}


@router.patch("/contacts/{contact_id}/opt-out")
async def toggle_contact_opt_out(
    contact_id: int,
    data: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Marca/desmarca manualmente a un contacto como dado de baja de
    campañas (lo mismo que pasa automático si el contacto escribe
    "BAJA"/"STOP" por WhatsApp). Sigue pudiendo usar el chat.
    """
    if not has_permission(user, "manage_contacts"):
        raise HTTPException(403, "Tu cuenta de agente no puede gestionar contactos")
    res = await db.execute(
        select(Contact).where(Contact.id == contact_id, Contact.owner_id == user.id))
    c = res.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Contacto no encontrado")
    c.opted_out = bool(data.get("opted_out", True))
    await db.commit()
    return {"ok": True, "opted_out": c.opted_out}


@router.get("/contacts/export")
async def export_contacts_xlsx(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not (has_permission(user, "view_reports") or has_permission(user, "manage_contacts")):
        raise HTTPException(403, "Tu cuenta de agente no puede exportar reportes")
    res = await db.execute(select(Contact).where(Contact.owner_id == user.id))
    contacts = res.scalars().all()
    fields_res = await db.execute(select(CustomFieldDef).where(CustomFieldDef.client_id == user.id))
    field_defs = fields_res.scalars().all()
    buf = export_service.contacts_to_xlsx(contacts, field_defs)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=contactos.xlsx"}
    )


@router.get("/contacts/fields")
async def list_my_custom_fields(user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    """
    Campos personalizados definidos para esta empresa (los define el
    superadmin según el rubro — ver /admin/clients/{id}/fields). El
    cliente solo los lee para saber qué mostrar/llenar por contacto.
    """
    res = await db.execute(select(CustomFieldDef).where(CustomFieldDef.client_id == user.id))
    return [{"key": f.key, "label": f.label, "field_type": f.field_type}
            for f in res.scalars().all()]


@router.post("/contacts/import")
async def import_contacts_csv(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Importación masiva desde CSV. Columnas esperadas: name, phone, tag
    (opcional) — cualquier columna adicional que coincida con la `key`
    de un campo personalizado de esta empresa se guarda en custom_fields.
    Filas con teléfono ya existente se omiten (no duplica).
    """
    if not has_permission(user, "manage_contacts"):
        raise HTTPException(403, "Tu cuenta de agente no puede gestionar contactos")

    raw = await file.read()
    try:
        text_content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text_content = raw.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text_content))
    if not reader.fieldnames or "name" not in [f.strip().lower() for f in reader.fieldnames]:
        raise HTTPException(400, "El CSV debe tener al menos las columnas: name, phone")

    field_defs_res = await db.execute(select(CustomFieldDef).where(CustomFieldDef.client_id == user.id))
    field_keys = {f.key for f in field_defs_res.scalars().all()}

    existing_res = await db.execute(select(Contact.phone).where(Contact.owner_id == user.id))
    existing_phones = {p for (p,) in existing_res.all()}

    created = skipped = invalid = 0
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = row.get("name")
        phone = normalize_phone(row.get("phone"))
        if not name or not phone:
            invalid += 1
            continue
        if phone in existing_phones:
            skipped += 1
            continue
        c = Contact(owner_id=user.id, name=name, phone=phone, tag=row.get("tag") or None)
        extra = {k: v for k, v in row.items() if k in field_keys and v}
        c.set_fields(extra)
        db.add(c)
        existing_phones.add(phone)
        created += 1

    await db.commit()
    return {"created": created, "skipped": skipped, "invalid": invalid}

# ── Plantillas asignadas (solo lectura para el cliente/agente) ────
# El cliente NO puede crear ni editar plantillas — solo el superadmin
# (ver routers/admin.py). Aquí solo se listan las que le asignaron.

@router.get("/templates/mine")
async def list_my_templates(user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(CampaignTemplate).where(
            CampaignTemplate.is_active == True,
            (CampaignTemplate.client_id == user.id) | (CampaignTemplate.client_id.is_(None))
        ).order_by(CampaignTemplate.created_at.desc())
    )
    return [{
        "id": t.id, "name": t.name, "category": t.category,
        "message_template": t.message_template,
        # El cliente necesita saber si esta plantilla puede iniciar una
        # conversación fuera de la ventana de 24h (Meta lo exige); si no
        # tiene meta_template_name, esos contactos quedarán fuera.
        "meta_template_name": t.meta_template_name,
        "meta_template_language": t.meta_template_language,
    } for t in res.scalars().all()]

# ── Campaigns ─────────────────────────────────────────────────────

@router.post("/campaigns")
async def create_campaign(data: CampaignIn,
                          user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "send_campaigns"):
        raise HTTPException(403, "Tu cuenta de agente no puede enviar campañas")

    # Las empresas/clientes no redactan el contenido del mensaje libremente:
    # solo el superadmin puede crear texto de campaña desde cero. Un
    # cliente (o su agente) debe elegir una plantilla ya asignada por el
    # administrador — así se garantiza consistencia (ej. recordatorios
    # de medicamentos con el texto correcto aprobado por el negocio).
    message_template = data.message_template
    meta_template_name = None
    meta_template_language = "es"

    if data.template_id:
        res_t = await db.execute(select(CampaignTemplate).where(
            CampaignTemplate.id == data.template_id,
            CampaignTemplate.is_active == True,
            (CampaignTemplate.client_id == user.id) | (CampaignTemplate.client_id.is_(None))
        ))
        template = res_t.scalar_one_or_none()
        if not template:
            raise HTTPException(404, "Plantilla no encontrada o no asignada a tu empresa")
        message_template = template.message_template
        meta_template_name = template.meta_template_name
        meta_template_language = template.meta_template_language or "es"
    elif not getattr(user, "_is_superadmin", False):
        raise HTTPException(
            400,
            "Selecciona una plantilla de campaña asignada por el administrador."
        )

    if not message_template:
        raise HTTPException(400, "Falta el mensaje de la campaña")

    # Los contactos que se dieron de baja (BAJA/STOP) quedan fuera de
    # campañas y recordatorios, aunque sigan pudiendo usar el chat.
    q = select(Contact).where(Contact.owner_id == user.id, Contact.opted_out == False)
    if data.contact_tag:
        q = q.where(Contact.tag == data.contact_tag)
    contacts = (await db.execute(q)).scalars().all()
    if not contacts:
        raise HTTPException(400, "No hay contactos activos con ese filtro. Agrega contactos o revisa las bajas.")

    camp = Campaign(owner_id=user.id, name=data.name,
                    message_template=message_template,
                    meta_template_name=meta_template_name,
                    meta_template_language=meta_template_language,
                    total_contacts=len(contacts))
    db.add(camp)
    await db.flush()

    for c in contacts:
        db.add(Message(campaign_id=camp.id,
                       contact_phone=c.phone, contact_name=c.name))
    await db.commit()
    await db.refresh(camp)
    return {"id": camp.id, "name": camp.name,
            "total_contacts": camp.total_contacts, "status": camp.status}


@router.post("/campaigns/{campaign_id}/send")
async def send_campaign(campaign_id: int,
                        background_tasks: BackgroundTasks,
                        user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "send_campaigns"):
        raise HTTPException(403, "Tu cuenta de agente no puede enviar campañas")
    if not user.whatsapp_token or not user.whatsapp_phone_id:
        raise HTTPException(400, "Tu línea de WhatsApp aún no está conectada. Contacta al administrador.")
    res = await db.execute(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.owner_id == user.id))
    camp = res.scalar_one_or_none()
    if not camp:
        raise HTTPException(404, "Campaña no encontrada")
    if camp.status == CampaignStatus.sending:
        raise HTTPException(400, "La campaña ya está enviándose")
    background_tasks.add_task(
        run_campaign, campaign_id, db, user.whatsapp_phone_id, user.whatsapp_token)
    return {"ok": True, "message": "Campaña iniciada"}


@router.get("/campaigns")
async def list_campaigns(user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Campaign).where(Campaign.owner_id == user.id)
        .order_by(Campaign.created_at.desc()))
    out = []
    for c in res.scalars().all():
        await db.refresh(c)
        out.append({"id": c.id, "name": c.name, "status": c.status,
                    "total_contacts": c.total_contacts, "sent": c.sent,
                    "delivered": c.delivered, "read": c.read, "failed": c.failed,
                    "created_at": c.created_at.isoformat()})
    return out


@router.get("/campaigns/{campaign_id}/stats")
async def campaign_stats(campaign_id: int,
                         user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.owner_id == user.id))
    c = res.scalar_one_or_none()
    if not c:
        raise HTTPException(404)
    await recalc_campaign_counters(campaign_id, db)
    await db.refresh(c)
    msgs = (await db.execute(
        select(Message).where(Message.campaign_id == campaign_id)
    )).scalars().all()
    return {
        "campaign": {"id": c.id, "name": c.name, "status": c.status,
                     "template": c.message_template},
        "stats": {"total": c.total_contacts, "sent": c.sent,
                  "delivered": c.delivered, "read": c.read, "failed": c.failed},
        "messages": [{"phone": m.contact_phone, "name": m.contact_name,
                      "status": m.status,
                      "sent_at": m.sent_at.isoformat() if m.sent_at else None}
                     for m in msgs[:100]]
    }


@router.get("/campaigns/{campaign_id}/export")
async def export_campaign_report_xlsx(
    campaign_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not has_permission(user, "view_reports"):
        raise HTTPException(403, "Tu cuenta de agente no puede exportar reportes")
    res = await db.execute(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.owner_id == user.id))
    campaign = res.scalar_one_or_none()
    if not campaign:
        raise HTTPException(404, "Campaña no encontrada")
    msgs = (await db.execute(
        select(Message).where(Message.campaign_id == campaign_id)
    )).scalars().all()
    buf = export_service.campaign_report_to_xlsx(campaign, msgs)
    safe_name = "".join(ch for ch in campaign.name if ch.isalnum() or ch in " _-").strip() or "campana"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={safe_name}.xlsx"}
    )

# ── Meta Webhook ──────────────────────────────────────────────────

@router.get("/webhook")
async def webhook_verify(hub_mode: str = None,
                         hub_verify_token: str = None,
                         hub_challenge: str = None):
    if hub_mode == "subscribe" and hub_verify_token == WEBHOOK_VERIFY_TOKEN:
        return int(hub_challenge)
    raise HTTPException(403, "Token de verificación incorrecto")


@router.post("/webhook")
async def webhook_receive(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        data = await request.json()
    except Exception:
        return {"status": "ok"}

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                phone_id = value.get("metadata", {}).get("phone_number_id", "")

                for st in value.get("statuses", []):
                    wamid      = st.get("id")
                    new_status = st.get("status")
                    if not (wamid and new_status):
                        continue

                    # Log visible en Render: aquí verás "delivered", "read",
                    # o "failed" (con el motivo) para cada wamid enviado.
                    err_info = st.get("errors")
                    print(f"[Webhook status] wamid={wamid} status={new_status}" + (f" errors={err_info}" if err_info else ""))

                    res = await db.execute(
                        select(Message).where(Message.wamid == wamid))
                    camp_msg = res.scalar_one_or_none()
                    if camp_msg and camp_msg.status != new_status:
                        camp_msg.status = new_status
                        await db.commit()
                        await recalc_campaign_counters(camp_msg.campaign_id, db)

                    res2 = await db.execute(
                        select(ChatMessage).where(ChatMessage.wamid == wamid))
                    chat_msg = res2.scalar_one_or_none()
                    if chat_msg and chat_msg.status != new_status:
                        chat_msg.status = new_status
                        await db.commit()

                for inc in value.get("messages", []):
                    from_phone = inc.get("from")
                    msg_type   = inc.get("type", "text")
                    wamid_in   = inc.get("id")

                    body = ""
                    if msg_type == "text":
                        body = inc.get("text", {}).get("body", "")
                    elif msg_type == "image":
                        body = "[📷 Imagen recibida]"
                    elif msg_type == "audio":
                        body = "[🎤 Audio recibido]"
                    elif msg_type == "document":
                        body = "[📎 Documento recibido]"
                    elif msg_type == "reaction":
                        body = f"[Reacción: {inc.get('reaction',{}).get('emoji','')}]"
                    else:
                        body = f"[Mensaje tipo: {msg_type}]"

                    contacts_info = value.get("contacts", [{}])
                    contact_name  = (contacts_info[0].get("profile", {})
                                     .get("name", from_phone) if contacts_info else from_phone)

                    owner_res = await db.execute(
                        select(User).where(User.whatsapp_phone_id == phone_id))
                    owner = owner_res.scalar_one_or_none()
                    if not owner:
                        continue

                    conv_res = await db.execute(
                        select(Conversation).where(
                            Conversation.owner_id == owner.id,
                            Conversation.contact_phone == from_phone
                        ))
                    conv = conv_res.scalar_one_or_none()

                    if not conv:
                        conv = Conversation(
                            owner_id=owner.id,
                            contact_phone=from_phone,
                            contact_name=contact_name,
                            status=ConversationStatus.open,
                        )
                        db.add(conv)
                        await db.flush()

                    chat_msg = ChatMessage(
                        conversation_id=conv.id,
                        direction="in",
                        body=body,
                        msg_type=msg_type,
                        wamid=wamid_in,
                        status="delivered",
                    )
                    db.add(chat_msg)

                    conv.last_msg    = body
                    conv.last_msg_at = datetime.utcnow()
                    conv.last_inbound_at = datetime.utcnow()
                    conv.unread      = (conv.unread or 0) + 1
                    conv.status      = ConversationStatus.open

                    await db.commit()

                    await dispatch_webhook_event(owner.id, "message.received", {
                        "phone": from_phone, "contact_name": conv.contact_name,
                        "body": body, "msg_type": msg_type, "conversation_id": conv.id,
                    })

                    # ── Baja / opt-out ──────────────────────────────
                    if msg_type == "text" and body.strip().upper() in OPT_OUT_KEYWORDS:
                        contact_res = await db.execute(select(Contact).where(
                            Contact.owner_id == owner.id, Contact.phone == from_phone))
                        contact = contact_res.scalar_one_or_none()
                        if contact and not contact.opted_out:
                            contact.opted_out = True
                            await db.commit()
                        if owner.whatsapp_token and owner.whatsapp_phone_id:
                            try:
                                confirm = "Listo, ya no recibirás más campañas de nuestra parte. Si necesitas algo, escríbenos por aquí."
                                resp = await send_whatsapp_text(owner.whatsapp_phone_id, owner.whatsapp_token, from_phone, confirm)
                                if "messages" in resp:
                                    db.add(ChatMessage(conversation_id=conv.id, direction="out",
                                                       body=confirm, msg_type="text", status="sent",
                                                       wamid=resp["messages"][0]["id"]))
                                    await db.commit()
                            except Exception as e:
                                print(f"[OptOut] error al confirmar baja: {e}")

                    # ── Auto-respuesta (fuera de horario) ───────────
                    elif owner.whatsapp_token and owner.whatsapp_phone_id and should_send_auto_reply(owner, conv):
                        try:
                            resp = await send_whatsapp_text(owner.whatsapp_phone_id, owner.whatsapp_token, from_phone, owner.auto_reply_message)
                            if "messages" in resp:
                                db.add(ChatMessage(conversation_id=conv.id, direction="out",
                                                   body=owner.auto_reply_message, msg_type="text", status="sent",
                                                   wamid=resp["messages"][0]["id"]))
                                conv.last_auto_reply_at = datetime.utcnow()
                                await db.commit()
                        except Exception as e:
                            print(f"[AutoReply] error: {e}")

    except Exception as e:
        print(f"[Webhook] Error: {e}")

    return {"status": "ok"}


# ── CHAT API ──────────────────────────────────────────────────────

@router.get("/chat/conversations")
async def list_conversations(status: Optional[str] = None,
                             user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    q = select(Conversation).where(Conversation.owner_id == user.id)
    if status:
        q = q.where(Conversation.status == status)
    q = q.order_by(Conversation.last_msg_at.desc().nullslast())
    convs = (await db.execute(q)).scalars().all()
    return [{
        "id": c.id, "contact_phone": c.contact_phone,
        "contact_name": c.contact_name, "status": c.status,
        "unread": c.unread, "last_msg": c.last_msg,
        "last_msg_at": c.last_msg_at.isoformat() if c.last_msg_at else None,
        "created_at": c.created_at.isoformat(),
    } for c in convs]


@router.get("/chat/conversations/{conv_id}/messages")
async def get_conv_messages(conv_id: int,
                            user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Conversation).where(Conversation.id == conv_id,
                                   Conversation.owner_id == user.id))
    conv = res.scalar_one_or_none()
    if not conv:
        raise HTTPException(404, "Conversación no encontrada")

    conv.unread = 0
    await db.commit()

    msgs = (await db.execute(
        select(ChatMessage).where(ChatMessage.conversation_id == conv_id)
        .order_by(ChatMessage.created_at)
    )).scalars().all()

    return {
        "conversation": {
            "id": conv.id, "contact_name": conv.contact_name,
            "contact_phone": conv.contact_phone, "status": conv.status,
        },
        "messages": [{
            "id": m.id, "direction": m.direction, "body": m.body,
            "msg_type": m.msg_type, "status": m.status,
            "created_at": m.created_at.isoformat(),
        } for m in msgs]
    }


@router.post("/chat/conversations/{conv_id}/reply")
async def reply_to_conversation(conv_id: int,
                                data: ChatReplyIn,
                                user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "chat_support"):
        raise HTTPException(403, "Tu cuenta de agente no puede responder el chat")
    if not user.whatsapp_token or not user.whatsapp_phone_id:
        raise HTTPException(400, "Tu línea de WhatsApp aún no está conectada. Contacta al administrador.")

    res = await db.execute(
        select(Conversation).where(Conversation.id == conv_id,
                                   Conversation.owner_id == user.id))
    conv = res.scalar_one_or_none()
    if not conv:
        raise HTTPException(404)

    within = await is_within_24h_window(user.id, conv.contact_phone, db)
    used_template = False
    body_preview = (data.body or "").strip()

    if within and body_preview:
        resp = await send_whatsapp_text(
            user.whatsapp_phone_id, user.whatsapp_token,
            conv.contact_phone, body_preview
        )
    elif data.template_name:
        # Fuera de ventana (o respuesta forzada con plantilla)
        vars_ = data.template_variables or []
        resp = await send_whatsapp_template(
            user.whatsapp_phone_id, user.whatsapp_token,
            conv.contact_phone,
            data.template_name,
            data.template_language or "es",
            vars_,
        )
        used_template = True
        if not body_preview:
            body_preview = f"[Plantilla: {data.template_name}]" + (
                f" ({', '.join(vars_)})" if vars_ else ""
            )
    else:
        raise HTTPException(
            400,
            "El contacto está fuera de la ventana de 24h de WhatsApp. "
            "Debes enviar una plantilla aprobada por Meta (ej. mensaje_inicial) "
            "para reabrir la conversación. Usa template_name + template_language."
        )

    wamid = None
    err_detail = None
    if "messages" in resp:
        wamid = resp["messages"][0]["id"]
        status = "sent"
    else:
        status = "failed"
        err_detail = (resp.get("error") or {}).get("message") if isinstance(resp, dict) else str(resp)

    msg = ChatMessage(
        conversation_id=conv.id,
        direction="out",
        body=body_preview,
        msg_type="template" if used_template else "text",
        wamid=wamid,
        status=status,
    )
    db.add(msg)
    conv.last_msg = body_preview
    conv.last_msg_at = datetime.utcnow()
    if conv.status == ConversationStatus.closed:
        conv.status = ConversationStatus.open
    await db.commit()
    await db.refresh(msg)

    out = {
        "id": msg.id, "direction": "out", "body": msg.body,
        "status": msg.status, "msg_type": msg.msg_type,
        "used_template": used_template,
        "within_24h": within,
        "created_at": msg.created_at.isoformat(),
    }
    if err_detail:
        out["error"] = err_detail
    return out


@router.post("/chat/start")
async def start_conversation(
    data: ChatStartIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Inicia (o reabre) una conversación con un contacto enviando una
    plantilla HSM de Meta. Obligatorio cuando el contacto nunca ha
    escrito o lleva más de 24h sin escribir.

    Caso típico: plantilla marketing 'mensaje_inicial' (Spanish HND → es_HN).
    """
    if not has_permission(user, "chat_support"):
        raise HTTPException(403, "Tu cuenta de agente no puede iniciar chats")
    if not user.whatsapp_token or not user.whatsapp_phone_id:
        raise HTTPException(400, "Tu línea de WhatsApp aún no está conectada. Contacta al administrador.")
    if not data.template_name or not data.template_name.strip():
        raise HTTPException(400, "template_name es obligatorio (nombre exacto en Meta, ej. mensaje_inicial)")

    phone = None
    name = (data.contact_name or "").strip() or "Desconocido"

    if data.contact_id:
        cres = await db.execute(
            select(Contact).where(Contact.id == data.contact_id, Contact.owner_id == user.id)
        )
        contact = cres.scalar_one_or_none()
        if not contact:
            raise HTTPException(404, "Contacto no encontrado")
        phone = normalize_phone(contact.phone or "")
        name = contact.name or name
    elif data.phone:
        phone = normalize_phone(data.phone)
    else:
        raise HTTPException(400, "Indica contact_id o phone")

    if not phone:
        raise HTTPException(400, "Teléfono inválido. Usa solo dígitos con código de país (ej. 50495855720)")

    # Buscar conversación existente o crear una nueva
    cres = await db.execute(
        select(Conversation).where(
            Conversation.owner_id == user.id,
            Conversation.contact_phone == phone,
        )
    )
    conv = cres.scalar_one_or_none()
    if not conv:
        conv = Conversation(
            owner_id=user.id,
            contact_phone=phone,
            contact_name=name,
            status=ConversationStatus.open,
            unread=0,
        )
        db.add(conv)
        await db.flush()
    else:
        conv.contact_name = name or conv.contact_name
        conv.status = ConversationStatus.open

    vars_ = data.template_variables or []
    lang = (data.template_language or "es").strip()
    # Meta a veces registra Spanish (HND) como es_HN
    resp = await send_whatsapp_template(
        user.whatsapp_phone_id, user.whatsapp_token,
        phone,
        data.template_name.strip(),
        lang,
        vars_,
    )

    body_preview = f"[Plantilla: {data.template_name}]" + (
        f" ({', '.join(vars_)})" if vars_ else ""
    )
    wamid = None
    err_detail = None
    if "messages" in resp:
        wamid = resp["messages"][0]["id"]
        status = "sent"
    else:
        status = "failed"
        err_detail = (resp.get("error") or {}).get("message") if isinstance(resp, dict) else str(resp)

    msg = ChatMessage(
        conversation_id=conv.id,
        direction="out",
        body=body_preview,
        msg_type="template",
        wamid=wamid,
        status=status,
    )
    db.add(msg)
    conv.last_msg = body_preview
    conv.last_msg_at = datetime.utcnow()
    await db.commit()
    await db.refresh(msg)
    await db.refresh(conv)

    out = {
        "ok": status == "sent",
        "conversation_id": conv.id,
        "contact_phone": phone,
        "contact_name": conv.contact_name,
        "message": {
            "id": msg.id, "body": msg.body, "status": msg.status,
            "msg_type": "template", "created_at": msg.created_at.isoformat(),
        },
        "template_name": data.template_name,
        "template_language": lang,
    }
    if err_detail:
        out["error"] = err_detail
        raise HTTPException(400, f"Meta rechazó el envío: {err_detail}")
    return out


@router.get("/chat/meta-templates")
async def list_chat_meta_templates(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Plantillas HSM sincronizadas de esta empresa, para el selector
    de 'Iniciar conversación' en el chat. Solo APPROVED.
    """
    if not has_permission(user, "chat_support"):
        raise HTTPException(403, "Sin permiso de chat")
    rows = (await db.execute(
        select(MetaTemplate).where(
            MetaTemplate.client_id == user.id,
            MetaTemplate.status == "APPROVED",
        ).order_by(MetaTemplate.name)
    )).scalars().all()
    return [{
        "id": t.id,
        "name": t.name,
        "language": t.language,
        "category": t.category,
        "body_text": t.body_text,
        "variable_count": t.variable_count or 0,
    } for t in rows]


@router.patch("/chat/conversations/{conv_id}/status")
async def update_conv_status(conv_id: int,
                             data: ConvStatusIn,
                             user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Conversation).where(Conversation.id == conv_id,
                                   Conversation.owner_id == user.id))
    conv = res.scalar_one_or_none()
    if not conv:
        raise HTTPException(404)
    try:
        conv.status = ConversationStatus(data.status)
    except ValueError:
        raise HTTPException(400, "Estado inválido")
    await db.commit()
    return {"ok": True, "status": conv.status}


@router.post("/chat/conversations/{conv_id}/send_document")
async def send_chat_document(
    conv_id: int,
    file: UploadFile = File(...),
    caption: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Manda un documento puntual DENTRO de una conversación — el caso de
    "el cliente pide por chat una copia de su factura/receta y se la
    mando en el momento". Las campañas masivas ya NO llevan adjunto
    (se movió aquí a propósito, ver /api/campaigns).
    """
    if not has_permission(user, "chat_support"):
        raise HTTPException(403, "Tu cuenta de agente no puede responder el chat")
    if not user.whatsapp_token or not user.whatsapp_phone_id:
        raise HTTPException(400, "Tu línea de WhatsApp aún no está conectada. Contacta al administrador.")

    res = await db.execute(
        select(Conversation).where(Conversation.id == conv_id, Conversation.owner_id == user.id))
    conv = res.scalar_one_or_none()
    if not conv:
        raise HTTPException(404, "Conversación no encontrada")

    ext = (file.filename.rsplit(".", 1)[-1] if "." in file.filename else "pdf").lower()
    if ext not in ("pdf", "jpg", "jpeg", "png"):
        raise HTTPException(400, "Solo se pueden enviar PDF o imágenes (jpg/png) por chat")

    chat_uploads_dir = "uploads/chat"
    os.makedirs(chat_uploads_dir, exist_ok=True)
    file_id = f"{uuid.uuid4()}.{ext}"
    content = await file.read()
    with open(f"{chat_uploads_dir}/{file_id}", "wb") as f:
        f.write(content)

    public_url = f"{get_public_base_url()}/uploads/chat/{file_id}"

    if ext == "pdf":
        resp = await send_whatsapp_document(
            phone_id=user.whatsapp_phone_id, token=user.whatsapp_token,
            to_phone=conv.contact_phone, document_url=public_url,
            filename=file.filename, caption=caption or ""
        )
    else:
        resp = await send_whatsapp_image(
            phone_id=user.whatsapp_phone_id, token=user.whatsapp_token,
            to_phone=conv.contact_phone, image_url=public_url, caption=caption or ""
        )

    wamid = None
    status = "failed"
    if "messages" in resp:
        wamid = resp["messages"][0]["id"]
        status = "sent"

    display_body = f"[📎 {file.filename}]" + (f" — {caption}" if caption else "")
    msg = ChatMessage(
        conversation_id=conv.id, direction="out",
        body=display_body, msg_type="document" if ext == "pdf" else "image",
        wamid=wamid, status=status,
    )
    db.add(msg)
    conv.last_msg    = display_body
    conv.last_msg_at = datetime.utcnow()
    await db.commit()
    await db.refresh(msg)

    if status == "failed":
        raise HTTPException(400, f"WhatsApp no aceptó el envío: {resp.get('error', {}).get('message', 'error desconocido')}")

    return {"id": msg.id, "status": msg.status, "url": public_url}


class ChatSettingsIn(BaseModel):
    auto_reply_enabled: Optional[bool] = None
    auto_reply_message: Optional[str] = None
    business_hours: Optional[dict] = None  # {"mon": ["08:00","17:00"], ...}


@router.get("/chat/settings")
async def get_chat_settings(user: User = Depends(get_current_user)):
    try:
        hours = json.loads(user.business_hours) if user.business_hours else {}
    except (ValueError, TypeError):
        hours = {}
    return {
        "auto_reply_enabled": bool(user.auto_reply_enabled),
        "auto_reply_message": user.auto_reply_message or "",
        "business_hours": hours,
    }


@router.patch("/chat/settings")
async def update_chat_settings(
    data: ChatSettingsIn,
    user: User = Depends(require_owner_or_superadmin),
    db: AsyncSession = Depends(get_db)
):
    if data.auto_reply_enabled is not None:
        user.auto_reply_enabled = data.auto_reply_enabled
    if data.auto_reply_message is not None:
        user.auto_reply_message = data.auto_reply_message
    if data.business_hours is not None:
        user.business_hours = json.dumps(data.business_hours)
    await db.commit()
    return {"ok": True}


@router.get("/chat/unread_count")
async def unread_count(user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Conversation).where(Conversation.owner_id == user.id,
                                   Conversation.unread > 0))
    count = len(res.scalars().all())
    return {"unread_conversations": count}


# ── Etiquetas gestionadas ────────────────────────────────────────
# Reemplaza el texto libre de Contact.tag por un catálogo propio por
# empresa (nombre + color + descripción), como en la mayoría de
# plataformas de marketing por WhatsApp — evita duplicados como
# "Vip" / "vip " / "VIP" y se ve mejor en la UI.

class ContactTagIn(BaseModel):
    name: str
    color: str = "#22d3a0"
    description: Optional[str] = None


@router.get("/tags")
async def list_tags(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(ContactTag).where(ContactTag.client_id == user.id))
    return [{"id": t.id, "name": t.name, "color": t.color, "description": t.description}
            for t in res.scalars().all()]


@router.post("/tags")
async def create_tag(data: ContactTagIn, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "manage_contacts"):
        raise HTTPException(403, "Tu cuenta de agente no puede gestionar etiquetas")
    dup = await db.execute(select(ContactTag).where(
        ContactTag.client_id == user.id, ContactTag.name == data.name))
    if dup.scalar_one_or_none():
        raise HTTPException(400, f"Ya existe una etiqueta llamada '{data.name}'")
    t = ContactTag(client_id=user.id, name=data.name, color=data.color, description=data.description)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"id": t.id, "name": t.name, "color": t.color, "description": t.description}


@router.patch("/tags/{tag_id}")
async def update_tag(tag_id: int, data: ContactTagIn, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "manage_contacts"):
        raise HTTPException(403, "Tu cuenta de agente no puede gestionar etiquetas")
    res = await db.execute(select(ContactTag).where(ContactTag.id == tag_id, ContactTag.client_id == user.id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Etiqueta no encontrada")
    old_name = t.name
    t.name, t.color, t.description = data.name, data.color, data.description
    if old_name != data.name:
        # Propagar el renombre a los contactos que ya la tenían.
        contacts_res = await db.execute(select(Contact).where(Contact.owner_id == user.id, Contact.tag == old_name))
        for c in contacts_res.scalars().all():
            c.tag = data.name
    await db.commit()
    return {"ok": True}


@router.delete("/tags/{tag_id}")
async def delete_tag(tag_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not has_permission(user, "manage_contacts"):
        raise HTTPException(403, "Tu cuenta de agente no puede gestionar etiquetas")
    res = await db.execute(select(ContactTag).where(ContactTag.id == tag_id, ContactTag.client_id == user.id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Etiqueta no encontrada")
    await db.delete(t)
    await db.commit()
    return {"ok": True}


# ── Integraciones: API Keys ──────────────────────────────────────
# Para que la empresa conecte WaSapinf con SUS propios sistemas (su
# sitio, su ERP, su hoja de planillas) sin depender de ti. Solo el
# dueño de la empresa las administra (no los agentes), igual que
# conectar WhatsApp.

class ApiKeyIn(BaseModel):
    name: str


@router.get("/integrations/api-keys")
async def list_api_keys(user: User = Depends(require_owner_or_superadmin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(ApiKey).where(ApiKey.client_id == user.id).order_by(ApiKey.created_at.desc()))
    return [{
        "id": k.id, "name": k.name, "key_prefix": k.key_prefix, "is_active": k.is_active,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    } for k in res.scalars().all()]


@router.post("/integrations/api-keys")
async def create_api_key(data: ApiKeyIn, user: User = Depends(require_owner_or_superadmin), db: AsyncSession = Depends(get_db)):
    raw, prefix, key_hash = generate_api_key()
    k = ApiKey(client_id=user.id, name=data.name, key_prefix=prefix, key_hash=key_hash)
    db.add(k)
    await db.commit()
    await db.refresh(k)
    return {
        "id": k.id, "name": k.name, "key_prefix": k.key_prefix,
        # SOLO aquí, en la respuesta de creación, va la key completa —
        # no se vuelve a poder ver después (ni nosotros la guardamos en claro).
        "api_key": raw,
    }


@router.delete("/integrations/api-keys/{key_id}")
async def revoke_api_key(key_id: int, user: User = Depends(require_owner_or_superadmin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.client_id == user.id))
    k = res.scalar_one_or_none()
    if not k:
        raise HTTPException(404, "API key no encontrada")
    await db.delete(k)
    await db.commit()
    return {"ok": True}


# ── Integraciones: Webhooks ──────────────────────────────────────

class WebhookIn(BaseModel):
    url: str
    events: List[str]
    is_active: bool = True


@router.get("/integrations/webhook-events")
async def list_webhook_events(user: User = Depends(get_current_user)):
    return [{"key": k, "label": v} for k, v in WEBHOOK_EVENTS.items()]


@router.get("/integrations/webhooks")
async def list_webhooks(user: User = Depends(require_owner_or_superadmin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(WebhookSubscription).where(WebhookSubscription.client_id == user.id))
    return [{
        "id": w.id, "url": w.url, "events": w.get_events(), "is_active": w.is_active,
        "last_triggered_at": w.last_triggered_at.isoformat() if w.last_triggered_at else None,
        "last_status": w.last_status,
    } for w in res.scalars().all()]


@router.post("/integrations/webhooks")
async def create_webhook(data: WebhookIn, user: User = Depends(require_owner_or_superadmin), db: AsyncSession = Depends(get_db)):
    if not data.url.startswith("https://") and not data.url.startswith("http://"):
        raise HTTPException(400, "La URL debe empezar con http:// o https://")
    w = WebhookSubscription(client_id=user.id, url=data.url, is_active=data.is_active,
                            secret=secrets.token_hex(24))
    w.set_events(data.events)
    db.add(w)
    await db.commit()
    await db.refresh(w)
    return {"id": w.id, "url": w.url, "events": w.get_events(), "secret": w.secret}


@router.patch("/integrations/webhooks/{webhook_id}")
async def update_webhook(webhook_id: int, data: WebhookIn, user: User = Depends(require_owner_or_superadmin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(WebhookSubscription).where(
        WebhookSubscription.id == webhook_id, WebhookSubscription.client_id == user.id))
    w = res.scalar_one_or_none()
    if not w:
        raise HTTPException(404, "Webhook no encontrado")
    w.url = data.url
    w.is_active = data.is_active
    w.set_events(data.events)
    await db.commit()
    return {"ok": True}


@router.delete("/integrations/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: int, user: User = Depends(require_owner_or_superadmin), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(WebhookSubscription).where(
        WebhookSubscription.id == webhook_id, WebhookSubscription.client_id == user.id))
    w = res.scalar_one_or_none()
    if not w:
        raise HTTPException(404, "Webhook no encontrado")
    await db.delete(w)
    await db.commit()
    return {"ok": True}