# routers/roles.py
#
# NOTA: esta versión anterior de este archivo hacía referencia a modelos
# que nunca existieron en models/database.py (Role, RolePermission,
# Permission, User.type), así que cualquier request a estas rutas
# lanzaba un error 500 — y de hecho ni siquiera estaba registrado en
# main.py, así que este router no corría en absoluto. Se reemplaza por
# un endpoint simple y real: expone el catálogo de permisos que sí
# existe (models.database.AGENT_PERMISSIONS) para que el frontend pinte
# los checkboxes al crear/editar agentes.
from fastapi import APIRouter, Depends
from models.database import AGENT_PERMISSIONS, ROLE_DEFAULT_PERMISSIONS, User
from auth import get_current_user

router = APIRouter(prefix="/roles", tags=["Roles"])


@router.get("/")
async def list_roles(user: User = Depends(get_current_user)):
    """
    Catálogo de roles y sus permisos por defecto. (El detalle fino de
    permisos por agente vive en GET /agents/permissions; este endpoint
    se mantiene por compatibilidad con quien ya llamaba /api/roles).
    """
    return [
        {
            "name": role,
            "permissions": perms,
        }
        for role, perms in ROLE_DEFAULT_PERMISSIONS.items()
    ]
