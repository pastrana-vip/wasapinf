"""
whatsapp_service.py - Versión mejorada para flujo OAuth estable
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


async def send_whatsapp_document(
    phone_id: str,
    token: str,
    to_phone: str,
    document_url: str,
    filename: str,
    caption: str = ""
) -> dict:
    """Envía documento PDF."""
    if not phone_id or len(phone_id) < 15:
        raise ValueError(f"Phone ID inválido: {phone_id}")

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
    if not payload["document"]["caption"]:
        del payload["document"]["caption"]

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        return response.json()


async def send_whatsapp_text(phone_id: str, token: str, to_phone: str, body: str) -> dict:
    """Envía texto (mejorado con logs)."""
    if not phone_id or len(phone_id) < 15:
        raise ValueError(f"Phone ID inválido para envío de texto: {phone_id}")

    url = f"{META_BASE}/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {"preview_url": False, "body": body}
    }

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        return r.json()


def get_public_base_url() -> str:
    url = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    return url


def personalize(template: str, name: str, phone: str) -> str:
    return template.replace("{{name}}", name or "").replace("{{phone}}", phone or "")


async def run_invoice_batch(batch_id: int, db: AsyncSession, phone_id: str, token: str):
    """Lote de facturas mejorado."""
    base_url = get_public_base_url()
    res = await db.execute(select(InvoiceBatch).where(InvoiceBatch.id == batch_id))
    batch = res.scalar_one()

    batch.status = InvoiceBatchStatus.sending
    await db.commit()

    res2 = await db.execute(select(InvoiceItem).where(
        InvoiceItem.batch_id == batch_id, InvoiceItem.status == "pending"
    ))
    items = res2.scalars().all()

    print(f"[Batch {batch_id}] Procesando {len(items)} PDFs con PhoneID: {phone_id}")

    sent = failed = 0
    for item in items:
        try:
            public_url = f"{base_url}/uploads/invoices/{item.file_path}"
            print(f"[Batch {batch_id}] → {item.contact_phone} | {public_url}")

            resp = await send_whatsapp_document(phone_id, token, item.contact_phone, public_url, item.original_name, batch.caption or "")

            if "messages" in resp:
                item.status = "sent"
                item.wamid = resp["messages"][0]["id"]
                item.sent_at = datetime.utcnow()
                sent += 1
                print(f"  ✅ Enviado | wamid: {item.wamid}")
            else:
                error = resp.get("error", {})
                error_msg = f"[{error.get('code')}] {error.get('message')}"
                item.status = "failed"
                item.error_msg = error_msg[:500]
                failed += 1
                print(f"  ❌ Falló: {error_msg}")
        except Exception as e:
            item.status = "failed"
            item.error_msg = str(e)[:500]
            failed += 1
            print(f"  ❌ Excepción: {e}")

        await db.commit()
        await asyncio.sleep(2)

    batch.status = InvoiceBatchStatus.completed
    batch.sent = sent
    batch.failed = failed
    await db.commit()
    print(f"[Batch {batch_id}] Finalizado → Enviados: {sent} | Fallidos: {failed}")


async def run_campaign(campaign_id: int, db: AsyncSession, phone_id: str, token: str):
    """Campañas de texto mejoradas."""
    res = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = res.scalar_one()
    template = campaign.message_template

    campaign.status = CampaignStatus.sending
    await db.commit()

    res2 = await db.execute(select(Message).where(
        Message.campaign_id == campaign_id, Message.status == "pending"
    ))
    messages = res2.scalars().all()

    sent_count = failed_count = 0
    for msg in messages:
        text_body = personalize(template, msg.contact_name, msg.contact_phone)
        try:
            resp = await send_whatsapp_text(phone_id, token, msg.contact_phone, text_body)
            if "messages" in resp:
                msg.status = "sent"
                msg.wamid = resp["messages"][0]["id"]
                msg.sent_at = datetime.utcnow()
                sent_count += 1
                print(f"  ✅ Campaña texto enviado a {msg.contact_phone}")
            else:
                error = resp.get("error", {})
                error_msg = f"[{error.get('code')}] {error.get('message', 'Error desconocido')}"
                msg.status = "failed"
                msg.error_msg = error_msg
                failed_count += 1
                print(f"  ❌ Falló campaña: {error_msg}")
        except Exception as e:
            msg.status = "failed"
            msg.error_msg = str(e)
            failed_count += 1

        await db.commit()
        await asyncio.sleep(1)  # Más conservador

    campaign.status = CampaignStatus.completed
    campaign.sent = sent_count
    campaign.failed = failed_count
    await db.commit()
    print(f"[Campaign {campaign_id}] Finalizado → Enviados: {sent_count} | Fallidos: {failed_count}")