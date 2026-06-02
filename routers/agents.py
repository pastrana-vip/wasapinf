# routers/agents.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, EmailStr
from typing import Optional

from models.database import Agent, AgentRole, User, get_db
from auth import hash_password, get_current_user

router = APIRouter(prefix="/agents", tags=["Agentes"])


class AgentCreate(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "sender"                    # Aceptamos string
    whatsapp_phone_id: Optional[str] = None
    whatsapp_token: Optional[str] = None


@router.post("/")
async def create_agent(
    data: AgentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # Verificar email duplicado
    existing = await db.execute(
        select(Agent).where(Agent.email == data.email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Este email ya está en uso")

    # Convertir string a enum
    try:
        role_enum = AgentRole(data.role)
    except ValueError:
        role_enum = AgentRole.sender

    agent = Agent(
        owner_id=user.id,
        name=data.name,
        email=data.email,
        hashed_password=hash_password(data.password),
        role=role_enum,
        whatsapp_phone_id=data.whatsapp_phone_id,
        whatsapp_token=data.whatsapp_token,
    )

    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    return {
        "message": "Agente creado correctamente",
        "agent": {
            "id": agent.id,
            "name": agent.name,
            "email": agent.email,
            "role": agent.role.value
        }
    }


@router.get("/")
async def list_agents(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Agent).where(Agent.owner_id == user.id)
    )
    agents = result.scalars().all()
    
    return [{
        "id": a.id,
        "name": a.name,
        "email": a.email,
        "role": a.role.value,
        "whatsapp_phone_id": a.whatsapp_phone_id,
        "is_active": a.is_active
    } for a in agents]