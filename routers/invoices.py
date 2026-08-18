# routers/invoices.py
#
# Módulo de envío de DOCUMENTOS por WhatsApp — antes pensado solo para
# "facturas", ahora genérico: cada empresa manda lo que le corresponde
# (facturas, recibos, cotizaciones, recetas médicas, resultados de
# examen, planillas...) e incluso puede mezclar tipos dentro del mismo
# lote, porque el tipo se guarda por documento individual
# (InvoiceItem.doc_type), no por lote completo.
#
# IMPORTANTE — coincidencia flexible del destinatario: por defecto cada
# documento se empareja por teléfono, pero un lote puede emparejar por
# CUALQUIER campo personalizado de la empresa (identidad, RTN,
# employee_id...) — así una planilla de 200 empleados se reparte 1-a-1
# sin que el mismo PDF le llegue a todos. Ver InvoiceBatch.match_field_key.
#
# Se mantienen los nombres de tabla/clase internos (InvoiceBatch,
# InvoiceItem) para no romper la base de datos existente — lo que
# cambió es el concepto expuesto en la API y el frontend: "Documentos".
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import uuid
import os
import re
import json
from typing import Optional, List

from models.database import (
    InvoiceBatch, InvoiceItem, Contact, CustomFieldDef, DOCUMENT_TYPE_SUGGESTIONS,
    get_db, normalize_phone,
)
from auth import get_current_user, has_permission
from models.database import User
from whatsapp_service import run_invoice_batch

router = APIRouter(prefix="/documents", tags=["Documentos"])

UPLOAD_DIR = "uploads/invoices"
os.makedirs(UPLOAD_DIR, exist_ok=True)

PHONE_RE = re.compile(r"^\d{8,15}$")

# Formatos que WhatsApp acepta como mensaje de documento — ya no solo
# PDF: planillas en Excel, comprobantes en Word, hojas de cálculo, etc.
ALLOWED_EXTENSIONS = {"pdf", "xlsx", "xls", "csv", "doc", "docx"}






def extract_value_from_filename(filename: str) -> Optional[str]:
    """
    Extrae el valor de emparejamiento del nombre del archivo — funciona
    para CUALQUIER campo (teléfono, RTN, identidad, código de empleado...),
    no solo teléfono. La regla es simple y siempre igual: se toma lo que
    viene después del ÚLTIMO guion bajo, sin importar la palabra que va
    antes (esa primera parte — "factura", "recibo", "receta"... — es
    solo para que TÚ identifiques el archivo; el sistema nunca la lee).

    Ejemplos:
      factura_9585-5720.pdf        → "9585-5720"
      recibo_RTN-08011990012345.pdf → "RTN-08011990012345"
      planilla_EMP-0042.xlsx        → "EMP-0042"

    Si el nombre no tiene guion bajo, no hay nada que extraer.
    """
    try:
        name_part = filename.rsplit(".", 1)[0]
        if "_" in name_part:
            return name_part.split("_")[-1].strip() or None
    except Exception:
        pass
    return None


def extract_phone_from_filename(filename: str) -> Optional[str]:
    """Caso particular de extract_value_from_filename para teléfono (normaliza a E.164)."""
    raw = extract_value_from_filename(filename)
    return normalize_phone(raw) if raw else None


@router.get("/types")
async def document_type_suggestions(user: User = Depends(get_current_user)):
    """Sugerencias para el selector de tipo de documento — el campo es libre."""
    return {"suggestions": DOCUMENT_TYPE_SUGGESTIONS, "allowed_extensions": sorted(ALLOWED_EXTENSIONS)}


