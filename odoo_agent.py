# odoo_agent.py
# Bridge FastAPI para Odoo 17: envío a VendorBills (SMTP), búsqueda por checksum,
# rellenado de borradores y utilidades de proveedor/impuestos/cuentas.
# Mantiene naming y variables de entorno acordadas para coherencia con Render.

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict, Any, Tuple
import xmlrpc.client
import base64
import hashlib
import os
from datetime import datetime
import mimetypes
from secrets import compare_digest
import re
import requests
import smtplib
from email.message import EmailMessage

# ──────────────────────────────────────────────────────────────────────────────
# Variables de entorno (incluye todas las acordadas para coherencia)
# ──────────────────────────────────────────────────────────────────────────────

APP_ENV = os.getenv("APP_ENV", "production")
TZ = os.getenv("TZ", os.getenv("TIMEZONE", "Europe/Madrid"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info")

REPO_URL = os.getenv("REPO_URL", "")
REPO_BRANCH = os.getenv("REPO_BRANCH", "")
BUILD_CMD = os.getenv("BUILD_CMD", "")
WEB_START_CMD = os.getenv("WEB_START_CMD", "")
WORKER_START_CMD = os.getenv("WORKER_START_CMD", "")

# Email / SMTP
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")
SMTP_SECURITY = os.getenv("SMTP_SECURITY", "ssl")  # ssl | starttls | none
MAIL_SUBJECT_TEMPLATE = os.getenv("MAIL_SUBJECT_TEMPLATE", "")
MAIL_SUBJECT_MODE = os.getenv("MAIL_SUBJECT_MODE", "blank")  # blank | filename
MAIL_BODY_EMPTY = (os.getenv("MAIL_BODY_EMPTY", "true").lower() == "true")
ALIAS_TO = os.getenv("ALIAS_TO", "")
VENDORBILLS_EMAIL = os.getenv("VENDORBILLS_EMAIL", ALIAS_TO or "vendor-bills@energy-blind-sl1.odoo.com")

# Odoo
ODOO_URL  = os.getenv("ODOO_URL", os.getenv("ODOO_BASE_URL", "")).rstrip("/")
ODOO_DB   = os.getenv("ODOO_DB", "")
ODOO_USER = os.getenv("ODOO_USER", "")
ODOO_API  = os.getenv("ODOO_API_KEY", "")
ODOO_VERSION = os.getenv("ODOO_VERSION", "17")
COMPANY_ID = os.getenv("COMPANY_ID", "")
PURCHASE_JOURNAL_ID = os.getenv("PURCHASE_JOURNAL_ID", "")

# Reglas de envío/ingesta
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "EUR")
MAX_ATTACHMENT_MB = float(os.getenv("MAX_ATTACHMENT_MB", "20"))
SENDING_STRATEGY = os.getenv("SENDING_STRATEGY", "one_file_per_email")
PDF_MULTIPAGE_POLICY = os.getenv("PDF_MULTIPAGE_POLICY", "one_invoice_per_pdf")
ALLOWED_EXT = os.getenv("ALLOWED_EXT", "jpg,png,heic,pdf")
EXIF_ORIENTATION_FIX = (os.getenv("EXIF_ORIENTATION_FIX", "false").lower() == "true")
HEIC_CONVERT_TO = os.getenv("HEIC_CONVERT_TO", "none")

# Renombrado (no se aplica al binario enviado; conservamos el nombre original, pero exponemos vars por coherencia)
RENAME_ENABLED = (os.getenv("RENAME_ENABLED", "true").lower() == "true")
FILENAME_DATE_FMT = os.getenv("FILENAME_DATE_FMT", "YYYYMMDD")
FILENAME_COUNTER_PAD = int(os.getenv("FILENAME_COUNTER_PAD", "2"))
FILENAME_SEPARATOR = os.getenv("FILENAME_SEPARATOR", "_")
FILENAME_PATTERN = os.getenv("FILENAME_PATTERN", "{DATE}{SEP}{IDX}")
FILENAME_TIMEZONE = os.getenv("FILENAME_TIMEZONE", TZ)

# Idempotencia / token (no imprescindible aquí, pero lo mantenemos)
USE_TOKEN = (os.getenv("USE_TOKEN", "false").lower() == "true")
TOKEN_FORMAT = os.getenv("TOKEN_FORMAT", "")
TOKEN_IN_SUBJECT = (os.getenv("TOKEN_IN_SUBJECT", "false").lower() == "true")
TOKEN_IN_FILENAME = (os.getenv("TOKEN_IN_FILENAME", "false").lower() == "true")
TOKEN_SAVE_FIELD = os.getenv("TOKEN_SAVE_FIELD", "")

# Polling y backoff (el orquestador los usa; aquí devolvemos delay)
DEFAULT_DELAY_SEC = int(os.getenv("DEFAULT_DELAY_SEC", "60"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "10"))
POLL_TIMEOUT_SEC = int(os.getenv("POLL_TIMEOUT_SEC", "180"))
EMAIL_RETRY_COUNT = int(os.getenv("EMAIL_RETRY_COUNT", "0"))
BACKOFF_SCHEDULE = [int(x) for x in os.getenv("BACKOFF_SCHEDULE", "120,300,900").split(",") if x.strip()]

# Seguridad API
API_TOKEN = os.getenv("API_TOKEN", "")

# Anti-falsos positivos (excluir tu propia empresa)
SELF_COMPANY_KEYWORDS = [s.strip().lower() for s in (os.getenv("SELF_COMPANY_KEYWORDS", "") or "").split(",") if s.strip()]
SELF_EMAIL_DOMAINS    = [s.strip().lower() for s in (os.getenv("SELF_EMAIL_DOMAINS", "") or "").split(",") if s.strip()]

# OpenAI files (solo para descargar file_... si el upload viene como id)
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY", "") or "").strip()

POST_TOLERANCE = 0.01  # tolerancia para totales (validación opcional)

# ──────────────────────────────────────────────────────────────────────────────
# Inicialización Odoo (XML-RPC)
# ──────────────────────────────────────────────────────────────────────────────

if not (ODOO_URL and ODOO_DB and ODOO_USER and ODOO_API):
    raise RuntimeError("Faltan variables ODOO_* en el entorno (ODOO_URL/DB/USER/API_KEY).")

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API, {})
if not uid:
    raise RuntimeError("No se pudo autenticar con Odoo. Revisa ODOO_* y permisos.")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Odoo Agent Bridge", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ajusta en prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def auth(authorization: Optional[str] = Header(None)):
    if not API_TOKEN:
        raise HTTPException(status_code=500, detail="API_TOKEN no configurado en el servidor")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not compare_digest(token, API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return True

# ──────────────────────────────────────────────────────────────────────────────
# Utilidades genéricas
# ──────────────────────────────────────────────────────────────────────────────

def _sanitize_filename(name: Optional[str]) -> str:
    base = (name or "adjunto").strip() or "adjunto"
    return os.path.basename(base)

def _detect_mimetype(filename: str, fallback: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or fallback

def _ensure_binary_payload(data: bytes) -> str:
    if not data:
        raise HTTPException(status_code=422, detail="El adjunto está vacío")
    limit = int(MAX_ATTACHMENT_MB * 1024 * 1024)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"Adjunto > {MAX_ATTACHMENT_MB:.0f} MB")
    return base64.b64encode(data).decode()

def _decode_b64_forgiving(payload: str) -> bytes:
    s = payload.strip()
    if "base64," in s:
        s = s.split("base64,", 1)[1]
    s = "".join(s.split())
    if not s:
        raise HTTPException(status_code=422, detail="attachment_b64 vacío")
    s = s + "=" * (-len(s) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(s)
        except Exception:
            continue
    raise HTTPException(status_code=422, detail="attachment_b64 inválido")

def _download_openai_file(file_id: str) -> Tuple[bytes, str, str]:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY no configurada")
    url = f"https://api.openai.com/v1/files/{file_id}/content"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=60)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error conectando a OpenAI: {e!s}")
    if r.status_code != 200:
        snippet = (r.text or "")[:200]
        raise HTTPException(status_code=422, detail=f"No se pudo descargar {file_id} (HTTP {r.status_code}): {snippet}")
    fname = None
    cd = r.headers.get("content-disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        fname = m.group(1)
    mt = _detect_mimetype(fname or "") or "application/pdf"
    return r.content, (fname or f"{file_id}.bin"), mt

async def _resolve_upload_to_bytes(file: UploadFile) -> Tuple[bytes, str, str]:
    raw = await file.read()
    # ¿Texto (file_id/base64) o binario real?
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        text = None
    if text:
        s = text.strip().strip('"').strip("'")
        if re.fullmatch(r'file[_-][A-Za-z0-9]+', s):
            data, fname, mt = _download_openai_file(s)
            return data, fname, mt
        if "base64," in s or (len(s) > 120 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", s)):
            try:
                data = _decode_b64_forgiving(s)
                fname = file.filename or "adjunto"
                mt = _detect_mimetype(fname) or "application/pdf"
                return data, fname, mt
            except Exception:
                pass
    fname = file.filename or "adjunto"
    mt = file.content_type or _detect_mimetype(fname) or "application/octet-stream"
    return raw, fname, mt

# ──────────────────────────────────────────────────────────────────────────────
# Utilidades Odoo: compañía, partner, cuentas, impuestos, adjuntos
# ──────────────────────────────────────────────────────────────────────────────

def get_company_id() -> int:
    user = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.users", "read", [[uid], ["company_id"]])[0]
    return user["company_id"][0]

def get_company_partner_id() -> int:
    company_id = get_company_id()
    comp = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.company", "read", [[company_id], ["partner_id"]])[0]
    return comp["partner_id"][0]

def find_partner(q: Optional[str], vat: Optional[str], limit: int = 10) -> List[Dict[str, Any]]:
    company_partner_id = get_company_partner_id()
    domain_base = [
        ("supplier_rank", ">", 0),
        ("active", "=", True),
        ("commercial_partner_id", "!=", company_partner_id),
        ("id", "!=", company_partner_id),
    ]
    for kw in SELF_COMPANY_KEYWORDS:
        domain_base.append(("name", "not ilike", kw))
    for dom in SELF_EMAIL_DOMAINS:
        domain_base.append(("email", "not ilike", f"@{dom}"))

    found: List[Dict[str, Any]] = []
    if vat:
        found = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "res.partner", "search_read",
            [[("vat", "=", vat)] + domain_base],
            {"fields": ["id", "name", "vat", "email"], "limit": limit}
        )
    elif q:
        exact = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "res.partner", "search_read",
            [[("name", "=", q)] + domain_base],
            {"fields": ["id", "name", "vat", "email"], "limit": limit}
        )
        found = exact or models.execute_kw(
            ODOO_DB, uid, ODOO_API, "res.partner", "search_read",
            [[("name", "ilike", q)] + domain_base],
            {"fields": ["id", "name", "vat", "email"], "limit": limit}
        )
    else:
        found = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "res.partner", "search_read",
            [domain_base], {"fields": ["id", "name", "vat", "email"], "limit": limit}
        )

    for f in found:
        cnt = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "account.move", "search_count",
            [[("move_type", "in", ["in_invoice", "in_refund"]), ("partner_id", "=", f["id"])]]
        )
        f["usage_count"] = cnt
    found.sort(key=lambda x: (-x.get("usage_count", 0), x.get("name", "")))
    return found

