# routers/invoices.py
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import shutil
import uuid
import os
from typing import Optional, List

from models.database import InvoiceBatch, InvoiceItem, get_db
from auth import get_current_user
from models.database import User
from whatsapp_service import run_invoice_batch

router = APIRouter(prefix="/invoices", tags=["Facturas"])

UPLOAD_DIR = "uploads/invoices"
os.makedirs(UPLOAD_DIR, exist_ok=True)


@router.post("/batches")
async def create_invoice_batch(
    name: str = Form(...),
    caption: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not files:
        raise HTTPException(400, "Debes subir al menos un PDF")

    batch = InvoiceBatch(
        owner_id=user.id,
        name=name,
        caption=caption,
        total=len(files)
    )
    db.add(batch)
    await db.flush()

    for file in files:
        if not file.filename.lower().endswith(".pdf"):
            continue

        file_id = str(uuid.uuid4())
        file_path = f"{file_id}.pdf"
        full_path = f"{UPLOAD_DIR}/{file_path}"

        # Guardar el archivo
        with open(full_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # Intentar extraer teléfono del nombre del archivo
        phone = None
        try:
            # Ejemplos: factura_50499112233.pdf o cliente_50499887766.pdf
            name_part = file.filename.split('.')[0]
            if '_' in name_part:
                possible_phone = name_part.split('_')[-1]
                if possible_phone.isdigit() and len(possible_phone) >= 8:
                    phone = f"+504{possible_phone}" if len(possible_phone) == 8 else f"+{possible_phone}"
        except:
            pass

        item = InvoiceItem(
            batch_id=batch.id,
            contact_phone=phone or "pendiente_validar",
            contact_name="Cliente",
            file_path=file_path,
            original_name=file.filename,
            file_size_kb=len(content) // 1024
        )
        db.add(item)

    await db.commit()
    return {"batch_id": batch.id, "total": batch.total}


@router.post("/batches/{batch_id}/send")
async def send_invoice_batch(
    batch_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not user.whatsapp_token or not user.whatsapp_phone_id:
        raise HTTPException(400, "Configura tu WhatsApp primero")

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