"""
whatsapp_service.py 
=========================================
Cambios:
- base_url ya NO está hardcodeada. Se lee desde os.environ["PUBLIC_BASE_URL"]
  que main.py setea al arrancar (auto-detecta ngrok o usa la variable de entorno).
- URL de documento construida correctamente con path /uploads/invoices/
- Header ngrok-skip-browser-warning añadido al envío de documento
  (Meta lo necesita al descargar desde el link)
"""
import httpx
import asyncio
import os
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from models.database import (
    Campaign, Message, CampaignStatus,
    InvoiceBatch, InvoiceItem, InvoiceBatchStatus,
    Conversation, ChatMessage, Contact, CampaignTemplate, RecurringCampaign
)
from webhooks_service import dispatch_webhook_event

META_API_VERSION = "v21.0"
META_BASE = f"https://graph.facebook.com/{META_API_VERSION}"


# ══════════════════════════════════════════════════════════════════════════════
# ENVÍO DE DOCUMENTOS / FACTURAS
# ══════════════════════════════════════════════════════════════════════════════

async def send_whatsapp_document(
    phone_id: str,
    token: str,
    to_phone: str,
    document_url: str,
    filename: str,
    caption: str = ""
) -> dict:
    """
    Envía un documento PDF nativo vía WhatsApp Cloud API.

    IMPORTANTE sobre la URL:
    - Debe ser HTTPS públicamente accesible (ngrok o servidor real)
    - NO debe requerir autenticación
    - Debe devolver el PDF con Content-Type: application/pdf
    - Con ngrok FREE: la URL debe incluir el header ngrok-skip-browser-warning
      PERO ese header lo añade tu servidor (main.py), no aquí.
      Meta llama a tu URL directamente y verá los headers de tu Response.
    """
    url = f"{META_BASE}/{phone_id}/messages"

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "document",
        "document": {
            "link": document_url,
            "filename": filename,
            "caption": caption[:1024] if caption else ""
        }
    }

    # Limpiar None del caption si está vacío
    if not payload["document"]["caption"]:
        del payload["document"]["caption"]

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )
        return response.json()


async def send_whatsapp_image(
    phone_id: str,
    token: str,
    to_phone: str,
    image_url: str,
    caption: str = ""
) -> dict:
    """Envía una imagen (jpg/png) nativa vía WhatsApp Cloud API — usado
    para el envío puntual de documentos desde el chat en vivo."""
    url = f"{META_BASE}/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "image",
        "image": {"link": image_url, **({"caption": caption[:1024]} if caption else {})}
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        return response.json()


def get_public_base_url() -> str:
    """
    Lee la URL pública desde la variable de entorno.
    main.py la setea al arrancar (auto-detecta ngrok o usa PUBLIC_BASE_URL del .env).
    """
    url = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    if url == "http://localhost:8000":
        print("⚠️  [WARNING] PUBLIC_BASE_URL no está configurada. "
              "Meta no podrá descargar los PDFs desde localhost. "
              "Inicia ngrok y define PUBLIC_BASE_URL o usa la detección automática.")
    return url