def ensure_partner(data: Dict[str, Any], allow_create: bool = False) -> Dict[str, Any]:
    name = (data.get("name") or data.get("display_name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="El nombre del proveedor es obligatorio")
    vat = (data.get("vat") or "").strip().upper() or None
    email = (data.get("email") or "").strip().lower() or None
    phone = (data.get("phone") or "").strip() or None

    company_partner_id = get_company_partner_id()
    domain_base = [
        ("supplier_rank", ">", 0),
        ("active", "=", True),
        ("id", "!=", company_partner_id),
        ("commercial_partner_id", "!=", company_partner_id),
    ]

    def _read_one(ids):
        return models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "read", [ids, ["id", "name", "vat", "email", "phone"]])[0]

    # VAT exacto
    if vat:
        vat_ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "search", [[("vat", "=", vat)] + domain_base], {"limit": 1})
        if vat_ids:
            partner = _read_one(vat_ids)
            updates = {}
            if email and not partner.get("email"): updates["email"] = email
            if phone and not partner.get("phone"): updates["phone"] = phone
            if updates:
                models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "write", [[partner["id"]], updates])
                partner.update(updates)
            return partner

    # email ilike
    if email:
        email_ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "search", [[("email", "ilike", email)] + domain_base], {"limit": 1})
        if email_ids:
            partner = _read_one(email_ids)
            updates = {}
            if vat and not partner.get("vat"): updates["vat"] = vat
            if phone and not partner.get("phone"): updates["phone"] = phone
            if updates:
                models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "write", [[partner["id"]], updates])
                partner.update(updates)
            return partner

    # nombre exacto
    name_ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "search", [[("name", "=", name)] + domain_base], {"limit": 1})
    if name_ids:
        partner = _read_one(name_ids)
        updates = {}
        if vat and not partner.get("vat"): updates["vat"] = vat
        if email and not partner.get("email"): updates["email"] = email
        if phone and not partner.get("phone"): updates["phone"] = phone
        if updates:
            models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "write", [[partner["id"]], updates])
            partner.update(updates)
        return partner

    # nombre ilike
    ilike_ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "search", [[("name", "ilike", name)] + domain_base], {"limit": 1})
    if ilike_ids:
        partner = _read_one(ilike_ids)
        updates = {}
        if vat and not partner.get("vat"): updates["vat"] = vat
        if email and not partner.get("email"): updates["email"] = email
        if phone and not partner.get("phone"): updates["phone"] = phone
        if updates:
            models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "write", [[partner["id"]], updates])
            partner.update(updates)
        return partner

    if not allow_create:
        raise HTTPException(status_code=409, detail="AWAITING_SUPPLIER_CONFIRMATION")

    vals = {"name": name, "supplier_rank": 1, "type": "contact"}
    if vat: vals["vat"] = vat
    if email: vals["email"] = email
    if phone: vals["phone"] = phone
    partner_id = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "create", [vals])
    return models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "read", [[partner_id], ["id", "name", "vat", "email", "phone"]])[0]

