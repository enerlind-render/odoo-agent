# odoo_agent.py
# FastAPI bridge para Odoo (proveedores, facturas, banco y conciliación)

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict, Any
import xmlrpc.client
import base64
import hashlib
import os
from datetime import datetime, timedelta
import mimetypes
from secrets import compare_digest

# ──────────────────────────────────────────────────────────────────────────────
# Configuración / Seguridad
# ──────────────────────────────────────────────────────────────────────────────

ODOO_URL  = os.getenv("ODOO_URL", "").rstrip("/")
ODOO_DB   = os.getenv("ODOO_DB", "")
ODOO_USER = os.getenv("ODOO_USER", "")
ODOO_API  = os.getenv("ODOO_API_KEY", "")

API_TOKEN = os.getenv("API_TOKEN", "")  # token para Bearer desde ChatGPT/Agente

try:
    MAX_ATTACHMENT_MB = float(os.getenv("MAX_ATTACHMENT_MB", "15"))
except ValueError:
    MAX_ATTACHMENT_MB = 15.0

if not (ODOO_URL and ODOO_DB and ODOO_USER and ODOO_API):
    raise RuntimeError("Faltan variables ODOO_* en el entorno de Render.")

# Cliente XML-RPC
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API, {})
if not uid:
    raise RuntimeError("No se pudo autenticar con Odoo. Revisa ODOO_*.")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

app = FastAPI(title="Odoo ChatGPT Bridge", version="1.0.0")

