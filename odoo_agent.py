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

# ──────────────────────────────────────────────────────────────────────────────
# Configuración / Seguridad
# ──────────────────────────────────────────────────────────────────────────────

ODOO_URL  = os.getenv("ODOO_URL", "").rstrip("/")
ODOO_DB   = os.getenv("ODOO_DB", "")
ODOO_USER = os.getenv("ODOO_USER", "")
ODOO_API  = os.getenv("ODOO_API_KEY", "")

API_TOKEN = os.getenv("API_TOKEN", "")  # token para Bearer desde ChatGPT/Agente

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
    """Autenticación Bearer sencilla frente a API_TOKEN."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=403, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid API Token")
    return True

# ──────────────────────────────────────────────────────────────────────────────
# Utilidades Odoo
# ──────────────────────────────────────────────────────────────────────────────

def find_currency_id(code_or_name: Optional[str]) -> Optional[int]:
    if not code_or_name:
        return None
    # Buscar por name o por código
    domain = ["|", ("name", "=", code_or_name), ("symbol", "=", code_or_name)]
    ids = models.execute_kw(ODOO_DB, uid, ODOO_API, "res.currency", "search", [domain], {"limit": 1})
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

def find_taxes_by_names(codes: Optional[str]) -> List[int]:
    """Recibe 'IVA21,RE7' y devuelve IDs de taxes que contengan esos nombres/códigos."""
    if not codes:
        return []
    result = []
    company_id = get_company_id()
    for name in [x.strip() for x in codes.split(",") if x.strip()]:
        ids = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "account.tax", "search",
            [[("name", "ilike", name), ("company_id", "=", company_id)]], {"limit": 1}
        )
        if ids:
            result.append(ids[0])
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
    b64 = base64.b64encode(data).decode()
    att_id = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "ir.attachment", "create", [{
            "name": file.filename,
            "res_model": "account.move",
            "res_id": move_id,
            "type": "binary",
            "datas": b64,
            "mimetype": file.content_type or "application/octet-stream",
        }]
    )
    # Comentario en el chatter
    models.execute_kw(ODOO_DB, uid, ODOO_API, "mail.message", "create", [{
        "model": "account.move",
        "res_id": move_id,
        "body": f"Adjunto subido: {file.filename}",
        "message_type": "comment",
        "subtype_id": 1,
    }])
    return {"status": "ok", "move_id": move_id, "attachment_id": att_id}

# — Crear factura proveedor (JSON) ————————————————————————————————
# (B) Añadido: soporta adjunto inline con attachment_b64/attachment_name
@app.post("/odoo/invoices/create")
def invoices_create_json(
    payload: Dict[str, Any],
    authorized: bool = Depends(auth)
):
    """
    payload:
    {
      partner_id, invoice_date, ref, currency?, journal_name?, idempotency_key?,
      lines: [{name, price_unit, account_code?, tax_codes?}],
      attachment_b64?, attachment_name?
    }
    """
    partner_id = int(payload["partner_id"])
    invoice_date = payload["invoice_date"]
    ref = payload["ref"]
    lines = payload.get("lines", [])
    currency = payload.get("currency")
    journal_name = payload.get("journal_name")
    idem = payload.get("idempotency_key")

    # Anti-duplicados por partner + ref
    existing = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "account.move", "search",
        [[("move_type", "=", "in_invoice"), ("partner_id", "=", partner_id), ("ref", "=", ref)]],
        {"limit": 1}
    )
    if existing:
        move_id = existing[0]
    else:
        # Idempotency extra (si viene)
        if not idem and lines:
            total = sum(float(l.get("price_unit", 0) or 0) for l in lines)
            idem = bill_idempotency_key(partner_id, ref, total, invoice_date)
        found_by_idem = models.execute_kw(
            ODOO_DB, uid, ODOO_API, "account.move", "search",
            [[("move_type", "=", "in_invoice"), ("ref", "=", idem)]], {"limit": 1}
        )
        if found_by_idem:
            move_id = found_by_idem[0]
        else:
            # Journal (opcional) y currency (opcional)
            journal_id = None
            if journal_name:
                jids = models.execute_kw(
                    ODOO_DB, uid, ODOO_API, "account.journal", "search",
                    [[("name", "=", journal_name), ("type", "in", ["purchase", "general"])]], {"limit": 1}
                )
                journal_id = jids[0] if jids else None

            currency_id = find_currency_id(currency) if currency else None

            # Líneas
            aml_lines = []
            for l in lines:
                account_id = find_account_by_code(l.get("account_code") or "")
                taxes = find_taxes_by_names(l.get("tax_codes"))
                line_vals = {
                    "name": l.get("name", "Línea"),
                    "price_unit": float(l.get("price_unit", 0) or 0),
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
                "ref": idem or ref,  # guardamos idem si existe
                "invoice_line_ids": aml_lines or [],
            }
            if journal_id:
                move_vals["journal_id"] = journal_id
            if currency_id:
                move_vals["currency_id"] = currency_id

            move_id = models.execute_kw(ODOO_DB, uid, ODOO_API, "account.move", "create", [move_vals])

    # --- Opcional: adjuntar si viene attachment_b64 ---
    att_b64 = payload.get("attachment_b64")
    att_name = payload.get("attachment_name", "factura.pdf")
    if att_b64:
        try:
            att_id = models.execute_kw(
                ODOO_DB, uid, ODOO_API, "ir.attachment", "create", [{
                    "name": att_name,
                    "res_model": "account.move",
                    "res_id": move_id,
                    "type": "binary",
                    "datas": att_b64,
                    "mimetype": "application/pdf",
                }]
            )
            models.execute_kw(ODOO_DB, uid, ODOO_API, "mail.message", "create", [{
                "model": "account.move",
                "res_id": move_id,
                "body": f"Adjunto (JSON) añadido: {att_name}",
                "message_type": "comment",
                "subtype_id": 1,
            }])
        except Exception:
            pass

    status = "exists" if existing else "created"
    return {"status": status, "move_id": move_id}

# (C) — Crear factura + adjuntar archivo (multipart “todo en uno”)
@app.post("/odoo/invoices/create_with_file")
async def invoices_create_with_file(
    authorized: bool = Depends(auth),
    partner_id: int = Form(...),
    invoice_date: str = Form(...),
    ref: str = Form(...),
    currency: Optional[str] = Form(None),
    journal_name: Optional[str] = Form(None),
    auto_validate: bool = Form(False),
    file: UploadFile = File(...)
):
    # 1) Crear factura con una línea placeholder (mantiene tu lógica actual)
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
    b64 = base64.b64encode(data).decode()
    att_id = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "ir.attachment", "create", [{
            "name": file.filename,
            "res_model": "account.move",
            "res_id": move_id,
            "type": "binary",
            "datas": b64,
            "mimetype": file.content_type or "application/octet-stream",
        }]
    )
    models.execute_kw(ODOO_DB, uid, ODOO_API, "mail.message", "create", [{
        "model": "account.move",
        "res_id": move_id,
        "body": f"Adjunto subido al crear la factura: {file.filename}",
        "message_type": "comment",
        "subtype_id": 1,
    }])

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
    # 1) Buscar proveedor
    partners = find_partner(q=supplier_hint, vat=vat_hint, limit=1)
    if not partners:
        raise HTTPException(status_code=422, detail="No se encontró proveedor con los hints dados.")
    partner_id = partners[0]["id"]

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
    b64 = base64.b64encode(data).decode()
    att_id = models.execute_kw(
        ODOO_DB, uid, ODOO_API, "ir.attachment", "create", [{
            "name": file.filename,
            "res_model": "account.move",
            "res_id": move_id,
            "type": "binary",
            "datas": b64,
            "mimetype": file.content_type or "application/octet-stream",
        }]
    )
    models.execute_kw(ODOO_DB, uid, ODOO_API, "mail.message", "create", [{
        "model": "account.move",
        "res_id": move_id,
        "body": f"Factura creada desde imagen. Adjunto: {file.filename}",
        "message_type": "comment",
        "subtype_id": 1,
    }])

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