def find_account_by_reference(code_or_id: Optional[Any]) -> Optional[int]:
    if not code_or_id:
        return None
    company_id = get_company_id()
    if isinstance(code_or_id, int):
        ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.account", "search", [[("id", "=", code_or_id), ("company_id", "=", company_id)]], {"limit": 1})
        return ids[0] if ids else None
    s = str(code_or_id).strip()
    if not s:
        return None
    if s.lower().startswith("id:"):
        try:
            acc_id = int(s.split(":", 1)[1])
        except ValueError:
            return None
        ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.account", "search", [[("id", "=", acc_id), ("company_id", "=", company_id)]], {"limit": 1})
        return ids[0] if ids else None
    ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.account", "search", [[("code", "=", s), ("company_id", "=", company_id)]], {"limit": 1})
    if ids: return ids[0]
    ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.account", "search", [[("name", "=", s), ("company_id", "=", company_id)]], {"limit": 1})
    return ids[0] if ids else None

def _normalize_list_of_strings(value: Optional[Any]) -> List[str]:
    if value is None: return []
    if isinstance(value, (list, tuple)): items = value
    else: items = str(value).split(",")
    return [str(x).strip() for x in items if str(x).strip()]

def find_taxes_by_names(codes: Optional[Any]) -> List[int]:
    """Acepta: id:NN, nombre, description, o prefijo numérico ('21','10','4','0'). Filtra por compras."""
    names = _normalize_list_of_strings(codes)
    if not names: return []
    company_id = get_company_id()
    result: List[int] = []
    for raw in names:
        s = str(raw).strip()
        if not s: continue
        if s.lower().startswith("id:"):
            try: tax_id = int(s.split(":", 1)[1])
            except ValueError: tax_id = None
            if tax_id:
                ok = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.tax", "search",
                                       [[("id","=",tax_id),("company_id","=",company_id)]], {"limit":1})
                if ok: result.append(ok[0])
            continue
        perc = None
        if re.fullmatch(r"\d{1,2}", s):
            try: perc = int(s)
            except Exception: perc = None
        if perc is not None:
            cand = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.tax", "search",
                [[("company_id","=",company_id),("active","=",True),("type_tax_use","=","purchase"),("amount","=",float(perc))]],
                {"limit":1})
            if cand: result.append(cand[0]); continue
            cand = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.tax", "search",
                [[("company_id","=",company_id),("active","=",True),("type_tax_use","=","purchase"),("description","ilike",s)]],
                {"limit":1})
            if cand: result.append(cand[0]); continue
        code_ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.tax", "search",
            [[("company_id","=",company_id),("active","=",True),("type_tax_use","=","purchase"),("description","=",s)]],
            {"limit":1})
        if code_ids: result.append(code_ids[0]); continue
        exact_ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.tax", "search",
            [[("company_id","=",company_id),("active","=",True),("type_tax_use","=","purchase"),("name","=",s)]],
            {"limit":1})
        if exact_ids: result.append(exact_ids[0]); continue
        fuzzy_ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.tax", "search",
            [[("company_id","=",company_id),("active","=",True),("type_tax_use","=","purchase"),("name","ilike",s)]],
            {"limit":1})
        if fuzzy_ids: result.append(fuzzy_ids[0])
    return result