# CORS opcional (si necesitas probar desde origenes distintos)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restringe si quieres
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def auth(authorization: Optional[str] = Header(None)):
    """Autenticación Bearer segura frente a API_TOKEN."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not API_TOKEN or not compare_digest(token, API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return True

# ──────────────────────────────────────────────────────────────────────────────
# Utilidades Odoo
# ──────────────────────────────────────────────────────────────────────────────

def find_currency_id(code_or_name: Optional[str]) -> Optional[int]:
    if not code_or_name:
        return None
    s = str(code_or_name).strip().upper()
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "res.currency", "search",
        [[("active", "=", True), "|", ("name", "=", s), ("symbol", "=", s)]],
        {"limit": 1}
    )
    return ids[0] if ids else None

def get_company_id() -> int:
    """Devuelve la compañía del usuario autenticado."""
    user = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.users", "read", [[uid], ["company_id"]])[0]
    return user["company_id"][0]

# (A) — Bloqueo de tu propia empresa / dominios al buscar proveedor
def get_company_partner_id() -> int:
    """Partner principal de tu compañía (nunca debe usarse como proveedor)."""
    company_id = get_company_id()
    comp = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.company", "read", [[company_id], ["partner_id"]])[0]
    return comp["partner_id"][0]

def find_partner(q: Optional[str], vat: Optional[str], limit: int = 10) -> List[Dict[str, Any]]:
    """
    Busca proveedores priorizando VAT exacto y el más usado.
    EXCLUYE la propia compañía (y sus emails/nombre).
    """
    company_partner_id = get_company_partner_id()

    # Bloqueos configurables (Render → Environment)
    SELF_COMPANY_KEYWORDS = (os.getenv("SELF_COMPANY_KEYWORDS", "") or "").lower().split(",")
    SELF_EMAIL_DOMAINS = [d.strip().lower() for d in (os.getenv("SELF_EMAIL_DOMAINS", "") or "").split(",") if d.strip()]

    # Dominio base: proveedores activos / NO la compañía propia
    domain_base = [
        ("supplier_rank", ">", 0),
        ("active", "=", True),
        ("commercial_partner_id", "!=", company_partner_id),
        ("id", "!=", company_partner_id),
    ]
    # Excluir por nombre
    for kw in SELF_COMPANY_KEYWORDS:
        if kw:
            domain_base.append(("name", "not ilike", kw))
    # Excluir por email de dominio
    for dom in SELF_EMAIL_DOMAINS:
        domain_base.append(("email", "not ilike", f"@{dom}"))

    # Búsqueda por prioridad
    if vat:
        found = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "res.partner", "search_read",
            [[("vat", "=", vat)] + domain_base[1:]],
            {"fields": ["id", "name", "vat", "email"], "limit": limit}
        )
    elif q:
        exact = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "res.partner", "search_read",
            [[("name", "=", q)] + domain_base[1:]],
            {"fields": ["id", "name", "vat", "email"], "limit": limit}
        )
        if exact:
            found = exact
        else:
            found = models.execute_kw(
                ODOO_DB, uid, ODOO_API, "res.partner", "search_read",
                [[("name", "ilike", q)] + domain_base[1:]],
                {"fields": ["id", "name", "vat", "email"], "limit": limit}
            )
    else:
        found = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "res.partner", "search_read",
            [domain_base], {"fields": ["id", "name", "vat", "email"], "limit": limit}
        )

    # Desempate: el más usado (más facturas proveedor)
    for f in found:
        cnt = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "account.move", "search_count",
            [[("move_type", "in", ["in_invoice", "in_refund"]), ("partner_id", "=", f["id"])]]
        )
        f["usage_count"] = cnt
    found.sort(key=lambda x: (-x.get("usage_count", 0), x.get("name", "")))
    return found

def ensure_partner(data: Dict[str, Any]) -> Dict[str, Any]:
    """Busca o crea un proveedor evitando duplicados (VAT > email > nombre).
    Actualiza email/phone/vat si faltan en el existente."""
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

    # 1) VAT exacto
    if vat:
        vat_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "res.partner", "search",
            [[("vat", "=", vat)] + domain_base],
            {"limit": 1}
        )
        if vat_ids:
            partner = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "read", [vat_ids, ["id", "name", "vat", "email", "phone"]])[0]
            updates = {}
            if email and not partner.get("email"):
                updates["email"] = email
            if phone and not partner.get("phone"):
                updates["phone"] = phone
            if updates:
                models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "write", [[partner["id"]], updates])
                partner.update(updates)
            return partner

    # 2) Email ilike
    if email:
        email_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "res.partner", "search",
            [[("email", "ilike", email)] + domain_base],
            {"limit": 1}
        )
        if email_ids:
            partner = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "read", [email_ids, ["id", "name", "vat", "email", "phone"]])[0]
            updates = {}
            if vat and not partner.get("vat"):
                updates["vat"] = vat
            if phone and not partner.get("phone"):
                updates["phone"] = phone
            if updates:
                models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "write", [[partner["id"]], updates])
                partner.update(updates)
            return partner

    # 3) Nombre exacto
    name_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "res.partner", "search",
        [[("name", "=", name)] + domain_base],
        {"limit": 1}
    )
    if name_ids:
        partner = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "read", [name_ids, ["id", "name", "vat", "email", "phone"]])[0]
        updates = {}
        if vat and not partner.get("vat"):
            updates["vat"] = vat
        if email and not partner.get("email"):
            updates["email"] = email
        if phone and not partner.get("phone"):
            updates["phone"] = phone
        if updates:
            models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "write", [[partner["id"]], updates])
            partner.update(updates)
        return partner

    # 4) Nombre ilike (último recurso)
    ilike_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "res.partner", "search",
        [[("name", "ilike", name)] + domain_base],
        {"limit": 1}
    )
    if ilike_ids:
        partner = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "read", [ilike_ids, ["id", "name", "vat", "email", "phone"]])[0]
        updates = {}
        if vat and not partner.get("vat"):
            updates["vat"] = vat
        if email and not partner.get("email"):
            updates["email"] = email
        if phone and not partner.get("phone"):
            updates["phone"] = phone
        if updates:
            models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "write", [[partner["id"]], updates])
            partner.update(updates)
        return partner

    # 5) Crear proveedor
    vals = {
        "name": name,
        "supplier_rank": 1,
        "type": "contact",
    }
    if vat:
        vals["vat"] = vat
    if email:
        vals["email"] = email
    if phone:
        vals["phone"] = phone

    partner_id = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "create", [vals])
    partner = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.partner", "read", [[partner_id], ["id", "name", "vat", "email", "phone"]])[0]
    return partner

def find_account_by_code(code: str) -> Optional[int]:
    """Busca la cuenta contable por código dentro de la compañía actual."""
    if not code:
        return None
    company_id = get_company_id()
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "account.account", "search",
        [[("code", "=", code), ("company_id", "=", company_id)]], {"limit": 1}
    )
    return ids[0] if ids else None

def find_account_by_reference(code_or_id: Optional[Any]) -> Optional[int]:
    """Busca la cuenta por ID, 'id:XX', código exacto o nombre exacto."""
    if not code_or_id:
        return None
    company_id = get_company_id()

    if isinstance(code_or_id, int):
        ids = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "account.account", "search",
            [[("id", "=", code_or_id), ("company_id", "=", company_id)]],
            {"limit": 1}
        )
        return ids[0] if ids else None

    s = str(code_or_id).strip()
    if not s:
        return None

    if s.lower().startswith("id:"):
        try:
            acc_id = int(s.split(":", 1)[1])
        except ValueError:
            return None
        ids = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "account.account", "search",
            [[("id", "=", acc_id), ("company_id", "=", company_id)]],
            {"limit": 1}
        )
        return ids[0] if ids else None

    # código exacto
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "account.account", "search",
        [[("code", "=", s), ("company_id", "=", company_id)]],
        {"limit": 1}
    )
    if ids:
        return ids[0]

    # nombre exacto
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "account.account", "search",
        [[("name", "=", s), ("company_id", "=", company_id)]],
        {"limit": 1}
    )
    return ids[0] if ids else None

def _normalize_list_of_strings(value: Optional[Any]) -> List[str]:
    """Normaliza entradas separadas por comas o listas a una lista de strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).split(",")
    return [str(x).strip() for x in items if str(x).strip()]