@router.post("/batches")
async def create_document_batch(
    name: str = Form(...),
    caption: Optional[str] = Form(None),
    default_doc_type: str = Form("Documento"),
    # "phone" (default) o la clave de un campo personalizado (identidad,
    # rtn, employee_id...) — determina cómo se resuelve el destinatario.
    match_field_key: str = Form("phone"),
    files: List[UploadFile] = File(...),
    # JSON opcional: [{"filename": "recibo1.pdf", "match_value": "0801199001234",
    #                  "contact_name": "...", "doc_type": "Recibo"}]
    # match_value es el teléfono si match_field_key=="phone", o el valor
    # del campo elegido (ej. el RTN) en cualquier otro caso. Si se envía,
    # tiene prioridad sobre la extracción automática del nombre del archivo.
    phones_map: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not has_permission(user, "send_invoices"):
        raise HTTPException(403, "Tu cuenta de agente no puede enviar documentos")
    if not files:
        raise HTTPException(400, "Debes subir al menos un archivo")

    if match_field_key != "phone":
        fdefs = await db.execute(select(CustomFieldDef).where(
            CustomFieldDef.client_id == user.id, CustomFieldDef.key == match_field_key))
        if not fdefs.scalar_one_or_none():
            raise HTTPException(400, f"No existe el campo personalizado '{match_field_key}' para tu empresa")

    mapping = {}
    if phones_map:
        try:
            parsed = json.loads(phones_map)
            for entry in parsed:
                mapping[entry["filename"]] = {
                    "match_value": entry.get("match_value") or entry.get("phone"),
                    "contact_name": entry.get("contact_name"),
                    "doc_type": entry.get("doc_type"),
                }
        except Exception:
            raise HTTPException(400, "phones_map inválido: debe ser JSON con [{filename, match_value, contact_name, doc_type}]")

    batch = InvoiceBatch(
        owner_id=user.id,
        name=name,
        caption=caption,
        match_field_key=match_field_key or "phone",
        total=len(files)
    )
    db.add(batch)
    await db.flush()

    items_pending_resolution = []

    for file in files:
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            continue

        file_id = str(uuid.uuid4())
        file_path = f"{file_id}.{ext}"
        full_path = f"{UPLOAD_DIR}/{file_path}"

        content = await file.read()
        with open(full_path, "wb") as f:
            f.write(content)

        contact_name = "Cliente"
        doc_type = default_doc_type or "Documento"
        match_value = None
        phone = None

        if file.filename in mapping:
            match_value = mapping[file.filename].get("match_value")
            contact_name = mapping[file.filename].get("contact_name") or "Cliente"
            doc_type = mapping[file.filename].get("doc_type") or doc_type

        if match_field_key == "phone":
            phone = normalize_phone(match_value or "") if match_value else None
            if not phone:
                phone = extract_phone_from_filename(file.filename)
            match_value = phone
            if not phone:
                items_pending_resolution.append(file.filename)
        else:
            # Emparejamiento por campo personalizado: si no vino un
            # match_value explícito (via phones_map), se intenta sacar
            # del nombre del archivo igual que con teléfono — lo que
            # va después del último guion bajo (ej. recibo_08011990012345
            # → "08011990012345"). El teléfono real de contacto se
            # resuelve contra Contact.custom_fields al momento de
            # enviar (así se puede corregir el contacto entre subir el
            # lote y enviarlo, sin tener que re-subir nada).
            if not match_value:
                match_value = extract_value_from_filename(file.filename)
            if not match_value:
                items_pending_resolution.append(file.filename)

        item = InvoiceItem(
            batch_id=batch.id,
            contact_phone=phone or "",
            contact_name=contact_name,
            doc_type=doc_type,
            match_value=match_value,
            file_path=file_path,
            original_name=file.filename,
            file_size_kb=len(content) // 1024
        )
        db.add(item)

    await db.commit()

    response = {"batch_id": batch.id, "total": batch.total, "match_field_key": match_field_key}
    if items_pending_resolution:
        field_label = "teléfono" if match_field_key == "phone" else f"valor de '{match_field_key}'"
        response["warning"] = (
            f"{len(items_pending_resolution)} archivo(s) no tienen {field_label} "
            f"y no se enviarán hasta que los corrijas: {', '.join(items_pending_resolution)}"
        )
    return response


@router.patch("/items/{item_id}")
async def update_item(
    item_id: int,
    data: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Permite corregir el teléfono/valor de emparejamiento, nombre y/o tipo de un documento antes de enviar."""
    res = await db.execute(
        select(InvoiceItem).join(InvoiceBatch).where(
            InvoiceItem.id == item_id, InvoiceBatch.owner_id == user.id
        )
    )
    item = res.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Documento no encontrado")

    if "phone" in data:
        phone = normalize_phone(data.get("phone", ""))
        if not phone:
            raise HTTPException(400, "Teléfono inválido. Usa solo dígitos con código de país, ej: 50499887766")
        item.contact_phone = phone
        item.match_value = phone
    if "match_value" in data:
        item.match_value = data["match_value"]
        item.contact_phone = ""  # se re-resolverá al enviar
    if data.get("contact_name"):
        item.contact_name = data["contact_name"]
    if data.get("doc_type"):
        item.doc_type = data["doc_type"]
    await db.commit()
    return {"ok": True, "contact_phone": item.contact_phone, "match_value": item.match_value, "doc_type": item.doc_type}


async def _resolve_recipients(batch: InvoiceBatch, items: list, owner_id: int, db: AsyncSession) -> list:
    """
    Para lotes que emparejan por un campo personalizado (no por
    teléfono), busca entre los contactos de la empresa cuál tiene ese
    valor exacto y completa item.contact_phone. Devuelve la lista de
    nombres de archivo que quedaron sin resolver.
    """
    unresolved = []
    if batch.match_field_key == "phone":
        for item in items:
            if not normalize_phone(item.contact_phone or ""):
                unresolved.append(item.original_name)
        return unresolved

    contacts_res = await db.execute(select(Contact).where(Contact.owner_id == owner_id))
    contacts = contacts_res.scalars().all()
    by_field_value = {}
    for c in contacts:
        val = c.get_fields().get(batch.match_field_key)
        if val:
            by_field_value[str(val).strip().lower()] = c

    for item in items:
        if normalize_phone(item.contact_phone or ""):
            continue  # ya resuelto
        key = (item.match_value or "").strip().lower()
        match = by_field_value.get(key) if key else None
        if match:
            item.contact_phone = match.phone
            if not item.contact_name or item.contact_name == "Cliente":
                item.contact_name = match.name
        else:
            unresolved.append(f"{item.original_name} ({batch.match_field_key}={item.match_value or '—'})")
    await db.commit()
    return unresolved


@router.post("/batches/{batch_id}/resolve")
async def resolve_document_batch(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Vuelve a intentar emparejar los documentos pendientes contra los
    contactos actuales — útil si acabas de agregar/corregir contactos
    después de subir el lote (ej. subiste la planilla antes de importar
    el CSV de empleados con sus RTN).
    """
    res = await db.execute(
        select(InvoiceBatch).where(InvoiceBatch.id == batch_id, InvoiceBatch.owner_id == user.id))
    batch = res.scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Lote no encontrado")
    items_res = await db.execute(select(InvoiceItem).where(InvoiceItem.batch_id == batch_id))
    items = items_res.scalars().all()
    unresolved = await _resolve_recipients(batch, items, user.id, db)
    return {"resolved": len(items) - len(unresolved), "unresolved": unresolved}


@router.post("/batches/{batch_id}/send")
async def send_document_batch(
    batch_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not has_permission(user, "send_invoices"):
        raise HTTPException(403, "Tu cuenta de agente no puede enviar documentos")
    if not user.whatsapp_token or not user.whatsapp_phone_id:
        raise HTTPException(400, "Tu línea de WhatsApp aún no está conectada. Contacta al administrador.")

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

    unresolved = await _resolve_recipients(batch, items, user.id, db)
    if unresolved:
        field_label = "teléfono válido" if batch.match_field_key == "phone" else f"contacto con ese '{batch.match_field_key}'"
        raise HTTPException(
            400,
            f"No se puede enviar: {len(unresolved)} documento(s) sin {field_label}. "
            f"Corrígelos con PATCH /documents/items/{{item_id}} o agrega/actualiza el contacto y reintenta con "
            f"POST /documents/batches/{batch_id}/resolve: {', '.join(unresolved)}"
        )

    background_tasks.add_task(
        run_invoice_batch,
        batch_id,
        db,
        user.whatsapp_phone_id,
        user.whatsapp_token
    )
    return {"status": "sending", "message": "Envío de documentos iniciado"}


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
    """Para que el frontend pueda mostrar y corregir destinatarios/tipo antes de enviar."""
    res = await db.execute(
        select(InvoiceBatch).where(InvoiceBatch.id == batch_id, InvoiceBatch.owner_id == user.id)
    )
    batch = res.scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Lote no encontrado")

    items_res = await db.execute(
        select(InvoiceItem).where(InvoiceItem.batch_id == batch_id)
    )
    return [{
        "id": i.id,
        "original_name": i.original_name,
        "contact_name": i.contact_name,
        "contact_phone": i.contact_phone,
        "match_value": i.match_value,
        "doc_type": i.doc_type,
        "status": i.status,
        "error_msg": i.error_msg,
        "has_valid_phone": bool(normalize_phone(i.contact_phone or "")),
    } for i in items_res.scalars().all()]
