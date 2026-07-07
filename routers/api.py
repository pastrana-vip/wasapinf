from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime
import httpx
import os

from models.database import (
    User, Campaign, Contact, Message, CampaignStatus,
    Conversation, ConversationStatus, ChatMessage, Agent, AgentRole, get_db
)
from auth import hash_password, verify_password, create_token, get_current_user
from whatsapp_service import run_campaign, recalc_campaign_counters, send_whatsapp_text


router = APIRouter()

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

class ContactIn(BaseModel):
    name: str
    phone: str
    tag: Optional[str] = None

class CampaignIn(BaseModel):
    name: str
    message_template: str
    contact_tag: Optional[str] = None

class ChatReplyIn(BaseModel):
    body: str

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
    return {"token": create_token(user.id, user.email),
            "user": {"id": user.id, "name": user.name, "email": user.email}}


@router.post("/auth/login")
async def login(data: LoginIn, db: AsyncSession = Depends(get_db)):
    # === 1. Intentar login como Usuario Principal (Dueño) ===
    res = await db.execute(select(User).where(User.email == data.email))
    user = res.scalar_one_or_none()

    if user and verify_password(data.password, user.hashed_password):
        return {
            "token": create_token(user.id, user.email),
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "type": "owner",
                "role": "admin"
            }
        }

    # === 2. Intentar login como Agente ===
    res2 = await db.execute(
        select(Agent).where(Agent.email == data.email, Agent.is_active == True)
    )
    agent = res2.scalar_one_or_none()

    if agent and verify_password(data.password, agent.hashed_password):
        return {
            "token": create_token(agent.id, agent.email),
            "user": {
                "id": agent.id,
                "name": agent.name,
                "email": agent.email,
                "type": "agent",
                "role": agent.role.value,
                "owner_id": agent.owner_id,
                "whatsapp_phone_id": agent.whatsapp_phone_id
            }
        }

    raise HTTPException(status_code=401, detail="Credenciales incorrectas")


@router.get("/auth/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id, "name": user.name, "email": user.email,
        "has_whatsapp": bool(user.whatsapp_token),
        "whatsapp_phone_id": user.whatsapp_phone_id,
        "profile_name": user.profile_name,
    }

# ── WhatsApp Config (manual) ──────────────────────────────────────

