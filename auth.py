from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.database import User, Agent, ApiKey, get_db
import os
import secrets
import hashlib

# En producción SIEMPRE se debe fijar la variable de entorno SECRET_KEY
# (así lo hace render.yaml, generándola sola). El valor de aquí abajo es
# solo un respaldo para correr en local sin configurar nada.
SECRET_KEY = os.getenv("SECRET_KEY", "CAMBIA_ESTO_POR_UNA_CLAVE_SEGURA_EN_PRODUCCION")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")
bearer_scheme = HTTPBearer()


def hash_password(password: str) -> str:
    # sha256_crypt no tiene la limitación de 72 caracteres de bcrypt
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(user_id: int, email: str, typ: str = "owner") -> str:
    """
    typ distingue el tipo de cuenta dentro del propio JWT:
      - "owner": dueño de la empresa (User)
      - "agent": sub-usuario de una empresa (Agent)
    Antes, un token de Agent se decodificaba como si fuera un User,
    lo que rompía (o autenticaba incorrectamente) el login de agentes.
    """
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "typ": typ, "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Resuelve el "actor" autenticado a partir del JWT.

    Siempre devuelve un objeto User -- para un Agent, devuelve el User
    dueño de la empresa (asi todas las consultas existentes que filtran
    por `owner_id == user.id` siguen funcionando sin cambios) -- pero le
    anexa atributos extra que identifican que quien realmente esta
    operando es un Agent, y con que permisos:

      user._actor_type    -> "owner" | "agent"
      user._is_superadmin -> bool
      user._agent         -> instancia Agent, o None si es owner
      user._permissions   -> set de permisos (solo aplica si es agent)

    Usa has_permission(user, "perm") para chequear permisos en los
    endpoints, y require_permission/require_superadmin como dependencias.
    """
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        actor_id = int(payload["sub"])
        typ = payload.get("typ", "owner")
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")

    if typ == "agent":
        res = await db.execute(select(Agent).where(Agent.id == actor_id))
        agent = res.scalar_one_or_none()
        if not agent or not agent.is_active:
            raise HTTPException(status_code=404, detail="Agente no encontrado o inactivo")

        owner_res = await db.execute(select(User).where(User.id == agent.owner_id))
        owner = owner_res.scalar_one_or_none()
        if not owner:
            raise HTTPException(status_code=404, detail="Empresa dueña no encontrada")

        owner._actor_type = "agent"
        owner._is_superadmin = False
        owner._agent = agent
        owner._permissions = set(agent.get_permissions())
        return owner

    # typ == "owner" (o tokens viejos sin "typ", por compatibilidad)
    result = await db.execute(select(User).where(User.id == actor_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    user._actor_type = "owner"
    user._is_superadmin = bool(user.is_superadmin)
    user._agent = None
    user._permissions = None  # None = acceso total (no es un agente restringido)
    return user


def has_permission(user: User, perm: str) -> bool:
    """
    True si el actor puede realizar una accion que requiere `perm`.
    Los dueños de empresa y el superadmin siempre tienen acceso total;
    solo los Agents estan limitados a su lista de permisos guardada.
    """
    if getattr(user, "_actor_type", "owner") != "agent":
        return True
    perms = getattr(user, "_permissions", None) or set()
    return perm in perms


def require_permission(perm: str):
    """Dependencia FastAPI: 403 si el actor (agente) no tiene `perm`."""
    async def _dep(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user, perm):
            raise HTTPException(
                status_code=403,
                detail=f"Tu cuenta de agente no tiene el permiso requerido: {perm}"
            )
        return user
    return _dep


async def require_superadmin(user: User = Depends(get_current_user)) -> User:
    """Dependencia FastAPI: 403 si el actor no es el superadmin de la plataforma."""
    if not getattr(user, "_is_superadmin", False):
        raise HTTPException(status_code=403, detail="Solo el administrador de la plataforma puede hacer esto")
    return user


async def require_owner_or_superadmin(user: User = Depends(get_current_user)) -> User:
    """Dependencia FastAPI: bloquea a los Agents (solo dueños de empresa o superadmin)."""
    if getattr(user, "_actor_type", "owner") == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no pueden administrar esta sección")
    return user


# ════════════════════════════════════════════════════════════════
# API KEYS — autenticación para integraciones externas (el sistema
# propio del cliente llamando a nuestra API, no un humano logueado)
# ════════════════════════════════════════════════════════════════

def generate_api_key() -> tuple:
    """
    Genera una API key nueva. Devuelve (raw_key, prefix, hash):
      - raw_key  se muestra UNA sola vez al crearla, nunca se vuelve a mostrar.
      - prefix   es lo que se guarda para mostrarla enmascarada después.
      - hash     es lo único que se guarda en la base de datos.
    """
    raw = "wsp_live_" + secrets.token_urlsafe(32)
    prefix = raw[:14] + "…" + raw[-4:]
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, prefix, key_hash


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ════════════════════════════════════════════════════════════════
# ACCESO A LA DOCUMENTACIÓN DE LA API (Swagger /docs)
# ════════════════════════════════════════════════════════════════
# Antes /docs quedaba público para cualquiera en internet. Ahora se
# genera un token de un solo uso, de vida muy corta, únicamente para
# quien tiene el permiso "view_api_docs" (dueño/superadmin siempre,
# agente solo si se le otorgó). El botón "Ver documentación" del
# frontend pide este token y abre /docs?t=<token>.

DOCS_TOKEN_EXPIRE_MINUTES = 10

def create_docs_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(minutes=DOCS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(user_id), "typ": "docs_access", "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM
    )

def verify_docs_token(token: str) -> bool:
    """True si el token es válido, no expiró, y es específicamente de tipo
    docs_access (no sirve reusar aquí un JWT de sesión normal)."""
    if not token:
        return False
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return False
    return payload.get("typ") == "docs_access"


async def get_client_from_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Autentica peticiones de la API externa (/api/v1/...) usando
    `Authorization: Bearer <api_key>` en vez de un JWT de sesión.
    Cada API key pertenece a UNA empresa — nunca a un agente ni al
    superadmin — así que siempre resuelve a esa empresa.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(401, "Falta el header: Authorization: Bearer <tu_api_key>")
    raw_key = auth_header[7:].strip()
    if not raw_key:
        raise HTTPException(401, "API key vacía")

    key_hash = hash_api_key(raw_key)
    res = await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active == True))
    api_key = res.scalar_one_or_none()
    if not api_key:
        raise HTTPException(401, "API key inválida, revocada, o no encontrada")

    user_res = await db.execute(select(User).where(User.id == api_key.client_id))
    user = user_res.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "La cuenta dueña de esta API key ya no existe")

    api_key.last_used_at = datetime.utcnow()
    await db.commit()

    user._actor_type = "api_key"
    user._is_superadmin = False
    user._agent = None
    user._permissions = None
    return user