def _create_invoice_attachment(move_id: int, filename: str, data: bytes, mimetype: Optional[str] = None) -> int:
    clean = _sanitize_filename(filename)
    mt = mimetype or _detect_mimetype(clean)
    datas = _ensure_binary_payload(data)
    att_id = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "ir.attachment", "create", [[{
            "name": clean,
            "res_model": "account.move",
            "res_id": move_id,
            "datas": datas,
            "mimetype": mt,
        }]]
    )
    return att_id

def _attach_comment(move_id: int, body: str) -> None:
    try:
        models.execute_kw(ODOO_DB, uid, ODOO_API, "mail.message", "create", [[{
            "model": "account.move",
            "res_id": move_id,
            "body": body,
            "message_type": "comment",
        }]])
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────────────────────
# SMTP: enviar a VendorBills (una imagen = un email)
# ──────────────────────────────────────────────────────────────────────────────

def _send_blank_email_with_attachment(to_addr: str, filename: str, file_bytes: bytes, subject_mode: Optional[str] = None):
    if not (SMTP_HOST and SMTP_FROM and to_addr):
        raise HTTPException(status_code=500, detail="SMTP no configurado (SMTP_HOST/SMTP_FROM o destino vacío)")
    mode = (subject_mode or MAIL_SUBJECT_MODE or "blank").lower()
    subject = (MAIL_SUBJECT_TEMPLATE or (filename if mode == "filename" else "")).format(FILENAME=filename)
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content("" if MAIL_BODY_EMPTY else (os.getenv("MAIL_BODY_TEXT", "") or ""))
    msg.add_attachment(file_bytes, maintype="application", subtype="octet-stream", filename=_sanitize_filename(filename))
    if SMTP_SECURITY == "ssl":
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
            if SMTP_USER: s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    elif SMTP_SECURITY == "starttls":
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            if SMTP_USER: s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            if SMTP_USER: s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI endpoints
# ──────────────────────────────────────────────────────────────────────────────

