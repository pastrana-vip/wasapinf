import os

from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, ForeignKey, Enum, BigInteger
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from datetime import datetime
import enum


def _normalize_database_url(url: str) -> str:
    """
    Render (y Heroku, y la mayoría de proveedores) entregan la URL de
    Postgres como "postgres://..." o "postgresql://..." — SQLAlchemy
    async necesita el driver explícito "postgresql+asyncpg://...".
    Se normaliza sola para que copiar/pegar la URL tal cual funcione
    sin que el usuario tenga que editarla a mano.
    """
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL no está configurada")

print(f"[DB] Usando DATABASE_URL: {DATABASE_URL}")

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args={
        "timeout": 30,
    },
)

AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()


class CampaignStatus(str, enum.Enum):
    draft     = "draft"
    sending   = "sending"
    completed = "completed"
    failed    = "failed"


class User(Base):
    __tablename__ = "users"
    id                = Column(Integer, primary_key=True, index=True)
    name              = Column(String(100))
    email             = Column(String(100), unique=True, index=True)
    hashed_password   = Column(String(200))
    whatsapp_token    = Column(String(500),  nullable=True)
    whatsapp_phone_id = Column(String(100),  nullable=True)
    profile_name      = Column(String(200),  nullable=True)
    waba_id           = Column(String(100),  nullable=True)
    # Cuenta maestra de la plataforma (PRIME). Un superadmin tiene control
    # total: conecta WhatsApp de los clientes, crea plantillas, ve todo.
    # Un cliente normal (is_superadmin=False) es una "empresa" con acceso
    # restringido (no puede conectar WhatsApp ni crear plantillas).
    is_superadmin     = Column(Boolean, default=False)
    # Rubro del cliente (salud, ferretería, camaronera, agroexportador...).
    # Libre — se usa solo para sugerir campos personalizados y como
    # referencia en el Panel Admin. No condiciona funcionalidad.
    industry          = Column(String(100), nullable=True)
    # Horario de atención + auto-respuesta del chat en vivo.
    business_hours    = Column(Text, nullable=True)   # JSON: {"mon":["08:00","17:00"], ...}
    auto_reply_enabled = Column(Boolean, default=False)
    auto_reply_message = Column(Text, nullable=True)
    # Último error real que devolvió Meta al intentar enviar algo con la
    # línea de este cliente (token vencido, número desconectado, etc.) —
    # así el Panel Admin puede alertar sin tener que ir a revisar cliente
    # por cliente.
    last_whatsapp_error    = Column(Text, nullable=True)
    last_whatsapp_error_at = Column(DateTime, nullable=True)
    # Acceso a /docs (Swagger). Por defecto False: ni el dueño de la
    # empresa ve la documentación hasta que el superadmin lo autorice
    # desde el Panel Admin. Los agentes además necesitan el permiso
    # view_api_docs en su cuenta.
    api_docs_allowed  = Column(Boolean, default=False)
    created_at        = Column(DateTime, default=datetime.utcnow)
    campaigns         = relationship("Campaign",     back_populates="owner")
    contacts          = relationship("Contact",      back_populates="owner")
    conversations     = relationship("Conversation", back_populates="owner")
    invoice_batches   = relationship("InvoiceBatch", back_populates="owner")


class Contact(Base):
    __tablename__ = "contacts"
    id       = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"))
    name     = Column(String(100))
    phone    = Column(String(20))
    tag      = Column(String(50), nullable=True)
    # Si escribió "BAJA"/"STOP" (o un admin lo marcó a mano), se excluye
    # de campañas y recordatorios — pero sigue pudiendo usar el chat.
    opted_out = Column(Boolean, default=False)
    # Campos dinámicos por industria (medicamento, próxima cosecha, monto
    # de cotización, lo que sea) — JSON: {"medicamento": "...", "next_reminder_date": "2026-09-01"}
    # Las claves válidas por empresa viven en CustomFieldDef.
    custom_fields = Column(Text, nullable=True)
    owner    = relationship("User",    back_populates="contacts")

    def get_fields(self) -> dict:
        import json
        try:
            return json.loads(self.custom_fields) if self.custom_fields else {}
        except (ValueError, TypeError):
            return {}

    def set_fields(self, data: dict):
        import json
        self.custom_fields = json.dumps(data or {})


