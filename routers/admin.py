# routers/admin.py
#
# Panel de administración de la plataforma (superadmin / PRIME).
# Todo lo expuesto aquí requiere require_superadmin: control total
# sobre las empresas (clientes), conexión de WhatsApp en su nombre,
# plantillas de campaña asignadas por cliente, campos personalizados
# por rubro, plantillas Meta (HSM), recordatorios recurrentes, y
# verificación de agentes/roles en todo el sistema.
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta

from models.database import (
    User, Agent, Campaign, Contact, Message, Conversation, ChatMessage,
    CampaignTemplate, InvoiceBatch, InvoiceItem, CustomFieldDef, MetaTemplate,
    RecurringCampaign, ContactTag, ApiKey, WebhookSubscription,
    AGENT_PERMISSIONS, INDUSTRY_FIELD_SUGGESTIONS, get_db
)
from auth import require_superadmin, hash_password
from routers.api import (
    provider_token, register_phone_number, subscribe_app_to_waba, fetch_meta_templates
)
from whatsapp_service import run_recurring_campaigns
import export_service

router = APIRouter(prefix="/admin", tags=["Admin"])

FAILURE_RATE_ALERT_THRESHOLD = 0.3  # 30% de mensajes fallidos en la última campaña = alerta


# ── Overview / salud del sistema ───────────────────────────────────