app.add_api_route("/", lambda: {"message": "Odoo Agent Bridge OK"}, methods=["GET"])

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "odoo-agent", "time": datetime.utcnow().isoformat() + "Z"}

@app.get("/odoo/ping")
def ping(authorized: bool = Depends(auth)):
    return {"ok": True, "db": ODOO_DB, "user": ODOO_USER}

# 1) Enviar email a VendorBills y devolver SHA-1 (contrato con el orquestador)
@app.post("/vendorbills/send")
async def vendorbills_send(
    authorized: bool = Depends(auth),
    file: UploadFile = File(...),
    alias_to: Optional[str] = Form(None),
    subject_mode: Optional[str] = Form(None),
    delay_seconds: Optional[int] = Form(None)
):
    data, fname, _ = await _resolve_upload_to_bytes(file)
    if len(data) > int(MAX_ATTACHMENT_MB * 1024 * 1024):
        raise HTTPException(status_code=413, detail=f"Adjunto > {MAX_ATTACHMENT_MB:.0f} MB")
    sha1 = hashlib.sha1(data).hexdigest()
    to_addr = alias_to or VENDORBILLS_EMAIL
    try:
        _send_blank_email_with_attachment(to_addr, fname, data, subject_mode)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SMTP send failed: {e!s}")
    return {
        "status": "sent",
        "file_sha1": sha1,
        "filename": _sanitize_filename(fname),
        "to": to_addr,
        "delay_seconds": int(delay_seconds or DEFAULT_DELAY_SEC)
    }