# ════════════════════════════════════════════════════════════════
# TELÉFONOS — formato estándar único en toda la plataforma
# ════════════════════════════════════════════════════════════════
# Todo número se guarda SIEMPRE igual: solo dígitos, código de país
# pegado adelante, sin "+", sin espacios, sin guiones.
#   Honduras:    50490908080   (504 + 90908080)
#   Nicaragua:   50585477750   (505 + 85477750)
# Se normaliza aquí — un único lugar — y se reutiliza en la creación
# de contactos (UI, importación CSV, y la API externa /v1) y en el
# emparejamiento de documentos por teléfono, para que un mismo
# contacto nunca quede guardado con dos formatos distintos.
import re as _re

_PHONE_RE = _re.compile(r"^\d{8,15}$")

# Códigos de país predefinidos: Centroamérica, México y USA/Canadá.
CENTRAL_AMERICA_MX_US_CODES = {
    "501": "Belice", "502": "Guatemala", "503": "El Salvador",
    "504": "Honduras", "505": "Nicaragua", "506": "Costa Rica",
    "507": "Panamá", "52": "México", "1": "USA/Canadá",
}


def normalize_phone(raw: str, default_country_code: str = "504") -> "str | None":
    """
    Normaliza un número de teléfono al formato estándar de la plataforma:
    solo dígitos, con código de país incluido, sin '+'. Devuelve None si
    no es un número válido (en vez de guardar un placeholder que rompía
    el envío silenciosamente).
    """
    if not raw:
        return None
    digits = _re.sub(r"\D", "", raw)
    if not digits:
        return None
    # Si vino como número local de 8 dígitos (típico en Centroamérica sin
    # código de país), se asume el código por defecto de la empresa.
    if len(digits) == 8:
        digits = default_country_code + digits
    if not _PHONE_RE.match(digits):
        return None
    return digits


# ════════════════════════════════════════════════════════════════
# CAMPOS PERSONALIZADOS POR EMPRESA (para que la plataforma sirva
# a cualquier rubro sin tocar código: farmacia, ferretería,
# camaronera, agroexportador...)
# ════════════════════════════════════════════════════════════════

class CustomFieldDef(Base):
    __tablename__ = "custom_field_defs"
    id        = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("users.id"), index=True)
    key       = Column(String(80))     # slug usado en {{variables}} y en custom_fields
    label     = Column(String(150))    # lo que ve el usuario
    field_type = Column(String(20), default="text")  # text | date | number
    created_at = Column(DateTime, default=datetime.utcnow)


# Sugerencias de campos por rubro — solo para precargar el formulario al
# dar de alta un cliente nuevo desde el Panel Admin; el admin las puede
# editar o ignorar libremente.
INDUSTRY_FIELD_SUGGESTIONS = {
    "salud": [
        {"key": "medicamento", "label": "Medicamento", "field_type": "text"},
        {"key": "next_reminder_date", "label": "Próximo recordatorio", "field_type": "date"},
    ],
    "ferreteria": [
        {"key": "ultima_compra", "label": "Última compra", "field_type": "text"},
        {"key": "next_reminder_date", "label": "Próximo seguimiento", "field_type": "date"},
    ],
    "camaronera": [
        {"key": "ciclo", "label": "Ciclo / lote", "field_type": "text"},
        {"key": "next_reminder_date", "label": "Próxima cosecha", "field_type": "date"},
    ],
    "agroexportador": [
        {"key": "certificacion", "label": "Certificación", "field_type": "text"},
        {"key": "next_reminder_date", "label": "Próximo vencimiento", "field_type": "date"},
    ],
    "otro": [
        {"key": "next_reminder_date", "label": "Próximo recordatorio", "field_type": "date"},
    ],
}


class Campaign(Base):
    __tablename__ = "campaigns"
    id               = Column(Integer, primary_key=True, index=True)
    owner_id         = Column(Integer, ForeignKey("users.id"))
    name             = Column(String(200))
    message_template = Column(Text)
    # Si el contacto ya no está en la ventana de 24h de WhatsApp, el envío
    # debe usar esta plantilla aprobada por Meta en vez de texto libre
    # (copiado desde CampaignTemplate al crear la campaña).
    meta_template_name     = Column(String(200), nullable=True)
    meta_template_language = Column(String(10), nullable=True, default="es")
    is_recurring     = Column(Boolean, default=False)  # generada por un RecurringCampaign
    status           = Column(Enum(CampaignStatus), default=CampaignStatus.draft)
    total_contacts   = Column(Integer, default=0)
    sent             = Column(Integer, default=0)
    delivered        = Column(Integer, default=0)
    read             = Column(Integer, default=0)
    failed           = Column(Integer, default=0)
    created_at       = Column(DateTime, default=datetime.utcnow)
    owner            = relationship("User",    back_populates="campaigns")
    messages         = relationship("Message", back_populates="campaign")


