import os

from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, ForeignKey, Enum, BigInteger
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from datetime import datetime
import enum

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./wasapi.db")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
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
    owner    = relationship("User",    back_populates="contacts")


class Campaign(Base):
    __tablename__ = "campaigns"
    id               = Column(Integer, primary_key=True, index=True)
    owner_id         = Column(Integer, ForeignKey("users.id"))
    name             = Column(String(200))
    message_template = Column(Text)
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
    Un documento individual dentro de un lote.
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
    admin = "admin"
    sender = "sender"   # Solo envía facturas


class AgentRole(str, enum.Enum):
    admin = "admin"
    sender = "sender"   # Solo envía facturas


class Agent(Base):
    __tablename__ = "agents"
    
    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"))   # Empresa dueña
    
    name = Column(String(100))
    email = Column(String(100), unique=True, index=True)   # ← Para login
    hashed_password = Column(String(200))                  # ← Para login
    
    role = Column(Enum(AgentRole), default=AgentRole.sender)
    
    whatsapp_phone_id = Column(String(100), nullable=True)
    whatsapp_token = Column(String(500), nullable=True)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    owner = relationship("User", backref="agents")
    
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