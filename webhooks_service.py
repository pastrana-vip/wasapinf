"""
webhooks_service.py
====================
Envío de webhooks salientes a los sistemas de cada cliente (su ERP,
su sitio, lo que sea) cuando pasa algo relevante: llega un mensaje,
se crea un contacto, termina una campaña, etc.

Cada intento es "fire and forget" en una tarea de fondo — nunca bloquea
el flujo principal (procesar un mensaje entrante no debe esperar a que
el servidor del cliente responda). Es un solo intento, sin cola de
reintentos — para un negocio del tamaño que maneja esta plataforma es
suficiente robustez; si crece mucho, lo primero a mejorar sería una
cola de reintentos con backoff.
"""
import asyncio
import hashlib
import hmac
import json
import httpx
from datetime import datetime
from sqlalchemy import select

from models.database import AsyncSessionLocal, WebhookSubscription


async def dispatch_webhook_event(client_id: int, event: str, payload: dict):
    """
    Punto de entrada: se llama desde cualquier parte de la app cuando
    ocurre un evento (ej. dispatch_webhook_event(user.id, "contact.created", {...})).
    Abre su propia sesión de DB (no reutiliza la del request que la
    disparó) y lanza una tarea de fondo por cada suscripción activa
    que tenga ese evento marcado.
    """
    try:
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(WebhookSubscription).where(
                WebhookSubscription.client_id == client_id,
                WebhookSubscription.is_active == True
            ))
            subs = res.scalars().all()
        for sub in subs:
            if event in sub.get_events():
                asyncio.create_task(_send_webhook(sub.id, sub.url, sub.secret, event, payload))
    except Exception as e:
        print(f"[Webhooks] Error buscando suscripciones para client={client_id}: {e}")


async def _send_webhook(sub_id: int, url: str, secret: str, event: str, payload: dict):
    body = {
        "event": event,
        "data": payload,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    raw = json.dumps(body, default=str)
    signature = hmac.new((secret or "").encode(), raw.encode(), hashlib.sha256).hexdigest()

    status_result = "error"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                url,
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-WaSapinf-Event": event,
                    "X-WaSapinf-Signature": f"sha256={signature}",
                },
            )
        status_result = "ok" if r.status_code < 400 else "error"
    except Exception as e:
        print(f"[Webhooks] Fallo enviando a {url}: {e}")

    # Registrar el resultado en su propia sesión (la tarea corre suelta,
    # separada del request original).
    try:
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == sub_id))
            sub = res.scalar_one_or_none()
            if sub:
                sub.last_triggered_at = datetime.utcnow()
                sub.last_status = status_result
                await db.commit()
    except Exception as e:
        print(f"[Webhooks] No se pudo registrar el resultado del webhook {sub_id}: {e}")