class Message(Base):
    __tablename__ = "messages"
    id            = Column(Integer, primary_key=True, index=True)
    campaign_id   = Column(Integer, ForeignKey("campaigns.id"))
    contact_phone = Column(String(20))
    contact_name  = Column(String(100))
    status        = Column(String(20), default="pending")
    wamid         = Column(String(200), nullable=True)
    error_msg     = Column(String(500), nullable=True)
    sent_at       = Column(DateTime,    nullable=True)
    campaign      = relationship("Campaign", back_populates="messages")


# ════════════════════════════════════════════════════════════════
# INVOICE / DOCUMENT BLAST MODULE
# ════════════════════════════════════════════════════════════════

class InvoiceBatchStatus(str, enum.Enum):
    pending   = "pending"    # recién creado, sin procesar
    sending   = "sending"    # en proceso de envío
    completed = "completed"  # terminado (con o sin errores parciales)
    failed    = "failed"     # error crítico al iniciar


class InvoiceBatch(Base):
    """
    Un lote de documentos/facturas.
    Cada lote agrupa N envíos individuales (uno por contacto).
    Un usuario puede tener muchos lotes (histórico).
    """
    __tablename__ = "invoice_batches"

    id             = Column(Integer,  primary_key=True, index=True)
    owner_id       = Column(Integer,  ForeignKey("users.id"), index=True)
    name           = Column(String(200))          # "Facturas Junio 2025"
    caption        = Column(Text,  nullable=True) # Texto que acompaña el documento
    # Campo usado para encontrar al destinatario correcto de cada
    # documento: "phone" (por defecto) o la clave de un campo
    # personalizado de esta empresa (ej. "identidad", "rtn", "employee_id").
    # Así una planilla se reparte 1-a-1 sin que el mismo archivo le
    # llegue a todos — cada documento va a quien tenga ese valor exacto
    # en su campo personalizado.
    match_field_key = Column(String(80), default="phone")
    status         = Column(Enum(InvoiceBatchStatus), default=InvoiceBatchStatus.pending)
    total          = Column(Integer, default=0)
    sent           = Column(Integer, default=0)
    delivered      = Column(Integer, default=0)
    failed         = Column(Integer, default=0)
    # Seguridad: cuándo expirar/borrar los archivos del servidor
    auto_delete_at = Column(DateTime, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)

    owner    = relationship("User",          back_populates="invoice_batches")
    invoices = relationship("InvoiceItem",   back_populates="batch",
                            cascade="all, delete-orphan")


class InvoiceItem(Base):
    """
    Un DOCUMENTO individual dentro de un lote (antes solo "factura" —
    ahora cualquier empresa puede mandar lo que le corresponda: facturas,
    recetas, resultados de examen, cotizaciones... incluso mezclado
    dentro del mismo lote, porque el tipo se guarda por ítem).

    Relación: 1 InvoiceItem ↔ 1 contacto ↔ 1 archivo PDF.

    Estrategia de match (elige una por proyecto):
      A) Por phone exacto  → contact_phone = '50499112233'
      B) Por nombre de archivo que incluye el teléfono → se parsea al subir
      C) Por referencia externa (ej. número de factura) → ref_id

    Aquí implementamos A + C para máxima flexibilidad.
    """
    __tablename__ = "invoice_items"

    id            = Column(Integer,  primary_key=True, index=True)
    batch_id      = Column(Integer,  ForeignKey("invoice_batches.id"), index=True)

    # ── Destinatario ────────────────────────────────────────────
    contact_phone = Column(String(20),  index=True)   # +50499112233
    contact_name  = Column(String(100), nullable=True)

    # ── Tipo de documento (libre: Factura, Receta, Cotización, Resultado
    # de examen, Recibo...) — cada ítem del lote puede ser distinto.
    doc_type      = Column(String(50), default="Documento")

    # Valor crudo usado para encontrar al destinatario cuando el lote
    # NO empareja por teléfono (ej. RTN "08011990012345", identidad,
    # employee_id...). Se resuelve contra Contact.custom_fields al
    # momento de enviar, y de ahí se completa contact_phone.
    match_value   = Column(String(200), nullable=True)

    # ── Archivo ─────────────────────────────────────────────────
    file_path     = Column(String(500))        # ruta local: uploads/invoices/<uuid>.pdf
    original_name = Column(String(255))        # nombre original del archivo del usuario
    file_size_kb  = Column(Integer, default=0) # tamaño en KB para validación
    ref_id        = Column(String(100), nullable=True, index=True)  # nº factura / referencia

    # ── Estado de envío ─────────────────────────────────────────
    status        = Column(String(20), default="pending")  # pending/sending/sent/delivered/read/failed
    wamid         = Column(String(200), nullable=True)     # ID de mensaje en Meta
    error_msg     = Column(String(500), nullable=True)
    sent_at       = Column(DateTime,    nullable=True)
    delivered_at  = Column(DateTime,    nullable=True)

    # ── Seguridad: controlar si el archivo ya fue enviado y puede borrarse ──
    file_deleted  = Column(Boolean, default=False)  # True = el PDF ya fue eliminado del disco

    batch = relationship("InvoiceBatch", back_populates="invoices")