async def run_invoice_batch(
    batch_id: int,
    db: AsyncSession,
    phone_id: str,
    token: str,
    base_url: str = None   # Si None, lo lee de la variable de entorno
):
    """
    Procesa un lote de facturas/documentos:
    1. Obtiene los InvoiceItem pendientes del lote
    2. Para cada uno construye la URL pública del PDF
    3. Llama a la WhatsApp API para enviar el documento nativo
    4. Guarda el estado (sent/failed) y el wamid en la DB
    5. Pausa 2 segundos entre envíos (rate-limit Meta)
    """
    # Resolver base_url
    if not base_url:
        base_url = get_public_base_url()

    # Cargar el lote
    res = await db.execute(select(InvoiceBatch).where(InvoiceBatch.id == batch_id))
    batch = res.scalar_one()

    batch.status = InvoiceBatchStatus.sending
    await db.commit()

    # Items pendientes
    res2 = await db.execute(
        select(InvoiceItem).where(
            InvoiceItem.batch_id == batch_id,
            InvoiceItem.status == "pending"
        )
    )
    items = res2.scalars().all()
    print(f"[Batch {batch_id}] Procesando {len(items)} documentos desde {base_url}")

    sent = failed = 0

    for item in items:
        try:
            # ── CONSTRUCCIÓN CORRECTA DE LA URL ─────────────────────────────
            # file_path en DB guarda solo el nombre: "abc123.pdf"
            # La ruta pública es: /uploads/invoices/abc123.pdf
            # main.py maneja /uploads/{subpath:path} y añade los headers ngrok
            public_url = f"{base_url}/uploads/invoices/{item.file_path}"

            print(f"[Batch {batch_id}] → {item.contact_phone} | {public_url}")

            resp = await send_whatsapp_document(
                phone_id=phone_id,
                token=token,
                to_phone=item.contact_phone,
                document_url=public_url,
                filename=item.original_name,
                caption=batch.caption or item.doc_type or ""
            )

            if "messages" in resp:
                item.status  = "sent"
                item.wamid   = resp["messages"][0]["id"]
                item.sent_at = datetime.utcnow()
                sent += 1
                print(f"  ✅ Enviado | wamid: {item.wamid}")
            else:
                # La API de Meta devuelve error estructurado
                error_detail = resp.get("error", {})
                error_msg = (
                    f"[{error_detail.get('code', '?')}] "
                    f"{error_detail.get('message', str(resp))}"
                )
                item.status    = "failed"
                item.error_msg = error_msg[:500]
                failed += 1
                print(f"  ❌ Falló: {error_msg}")

        except httpx.TimeoutException:
            item.status    = "failed"
            item.error_msg = "Timeout al contactar WhatsApp API"
            failed += 1
            print(f"  ❌ Timeout para {item.contact_phone}")

        except Exception as e:
            item.status    = "failed"
            item.error_msg = str(e)[:500]
            failed += 1
            print(f"  ❌ Excepción: {e}")

        # Guardar estado por ítem (no perder progreso si cae el proceso)
        await db.commit()

        # ── RATE LIMITING ────────────────────────────────────────────────────
        # Meta tier básico: ~80 mensajes/s pero para documentos
        # se recomienda pausa mayor para evitar errores 131056
        await asyncio.sleep(2)

    # Cerrar lote
    batch.status = InvoiceBatchStatus.completed
    batch.sent   = sent
    batch.failed = failed
    await db.commit()

    print(f"[Batch {batch_id}] ✅ Finalizado → Enviados: {sent} | Fallidos: {failed}")


# ══════════════════════════════════════════════════════════════════════════════
# ENVÍO DE TEXTO (campañas)
# ══════════════════════════════════════════════════════════════════════════════

async def send_whatsapp_text(phone_id: str, token: str, to_phone: str, body: str) -> dict:
    """Envía texto simple via API oficial."""
    url = f"{META_BASE}/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        return r.json()


async def send_whatsapp_template(
    phone_id: str, token: str, to_phone: str,
    template_name: str, language: str, variables: list
) -> dict:
    """
    Envía una plantilla (HSM) aprobada por Meta. OBLIGATORIO cuando el
    contacto lleva más de 24h sin escribir — WhatsApp ya no permite texto
    libre en ese caso, solo plantillas pre-aprobadas.
    """
    url = f"{META_BASE}/{phone_id}/messages"
    components = []
    if variables:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(v)} for v in variables]
        })
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language or "es"},
            **({"components": components} if components else {})
        }
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        return r.json()


async def is_within_24h_window(owner_id: int, contact_phone: str, db: AsyncSession) -> bool:
    """
    True si el contacto escribió en las últimas 24h (podemos mandarle
    texto libre); False si ya toca usar una plantilla aprobada por Meta.
    """
    res = await db.execute(
        select(Conversation).where(
            Conversation.owner_id == owner_id,
            Conversation.contact_phone == contact_phone
        )
    )
    conv = res.scalar_one_or_none()
    if not conv or not conv.last_inbound_at:
        return False
    return (datetime.utcnow() - conv.last_inbound_at) < timedelta(hours=24)


def personalize(template: str, name: str, phone: str, extra: dict = None) -> str:
    out = template.replace("{{name}}", name or "").replace("{{phone}}", phone or "")
    for k, v in (extra or {}).items():
        out = out.replace("{{" + k + "}}", str(v) if v is not None else "")
    return out