def find_taxes_by_names(codes: Optional[Any]) -> List[int]:
    """Busca impuestos por ID, código interno (description) o nombre."""
    names = _normalize_list_of_strings(codes)
    if not names:
        return []
    company_id = get_company_id()
    result: List[int] = []

    for raw in names:
        s = str(raw).strip()
        if not s:
            continue

        # Permitir "id:15"
        if s.lower().startswith("id:"):
            try:
                tax_id = int(s.split(":", 1)[1])
            except ValueError:
                tax_id = None
            if tax_id:
                exists = models.execute_kw(
                    ODOO_DB, uid, ODOO_API, "account.tax", "search",
                    [[("id", "=", tax_id), ("company_id", "=", company_id)]],
                    {"limit": 1}
                )
                if exists:
                    result.append(exists[0])
            continue

        # 1) Por código interno exacto (description)
        code_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "account.tax", "search",
            [[("type_tax_use", "!=", "none"), ("company_id", "=", company_id), ("description", "=", s)]],
            {"limit": 1}
        )
        if code_ids:
            result.append(code_ids[0])
            continue

        # 2) Por nombre exacto
        exact_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "account.tax", "search",
            [[("name", "=", s), ("company_id", "=", company_id)]],
            {"limit": 1}
        )
        if exact_ids:
            result.append(exact_ids[0])
            continue

        # 3) Fuzzy por nombre (último recurso)
        fuzzy_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "account.tax", "search",
            [[("name", "ilike", s), ("company_id", "=", company_id)]],
            {"limit": 1}
        )
        if fuzzy_ids:
            result.append(fuzzy_ids[0])

    return result

def bill_idempotency_key(partner_id: int, ref: str, total: float, inv_date: str) -> str:
    base = f"{partner_id}|{ref}|{total}|{inv_date}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

def get_invoice_payable_line_ids(move_id: int) -> List[int]:
    """Devuelve líneas 'payable' no conciliadas de una factura."""
    return models.execute_kw(
        ODOO_DB, uid, ODOO_API, "account.move.line", "search",
        [[("move_id", "=", move_id), ("reconciled", "=", False), ("account_internal_type", "=", "payable")]]
    )

# --- Helpers de adjuntos/PDF ---
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