# Sugerencias de tipo de documento para el selector del frontend —
# puramente cosmético, el campo es texto libre así que cualquier
# empresa puede escribir el suyo.
DOCUMENT_TYPE_SUGGESTIONS = [
    "Factura", "Recibo", "Cotización", "Receta médica",
    "Resultado de examen", "Contrato", "Comprobante", "Otro"
]


# ════════════════════════════════════════════════════════════════
# CHAT MODULE
# ════════════════════════════════════════════════════════════════

class ConversationStatus(str, enum.Enum):
    open    = "open"
    waiting = "waiting"
    closed  = "closed"


class Conversation(Base):
    __tablename__ = "conversations"
    id            = Column(Integer, primary_key=True, index=True)
    owner_id      = Column(Integer, ForeignKey("users.id"))
    contact_phone = Column(String(20))
    contact_name  = Column(String(100), default="Desconocido")
    status        = Column(Enum(ConversationStatus), default=ConversationStatus.open)
    unread        = Column(Integer, default=0)
    last_msg      = Column(Text,    nullable=True)
    last_msg_at   = Column(DateTime, nullable=True)
    # Último mensaje ENTRANTE del contacto — determina si seguimos dentro
    # de la ventana de 24h de Meta para poder mandar texto libre, o si ya
    # toca usar una plantilla aprobada (message_template / HSM).
    last_inbound_at    = Column(DateTime, nullable=True)
    last_auto_reply_at = Column(DateTime, nullable=True)  # throttle de auto-respuesta
    created_at    = Column(DateTime, default=datetime.utcnow)
    owner         = relationship("User",        back_populates="conversations")
    chat_messages = relationship("ChatMessage", back_populates="conversation",
                                 order_by="ChatMessage.created_at")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id              = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"))
    direction       = Column(String(10))
    body            = Column(Text)
    msg_type        = Column(String(20), default="text")
    wamid           = Column(String(200), nullable=True)
    status          = Column(String(20),  default="sent")
    created_at      = Column(DateTime, default=datetime.utcnow)
    conversation    = relationship("Conversation", back_populates="chat_messages")

class AgentRole(str, enum.Enum):
    admin  = "admin"    # tiene marcados TODOS los permisos
    custom = "custom"   # combinación cualquiera de permisos (lo normal)
    sender = "sender"   # sin permisos marcados todavía


# ════════════════════════════════════════════════════════════════
# PERMISOS / ROLES DE AGENTES
# ════════════════════════════════════════════════════════════════
# Catálogo único de roles-permiso disponibles. El frontend renderiza un
# checkbox por cada uno al crear/editar un agente — y SE PUEDE MARCAR
# MÁS DE UNO (un agente puede, a la vez, enviar campañas Y atender el
# chat, por ejemplo). El backend valida y persiste exactamente lo que
# quedó marcado, y lo devuelve con check ✓ de confirmación.
AGENT_PERMISSIONS = {
    "send_invoices":   "Enviar documentos (facturas, recetas, cotizaciones...)",
    "send_campaigns":  "Enviar campañas (con plantillas asignadas)",
    "manage_contacts": "Gestionar contactos (agregar/eliminar)",
    "view_reports":    "Ver reportes y estadísticas",
    "chat_support":    "Atender chat en vivo",
    # No cualquiera debe poder abrir el Swagger de la API — solo el
    # dueño de la empresa/superadmin (siempre) o un agente al que se le
    # haya otorgado este permiso explícitamente.
    "view_api_docs":   "Ver documentación de la API (Swagger)",
}