async def run_campaign(campaign_id: int, db: AsyncSession, phone_id: str, token: str):
    """
    Procesa todos los mensajes pendientes de una campaña (corre en
    background). Por cada contacto revisa si está dentro de la ventana
    de 24h: si sí, manda el texto tal cual; si ya se salió de la ventana
    y la campaña tiene una plantilla Meta (HSM) enlazada, la usa; si no
    hay plantilla enlazada, el mensaje queda marcado como fallido con un
    error explicando por qué (para que el admin enlace una plantilla).
    """
    res = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = res.scalar_one()
    template = campaign.message_template

    campaign.status = CampaignStatus.sending
    await db.commit()

    res2 = await db.execute(
        select(Message).where(Message.campaign_id == campaign_id, Message.status == "pending")
    )
    messages = res2.scalars().all()

    # Traemos los contactos de una vez para poder personalizar con sus
    # campos dinámicos (medicamento, próxima cosecha, lo que sea).
    contacts_res = await db.execute(select(Contact).where(Contact.owner_id == campaign.owner_id))
    contacts_by_phone = {c.phone: c for c in contacts_res.scalars().all()}

    sent_count = failed_count = 0

    for msg in messages:
        contact = contacts_by_phone.get(msg.contact_phone)
        extra_fields = contact.get_fields() if contact else {}
        text_body = personalize(template, msg.contact_name, msg.contact_phone, extra_fields)

        try:
            within_window = await is_within_24h_window(campaign.owner_id, msg.contact_phone, db)

            if within_window:
                resp = await send_whatsapp_text(phone_id, token, msg.contact_phone, text_body)
            elif campaign.meta_template_name:
                # Fuera de ventana: usamos la plantilla aprobada. Mandamos
                # el texto ya personalizado como única variable de cuerpo —
                # funciona para plantillas de 1 variable tipo "Hola {{1}}...".
                # Para plantillas más complejas, ajustar variables aquí.
                resp = await send_whatsapp_template(
                    phone_id, token, msg.contact_phone,
                    campaign.meta_template_name, campaign.meta_template_language,
                    [msg.contact_name or msg.contact_phone]
                )
            else:
                msg.status = "failed"
                msg.error_msg = ("Contacto fuera de la ventana de 24h y esta campaña no tiene "
                                  "una plantilla de Meta (HSM) enlazada. Pide al admin que enlace una.")
                failed_count += 1
                await db.commit()
                await asyncio.sleep(0.1)
                continue

            if "messages" in resp:
                msg.status  = "sent"
                msg.wamid   = resp["messages"][0]["id"]
                msg.sent_at = datetime.utcnow()
                sent_count += 1
                await dispatch_webhook_event(campaign.owner_id, "message.sent", {
                    "phone": msg.contact_phone, "name": msg.contact_name,
                    "campaign_id": campaign.id, "wamid": msg.wamid,
                })
            else:
                msg.status    = "failed"
                msg.error_msg = str(resp.get("error", {}).get("message", "Error desconocido"))
                failed_count += 1
        except Exception as e:
            msg.status    = "failed"
            msg.error_msg = str(e)
            failed_count += 1

        await db.commit()
        await asyncio.sleep(0.3)

    campaign.status = CampaignStatus.completed
    campaign.sent   = sent_count
    campaign.failed = failed_count
    db.add(campaign)
    await db.commit()
    print(f"[Campaign {campaign_id}] done — sent:{sent_count} failed:{failed_count}")

    await dispatch_webhook_event(campaign.owner_id, "campaign.completed", {
        "campaign_id": campaign.id, "name": campaign.name,
        "sent": sent_count, "failed": failed_count, "total": campaign.total_contacts,
    })


# ══════════════════════════════════════════════════════════════════════════════
# RECORDATORIOS RECURRENTES (campañas que se disparan solas)
# ══════════════════════════════════════════════════════════════════════════════