def _create_invoice_attachment(move_id: int, filename: str, data: bytes, mimetype: Optional[str] = None) -> int:
    clean = _sanitize_filename(filename)
    mt = mimetype or _detect_mimetype(clean)
    datas = _ensure_binary_payload(data)
    att_id = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "ir.attachment", "create", [[{
            "name": clean,
            "datas_fname": clean,
            "res_model": "account.move",
            "res_id": move_id,
            "type": "binary",
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
        pass  # no romper por chatter

def _handle_inline_attachment(move_id: int, att_b64: Optional[str], att_name: Optional[str]) -> Optional[int]:
    if not att_b64:
        return None
    payload = att_b64.strip()
    if "base64," in payload:
        payload = payload.split("base64,", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="attachment_b64 inválido")
    att_id = _create_invoice_attachment(move_id, att_name or "factura.pdf", raw, "application/pdf")
    _attach_comment(move_id, f"Adjunto (JSON) añadido: {_sanitize_filename(att_name or 'factura.pdf')}")
    return att_id

# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"message": "Hello from Odoo Agent!"}

@app.get("/odoo/ping")
def ping(authorized: bool = Depends(auth)):
    # Información mínima de conexión
    return {"ok": True, "db": ODOO_DB, "user": ODOO_USER}

# — Proveedores ————————————————————————————————————————————————

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
    partner = ensure_partner(payload)
    return {"status": "ok", "partner": partner}

# — Facturas proveedor pendientes / parciales ————————————————

@app.get("/odoo/invoices/unreconciled")
def invoices_unreconciled(
    authorized: bool = Depends(auth),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    company_id: Optional[int] = None,
    limit: int = 200
):
    domain: List[Any] = [
        ("move_type", "in", ["in_invoice", "in_refund"]),
        ("state", "in", ["posted", "draft"]),
        "|", ("amount_residual", ">", 0.01), ("payment_state", "!=", "paid")
    ]
    if date_from:
        domain.append(("invoice_date", ">=", date_from))
    if date_to:
        domain.append(("invoice_date", "<=", date_to))
    if company_id:
        domain.append(("company_id", "=", company_id))

    fields = [
        "id", "name", "invoice_date", "partner_id", "amount_total",
        "amount_residual", "currency_id", "payment_state", "state"
    ]
    rows = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "account.move", "search_read",
        [domain], {"fields": fields, "limit": limit, "order": "invoice_date desc, id desc"}
    )
    items = [{
        "id": r["id"],
        "name": r.get("name"),
        "invoice_date": r.get("invoice_date"),
        "partner": r.get("partner_id")[1] if r.get("partner_id") else None,
        "amount_total": r.get("amount_total"),
        "amount_residual": r.get("amount_residual"),
        "currency": r.get("currency_id")[1] if r.get("currency_id") else "EUR",
        "payment_state": r.get("payment_state"),
        "state": r.get("state"),
    } for r in rows]
    return {"count": len(items), "items": items}

# — Adjuntar archivo a factura ————————————————————————————————

@app.post("/odoo/invoices/attach")
async def invoices_attach(
    authorized: bool = Depends(auth),
    move_id: int = Form(...),
    file: UploadFile = File(...)
):
    data = await file.read()
    att_id = _create_invoice_attachment(move_id, file.filename or "adjunto", data, file.content_type or None)
    _attach_comment(move_id, f"Adjunto subido: {_sanitize_filename(file.filename)}")
    return {"status": "ok", "move_id": move_id, "attachment_id": att_id}

# — Crear factura proveedor (JSON) ————————————————————————————————
# (B) Añadido: soporta creación/ensure de partner, idempotencia y adjunto inline
@app.post("/odoo/invoices/create")
def invoices_create_json(
    payload: Dict[str, Any],
    authorized: bool = Depends(auth)
):
    """
    payload:
    {
      partner_id?  o  partner: {name, vat?, email?, phone?},
      invoice_date, ref, currency?, journal_name?, idempotency_key?,
      lines: [{name, price_unit, quantity?, account_id?/account_code?/account?, tax_codes?/tax_ids?}],
      attachment_b64?, attachment_name?
    }
    """
    partner_id = payload.get("partner_id")
    partner_data = payload.get("partner")
    if not partner_id:
        if partner_data:
            partner = ensure_partner(partner_data)
            partner_id = partner["id"]
        else:
            raise HTTPException(status_code=422, detail="Debe indicar partner_id o datos de partner")
    partner_id = int(partner_id)

    invoice_date = payload["invoice_date"]
    ref = payload["ref"]
    lines = payload.get("lines", [])
    currency = payload.get("currency")
    journal_name = payload.get("journal_name")
    idem = payload.get("idempotency_key")

    # Verificar si ya existe una factura ACTIVA con mismo partner + ref
    existing_active = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "account.move", "search",
        [[
            ("move_type", "=", "in_invoice"),
            ("partner_id", "=", partner_id),
            ("ref", "=", ref),
            ("state", "!=", "cancel")
        ]],
        {"limit": 1}
    )
    if existing_active:
        return {
            "status": "duplicate",
            "reason": "existing_active_invoice",
            "message": f"Ya existe una factura activa con ref='{ref}' para este proveedor.",
            "existing_move_id": existing_active[0]
        }

    # Idempotencia (hash) → se guarda en invoice_origin (no en ref)
    if not idem and lines:
        total = sum(float(l.get("price_unit", 0) or 0) for l in lines)
        idem = bill_idempotency_key(partner_id, ref, total, invoice_date)

    found_by_idem = []
    if idem:
        found_by_idem = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "account.move", "search",
            [[("move_type", "=", "in_invoice"), ("invoice_origin", "=", idem)]],
            {"limit": 1}
        )

    reused_existing = False
    if found_by_idem:
        move_id = found_by_idem[0]
        reused_existing = True
    else:
        # Journal (opcional)
        journal_id = None
        if journal_name:
            jids = models.execute_kw(
                ODOO_DB, uid, ODOO_API, "account.journal", "search",
                [[("name", "=", journal_name), ("type", "in", ["purchase", "general"])]],
                {"limit": 1}
            )
            journal_id = jids[0] if jids else None

        # Moneda (opcional)
        currency_id = find_currency_id(currency) if currency else None

        # Crear líneas contables
        aml_lines = []
        for l in lines:
            account_id = None
            if "account_id" in l and l.get("account_id") is not None:
                account_id = find_account_by_reference(l.get("account_id"))
            if not account_id:
                account_id = find_account_by_reference(l.get("account_code") or l.get("account"))

            taxes = find_taxes_by_names(l.get("tax_codes") or l.get("tax_ids"))

            line_vals = {
                "name": l.get("name", "Línea"),
                "price_unit": float(l.get("price_unit", 0) or 0),
                "quantity": float(l.get("quantity", 1) or 1),
            }
            if account_id:
                line_vals["account_id"] = account_id
            if taxes:
                line_vals["tax_ids"] = [(6, 0, taxes)]
            aml_lines.append((0, 0, line_vals))

        move_vals = {
            "move_type": "in_invoice",
            "partner_id": partner_id,
            "invoice_date": invoice_date,
            "ref": ref,                 # referencia del proveedor intacta
            "invoice_origin": idem,     # idempotencia
            "invoice_line_ids": aml_lines or [],
        }
        if journal_id:
            move_vals["journal_id"] = journal_id
        if currency_id:
            move_vals["currency_id"] = currency_id

        move_id = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move", "create", [move_vals])
        _handle_inline_attachment(move_id, payload.get("attachment_b64"), payload.get("attachment_name"))

    return {"status": "existing" if reused_existing else "created", "move_id": move_id}