# 2) Buscar borrador por checksum en ir.attachment
@app.get("/odoo/attachments/find_by_checksum")
def attachments_find_by_checksum(
    sha1: str = Query(..., description="checksum SHA-1 del binario enviado por email"),
    authorized: bool = Depends(auth),
    limit: int = 5
):
    att_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "ir.attachment", "search",
        [[("res_model", "=", "account.move"), ("checksum", "=", sha1)]],
        {"limit": limit, "order": "create_date desc"}
    )
    if not att_ids:
        return {"count": 0, "move_id": None, "attachments": []}
    atts = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "ir.attachment", "read",
        [att_ids, ["id", "res_id", "name", "create_date"]]
    )
    move_id = next((a["res_id"] for a in atts if a.get("res_id")), None)
    return {"count": len(att_ids), "move_id": move_id, "attachments": atts}

# 3) Rellenar borrador con datos (los proporciona la API de ChatGPT)
@app.post("/odoo/invoices/fill_draft")
def invoices_fill_draft(payload: Dict[str, Any], authorized: bool = Depends(auth)):
    """
    payload:
      move_id: int
      partner_id? | partner? + allow_create_supplier?: bool=False
      invoice_ref?: str
      invoice_date?: 'YYYY-MM-DD'
      amount_untaxed?: float
      tax_total?: float
      amount_total?: float
      description?: str (default 'Sin producto')
      account_code?: str
      tax_code_prefix?: str ('21','10','4','0' o 'id:NN' o nombres)
      file_sha1?: str, filename?: str
      auto_post?: bool (solo si totales cuadran)
    """
    move_id = int(payload["move_id"])
    allow_create = bool(payload.get("allow_create_supplier", False))

    partner_id = payload.get("partner_id")
    if not partner_id:
        partner_data = payload.get("partner")
        if not partner_data:
            raise HTTPException(status_code=422, detail="Falta partner_id o datos de partner")
        partner = ensure_partner({
            "name": partner_data.get("name", ""),
            "vat": partner_data.get("vat"),
            "email": partner_data.get("email"),
            "phone": partner_data.get("phone"),
        }, allow_create=allow_create)
        partner_id = partner["id"]

    account_id = find_account_by_reference(payload.get("account_code") or payload.get("account_id") or payload.get("account"))
    taxes = find_taxes_by_names(payload.get("tax_code_prefix") or payload.get("tax_codes") or payload.get("tax_ids"))

    au = payload.get("amount_untaxed"); tv = payload.get("tax_total"); at = payload.get("amount_total")
    ok_amounts = False
    if au is not None and tv is not None and at is not None:
        try:
            ok_amounts = abs((float(au) + float(tv)) - float(at)) <= POST_TOLERANCE
        except Exception:
            ok_amounts = False

    line_vals = {"name": payload.get("description") or "Sin producto", "quantity": 1.0, "price_unit": float(au or 0.0)}
    if account_id: line_vals["account_id"] = int(account_id)
    if taxes: line_vals["tax_ids"] = [(6, 0, taxes)]

    vals = {
        "partner_id": int(partner_id),
        "ref": payload.get("invoice_ref") or False,
        "invoice_date": payload.get("invoice_date") or False,
        "invoice_line_ids": [(0, 0, line_vals)],
    }
    models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move", "write", [[move_id], vals])

    sha1 = payload.get("file_sha1") or ""
    fname = payload.get("filename") or ""
    note = f"Agente: borrador completado. Ref={vals['ref'] or ''}. Archivo={_sanitize_filename(fname)} SHA1={sha1}".strip()
    _attach_comment(move_id, note)

    if payload.get("auto_post") and ok_amounts:
        try:
            models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move", "action_post", [[move_id]])
            return {"status": "posted", "move_id": move_id, "totals_ok": ok_amounts}
        except Exception as e:
            return {"status": "filled_needs_review", "move_id": move_id, "totals_ok": ok_amounts, "error": str(e)}

    return {"status": "filled", "move_id": move_id, "totals_ok": ok_amounts}