async def run_recurring_campaigns(db: AsyncSession):
    """
    Revisa todos los RecurringCampaign activos y, por cada uno, arma
    (si aplica) una campaña automática con los contactos cuya fecha en
    `custom_fields[date_field_key]` ya se cumplió. Si el recordatorio
    tiene interval_days, reprograma esa fecha sola tras enviar — así el
    ciclo de "medicamento mensual" (o cosecha, revisión, etc.) no
    requiere que nadie lo dispare a mano cada vez.

    Devuelve un resumen por recordatorio, para loguear o mostrar en el
    Panel Admin ("Ejecutar ahora").
    """
    from models.database import User  # import local para evitar ciclo

    today = datetime.utcnow().date()
    res = await db.execute(select(RecurringCampaign).where(RecurringCampaign.is_active == True))
    reminders = res.scalars().all()

    summary = []

    for rc in reminders:
        client_res = await db.execute(select(User).where(User.id == rc.client_id))
        client = client_res.scalar_one_or_none()
        if not client or not client.whatsapp_token or not client.whatsapp_phone_id:
            summary.append({"recurring_id": rc.id, "name": rc.name, "skipped": "sin WhatsApp conectado"})
            continue

        tmpl_res = await db.execute(select(CampaignTemplate).where(CampaignTemplate.id == rc.template_id))
        template = tmpl_res.scalar_one_or_none()
        if not template:
            summary.append({"recurring_id": rc.id, "name": rc.name, "skipped": "plantilla no encontrada"})
            continue

        q = select(Contact).where(Contact.owner_id == rc.client_id, Contact.opted_out == False)
        if rc.contact_tag:
            q = q.where(Contact.tag == rc.contact_tag)
        contacts = (await db.execute(q)).scalars().all()

        due_contacts = []
        for c in contacts:
            fields = c.get_fields()
            raw_date = fields.get(rc.date_field_key)
            if not raw_date:
                continue
            try:
                due_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if due_date <= today:
                due_contacts.append(c)

        rc.last_run_at = datetime.utcnow()
        if not due_contacts:
            summary.append({"recurring_id": rc.id, "name": rc.name, "sent_to": 0})
            await db.commit()
            continue

        camp = Campaign(
            owner_id=rc.client_id,
            name=f"{rc.name} — automático {today.isoformat()}",
            message_template=template.message_template,
            meta_template_name=template.meta_template_name,
            meta_template_language=template.meta_template_language,
            is_recurring=True,
            total_contacts=len(due_contacts),
        )
        db.add(camp)
        await db.flush()

        for c in due_contacts:
            db.add(Message(campaign_id=camp.id, contact_phone=c.phone, contact_name=c.name))
            # Reprogramar la fecha si corresponde (ciclo mensual, etc.)
            if rc.interval_days:
                fields = c.get_fields()
                fields[rc.date_field_key] = (today + timedelta(days=rc.interval_days)).isoformat()
                c.set_fields(fields)

        await db.commit()
        await db.refresh(camp)

        await run_campaign(camp.id, db, client.whatsapp_phone_id, client.whatsapp_token)
        summary.append({"recurring_id": rc.id, "name": rc.name, "sent_to": len(due_contacts), "campaign_id": camp.id})

    return summary


# ══════════════════════════════════════════════════════════════════════════════
# CONTADORES DE CAMPAÑAS (webhook delivered/read)
# ══════════════════════════════════════════════════════════════════════════════

async def recalc_campaign_counters(campaign_id: int, db: AsyncSession):
    res = await db.execute(
        text("""
            SELECT
                SUM(CASE WHEN status IN ('delivered','read') THEN 1 ELSE 0 END) as delivered,
                SUM(CASE WHEN status = 'read'                THEN 1 ELSE 0 END) as read_count,
                SUM(CASE WHEN status = 'failed'              THEN 1 ELSE 0 END) as failed_count,
                SUM(CASE WHEN status = 'sent'                THEN 1 ELSE 0 END) as sent_count
            FROM messages WHERE campaign_id = :cid
        """),
        {"cid": campaign_id}
    )
    row = res.fetchone()
    if row:
        await db.execute(
            text("""UPDATE campaigns SET
                        delivered = :d, read = :r, failed = :f, sent = :s
                    WHERE id = :cid"""),
            {"d": row[0] or 0, "r": row[1] or 0,
             "f": row[2] or 0, "s": row[3] or 0, "cid": campaign_id}
        )
        await db.commit()