@router.get("/overview")
async def system_overview(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Resumen de salud de toda la plataforma: cuántas empresas hay,
    cuántas tienen WhatsApp conectado, volumen de mensajería, fallos
    recientes, y una lista de ALERTAS concretas (token vencido, tasa de
    fallos alta) — para verificar rápido que todo funciona bien sin
    tener que entrar cliente por cliente.
    """
    total_clients = (await db.execute(
        select(func.count(User.id)).where(User.is_superadmin == False)
    )).scalar() or 0

    connected_clients = (await db.execute(
        select(func.count(User.id)).where(
            User.is_superadmin == False, User.whatsapp_phone_id.isnot(None)
        )
    )).scalar() or 0

    total_agents = (await db.execute(select(func.count(Agent.id)))).scalar() or 0
    active_agents = (await db.execute(
        select(func.count(Agent.id)).where(Agent.is_active == True)
    )).scalar() or 0

    total_campaigns = (await db.execute(select(func.count(Campaign.id)))).scalar() or 0
    total_contacts  = (await db.execute(select(func.count(Contact.id)))).scalar() or 0

    sent_count = (await db.execute(
        select(func.count(Message.id)).where(Message.status.in_(["sent", "delivered", "read"]))
    )).scalar() or 0
    failed_count = (await db.execute(
        select(func.count(Message.id)).where(Message.status == "failed")
    )).scalar() or 0

    open_conversations = (await db.execute(
        select(func.count(Conversation.id)).where(Conversation.unread > 0)
    )).scalar() or 0

    # ── Alertas concretas ────────────────────────────────────────
    alerts = []

    error_clients = (await db.execute(
        select(User).where(
            User.is_superadmin == False,
            User.last_whatsapp_error.isnot(None)
        )
    )).scalars().all()
    for c in error_clients:
        alerts.append({
            "type": "whatsapp_error",
            "client_id": c.id,
            "client_name": c.name,
            "message": c.last_whatsapp_error,
            "at": c.last_whatsapp_error_at.isoformat() if c.last_whatsapp_error_at else None,
        })

    recent_campaigns = (await db.execute(
        select(Campaign).where(Campaign.total_contacts > 0)
        .order_by(Campaign.created_at.desc()).limit(30)
    )).scalars().all()
    for camp in recent_campaigns:
        if camp.sent + camp.failed == 0:
            continue
        rate = camp.failed / max(camp.sent + camp.failed, 1)
        if rate >= FAILURE_RATE_ALERT_THRESHOLD:
            owner_res = await db.execute(select(User).where(User.id == camp.owner_id))
            owner = owner_res.scalar_one_or_none()
            alerts.append({
                "type": "high_failure_rate",
                "client_id": camp.owner_id,
                "client_name": owner.name if owner else "—",
                "message": f"Campaña \"{camp.name}\": {camp.failed} de {camp.sent + camp.failed} mensajes fallaron ({round(rate*100)}%)",
                "at": camp.created_at.isoformat() if camp.created_at else None,
            })

    return {
        "clients": {
            "total": total_clients,
            "whatsapp_connected": connected_clients,
            "whatsapp_pending": total_clients - connected_clients,
        },
        "agents": {"total": total_agents, "active": active_agents},
        "campaigns": {"total": total_campaigns},
        "contacts": {"total": total_contacts},
        "messages": {"sent_or_better": sent_count, "failed": failed_count},
        "chat": {"conversations_with_unread": open_conversations},
        "alerts": alerts,
        "checked_at": datetime.utcnow().isoformat(),
    }


# ── Clientes (empresas) ─────────────────────────────────────────────

@router.get("/clients")
async def list_clients(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(User).where(User.is_superadmin == False))
    clients = res.scalars().all()

    out = []
    for c in clients:
        n_contacts = (await db.execute(
            select(func.count(Contact.id)).where(Contact.owner_id == c.id)
        )).scalar() or 0
        n_campaigns = (await db.execute(
            select(func.count(Campaign.id)).where(Campaign.owner_id == c.id)
        )).scalar() or 0
        n_agents = (await db.execute(
            select(func.count(Agent.id)).where(Agent.owner_id == c.id)
        )).scalar() or 0
        out.append({
            "id": c.id, "name": c.name, "email": c.email,
            "industry": c.industry,
            "whatsapp_connected": bool(c.whatsapp_phone_id),
            "whatsapp_phone_id": c.whatsapp_phone_id,
            "profile_name": c.profile_name,
            "api_docs_allowed": bool(c.api_docs_allowed),
            "contacts": n_contacts, "campaigns": n_campaigns, "agents": n_agents,
            "last_whatsapp_error": c.last_whatsapp_error,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })
    return out


@router.get("/industries")
async def list_industry_suggestions(admin: User = Depends(require_superadmin)):
    """Rubros conocidos + campos sugeridos, para el formulario de alta de cliente."""
    return {
        "industries": [
            {"key": k, "fields": v} for k, v in INDUSTRY_FIELD_SUGGESTIONS.items()
        ]
    }


class ClientCreateIn(BaseModel):
    name: str
    email: str
    password: str
    industry: Optional[str] = None
    apply_suggested_fields: bool = True


@router.post("/clients")
async def create_client(
    data: ClientCreateIn,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Alta de empresa nueva directamente desde el Panel Admin — tú creas
    la cuenta (con contraseña temporal) en vez de pedirle al cliente que
    se registre solo. Si eliges un rubro conocido, se precargan sus
    campos personalizados sugeridos (editables después).
    """
    existing = await db.execute(select(User).where(User.email == data.email))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Ya existe una cuenta con ese email")

    client = User(
        name=data.name, email=data.email,
        hashed_password=hash_password(data.password),
        industry=data.industry,
    )
    db.add(client)
    await db.flush()

    created_fields = []
    if data.apply_suggested_fields and data.industry in INDUSTRY_FIELD_SUGGESTIONS:
        for f in INDUSTRY_FIELD_SUGGESTIONS[data.industry]:
            fd = CustomFieldDef(client_id=client.id, key=f["key"], label=f["label"], field_type=f["field_type"])
            db.add(fd)
            created_fields.append(f["key"])

    await db.commit()
    return {"id": client.id, "name": client.name, "email": client.email, "fields_created": created_fields}


@router.get("/clients/{client_id}")
async def get_client(
    client_id: int,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(User).where(User.id == client_id, User.is_superadmin == False))
    c = res.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    return {
        "id": c.id, "name": c.name, "email": c.email, "industry": c.industry,
        "whatsapp_connected": bool(c.whatsapp_phone_id), "whatsapp_phone_id": c.whatsapp_phone_id,
        "waba_id": c.waba_id, "profile_name": c.profile_name,
        "api_docs_allowed": bool(c.api_docs_allowed),
    }


class ClientUpdateIn(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    industry: Optional[str] = None
    new_password: Optional[str] = None   # para restablecer contraseña del cliente
    api_docs_allowed: Optional[bool] = None  # autorizar acceso a /docs por empresa


@router.patch("/clients/{client_id}")
async def update_client(
    client_id: int,
    data: ClientUpdateIn,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(User).where(User.id == client_id, User.is_superadmin == False))
    client = res.scalar_one_or_none()
    if not client:
        raise HTTPException(404, "Cliente no encontrado")

    if data.email and data.email != client.email:
        dup = await db.execute(select(User).where(User.email == data.email))
        if dup.scalar_one_or_none():
            raise HTTPException(400, "Ya existe una cuenta con ese email")
        client.email = data.email
    if data.name:
        client.name = data.name
    if data.industry is not None:
        client.industry = data.industry or None
    if data.new_password:
        if len(data.new_password) < 6:
            raise HTTPException(400, "La contraseña debe tener al menos 6 caracteres")
        client.hashed_password = hash_password(data.new_password)
    if data.api_docs_allowed is not None:
        client.api_docs_allowed = bool(data.api_docs_allowed)

    await db.commit()
    return {"ok": True, "api_docs_allowed": client.api_docs_allowed}


@router.delete("/clients/{client_id}")
async def delete_client(
    client_id: int,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Elimina una empresa y TODO lo que le pertenece (contactos, campañas,
    mensajes, agentes, documentos, conversaciones, campos personalizados,
    plantillas asignadas, recordatorios, etiquetas, API keys, webhooks).
    Es irreversible — se usa solo cuando de verdad quieres borrar al
    cliente, no para desactivarlo temporalmente (para eso, simplemente
    desconecta su WhatsApp).
    """
    res = await db.execute(select(User).where(User.id == client_id, User.is_superadmin == False))
    client = res.scalar_one_or_none()
    if not client:
        raise HTTPException(404, "Cliente no encontrado")

    try:
        # 1) Mensajes de campañas (dependen de campaigns)
        camp_ids_res = await db.execute(select(Campaign.id).where(Campaign.owner_id == client_id))
        camp_ids = [c for (c,) in camp_ids_res.all()]
        if camp_ids:
            msgs = await db.execute(select(Message).where(Message.campaign_id.in_(camp_ids)))
            for m in msgs.scalars().all():
                await db.delete(m)
        campaigns = await db.execute(select(Campaign).where(Campaign.owner_id == client_id))
        for c in campaigns.scalars().all():
            await db.delete(c)

        # 2) Documentos (invoice_items → invoice_batches)
        batch_ids_res = await db.execute(select(InvoiceBatch.id).where(InvoiceBatch.owner_id == client_id))
        batch_ids = [b for (b,) in batch_ids_res.all()]
        if batch_ids:
            items = await db.execute(select(InvoiceItem).where(InvoiceItem.batch_id.in_(batch_ids)))
            for i in items.scalars().all():
                await db.delete(i)
        batches = await db.execute(select(InvoiceBatch).where(InvoiceBatch.owner_id == client_id))
        for b in batches.scalars().all():
            await db.delete(b)

        # 3) Chat (chat_messages → conversations)
        conv_ids_res = await db.execute(select(Conversation.id).where(Conversation.owner_id == client_id))
        conv_ids = [cv for (cv,) in conv_ids_res.all()]
        if conv_ids:
            chatmsgs = await db.execute(select(ChatMessage).where(ChatMessage.conversation_id.in_(conv_ids)))
            for cm in chatmsgs.scalars().all():
                await db.delete(cm)
        convs = await db.execute(select(Conversation).where(Conversation.owner_id == client_id))
        for cv in convs.scalars().all():
            await db.delete(cv)

        # 4) Recordatorios recurrentes ANTES de campaign_templates
        #    (recurring_campaigns.template_id → campaign_templates.id)
        for model, col in [
            (RecurringCampaign, RecurringCampaign.client_id),
            (CampaignTemplate, CampaignTemplate.client_id),
            (MetaTemplate, MetaTemplate.client_id),
            (CustomFieldDef, CustomFieldDef.client_id),
            (ContactTag, ContactTag.client_id),
            (ApiKey, ApiKey.client_id),
            (WebhookSubscription, WebhookSubscription.client_id),
            (Contact, Contact.owner_id),
            (Agent, Agent.owner_id),
        ]:
            rows = await db.execute(select(model).where(col == client_id))
            for row in rows.scalars().all():
                await db.delete(row)

        # 5) Finalmente el usuario/empresa
        await db.delete(client)
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(500, f"Error al eliminar cliente: {str(e)}")

    return {"ok": True, "deleted_client_id": client_id}


class ClientWhatsAppIn(BaseModel):
    whatsapp_token: str
    whatsapp_phone_id: str
    profile_name: Optional[str] = None
    waba_id: Optional[str] = None


@router.post("/clients/{client_id}/whatsapp")
async def connect_client_whatsapp(
    client_id: int,
    data: ClientWhatsAppIn,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    El cliente (empresa) NO puede conectar su propia línea de WhatsApp;
    el superadmin la conecta por él a partir del número que el cliente
    le facilite. Reusa el mismo registro/suscripción que el flujo normal.
    """
    res = await db.execute(select(User).where(User.id == client_id, User.is_superadmin == False))
    client = res.scalar_one_or_none()
    if not client:
        raise HTTPException(404, "Cliente no encontrado")

    op_token = provider_token(data.whatsapp_token)
    reg_result = await register_phone_number(data.whatsapp_phone_id, op_token)
    sub_result = {}
    if data.waba_id:
        sub_result = await subscribe_app_to_waba(data.waba_id, op_token)

    client.whatsapp_token = data.whatsapp_token
    client.whatsapp_phone_id = data.whatsapp_phone_id
    client.profile_name = data.profile_name or client.profile_name
    client.waba_id = data.waba_id or client.waba_id

    if "error" in reg_result:
        client.last_whatsapp_error = str(reg_result["error"].get("message", reg_result["error"]))[:500]
        client.last_whatsapp_error_at = datetime.utcnow()
    else:
        client.last_whatsapp_error = None
        client.last_whatsapp_error_at = None

    await db.commit()

    return {
        "ok": True,
        "message": f"WhatsApp conectado para {client.name}",
        "register_status": "error" if "error" in reg_result else "ok",
        "subscribe_status": "error" if "error" in sub_result else "ok",
    }


@router.delete("/clients/{client_id}/whatsapp")
async def disconnect_client_whatsapp(
    client_id: int,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(User).where(User.id == client_id, User.is_superadmin == False))
    client = res.scalar_one_or_none()
    if not client:
        raise HTTPException(404, "Cliente no encontrado")
    client.whatsapp_token = None
    client.whatsapp_phone_id = None
    client.waba_id = None
    await db.commit()
    return {"ok": True}


# ── Plantillas de campaña ───────────────────────────────────────────

class TemplateIn(BaseModel):
    name: str
    message_template: str
    category: Optional[str] = None
    client_id: Optional[int] = None   # None = plantilla global (para todos)
    meta_template_name: Optional[str] = None      # plantilla HSM a usar fuera de la ventana de 24h
    meta_template_language: Optional[str] = "es"


@router.get("/templates")
async def list_templates(
    client_id: Optional[int] = None,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    q = select(CampaignTemplate)
    if client_id is not None:
        q = q.where(
            (CampaignTemplate.client_id == client_id) | (CampaignTemplate.client_id.is_(None))
        )
    res = await db.execute(q.order_by(CampaignTemplate.created_at.desc()))
    templates = res.scalars().all()
    return [{
        "id": t.id, "name": t.name, "category": t.category,
        "message_template": t.message_template,
        "client_id": t.client_id, "is_active": t.is_active,
        "meta_template_name": t.meta_template_name,
        "meta_template_language": t.meta_template_language,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    } for t in templates]


@router.post("/templates")
async def create_template(
    data: TemplateIn,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    if data.client_id is not None:
        res = await db.execute(select(User).where(User.id == data.client_id, User.is_superadmin == False))
        if not res.scalar_one_or_none():
            raise HTTPException(404, "Cliente no encontrado")

    t = CampaignTemplate(
        client_id=data.client_id,
        created_by=admin.id,
        name=data.name,
        category=data.category,
        message_template=data.message_template,
        meta_template_name=data.meta_template_name,
        meta_template_language=data.meta_template_language or "es",
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"id": t.id, "name": t.name, "client_id": t.client_id}


@router.patch("/templates/{template_id}")
async def update_template(
    template_id: int,
    data: TemplateIn,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(CampaignTemplate).where(CampaignTemplate.id == template_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Plantilla no encontrada")
    t.name = data.name
    t.category = data.category
    t.message_template = data.message_template
    t.client_id = data.client_id
    t.meta_template_name = data.meta_template_name
    t.meta_template_language = data.meta_template_language or "es"
    await db.commit()
    return {"ok": True}


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: int,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(CampaignTemplate).where(CampaignTemplate.id == template_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Plantilla no encontrada")
    await db.delete(t)
    await db.commit()
    return {"ok": True}


# ── Verificación de agentes en toda la plataforma ───────────────────

@router.get("/agents")
async def list_all_agents(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Lista todos los agentes de todas las empresas con sus permisos
    reales guardados — para que el superadmin pueda verificar que los
    roles/permisos que se asignaron sí quedaron aceptados.
    """
    res = await db.execute(select(Agent))
    agents = res.scalars().all()
    out = []
    for a in agents:
        owner_res = await db.execute(select(User).where(User.id == a.owner_id))
        owner = owner_res.scalar_one_or_none()
        out.append({
            "id": a.id, "name": a.name, "email": a.email,
            "role": a.role.value, "permissions": a.get_permissions(),
            "is_active": a.is_active,
            "company": owner.name if owner else "—",
            "company_id": a.owner_id,
        })
    return out


class AdminAgentUpdateIn(BaseModel):
    permissions: Optional[List[str]] = None
    is_active: Optional[bool] = None


@router.patch("/agents/{agent_id}")
async def admin_update_agent(
    agent_id: int,
    data: AdminAgentUpdateIn,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Control total sobre CUALQUIER agente de CUALQUIER empresa — a
    diferencia de PATCH /agents/{id} (que solo el dueño de esa empresa
    puede usar sobre sus propios agentes), esto es exclusivo del
    superadmin y no está limitado a una empresa.
    """
    res = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = res.scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agente no encontrado")
    if data.permissions is not None:
        agent.set_permissions(data.permissions)
    if data.is_active is not None:
        agent.is_active = data.is_active
    await db.commit()
    return {"ok": True, "permissions": agent.get_permissions(), "is_active": agent.is_active}


@router.delete("/agents/{agent_id}")
async def admin_delete_agent(
    agent_id: int,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = res.scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agente no encontrado")
    await db.delete(agent)
    await db.commit()
    return {"ok": True}


# ════════════════════════════════════════════════════════════════
# CAMPOS PERSONALIZADOS POR EMPRESA
# ════════════════════════════════════════════════════════════════
# Así la plataforma sirve a cualquier rubro (farmacia, ferretería,
# camaronera, agroexportador...) sin tocar código: tú defines qué
# campos aplican a cada cliente, y esas claves quedan disponibles como
# {{variable}} en sus plantillas y como columnas al importar contactos.

class CustomFieldIn(BaseModel):
    key: str
    label: str
    field_type: str = "text"   # text | date | number | phone

CUSTOM_FIELD_TYPES = {"text", "date", "number", "phone"}


@router.get("/fields")
async def list_all_fields(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Todos los campos personalizados de TODAS las empresas, de un
    vistazo — para no depender de elegir una empresa primero para ver
    que sí existen.
    """
    res = await db.execute(select(CustomFieldDef))
    fields = res.scalars().all()
    out = []
    for f in fields:
        owner_res = await db.execute(select(User).where(User.id == f.client_id))
        owner = owner_res.scalar_one_or_none()
        out.append({
            "id": f.id, "key": f.key, "label": f.label, "field_type": f.field_type,
            "client_id": f.client_id, "client_name": owner.name if owner else "—",
        })
    return out


@router.get("/clients/{client_id}/fields")
async def list_client_fields(
    client_id: int,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(CustomFieldDef).where(CustomFieldDef.client_id == client_id))
    return [{"id": f.id, "key": f.key, "label": f.label, "field_type": f.field_type}
            for f in res.scalars().all()]


@router.post("/clients/{client_id}/fields")
async def create_client_field(
    client_id: int,
    data: CustomFieldIn,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(User).where(User.id == client_id, User.is_superadmin == False))
    if not res.scalar_one_or_none():
        raise HTTPException(404, "Cliente no encontrado")

    dup = await db.execute(select(CustomFieldDef).where(
        CustomFieldDef.client_id == client_id, CustomFieldDef.key == data.key))
    if dup.scalar_one_or_none():
        raise HTTPException(400, f"Ya existe un campo con la clave '{data.key}' para este cliente")

    if data.field_type not in CUSTOM_FIELD_TYPES:
        raise HTTPException(400, f"field_type debe ser uno de: {', '.join(sorted(CUSTOM_FIELD_TYPES))}")

    f = CustomFieldDef(client_id=client_id, key=data.key, label=data.label, field_type=data.field_type)
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return {"id": f.id, "key": f.key, "label": f.label, "field_type": f.field_type}


@router.delete("/fields/{field_id}")
async def delete_client_field(
    field_id: int,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(CustomFieldDef).where(CustomFieldDef.id == field_id))
    f = res.scalar_one_or_none()
    if not f:
        raise HTTPException(404, "Campo no encontrado")
    await db.delete(f)
    await db.commit()
    return {"ok": True}


class CustomFieldUpdateIn(BaseModel):
    label: str
    field_type: str = "text"


@router.patch("/fields/{field_id}")
async def update_client_field(
    field_id: int,
    data: CustomFieldUpdateIn,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Edita la etiqueta visible y el tipo de un campo personalizado. La
    clave (`key`) NO se puede cambiar aquí a propósito — ya puede estar
    referenciada en plantillas ({{clave}}) o en contactos existentes;
    si necesitas otra clave, crea un campo nuevo y borra el viejo.
    """
    res = await db.execute(select(CustomFieldDef).where(CustomFieldDef.id == field_id))
    f = res.scalar_one_or_none()
    if not f:
        raise HTTPException(404, "Campo no encontrado")
    if data.field_type not in CUSTOM_FIELD_TYPES:
        raise HTTPException(400, f"field_type debe ser uno de: {', '.join(sorted(CUSTOM_FIELD_TYPES))}")
    f.label = data.label
    f.field_type = data.field_type
    await db.commit()
    return {"ok": True}


# ════════════════════════════════════════════════════════════════
# PLANTILLAS DE META (HSM) — necesarias fuera de la ventana de 24h
# ════════════════════════════════════════════════════════════════

@router.post("/clients/{client_id}/meta-templates/sync")
async def sync_meta_templates(
    client_id: int,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Trae del Business Manager de Meta las plantillas (HSM) aprobadas
    para la WABA de este cliente, y las guarda en caché local para
    poder enlazarlas a una CampaignTemplate (ver PATCH /admin/templates).
    """
    res = await db.execute(select(User).where(User.id == client_id, User.is_superadmin == False))
    client = res.scalar_one_or_none()
    if not client:
        raise HTTPException(404, "Cliente no encontrado")
    if not client.waba_id or not client.whatsapp_token:
        raise HTTPException(400, "Este cliente no tiene WABA ID o token configurado todavía")

    fetched = await fetch_meta_templates(client.waba_id, provider_token(client.whatsapp_token))

    # Reemplazamos el caché de este cliente por lo que Meta devolvió ahora.
    old = await db.execute(select(MetaTemplate).where(MetaTemplate.client_id == client_id))
    for old_t in old.scalars().all():
        await db.delete(old_t)

    for t in fetched:
        db.add(MetaTemplate(
            client_id=client_id, name=t["name"], language=t["language"],
            category=t["category"], status=t["status"], body_text=t["body_text"],
            variable_count=t["variable_count"], synced_at=datetime.utcnow(),
        ))
    await db.commit()
    return {"synced": len(fetched), "templates": fetched}


@router.get("/clients/{client_id}/meta-templates")
async def list_meta_templates(
    client_id: int,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(MetaTemplate).where(MetaTemplate.client_id == client_id))
    return [{
        "name": t.name, "language": t.language, "category": t.category,
        "status": t.status, "body_text": t.body_text, "variable_count": t.variable_count,
    } for t in res.scalars().all()]


# ════════════════════════════════════════════════════════════════
# RECORDATORIOS RECURRENTES (campañas que se disparan solas)
# ════════════════════════════════════════════════════════════════

class RecurringIn(BaseModel):
    client_id: int
    template_id: int
    name: str
    contact_tag: Optional[str] = None
    date_field_key: str = "next_reminder_date"
    interval_days: Optional[int] = None
    is_active: bool = True


@router.get("/recurring")
async def list_recurring(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(RecurringCampaign))
    out = []
    for rc in res.scalars().all():
        owner_res = await db.execute(select(User).where(User.id == rc.client_id))
        owner = owner_res.scalar_one_or_none()
        out.append({
            "id": rc.id, "name": rc.name, "client_id": rc.client_id,
            "client_name": owner.name if owner else "—",
            "template_id": rc.template_id, "contact_tag": rc.contact_tag,
            "date_field_key": rc.date_field_key, "interval_days": rc.interval_days,
            "is_active": rc.is_active,
            "last_run_at": rc.last_run_at.isoformat() if rc.last_run_at else None,
        })
    return out


@router.post("/recurring")
async def create_recurring(
    data: RecurringIn,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(User).where(User.id == data.client_id, User.is_superadmin == False))
    if not res.scalar_one_or_none():
        raise HTTPException(404, "Cliente no encontrado")
    tres = await db.execute(select(CampaignTemplate).where(CampaignTemplate.id == data.template_id))
    if not tres.scalar_one_or_none():
        raise HTTPException(404, "Plantilla no encontrada")

    rc = RecurringCampaign(
        client_id=data.client_id, template_id=data.template_id, name=data.name,
        contact_tag=data.contact_tag, date_field_key=data.date_field_key,
        interval_days=data.interval_days, is_active=data.is_active,
    )
    db.add(rc)
    await db.commit()
    await db.refresh(rc)
    return {"id": rc.id, "name": rc.name}


@router.patch("/recurring/{recurring_id}")
async def update_recurring(
    recurring_id: int,
    data: RecurringIn,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(RecurringCampaign).where(RecurringCampaign.id == recurring_id))
    rc = res.scalar_one_or_none()
    if not rc:
        raise HTTPException(404, "Recordatorio no encontrado")
    rc.name = data.name
    rc.template_id = data.template_id
    rc.contact_tag = data.contact_tag
    rc.date_field_key = data.date_field_key
    rc.interval_days = data.interval_days
    rc.is_active = data.is_active
    await db.commit()
    return {"ok": True}


@router.delete("/recurring/{recurring_id}")
async def delete_recurring(
    recurring_id: int,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(RecurringCampaign).where(RecurringCampaign.id == recurring_id))
    rc = res.scalar_one_or_none()
    if not rc:
        raise HTTPException(404, "Recordatorio no encontrado")
    await db.delete(rc)
    await db.commit()
    return {"ok": True}


@router.post("/recurring/run-now")
async def run_recurring_now(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Fuerza una corrida inmediata de todos los recordatorios recurrentes
    activos, sin esperar al próximo ciclo automático (cada 6h). Útil
    para probar que un recordatorio nuevo quedó bien configurado.
    """
    summary = await run_recurring_campaigns(db)
    return {"ran": len(summary), "summary": summary}


# ════════════════════════════════════════════════════════════════
# EXPORTES
# ════════════════════════════════════════════════════════════════

@router.get("/export/clients")
async def export_clients_xlsx(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    clients_data = await list_clients(admin, db)
    buf = export_service.clients_overview_to_xlsx(clients_data)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=empresas_wasapinf.xlsx"}
    )