# (C) — Crear factura + adjuntar archivo (multipart “todo en uno”)
@app.post("/odoo/invoices/create_with_file")
async def invoices_create_with_file(
    authorized: bool = Depends(auth),
    partner_id: Optional[int] = Form(None),
    partner_name: Optional[str] = Form(None),
    partner_vat: Optional[str] = Form(None),
    partner_email: Optional[str] = Form(None),
    partner_phone: Optional[str] = Form(None),
    invoice_date: str = Form(...),
    ref: str = Form(...),
    currency: Optional[str] = Form(None),
    journal_name: Optional[str] = Form(None),
    auto_validate: bool = Form(False),
    file: UploadFile = File(...)
):
    # 0) Resolver/crear partner
    if not partner_id:
        if partner_name or partner_vat or partner_email:
            partner = ensure_partner({
                "name": partner_name or "",
                "vat": partner_vat,
                "email": partner_email,
                "phone": partner_phone,
            })
            partner_id = partner["id"]
        else:
            raise HTTPException(status_code=422, detail="Falta partner_id o datos de partner (partner_name/vat/email).")

    # 1) Crear factura con una línea placeholder
    payload = {
        "partner_id": partner_id,
        "invoice_date": invoice_date,
        "ref": ref,
        "currency": currency,
        "journal_name": journal_name,
        "lines": [{"name": file.filename or "Gasto según adjunto", "price_unit": 0.0}],
    }
    res = invoices_create_json(payload, authorized)
    move_id = res.get("move_id")

    # 2) Adjuntar archivo
    data = await file.read()
    att_id = _create_invoice_attachment(move_id, file.filename or "adjunto", data, file.content_type or None)
    _attach_comment(move_id, f"Adjunto subido al crear la factura: {_sanitize_filename(file.filename)}")

    # 3) Validar opcionalmente
    if auto_validate:
        models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move", "action_post", [[move_id]])

    return {"status": "created", "move_id": move_id, "attachment_id": att_id, "validated": bool(auto_validate)}

