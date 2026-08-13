# routers/external_api.py
#
# API pública para que cada empresa integre WaSapinf con SUS PROPIOS
# sistemas (su ERP, su sitio, su hoja de planillas) — autenticada por
# API Key (Authorization: Bearer <api_key>), no por sesión de usuario.
#
# Alcance intencionalmente acotado: lo que un sistema externo
# legítimamente necesita (contactos, etiquetas, campañas, plantillas,
# envío de mensajes) — nunca acciones administrativas sensibles como
# conectar WhatsApp, crear agentes, o gestionar otros clientes. Si se
# filtra una API key, el daño posible queda limitado a los datos de
# ESA empresa, en las mismas operaciones que ya podía hacer un agente.
#
# Diseño inspirado en la API pública de Wasapi.io (Contactos, Etiquetas,
# Campañas, WhatsApp) — mismo tipo de recursos, adaptado a lo que
# soporta esta plataforma hoy.
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from models.database import (
    User, Contact, ContactTag, Campaign, CampaignTemplate, get_db
)
from auth import get_client_from_api_key
from whatsapp_service import send_whatsapp_text, is_within_24h_window, send_whatsapp_template

router = APIRouter(prefix="/v1", tags=["API Externa (API Key)"])


# ── Usuario / cuenta ─────────────────────────────────────────────

@router.get("/me")
async def whoami(user: User = Depends(get_client_from_api_key)):
    """Confirma qué cuenta pertenece a la API key usada — útil para probar la integración."""
    return {
        "company_name": user.name,
        "industry": user.industry,
        "whatsapp_connected": bool(user.whatsapp_token),
        "whatsapp_phone_id": user.whatsapp_phone_id,
    }


# ── Contactos ────────────────────────────────────────────────────

class ExtContactIn(BaseModel):
    name: str
    phone: str
    tag: Optional[str] = None
    custom_fields: Optional[dict] = None


def _ext_contact_out(c: Contact) -> dict:
    return {
        "id": c.id, "name": c.name, "phone": c.phone, "tag": c.tag,
        "opted_out": c.opted_out, "custom_fields": c.get_fields(),
    }


@router.get("/contacts")
async def ext_list_contacts(
    tag: Optional[str] = None,
    limit: int = 100,
    user: User = Depends(get_client_from_api_key),
    db: AsyncSession = Depends(get_db)
):
    q = select(Contact).where(Contact.owner_id == user.id)
    if tag:
        q = q.where(Contact.tag == tag)
    res = await db.execute(q.limit(min(limit, 500)))
    return [_ext_contact_out(c) for c in res.scalars().all()]


@router.post("/contacts")
async def ext_create_contact(
    data: ExtContactIn,
    user: User = Depends(get_client_from_api_key),
    db: AsyncSession = Depends(get_db)
):
    """Crea un contacto, o actualiza uno existente si el teléfono ya está registrado (upsert)."""
    from webhooks_service import dispatch_webhook_event
    existing = await db.execute(select(Contact).where(Contact.owner_id == user.id, Contact.phone == data.phone))
    c = existing.scalar_one_or_none()
    is_new = c is None
    if not c:
        c = Contact(owner_id=user.id, name=data.name, phone=data.phone, tag=data.tag)
    else:
        c.name = data.name
        if data.tag is not None:
            c.tag = data.tag
    if data.custom_fields is not None:
        merged = c.get_fields()
        merged.update(data.custom_fields)
        c.set_fields(merged)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    if is_new:
        await dispatch_webhook_event(user.id, "contact.created", {"id": c.id, "name": c.name, "phone": c.phone})
    return _ext_contact_out(c)


@router.get("/contacts/{contact_id}")
async def ext_get_contact(contact_id: int, user: User = Depends(get_client_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Contact).where(Contact.id == contact_id, Contact.owner_id == user.id))
    c = res.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Contacto no encontrado")
    return _ext_contact_out(c)


@router.put("/contacts/{contact_id}")
async def ext_update_contact(contact_id: int, data: ExtContactIn, user: User = Depends(get_client_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Contact).where(Contact.id == contact_id, Contact.owner_id == user.id))
    c = res.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Contacto no encontrado")
    c.name, c.phone, c.tag = data.name, data.phone, data.tag
    if data.custom_fields is not None:
        c.set_fields(data.custom_fields)
    await db.commit()
    return _ext_contact_out(c)


