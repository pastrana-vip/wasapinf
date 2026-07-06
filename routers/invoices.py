from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import shutil
import uuid
import os
import re
import json
from typing import Optional, List

from models.database import InvoiceBatch, InvoiceItem, get_db
from auth import get_current_user
from models.database import User
from whatsapp_service import run_invoice_batch

router = APIRouter(prefix="/invoices", tags=["Facturas"])

UPLOAD_DIR = "uploads/invoices"
os.makedirs(UPLOAD_DIR, exist_ok=True)

PHONE_RE = re.compile(r"^\d{8,15}$")


def normalize_phone(raw: str) -> Optional[str]:
    """
    Normaliza un número de teléfono a formato E.164 sin '+'.
    Devuelve None si no es un número válido (en vez de guardar un
    placeholder como 'pendiente_validar', que rompía el envío silenciosamente).
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not PHONE_RE.match(digits):
        return None
    # Si viene con 8 dígitos, asumimos código de país Honduras (504) por defecto.
    if len(digits) == 8:
        digits = "504" + digits
    return digits


def extract_phone_from_filename(filename: str) -> Optional[str]:
    """
    Fallback: intenta extraer el teléfono del nombre del archivo,
    ej. factura_50499112233.pdf. Si no se puede, devuelve None
    (el llamador debe exigir el teléfono explícito en ese caso).
    """
    try:
        name_part = filename.rsplit(".", 1)[0]
        if "_" in name_part:
            possible_phone = name_part.split("_")[-1]
            return normalize_phone(possible_phone)
    except Exception:
        pass
    return None


@router.post("/batches")
async def create_invoice_batch(
    name: str = Form(...),
    caption: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    # JSON opcional: [{"filename": "factura1.pdf", "phone": "50499112233", "contact_name": "Juan"}]
    # Si se envía, tiene prioridad sobre la extracción automática del nombre del archivo.
    phones_map: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not files:
        raise HTTPException(400, "Debes subir al menos un PDF")

    mapping = {}
    if phones_map:
        try:
            parsed = json.loads(phones_map)
            for entry in parsed:
                mapping[entry["filename"]] = {
                    "phone": entry.get("phone"),
                    "contact_name": entry.get("contact_name"),
                }
        except Exception:
            raise HTTPException(400, "phones_map inválido: debe ser JSON con [{filename, phone, contact_name}]")

    batch = InvoiceBatch(
        owner_id=user.id,
        name=name,
        caption=caption,
        total=len(files)
    )
    db.add(batch)
    await db.flush()

    items_without_phone = []

    for file in files:
        if not file.filename.lower().endswith(".pdf"):
            continue

        file_id = str(uuid.uuid4())
        file_path = f"{file_id}.pdf"
        full_path = f"{UPLOAD_DIR}/{file_path}"

        content = await file.read()
        with open(full_path, "wb") as f:
            f.write(content)

        # 1) Prioridad: mapping explícito enviado por el frontend.
        phone = None
        contact_name = "Cliente"
        if file.filename in mapping:
            phone = normalize_phone(mapping[file.filename].get("phone") or "")
            contact_name = mapping[file.filename].get("contact_name") or "Cliente"

        # 2) Fallback: intentar extraer del nombre del archivo.
        if not phone:
            phone = extract_phone_from_filename(file.filename)

        if not phone:
            # Ya NO se guarda "pendiente_validar": se guarda vacío y se marca
            # el item para que el usuario lo corrija antes de enviar.
            items_without_phone.append(file.filename)

        item = InvoiceItem(
            batch_id=batch.id,
            contact_phone=phone or "",
            contact_name=contact_name,
            file_path=file_path,
            original_name=file.filename,
            file_size_kb=len(content) // 1024
        )
        db.add(item)

    await db.commit()

    response = {"batch_id": batch.id, "total": batch.total}
    if items_without_phone:
        response["warning"] = (
            f"{len(items_without_phone)} archivo(s) no tienen un teléfono válido "
            f"y no se enviarán hasta que los corrijas: {', '.join(items_without_phone)}"
        )
    return response


@router.patch("/items/{item_id}/phone")
async def update_item_phone(
    item_id: int,
    data: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Permite corregir el teléfono de un documento antes de enviar el lote."""
    res = await db.execute(
        select(InvoiceItem).join(InvoiceBatch).where(
            InvoiceItem.id == item_id, InvoiceBatch.owner_id == user.id
        )
    )
    item = res.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Documento no encontrado")

    phone = normalize_phone(data.get("phone", ""))
    if not phone:
        raise HTTPException(400, "Teléfono inválido. Usa solo dígitos con código de país, ej: 50499887766")

    item.contact_phone = phone
    if data.get("contact_name"):
        item.contact_name = data["contact_name"]
    await db.commit()
    return {"ok": True, "contact_phone": item.contact_phone}


@router.post("/batches/{batch_id}/send")
async def send_invoice_batch(
    batch_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not user.whatsapp_token or not user.whatsapp_phone_id:
        raise HTTPException(400, "Configura tu WhatsApp primero (falta token o Phone Number ID)")

    res = await db.execute(
        select(InvoiceBatch).where(InvoiceBatch.id == batch_id, InvoiceBatch.owner_id == user.id)
    )
    batch = res.scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Lote no encontrado")

    items_res = await db.execute(
        select(InvoiceItem).where(InvoiceItem.batch_id == batch_id)
    )
    items = items_res.scalars().all()

    invalid = [i.original_name for i in items if not normalize_phone(i.contact_phone or "")]
    if invalid:
        raise HTTPException(
            400,
            f"No se puede enviar: {len(invalid)} documento(s) sin teléfono válido. "
            f"Corrígelos con PATCH /invoices/items/{{item_id}}/phone: {', '.join(invalid)}"
        )

    background_tasks.add_task(
        run_invoice_batch,
        batch_id,
        db,
        user.whatsapp_phone_id,
        user.whatsapp_token
    )
    return {"status": "sending", "message": "Envío de facturas iniciado"}


@router.get("/batches")
async def list_batches(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(InvoiceBatch).where(InvoiceBatch.owner_id == user.id)
        .order_by(InvoiceBatch.created_at.desc())
    )
    return result.scalars().all()


@router.get("/batches/{batch_id}/items")
async def list_batch_items(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Para que el frontend pueda mostrar y corregir teléfonos antes de enviar."""
    res = await db.execute(
        select(InvoiceBatch).where(InvoiceBatch.id == batch_id, InvoiceBatch.owner_id == user.id)
    )
    if not res.scalar_one_or_none():
        raise HTTPException(404, "Lote no encontrado")

    items_res = await db.execute(
        select(InvoiceItem).where(InvoiceItem.batch_id == batch_id)
    )
    return [{
        "id": i.id,
        "original_name": i.original_name,
        "contact_name": i.contact_name,
        "contact_phone": i.contact_phone,
        "status": i.status,
        "error_msg": i.error_msg,
        "has_valid_phone": bool(normalize_phone(i.contact_phone or "")),
    } for i in items_res.scalars().all()]