# 4) Adjuntar un archivo por API a una factura existente (para duplicados/avisos)
@app.post("/odoo/invoices/attach")
async def invoices_attach(
    authorized: bool = Depends(auth),
    move_id: int = Form(...),
    file: UploadFile = File(...)
):
    data, fname, ctype = await _resolve_upload_to_bytes(file)
    att_id = _create_invoice_attachment(move_id, fname, data, ctype or None)
    _attach_comment(move_id, f"Adjunto subido: {_sanitize_filename(fname)}")
    return {"status": "ok", "move_id": move_id, "attachment_id": att_id}

# 5) Utilidades de proveedores (búsqueda y ensure con confirmación explícita)
@app.get("/odoo/providers/search")
def providers_search(
    authorized: bool = Depends(auth),
    q: Optional[str] = Query(None, description="texto de nombre"),
    vat: Optional[str] = Query(None, description="CIF/NIF"),
    limit: int = 10
):
    items = find_partner(q=q, vat=vat, limit=limit)
    return {"count": len(items), "items": items}

@app.post("/odoo/providers/ensure")
def providers_ensure(payload: Dict[str, Any], authorized: bool = Depends(auth)):
    allow = bool(payload.get("allow_create_supplier", False))
    partner = ensure_partner(payload, allow_create=allow)
    return {"status": "ok", "partner": partner}

# 6) Validar facturas explícitamente (si lo pide el orquestador)
@app.post("/odoo/invoices/validate")
def invoices_validate(payload: Dict[str, Any], authorized: bool = Depends(auth)):
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(status_code=422, detail="Faltan ids")
    models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move", "action_post", [ids])
    return {"status": "ok", "validated": ids}

# (Opcional) endpoints de banca/conciliación pueden añadirse aquí si los necesitas.

# ──────────────────────────────────────────────────────────────────────────────
# Ejecutable local (deshabilitar reload en prod)
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("odoo_agent:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
