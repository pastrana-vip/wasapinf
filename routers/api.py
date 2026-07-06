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

# ───────────────────────────────────────────────6─────────────────
# CONFIGURACIÓN META  (reemplaza con tus valores reales)
# ────────────────────────────────────────────────────────────────
META_APP_ID      = os.getenv ("META_APP_ID")          # developers.facebook.com
META_APP_SECRET  = os.getenv ("META_APP_SECRET")
WEBHOOK_VERIFY_TOKEN  = os.getenv ("WEBHOOK_VERIFY_TOKEN","wablast_webhook_secret_2025")

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
                "type": "owner",           # ← Tipo principal
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
                "type": "agent",                    # ← Tipo agente
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
    user.whatsapp_token    = data.whatsapp_token    or None
    user.whatsapp_phone_id = data.whatsapp_phone_id or None
    user.profile_name      = data.profile_name      or None
    user.waba_id           = data.waba_id           or None
    await db.commit()
    return {"ok": True}

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
    """
    Meta redirige aquí después de que el usuario autoriza en Facebook.
    Intercambiamos el code por un token de larga duración y enviamos
    los datos al popup padre via postMessage.
    """
    if error:
        return HTMLResponse(_popup_close_html("error", {"error": error}))

    if not code:
        return HTMLResponse(_popup_close_html("error", {"error": "No se recibió código de autorización"}))

    # 1. Intercambiar code → short-lived token
    redirect_uri = str(request.url_for("facebook_callback"))
    token_url = (
        f"https://graph.facebook.com/v{19}.0/oauth/access_token"
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

    # 2. Convertir a token de larga duración (60 días)
    long_token = short_token  # fallback
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r2 = await client.get(
                f"https://graph.facebook.com/v19.0/oauth/access_token"
                f"?grant_type=fb_exchange_token"
                f"&client_id={META_APP_ID}"
                f"&client_secret={META_APP_SECRET}"
                f"&fb_exchange_token={short_token}"
            )
            ld = r2.json()
            if "access_token" in ld:
                long_token = ld["access_token"]
    except Exception:
        pass  # Usamos el short token si falla

    # 3. Obtener WhatsApp Business Accounts del usuario
    waba_id = None
    phone_numbers = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # Obtener WABAs vinculadas
            r3 = await client.get(
                "https://graph.facebook.com/v19.0/me/businesses",
                headers={"Authorization": f"Bearer {long_token}"}
            )
            biz_data = r3.json()
            if biz_data.get("data"):
                biz_id = biz_data["data"][0]["id"]
                # Obtener WABAs de este negocio
                r4 = await client.get(
                    f"https://graph.facebook.com/v19.0/{biz_id}/owned_whatsapp_business_accounts",
                    headers={"Authorization": f"Bearer {long_token}"}
                )
                waba_data = r4.json()
                if waba_data.get("data"):
                    waba_id = waba_data["data"][0]["id"]
                    # Obtener números de teléfono de esta WABA
                    r5 = await client.get(
                        f"https://graph.facebook.com/v19.0/{waba_id}/phone_numbers"
                        f"?fields=id,display_phone_number,verified_name,quality_rating",
                        headers={"Authorization": f"Bearer {long_token}"}
                    )
                    phones_data = r5.json()
                    phone_numbers = phones_data.get("data", [])
    except Exception as e:
        print(f"[OAuth] Error al obtener WABA/phones: {e}")

    # 4. Enviar todo al popup padre via postMessage (el JS del frontend lo escucha)
    payload = {
        "token":    long_token,
        "waba_id":  waba_id or "",
        "phones":   phone_numbers,  # lista de {id, display_phone_number, verified_name}
    }
    return HTMLResponse(_popup_close_html("success", payload))


def _popup_close_html(status: str, data: dict) -> str:
    """Genera HTML que envía postMessage al padre y cierra el popup."""
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
    por un token de acceso. A diferencia del flujo OAuth clásico,
    el Embedded Signup NO usa redirect_uri.
    """
    code = data.get("code")
    if not code:
        raise HTTPException(400, "Falta el código de autorización")

    # 1. Intercambiar code → token (sin redirect_uri, propio del Embedded Signup)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            "https://graph.facebook.com/v21.0/oauth/access_token",
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

    # 2. Convertir a token de larga duración
    long_token = short_token
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r2 = await client.get(
                "https://graph.facebook.com/v21.0/oauth/access_token",
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

    # 3. Obtener WABAs y números de teléfono asociados al negocio
    waba_id = None
    phone_numbers = []
    primary_phone_id = None
    if phone_numbers:
        primary_phone_id = phone_numbers[0].get("id")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r3 = await client.get(
                "https://graph.facebook.com/v21.0/me/businesses",
                headers={"Authorization": f"Bearer {long_token}"}
            )
            biz_data = r3.json()
            if biz_data.get("data"):
                for biz in biz_data["data"]:
                    biz_id = biz["id"]
                    r4 = await client.get(
                        f"https://graph.facebook.com/v21.0/{biz_id}/owned_whatsapp_business_accounts",
                        headers={"Authorization": f"Bearer {long_token}"}
                    )
                    waba_data = r4.json()
                    if waba_data.get("data"):
                        waba_id = waba_data["data"][0]["id"]
                        r5 = await client.get(
                            f"https://graph.facebook.com/v21.0/{waba_id}/phone_numbers",
                            params={"fields": "id,display_phone_number,verified_name,quality_rating"},
                            headers={"Authorization": f"Bearer {long_token}"}
                        )
                        phones_data = r5.json()
                        phone_numbers = phones_data.get("data", [])
                        if phone_numbers:
                            break
    except Exception as e:
        print(f"[OAuth Exchange] Error al obtener WABA/phones: {e}")

    return {
        "ok": True,
        "token": long_token,
        "waba_id": waba_id or "",
        "phones": phone_numbers,
        "phone_id": primary_phone_id,
    }
# Endpoint para que el frontend guarde el token/phone recibido del popup
@router.post("/auth/facebook/save")
async def save_facebook_connection(
    data: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Guarda el token OAuth de Facebook + Phone ID seleccionado por el usuario."""
    token    = data.get("token")
    phone_id = data.get("phone_id")
    profile  = data.get("profile_name")
    waba_id  = data.get("waba_id")

    if not token or not phone_id:
        raise HTTPException(400, "Token y Phone ID son obligatorios")

    user.whatsapp_token = token
    user.whatsapp_phone_id = phone_id
    user.profile_name = profile or None
    user.waba_id = waba_id or None
    await db.commit()
    await db.refresh(user)
    return {"ok": True, "message": "Línea de WhatsApp conectada exitosamente", "phone_id": user.whatsapp_phone_id}


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
    if not user.whatsapp_token:
        raise HTTPException(400, "Configura tu WhatsApp API Token primero.")
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
    # Recalcular contadores frescos desde los mensajes reales
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
    """
    Recibe notificaciones de Meta:
    - Actualizaciones de estado de mensajes de campaña (sent/delivered/read/failed)
    - Mensajes entrantes de clientes (para el módulo de chat)
    """
    try:
        data = await request.json()
    except Exception:
        return {"status": "ok"}

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                phone_id = value.get("metadata", {}).get("phone_number_id", "")

                # 1. Actualizaciones de estado (delivered, read, failed)
                for st in value.get("statuses", []):
                    wamid      = st.get("id")
                    new_status = st.get("status")   # sent/delivered/read/failed
                    if not (wamid and new_status):
                        continue

                    # Intentar actualizar mensaje de campaña
                    res = await db.execute(
                        select(Message).where(Message.wamid == wamid))
                    camp_msg = res.scalar_one_or_none()
                    if camp_msg and camp_msg.status != new_status:
                        camp_msg.status = new_status
                        # Recalcular contadores de campaña al terminar (evita dobles)
                        await db.commit()
                        await recalc_campaign_counters(camp_msg.campaign_id, db)

                    # También actualizar mensajes de chat salientes
                    res2 = await db.execute(
                        select(ChatMessage).where(ChatMessage.wamid == wamid))
                    chat_msg = res2.scalar_one_or_none()
                    if chat_msg and chat_msg.status != new_status:
                        chat_msg.status = new_status
                        await db.commit()

                # 2. Mensajes ENTRANTES (clientes escribiendo)
                for inc in value.get("messages", []):
                    from_phone = inc.get("from")
                    msg_type   = inc.get("type", "text")
                    wamid_in   = inc.get("id")

                    # Extraer cuerpo del mensaje
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

                    # Nombre del contacto (si Meta lo provee)
                    contacts_info = value.get("contacts", [{}])
                    contact_name  = (contacts_info[0].get("profile", {})
                                     .get("name", from_phone) if contacts_info else from_phone)

                    # Encontrar dueño por phone_id (la empresa que recibe el mensaje)
                    owner_res = await db.execute(
                        select(User).where(User.whatsapp_phone_id == phone_id))
                    owner = owner_res.scalar_one_or_none()
                    if not owner:
                        continue

                    # Buscar conversación existente
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

                    # Guardar mensaje entrante
                    chat_msg = ChatMessage(
                        conversation_id=conv.id,
                        direction="in",
                        body=body,
                        msg_type=msg_type,
                        wamid=wamid_in,
                        status="delivered",
                    )
                    db.add(chat_msg)

                    # Actualizar conversación
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
    # Verificar que pertenece al usuario
    res = await db.execute(
        select(Conversation).where(Conversation.id == conv_id,
                                   Conversation.owner_id == user.id))
    conv = res.scalar_one_or_none()
    if not conv:
        raise HTTPException(404, "Conversación no encontrada")

    # Marcar como leída
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

    # Enviar via API de Meta
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

    # Guardar mensaje saliente
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