# — Crear factura desde imagen (borrador + adjunto) ———————————————————

@app.post("/odoo/invoices/create_from_image")
async def invoices_create_from_image(
    authorized: bool = Depends(auth),
    file: UploadFile = File(...),
    supplier_hint: Optional[str] = Form(None),
    vat_hint: Optional[str] = Form(None),
    ref_hint: Optional[str] = Form(None),
    auto_validate: bool = Form(False)
):
    # 1) Buscar/crear proveedor
    partners = find_partner(q=supplier_hint, vat=vat_hint, limit=1)
    if partners:
        partner_id = partners[0]["id"]
    else:
        if supplier_hint or vat_hint:
            partner = ensure_partner({"name": supplier_hint or (vat_hint or "Proveedor sin nombre"), "vat": vat_hint})
            partner_id = partner["id"]
        else:
            raise HTTPException(status_code=422, detail="No se encontró proveedor y faltan datos para crearlo (supplier_hint/vat_hint).")

    # 2) Crear factura mínima en borrador
    today = datetime.utcnow().date().isoformat()
    move_vals = {
        "move_type": "in_invoice",
        "partner_id": partner_id,
        "invoice_date": today,
        "ref": ref_hint or f"IMG-{today}",
        "invoice_line_ids": [(0, 0, {"name": "Gasto según adjunto", "price_unit": 0.0})],
    }
    move_id = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move", "create", [move_vals])

    # 3) Adjuntar archivo
    data = await file.read()
    att_id = _create_invoice_attachment(move_id, file.filename or "adjunto", data, file.content_type or None)
    _attach_comment(move_id, f"Factura creada desde imagen. Adjunto: {_sanitize_filename(file.filename)}")

    # 4) Validar si procede
    if auto_validate:
        models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move", "action_post", [[move_id]])

    return {"status": "created", "move_id": move_id, "attachment_id": att_id, "validated": bool(auto_validate)}

# — Validar facturas (action_post) ————————————————————————————————

@app.post("/odoo/invoices/validate")
def invoices_validate(payload: Dict[str, Any], authorized: bool = Depends(auth)):
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(status_code=422, detail="Faltan ids")
    models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move", "action_post", [ids])
    return {"status": "ok", "validated": ids}

# — Movimientos de banco sin conciliar ————————————————————————————

@app.get("/odoo/bank/unreconciled")
def bank_unreconciled(
    authorized: bool = Depends(auth),
    journal_name: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 200
):
    # 1) Obtener diarios de banco
    j_domain = [("type", "=", "bank")]
    if journal_name:
        j_domain = [("type", "=", "bank"), ("name", "ilike", journal_name)]
    bank_journal_ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.journal", "search", [j_domain])
    if not bank_journal_ids:
        return {"count": 0, "items": []}

    # 2) Líneas no conciliadas en esos diarios
    domain = [("journal_id", "in", bank_journal_ids), ("reconciled", "=", False)]
    if date_from:
        domain.append(("date", ">=", date_from))
    if date_to:
        domain.append(("date", "<=", date_to))

    fields = ["id", "name", "date", "balance", "debit", "credit", "journal_id", "partner_id", "ref"]
    rows = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "account.move.line", "search_read",
        [domain], {"fields": fields, "limit": limit, "order": "date desc, id desc"}
    )
    items = [{
        "id": r["id"],
        "name": r.get("name"),
        "date": r.get("date"),
        "amount": abs(r.get("balance") or 0.0),
        "journal": r.get("journal_id")[1] if r.get("journal_id") else None,
        "partner": r.get("partner_id")[1] if r.get("partner_id") else None,
        "ref": r.get("ref"),
    } for r in rows]
    return {"count": len(items), "items": items}