# Ya no existe un único "rol" con permisos fijos — esto queda solo como
# atajo opcional en el frontend ("Marcar todos" / "Solo documentos").
ROLE_DEFAULT_PERMISSIONS = {
    "admin":  list(AGENT_PERMISSIONS.keys()),
    "sender": ["send_invoices"],
}


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"))   # Empresa dueña

    name = Column(String(100))
    email = Column(String(100), unique=True, index=True)   # ← Para login
    hashed_password = Column(String(200))                  # ← Para login

    # Etiqueta calculada automáticamente a partir de los permisos
    # (admin = tiene todos, sender = ninguno, custom = una mezcla) —
    # ya NO se elige a mano, es solo informativa.
    role = Column(Enum(AgentRole), default=AgentRole.sender)

    # Permisos/roles reales, guardados como string separado por comas
    # (ej. "send_invoices,view_reports"). Un agente puede tener varios
    # a la vez. Se valida contra AGENT_PERMISSIONS antes de guardar.
    permissions = Column(String(300), default="")

    whatsapp_phone_id = Column(String(100), nullable=True)
    whatsapp_token = Column(String(500), nullable=True)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", backref="agents")

    def get_permissions(self) -> list:
        return [p for p in (self.permissions or "").split(",") if p]

    def set_permissions(self, perms: list):
        clean = [p for p in perms if p in AGENT_PERMISSIONS]
        self.permissions = ",".join(clean)
        # Recalcular la etiqueta informativa a partir de lo que quedó.
        if not clean:
            self.role = AgentRole.sender
        elif set(clean) == set(AGENT_PERMISSIONS.keys()):
            self.role = AgentRole.admin
        else:
            self.role = AgentRole.custom


# ════════════════════════════════════════════════════════════════
# PLANTILLAS DE CAMPAÑA (creadas por el superadmin, asignadas a
# una empresa/cliente específica — o globales si client_id es None)
# ════════════════════════════════════════════════════════════════

class CampaignTemplate(Base):
    __tablename__ = "campaign_templates"

    id           = Column(Integer, primary_key=True, index=True)
    # Empresa (User) a la que se le asignó esta plantilla.
    # NULL = plantilla global, disponible para todos los clientes.
    client_id    = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_by   = Column(Integer, ForeignKey("users.id"), nullable=True)  # superadmin autor
    name         = Column(String(200))
    category     = Column(String(100), nullable=True)   # ej. "Recordatorio de medicamentos"
    message_template = Column(Text)                     # con variables {{name}}, {{phone}}, etc.
    # Si el contacto ya no está dentro de la ventana de 24h de WhatsApp
    # (no ha escrito recientemente), Meta EXIGE usar una plantilla HSM
    # aprobada en vez de texto libre. Aquí se enlaza cuál usar.
    meta_template_name     = Column(String(200), nullable=True)
    meta_template_language = Column(String(10), nullable=True, default="es")
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    client = relationship("User", foreign_keys=[client_id])


# ════════════════════════════════════════════════════════════════
# PLANTILLAS APROBADAS POR META (HSM) — sincronizadas por cliente
# ════════════════════════════════════════════════════════════════
# WhatsApp Cloud API solo permite texto 100% libre si el contacto te
# escribió en las últimas 24h. Fuera de esa ventana hay que usar una
# plantilla ya aprobada en Meta Business Manager. Este modelo cachea
# el catálogo de cada cliente para no tener que ir a Meta cada vez.

class MetaTemplate(Base):
    __tablename__ = "meta_templates"

    id         = Column(Integer, primary_key=True, index=True)
    client_id  = Column(Integer, ForeignKey("users.id"), index=True)
    name       = Column(String(200))          # nombre EXACTO registrado en Meta
    language   = Column(String(10), default="es")
    category   = Column(String(50), nullable=True)   # UTILITY / MARKETING / AUTHENTICATION
    status     = Column(String(30), nullable=True)   # APPROVED / PENDING / REJECTED
    body_text  = Column(Text, nullable=True)          # texto con {{1}}, {{2}}...
    variable_count = Column(Integer, default=0)
    synced_at  = Column(DateTime, default=datetime.utcnow)


