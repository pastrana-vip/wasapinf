# routers/roles.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import List, Optional

from models.database import Role, RolePermission, Permission, User, get_db
from auth import get_current_user

router = APIRouter(prefix="/roles", tags=["Roles"])


class RoleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    permissions: List[str]   # Lista de strings como "send_invoices"


@router.post("/")
async def create_role(
    data: RoleCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # Solo owners pueden crear roles
    if not hasattr(user, 'type') or user.type != "owner":
        raise HTTPException(403, "Solo el administrador principal puede crear roles")

    role = Role(name=data.name, description=data.description)
    db.add(role)
    await db.flush()

    for perm in data.permissions:
        try:
            p = Permission(perm)
            db.add(RolePermission(role_id=role.id, permission=p))
        except:
            pass  # Ignorar permisos inválidos

    await db.commit()
    await db.refresh(role)
    return {"message": "Rol creado", "role_id": role.id}


@router.get("/")
async def list_roles(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Role))
    roles = result.scalars().all()
    return [{"id": r.id, "name": r.name, "description": r.description} for r in roles]