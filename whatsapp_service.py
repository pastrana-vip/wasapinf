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
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from models.database import (
    Campaign, Message, CampaignStatus,
    InvoiceBatch, InvoiceItem, InvoiceBatchStatus,
    Conversation, ChatMessage
)

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
                caption=batch.caption or ""
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


def personalize(template: str, name: str, phone: str) -> str:
    return template.replace("{{name}}", name).replace("{{phone}}", phone)


async def run_campaign(campaign_id: int, db: AsyncSession, phone_id: str, token: str):
    """Procesa todos los mensajes pendientes de una campaña (corre en background)."""
    res = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = res.scalar_one()
    template = campaign.message_template

    campaign.status = CampaignStatus.sending
    await db.commit()

    res2 = await db.execute(
        select(Message).where(Message.campaign_id == campaign_id, Message.status == "pending")
    )
    messages = res2.scalars().all()

    sent_count = failed_count = 0

    for msg in messages:
        text_body = personalize(template, msg.contact_name, msg.contact_phone)
        try:
            resp = await send_whatsapp_text(phone_id, token, msg.contact_phone, text_body)
            if "messages" in resp:
                msg.status  = "sent"
                msg.wamid   = resp["messages"][0]["id"]
                msg.sent_at = datetime.utcnow()
                sent_count += 1
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
