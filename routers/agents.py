# routers/agents.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, EmailStr
from typing import Optional, List

from models.database import Agent, User, AGENT_PERMISSIONS, ROLE_DEFAULT_PERMISSIONS, get_db
from auth import hash_password, get_current_user, require_owner_or_superadmin

router = APIRouter(prefix="/agents", tags=["Agentes"])


def _agent_out(a: Agent) -> dict:
    """
    Serializa el agente incluyendo la lista real de roles/permisos
    guardados, para que el frontend pueda mostrar el check ✓ de
    confirmación de exactamente qué quedó persistido (y no solo lo que
    se envió). `role` es una etiqueta informativa calculada sola —
    "admin" si tiene todos marcados, "custom" si es una combinación,
    "sender" si no tiene ninguno — nunca se elige a mano.
    """
    return {
        "id": a.id,
        "name": a.name,
        "email": a.email,
        "role": a.role.value,
        "permissions": a.get_permissions(),
        "whatsapp_phone_id": a.whatsapp_phone_id,
        "is_active": a.is_active,
    }


class AgentCreate(BaseModel):
    name: str
    email: EmailStr
    password: str
    # Un agente puede tener VARIOS roles/permisos a la vez — ya no es
    # una sola opción de una lista, es todo lo que se marque aquí.
    permissions: List[str] = []
    whatsapp_phone_id: Optional[str] = None
    whatsapp_token: Optional[str] = None


class AgentUpdate(BaseModel):
    permissions: Optional[List[str]] = None
    is_active: Optional[bool] = None


@router.get("/permissions")
async def list_available_permissions(user: User = Depends(get_current_user)):
    """Catálogo de roles/permisos disponibles, para pintar los checkboxes en el frontend."""
    return {
        "permissions": [{"key": k, "label": v} for k, v in AGENT_PERMISSIONS.items()],
        "role_defaults": ROLE_DEFAULT_PERMISSIONS,  # solo para el botón "Marcar todos"/"Ninguno"
    }


@router.post("/")
async def create_agent(
    data: AgentCreate,
    user: User = Depends(require_owner_or_superadmin),
    db: AsyncSession = Depends(get_db)
):
    # Verificar email duplicado
    existing = await db.execute(
        select(Agent).where(Agent.email == data.email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Este email ya está en uso")

    perms = [p for p in data.permissions if p in AGENT_PERMISSIONS]
    if not perms:
        raise HTTPException(400, "Marca al menos un rol/permiso para este agente")

    agent = Agent(
        owner_id=user.id,
        name=data.name,
        email=data.email,
        hashed_password=hash_password(data.password),
        whatsapp_phone_id=data.whatsapp_phone_id,
        whatsapp_token=data.whatsapp_token,
    )
    agent.set_permissions(perms)   # también calcula agent.role automáticamente

    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    return {
        "message": "Agente creado correctamente",
        "agent": _agent_out(agent),
    }


@router.get("/")
async def list_agents(user: User = Depends(require_owner_or_superadmin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Agent).where(Agent.owner_id == user.id)
    )
    agents = result.scalars().all()
    return [_agent_out(a) for a in agents]


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: int,
    data: AgentUpdate,
    user: User = Depends(require_owner_or_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Permite corregir los roles/permisos/estado de un agente ya creado —
    útil para verificar y ajustar exactamente qué quedó aceptado.
    """
    res = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.owner_id == user.id)
    )
    agent = res.scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agente no encontrado")

    if data.permissions is not None:
        agent.set_permissions(data.permissions)

    if data.is_active is not None:
        agent.is_active = data.is_active

    await db.commit()
    await db.refresh(agent)
    return {"message": "Agente actualizado", "agent": _agent_out(agent)}
