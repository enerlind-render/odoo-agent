from __future__ import annotations
import os, time, base64, re, io, logging
from typing import Any, List, Optional
import requests
from fastapi import FastAPI, Depends, Header, HTTPException, status
from pydantic import BaseModel
from pypdf import PdfReader
from pdf2image import convert_from_bytes
from PIL import Image
import pytesseract

# ---------- SEGURIDAD ----------
def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    api_token_hdr: Optional[str] = Header(default=None, alias="API_TOKEN"),
):
    provided = x_api_key or api_token_hdr
    if not provided:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key header missing")
    expected = os.getenv("API_KEY") or os.getenv("API_TOKEN")
    if not expected or provided != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key")

# ---------- CLIENTE ODOO (JSON-RPC) ----------
class OdooClient:
    def __init__(self):
        self.url = (os.getenv("ODOO_URL") or "").rstrip("/")
        self.db = os.getenv("ODOO_DB") or ""
        self.user = os.getenv("ODOO_USER") or ""
        # Acepta ODOO_PASSWORD o ODOO_API_KEY como credencial
        self.password = os.getenv("ODOO_PASSWORD") or os.getenv("ODOO_API_KEY") or ""
        self.uid: Optional[int] = None

    def _jsonrpc(self, service: str, method: str, *args, **kwargs) -> Any:
        if not self.url:
            raise RuntimeError("Falta ODOO_URL")
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args, "kwargs": kwargs or {}},
            "id": int(time.time() * 1000),
        }
        r = requests.post(self.url + "/jsonrpc", json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(str(data["error"]))
        return data.get("result")

    def authenticate(self) -> int:
        if not self.db or not self.user or not self.password:
            raise RuntimeError("Faltan ODOO_DB/ODOO_USER/ODOO_PASSWORD")
        self.uid = self._jsonrpc("common", "authenticate", self.db, self.user, self.password, {})
        if not self.uid:
            raise RuntimeError("Odoo authentication failed")
        return self.uid

    def execute_kw(self, model: str, method: str, args: list, kwargs: dict | None = None):
        if self.uid is None:
            self.authenticate()
        return self._jsonrpc("object", "execute_kw", self.db, self.uid, self.password, model, method, args, kwargs or {})

    # Helpers
    def search(self, model: str, domain: list, limit: int = 80, order: str | None = None):
        kw = {"limit": limit}
        if order:
            kw["order"] = order
        return self.execute_kw(model, "search", [domain], kw)

    def read(self, model: str, ids: list[int], fields: list[str]):
        return self.execute_kw(model, "read", [ids, fields], {})

    def search_read(self, model: str, domain: list, fields: list[str], limit: int = 80, order: str | None = None):
        kw = {"fields": fields, "limit": limit}
        if order:
            kw["order"] = order
        return self.execute_kw(model, "search_read", [domain], kw)

    def create(self, model: str, vals: dict):
        return self.execute_kw(model, "create", [vals], {})

    def write(self, model: str, ids: list[int], vals: dict):
        return self.execute_kw(model, "write", [ids, vals], {})

    def read_group(self, model: str, domain: list, fields: list[str], groupby: list[str],
                   orderby: str | None = None, limit: int | None = None):
        kw = {}
        if orderby:
            kw["orderby"] = orderby
        if limit:
            kw["limit"] = limit
        # args posicionales correctos: [domain, fields, groupby]
        return self.execute_kw(model, "read_group", [domain, fields, groupby], kw)

# ---------- OCR / PARSER ----------
def _extract_text_pdf(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return ""
    out = []
    for p in reader.pages:
        try:
            t = p.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            out.append(t)
    return "\n".join(out)

def _ocr_pdf(pdf_bytes: bytes) -> str:
    images = convert_from_bytes(pdf_bytes, dpi=200, fmt="png")
    parts = [pytesseract.image_to_string(img, lang="spa+eng") for img in images]
    return "\n".join(parts)

def _ocr_image(img_bytes: bytes) -> str:
    img = Image.open(io.BytesIO(img_bytes))
    return pytesseract.image_to_string(img, lang="spa+eng")

def parse_invoice_content(filename: str, file_b64: str) -> dict:
    raw = base64.b64decode(file_b64)
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    text = ""
    if ext == "pdf":
        text = _extract_text_pdf(raw)
        if len(text.strip()) < 20:
            text = _ocr_pdf(raw)
    else:
        text = _ocr_image(raw)

    def _num(s: str):
        s = s.replace(".", "").replace(",", ".")
        m = re.findall(r"-?\d+(?:\.\d+)?", s)
        return float(m[0]) if m else None

    ref = None
    for pat in [
        r"Factura\s*(?:N[ºo]|No|#)\s*([A-Za-z0-9\-\/\.]+)",
        r"N[ºo]\s*Factura\s*([A-Za-z0-9\-\/\.]+)",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            ref = m.group(1).strip()
            break

    tot = None
    m = re.search(r"Total(?:\s*Factura)?\s*[:\-]?\s*([0-9\.\,]+)", text, re.IGNORECASE)
    if m:
        tot = _num(m.group(1))
    base_imp = None
    m = re.search(r"Base(?:\s*Imponible)?\s*[:\-]?\s*([0-9\.\,]+)", text, re.IGNORECASE)
    if m:
        base_imp = _num(m.group(1))
    iva = None
    m = re.search(r"(?:IVA|Impuesto)\s*(?:\d+%|\(.*?\))?\s*[:\-]?\s*([0-9\.\,]+)", text, re.IGNORECASE)
    if m:
        iva = _num(m.group(1))

    # vendor hint
    vendor_hint = None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines[:25]):
        if re.search(r"\b(CIF|NIF|VAT)\b", ln, re.IGNORECASE):
            for back in range(1, 3):
                if i - back >= 0 and len(lines[i - back]) > 3:
                    vendor_hint = lines[i - back]
                    break
            break

    desc = ""
    for ln in lines:
        if re.search(r"(concepto|descripción|servicio|detalle)", ln, re.IGNORECASE):
            idx = lines.index(ln)
            desc = " ".join(lines[idx: idx + 5])[:240]
            break
    if not desc:
        desc = " ".join(lines[:8])[:240]

    conf = 0.9 if tot and (base_imp or iva) else 0.65 if ref or vendor_hint else 0.5

    return {
        "text_excerpt": text[:8000],
        "vendor_hint": vendor_hint,
        "reference": ref,
        "base": base_imp,
        "tax": iva,
        "total": tot,
        "description": desc,
        "ocr_conf": round(conf, 2),
    }

# ---------- FASTAPI ----------
app = FastAPI(title="Odoo Invoice Tools")

@app.get("/health")
def health():
    return {"status": "ok"}

# Endpoint de diagnóstico para credenciales de Odoo
@app.get("/debug/odoo_auth")
def debug_odoo_auth():
    try:
        uid = OdooClient().authenticate()
        return {"ok": True, "uid": uid}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---- MODELOS (request) ----
class ParseReq(BaseModel):
    filename: str
    file_b64: str

class PartnerExistReq(BaseModel):
    vat: Optional[str] = None
    name: Optional[str] = None
    country_code: Optional[str] = None

class SupplierUsageReq(BaseModel):
    partner_ids: List[int]

class CreateSupplierReq(BaseModel):
    name: str
    vat: Optional[str] = None
    company_type: Optional[str] = "company"
    email: Optional[str] = None
    phone: Optional[str] = None
    street: Optional[str] = None
    city: Optional[str] = None
    zip: Optional[str] = None
    country_code: Optional[str] = None
    notes: Optional[str] = None

class ResolveAccountReq(BaseModel):
    account_code: str
    company_id: Optional[int] = None

class ResolveTaxesReq(BaseModel):
    names: Optional[List[str]] = None
    codes: Optional[List[str]] = None
    company_id: Optional[int] = None

class CheckDupReq(BaseModel):
    partner_id: int
    ref: str
    total: float
    tolerance: float = 0.02

class CreateBillLine(BaseModel):
    name: str
    quantity: float = 1
    price_unit: float
    account_code: str
    product_name: Optional[str] = "Sin producto"
    tax_names: Optional[List[str]] = None
    tax_codes: Optional[List[str]] = None

class CreateBillReq(BaseModel):
    partner_id: int
    invoice_date: str  # YYYY-MM-DD
    ref: Optional[str] = None
    company_id: Optional[int] = None
    line: CreateBillLine

class AttachReq(BaseModel):
    move_id: int
    filename: str
    file_b64: str

# ---------- ENDPOINTS / TOOLS ----------
@app.post("/tools/parse_invoice")
def t_parse(body: ParseReq, _=Depends(require_api_key)):
    return parse_invoice_content(body.filename, body.file_b64)

@app.post("/tools/check_partner_existence")
def t_check_partner(body: PartnerExistReq, _=Depends(require_api_key)):
    my_vat = (os.getenv("COMPANY_VAT") or "").upper().replace(" ", "")
    odoo = OdooClient(); odoo.authenticate()
    cand = []

    # VAT exacto
    if body.vat:
        ids = odoo.search("res.partner", [["vat", "=", body.vat]], limit=10)
        if ids:
            recs = odoo.read("res.partner", ids, ["name", "vat", "supplier_rank"])
            for r in recs:
                v = (r.get("vat") or "").upper().replace(" ", "")
                if v == my_vat:
                    continue
                cand.append({"id": r["id"], "name": r["name"], "vat": r.get("vat"), "score": 1.0})

    # Nombre aproximado
    if body.name:
        domain = [["supplier_rank", ">", 0], ["active", "=", True], ["name", "ilike", body.name]]
        recs = odoo.search_read("res.partner", domain, ["name", "vat"], limit=50)
        from difflib import SequenceMatcher
        for r in recs:
            v = (r.get("vat") or "").upper().replace(" ", "")
            if v == my_vat:
                continue
            sc = SequenceMatcher(None, body.name.lower(), (r["name"] or "").lower()).ratio()
            cand.append({"id": r["id"], "name": r["name"], "vat": r.get("vat"), "score": round(sc, 3)})

    # Ranking por uso
    if cand:
        ids = [c["id"] for c in cand]
        rg = odoo.read_group(
            "account.move",
            [["move_type", "in", ["in_invoice", "in_refund"]], ["partner_id", "in", ids]],
            ["id:count"],
            ["partner_id"],
            orderby="id_count desc",
        )
        usage = {
            (it["partner_id"][0] if isinstance(it.get("partner_id"), (list, tuple)) else None): it.get("id_count", 0)
            for it in rg
        }
        for c in cand:
            c["usage"] = usage.get(c["id"], 0)

    cand = sorted(cand, key=lambda x: (x.get("score", 0), x.get("usage", 0)), reverse=True)
    return {"candidates": cand}

@app.post("/tools/supplier_usage_rank")
def t_usage(body: SupplierUsageReq, _=Depends(require_api_key)):
    odoo = OdooClient(); odoo.authenticate()
    rg = odoo.read_group(
        "account.move",
        [["move_type", "in", ["in_invoice", "in_refund"]], ["partner_id", "in", body.partner_ids]],
        ["id:count"],
        ["partner_id"],
        orderby="id_count desc",
    )
    data = []
    for it in rg:
        pid = it["partner_id"][0] if isinstance(it.get("partner_id"), (list, tuple)) else None
        data.append({"partner_id": pid, "invoice_count": it.get("id_count", 0)})
    return {"usage": data}

@app.post("/tools/create_supplier_partner")
def t_create_partner(body: CreateSupplierReq, _=Depends(require_api_key)):
    odoo = OdooClient(); odoo.authenticate()
    vals = {
        "name": body.name,
        "supplier_rank": 1,
        "company_type": body.company_type or "company",
    }
    for k in ["vat", "email", "phone", "street", "city", "zip"]:
        v = getattr(body, k, None)
        if v:
            vals[k] = v
    if body.country_code:
        cid = odoo.search("res.country", [["code", "=", body.country_code]], limit=1)
        if cid:
            vals["country_id"] = cid[0]
    pid = odoo.create("res.partner", vals)
    return {"partner_id": pid}

@app.post("/tools/resolve_account")
def t_resolve_account(body: ResolveAccountReq, _=Depends(require_api_key)):
    odoo = OdooClient(); odoo.authenticate()
    dom = [["code", "=", body.account_code]]
    if body.company_id:
        dom.append(["company_id", "=", body.company_id])
    ids = odoo.search("account.account", dom, limit=1)
    return {"account_id": ids[0]} if ids else {"account_id": None}

@app.post("/tools/resolve_taxes")
def t_resolve_taxes(body: ResolveTaxesReq, _=Depends(require_api_key)):
    odoo = OdooClient(); odoo.authenticate()
    tax_ids = []
    for code in (body.codes or []):
        dom = [["type_tax_use", "in", ["purchase", "none"]], ["description", "=", code]]
        if body.company_id:
            dom.append(["company_id", "=", body.company_id])
        ids = odoo.search("account.tax", dom, limit=1)
        if ids:
            tax_ids += ids
    for name in (body.names or []):
        dom = [["type_tax_use", "in", ["purchase", "none"]], ["name", "ilike", name]]
        if body.company_id:
            dom.append(["company_id", "=", body.company_id])
        ids = odoo.search("account.tax", dom, limit=1)
        if ids:
            tax_ids += ids
    tax_ids = list(dict.fromkeys(tax_ids))
    return {"tax_ids": tax_ids}

@app.post("/tools/check_duplicate")
def t_check_dup(body: CheckDupReq, _=Depends(require_api_key)):
    odoo = OdooClient(); odoo.authenticate()
    dom = [["move_type", "=", "in_invoice"], ["partner_id", "=", body.partner_id], ["ref", "=", body.ref]]
    ids = odoo.search("account.move", dom, limit=1)
    if ids:
        return {"exists": True, "move_id": ids[0], "reason": "partner+ref"}
    near = odoo.search_read(
        "account.move",
        [["move_type", "=", "in_invoice"], ["partner_id", "=", body.partner_id]],
        ["id", "amount_total"],
        limit=50,
    )
    for r in near:
        if abs((r.get("amount_total") or 0.0) - body.total) <= body.tolerance * max(1.0, body.total):
            return {"exists": True, "move_id": r["id"], "reason": "amount_total ~"}
    return {"exists": False}

@app.post("/tools/create_vendor_bill")
def t_create_bill(body: CreateBillReq, _=Depends(require_api_key)):
    odoo = OdooClient(); odoo.authenticate()
    acc = odoo.search("account.account", [["code", "=", body.line.account_code]], limit=1)
    if not acc:
        raise HTTPException(400, detail=f"Cuenta {body.line.account_code} no existe")
    tax_ids = []
    for code in (body.line.tax_codes or []):
        dom = [["type_tax_use", "in", ["purchase", "none"]], ["description", "=", code]]
        if body.company_id:
            dom.append(["company_id", "=", body.company_id])
        ids = odoo.search("account.tax", dom, limit=1)
        tax_ids += ids
    for name in (body.line.tax_names or []):
        dom = [["type_tax_use", "in", ["purchase", "none"]], ["name", "ilike", name]]
        if body.company_id:
            dom.append(["company_id", "=", body.company_id])
        ids = odoo.search("account.tax", dom, limit=1)
        tax_ids += ids
    tax_ids = list(dict.fromkeys(tax_ids))
    # product "Sin producto"
    prod = None
    if body.line.product_name:
        dom = [["name", "=", body.line.product_name]]
        if body.company_id:
            dom.append(["company_id", "in", [body.company_id, False]])
        pids = odoo.search("product.product", dom, limit=1)
        if pids:
            prod = pids[0]
    line_vals = {
        "name": body.line.name,
        "quantity": body.line.quantity,
        "price_unit": body.line.price_unit,
        "account_id": acc[0],
    }
    if prod:
        line_vals["product_id"] = prod
    if tax_ids:
        line_vals["tax_ids"] = [(6, 0, tax_ids)]
    mv = {
        "move_type": "in_invoice",
        "partner_id": body.partner_id,
        "invoice_date": body.invoice_date,
        "invoice_line_ids": [(0, 0, line_vals)],
    }
    if body.company_id:
        mv["company_id"] = body.company_id
    if body.ref:
        mv["ref"] = body.ref
    move_id = odoo.create("account.move", mv)
    return {"move_id": move_id}

@app.post("/tools/attach_file")
def t_attach(body: AttachReq, _=Depends(require_api_key)):
    odoo = OdooClient(); odoo.authenticate()
    vals = {
        "name": body.filename,
        "res_model": "account.move",
        "res_id": body.move_id,
        "type": "binary",
        "datas": body.file_b64,
        "mimetype": "application/pdf" if body.filename.lower().endswith(".pdf") else None,
    }
    att_id = odoo.create("ir.attachment", vals)
    return {"attachment_id": att_id}

from fastapi import Response
from fastapi.responses import JSONResponse

@app.get("/", include_in_schema=False)
def root():
    return JSONResponse({
        "service": "Odoo Invoice Tools",
        "ok": True,
        "health": "/health",
        "debug_auth": "/debug/odoo_auth",
        "tools": ["/tools/parse_invoice", "/tools/check_partner_existence", "/tools/create_vendor_bill", "/tools/attach_file"]
    })

@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)