# — Sugerir conciliación (match por importe ±0.50€ y fecha ±5 días) ————————

@app.get("/odoo/reconciliation/suggest")
def reconciliation_suggest(
    authorized: bool = Depends(auth),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    journal_name: Optional[str] = None
):
    # Obtener líneas bancarias no conciliadas
    bank = bank_unreconciled(authorized, journal_name, date_from, date_to, limit=200)
    bank_items = bank["items"]

    # Obtener facturas con residual > 0
    inv = invoices_unreconciled(authorized, date_from, date_to, None, 500)
    inv_items = inv["items"]

    suggestions = []
    for bl in bank_items:
        b_amount = float(bl["amount"] or 0.0)
        b_date = datetime.strptime(bl["date"], "%Y-%m-%d").date() if bl.get("date") else None
        for mv in inv_items:
            resid = float(mv["amount_residual"] or 0.0)
            i_date = datetime.strptime(mv["invoice_date"], "%Y-%m-%d").date() if mv.get("invoice_date") else None

            # tolerancias
            if abs(b_amount - resid) <= 0.5:
                days_ok = True
                if b_date and i_date:
                    days_ok = abs((b_date - i_date).days) <= 5
                if days_ok:
                    score = 0.9
                    # bonus por partner parecido
                    if bl.get("partner") and mv.get("partner") and bl["partner"].lower()[:5] == mv["partner"].lower()[:5]:
                        score = 0.98
                    suggestions.append({
                        "bank_line_id": bl["id"],
                        "move_id": mv["id"],
                        "amount": resid,
                        "bank_date": bl.get("date"),
                        "invoice_date": mv.get("invoice_date"),
                        "invoice": mv.get("name"),
                        "partner": mv.get("partner"),
                        "score": score
                    })
    # ordenar por score
    suggestions.sort(key=lambda x: -x["score"])
    return {"count": len(suggestions), "items": suggestions}

# — Aplicar conciliación ————————————————————————————————————————

@app.post("/odoo/reconciliation/apply")
def reconciliation_apply(payload: Dict[str, Any], authorized: bool = Depends(auth)):
    """
    payload: { matches: [{bank_line_id, move_id}, ...] }
    """
    matches = payload.get("matches") or []
    applied = []
    skipped = []

    for m in matches:
        bl_id = int(m["bank_line_id"])
        mv_id = int(m["move_id"])
        # línea bancaria + línea(s) payable de la factura
        inv_lines = get_invoice_payable_line_ids(mv_id)
        if not inv_lines:
            skipped.append({"bank_line_id": bl_id, "move_id": mv_id, "reason": "No payable lines"})
            continue

        # Si la línea bancaria ya está conciliada, saltar
        bl = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move.line", "read", [[bl_id], ["reconciled"]])[0]
        if bl.get("reconciled"):
            skipped.append({"bank_line_id": bl_id, "move_id": mv_id, "reason": "Already reconciled"})
            continue

        line_ids = [bl_id] + inv_lines
        try:
            models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move.line", "reconcile", [line_ids])
            applied.append({"bank_line_id": bl_id, "move_id": mv_id})
        except Exception as e:
            skipped.append({"bank_line_id": bl_id, "move_id": mv_id, "reason": str(e)})

    return {"applied": applied, "skipped": skipped}

# — Job simple para auto-conciliar con umbral —————————————————————

@app.post("/tasks/auto_reconcile")
def task_auto_reconcile(authorized: bool = Depends(auth), min_score: float = 0.95):
    sug = reconciliation_suggest(authorized)
    high = [m for m in sug["items"] if m["score"] >= min_score]
    res = reconciliation_apply({"matches": [{"bank_line_id": m["bank_line_id"], "move_id": m["move_id"]} for m in high]}, authorized)
    return {"suggested": len(sug["items"]), "applied": len(res["applied"]), "skipped": len(res["skipped"])}