@router.delete("/contacts/{contact_id}")
async def ext_delete_contact(contact_id: int, user: User = Depends(get_client_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Contact).where(Contact.id == contact_id, Contact.owner_id == user.id))
    c = res.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Contacto no encontrado")
    await db.delete(c)
    await db.commit()
    return {"ok": True}


# ── Etiquetas (solo lectura — se administran desde la app) ──────

@router.get("/tags")
async def ext_list_tags(user: User = Depends(get_client_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(ContactTag).where(ContactTag.client_id == user.id))
    return [{"id": t.id, "name": t.name, "color": t.color} for t in res.scalars().all()]


# ── Plantillas asignadas (solo lectura) ──────────────────────────

@router.get("/templates")
async def ext_list_templates(user: User = Depends(get_client_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(CampaignTemplate).where(
        CampaignTemplate.is_active == True,
        (CampaignTemplate.client_id == user.id) | (CampaignTemplate.client_id.is_(None))
    ))
    return [{"id": t.id, "name": t.name, "category": t.category, "message_template": t.message_template}
            for t in res.scalars().all()]


# ── Campañas (solo lectura de las que ya existen en la app) ─────

@router.get("/campaigns")
async def ext_list_campaigns(user: User = Depends(get_client_from_api_key), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Campaign).where(Campaign.owner_id == user.id).order_by(Campaign.created_at.desc()).limit(100))
    return [{
        "id": c.id, "name": c.name, "status": c.status.value if hasattr(c.status, "value") else c.status,
        "total_contacts": c.total_contacts, "sent": c.sent, "delivered": c.delivered, "failed": c.failed,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    } for c in res.scalars().all()]


# ── WhatsApp: envío directo de un mensaje puntual ────────────────

class ExtSendMessageIn(BaseModel):
    phone: str
    message: Optional[str] = None            # texto libre — solo si el contacto escribió en las últimas 24h
    template_id: Optional[int] = None         # si está fuera de la ventana de 24h, usa una plantilla asignada


@router.post("/messages/send")
async def ext_send_message(
    data: ExtSendMessageIn,
    user: User = Depends(get_client_from_api_key),
    db: AsyncSession = Depends(get_db)
):
    """
    Envía un mensaje puntual a un número — pensado para integraciones
    tipo "avísale a este cliente que su pedido está listo". Respeta la
    misma regla de WhatsApp que el resto de la plataforma: si el
    contacto no ha escrito en las últimas 24h, hay que usar una
    plantilla aprobada (pásale template_id), no texto libre.
    """
    if not user.whatsapp_token or not user.whatsapp_phone_id:
        raise HTTPException(400, "Tu línea de WhatsApp aún no está conectada")

    within_window = await is_within_24h_window(user.id, data.phone, db)

    if data.template_id:
        res_t = await db.execute(select(CampaignTemplate).where(
            CampaignTemplate.id == data.template_id,
            (CampaignTemplate.client_id == user.id) | (CampaignTemplate.client_id.is_(None))
        ))
        template = res_t.scalar_one_or_none()
        if not template:
            raise HTTPException(404, "Plantilla no encontrada o no asignada a tu empresa")
        if within_window:
            resp = await send_whatsapp_text(user.whatsapp_phone_id, user.whatsapp_token, data.phone, template.message_template)
        elif template.meta_template_name:
            resp = await send_whatsapp_template(user.whatsapp_phone_id, user.whatsapp_token, data.phone,
                                                 template.meta_template_name, template.meta_template_language, [data.phone])
        else:
            raise HTTPException(400, "Este contacto está fuera de la ventana de 24h y la plantilla no tiene una plantilla Meta enlazada")
    elif data.message:
        if not within_window:
            raise HTTPException(400, "Este contacto está fuera de la ventana de 24h — usa template_id en vez de message")
        resp = await send_whatsapp_text(user.whatsapp_phone_id, user.whatsapp_token, data.phone, data.message)
    else:
        raise HTTPException(400, "Manda 'message' (dentro de 24h) o 'template_id' (fuera de 24h)")

    if "messages" not in resp:
        raise HTTPException(400, f"WhatsApp rechazó el envío: {resp.get('error', {}).get('message', 'error desconocido')}")

    return {"ok": True, "wamid": resp["messages"][0]["id"]}