# ════════════════════════════════════════════════════════════════
# RECORDATORIOS RECURRENTES (campañas que se disparan solas)
# ════════════════════════════════════════════════════════════════
# El caso típico: "cada contacto con medicamento mensual debe recibir
# su recordatorio en su propia fecha". Se apoya en un campo personalizado
# de tipo fecha (custom_fields[date_field_key]) por contacto. Un job
# corre periódicamente, manda el recordatorio a quien ya cumplió su
# fecha, y si hay interval_days, reprograma la fecha automáticamente.

class RecurringCampaign(Base):
    __tablename__ = "recurring_campaigns"

    id           = Column(Integer, primary_key=True, index=True)
    client_id    = Column(Integer, ForeignKey("users.id"), index=True)
    template_id  = Column(Integer, ForeignKey("campaign_templates.id"))
    name         = Column(String(200))
    contact_tag  = Column(String(50), nullable=True)     # filtro opcional
    date_field_key = Column(String(80), default="next_reminder_date")
    interval_days  = Column(Integer, nullable=True)      # si se define, se reprograma sola
    is_active    = Column(Boolean, default=True)
    last_run_at  = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    template = relationship("CampaignTemplate")


# ════════════════════════════════════════════════════════════════
# ETIQUETAS GESTIONADAS (antes: texto libre en Contact.tag)
# ════════════════════════════════════════════════════════════════
# Cada empresa administra su propio catálogo de etiquetas, con color
# y descripción — igual que en la mayoría de plataformas de WhatsApp
# marketing. Contact.tag sigue siendo el campo que se guarda por
# contacto (texto), pero ahora se elige de este catálogo en vez de
# escribirse a mano, evitando duplicados como "Vip"/"vip "/"VIP".

class ContactTag(Base):
    __tablename__ = "contact_tags"
    id          = Column(Integer, primary_key=True, index=True)
    client_id   = Column(Integer, ForeignKey("users.id"), index=True)
    name        = Column(String(80))
    color       = Column(String(20), default="#22d3a0")
    description = Column(String(255), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


# ════════════════════════════════════════════════════════════════
# API KEYS — para que cada empresa integre WaSapinf con SUS propios
# sistemas (ERP, sitio web, planillas...) sin depender de ti.
# ════════════════════════════════════════════════════════════════

class ApiKey(Base):
    __tablename__ = "api_keys"
    id           = Column(Integer, primary_key=True, index=True)
    client_id    = Column(Integer, ForeignKey("users.id"), index=True)
    name         = Column(String(150))          # "Integración con mi ERP"
    key_prefix   = Column(String(24))            # lo único visible tras crearla, ej. "wsp_live_ab12cd34…"
    key_hash     = Column(String(200))           # sha256 de la key completa — nunca se guarda en claro
    is_active    = Column(Boolean, default=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)


# ════════════════════════════════════════════════════════════════
# WEBHOOKS — para que el sistema del cliente se entere en tiempo
# real de eventos (mensaje recibido, campaña terminada, etc.) sin
# tener que preguntarle constantemente a la API.
# ════════════════════════════════════════════════════════════════

WEBHOOK_EVENTS = {
    "message.sent":       "Mensaje saliente enviado",
    "message.received":   "Mensaje entrante de un contacto",
    "campaign.completed": "Campaña finalizada",
    "contact.created":    "Contacto nuevo creado",
}

class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    id                 = Column(Integer, primary_key=True, index=True)
    client_id          = Column(Integer, ForeignKey("users.id"), index=True)
    url                = Column(String(500))
    events             = Column(String(300))     # claves de WEBHOOK_EVENTS separadas por coma
    secret             = Column(String(100))     # firma HMAC-SHA256 del payload
    is_active          = Column(Boolean, default=True)
    last_triggered_at  = Column(DateTime, nullable=True)
    last_status        = Column(String(20), nullable=True)   # "ok" | "error"
    created_at         = Column(DateTime, default=datetime.utcnow)

    def get_events(self) -> list:
        return [e for e in (self.events or "").split(",") if e]

    def set_events(self, evts: list):
        clean = [e for e in evts if e in WEBHOOK_EVENTS]
        self.events = ",".join(clean)


# En models/database.py
async def init_db():
    async with engine.begin() as conn:
        try:
            await conn.run_sync(Base.metadata.create_all)
            print("✅ Tablas sincronizadas con la base de datos.")
        except Exception as e:
            # Si el error es solo que la tabla existe, lo ignoramos y seguimos
            if "already exists" in str(e):
                print("ℹ️ Las tablas ya existen, omitiendo creación.")
            else:
                raise e # Si es otro error, sí queremos verlo


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session