@router.post("/config/whatsapp")
async def save_whatsapp_config(
    data: WhatsAppConfigIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    token    = data.whatsapp_token or None
    phone_id = data.whatsapp_phone_id or None
    waba_id  = data.waba_id or None

    if token and phone_id:
        op_token = provider_token(token)
        reg_result = await register_phone_number(phone_id, op_token)
        if "error" in reg_result:
            print(f"[Register] Aviso (puede ya estar registrado): {reg_result['error']}")
        if waba_id:
            sub_result = await subscribe_app_to_waba(waba_id, op_token)
            if "error" in sub_result:
                print(f"[Subscribe] Error al suscribir app al WABA: {sub_result['error']}")

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
        r = await client.post(
            f"{GRAPH_BASE}/{phone_id}/register",
            headers={"Authorization": f"Bearer {token}"},
            json={"messaging_product": "whatsapp", "pin": pin},
        )
        data = r.json()
        return data


async def subscribe_app_to_waba(waba_id: str, token: str) -> dict:
    """
    Suscribe TU app a los webhooks de la WABA del cliente.
    Sin este paso, tu backend no recibirá notificaciones de entrega/lectura
    ni mensajes entrantes de esa línea, aunque el envío en sí pueda funcionar.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{GRAPH_BASE}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {token}"},
        )
        return r.json()


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
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Guarda el token OAuth de Facebook + Phone ID seleccionado por el usuario,
    y completa los dos pasos que Meta exige antes de poder enviar/recibir
    mensajes por Cloud API: registrar el número y suscribir la app al WABA.
    """
    token    = data.get("token")
    phone_id = data.get("phone_id")
    profile  = data.get("profile_name")
    waba_id  = data.get("waba_id")

    if not token or not phone_id:
        raise HTTPException(400, "Token y Phone ID son obligatorios")

    # Usamos SYSTEM_USER_TOKEN (tuyo) cuando esté disponible: el token del
    # cliente recién emitido a veces todavía no tiene los permisos de
    # negocio propagados y estas dos llamadas fallan intermitentemente.
    op_token = provider_token(token)

    # 1. Registrar el número en Cloud API (idempotente: si ya está
    #    registrado, Meta devuelve error y simplemente lo ignoramos).
    reg_result = await register_phone_number(phone_id, op_token)
    if "error" in reg_result:
        print(f"[Register] Aviso (puede ya estar registrado): {reg_result['error']}")

    # 2. Suscribir tu app a los webhooks de esa WABA (necesario para
    #    recibir estados de entrega/lectura y mensajes entrantes).
    sub_result = {}
    if waba_id:
        sub_result = await subscribe_app_to_waba(waba_id, op_token)
        if "error" in sub_result:
            print(f"[Subscribe] Error al suscribir app al WABA: {sub_result['error']}")

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

@router.post("/contacts")
async def add_contact(data: ContactIn,
                      user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    c = Contact(owner_id=user.id, name=data.name, phone=data.phone, tag=data.tag)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return {"id": c.id, "name": c.name, "phone": c.phone, "tag": c.tag}


@router.get("/contacts")
async def list_contacts(user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Contact).where(Contact.owner_id == user.id))
    return [{"id": c.id, "name": c.name, "phone": c.phone, "tag": c.tag}
            for c in res.scalars().all()]


@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: int,
                         user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Contact).where(Contact.id == contact_id, Contact.owner_id == user.id))
    c = res.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Contacto no encontrado")
    await db.delete(c)
    await db.commit()
    return {"ok": True}

# ── Campaigns ─────────────────────────────────────────────────────

@router.post("/campaigns")
async def create_campaign(data: CampaignIn,
                          user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    q = select(Contact).where(Contact.owner_id == user.id)
    if data.contact_tag:
        q = q.where(Contact.tag == data.contact_tag)
    contacts = (await db.execute(q)).scalars().all()
    if not contacts:
        raise HTTPException(400, "No hay contactos. Agrega contactos primero.")

    camp = Campaign(owner_id=user.id, name=data.name,
                    message_template=data.message_template,
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
    if not user.whatsapp_token or not user.whatsapp_phone_id:
        raise HTTPException(400, "Configura tu WhatsApp API Token y Phone ID primero.")
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
                    conv.unread      = (conv.unread or 0) + 1
                    conv.status      = ConversationStatus.open

                    await db.commit()

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
    if not user.whatsapp_token:
        raise HTTPException(400, "Configura tu WhatsApp API antes de responder.")

    res = await db.execute(
        select(Conversation).where(Conversation.id == conv_id,
                                   Conversation.owner_id == user.id))
    conv = res.scalar_one_or_none()
    if not conv:
        raise HTTPException(404)

    resp = await send_whatsapp_text(
        user.whatsapp_phone_id, user.whatsapp_token,
        conv.contact_phone, data.body
    )

    wamid = None
    if "messages" in resp:
        wamid = resp["messages"][0]["id"]
        status = "sent"
    else:
        status = "failed"

    msg = ChatMessage(
        conversation_id=conv.id,
        direction="out",
        body=data.body,
        wamid=wamid,
        status=status,
    )
    db.add(msg)
    conv.last_msg    = data.body
    conv.last_msg_at = datetime.utcnow()
    await db.commit()
    await db.refresh(msg)

    return {
        "id": msg.id, "direction": "out", "body": msg.body,
        "status": msg.status, "created_at": msg.created_at.isoformat()
    }


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


@router.get("/chat/unread_count")
async def unread_count(user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Conversation).where(Conversation.owner_id == user.id,
                                   Conversation.unread > 0))
    count = len(res.scalars().all())
    return {"unread_conversations": count}