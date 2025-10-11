from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import os

app = FastAPI(title="Odoo ChatGPT Bridge")

# Configura tus credenciales de Odoo
ODOO_URL = "https://energy-blind-sl1.odoo.com"
ODOO_DB = "energy-blind-sl1"
ODOO_USER = "guillermo.lopez@enerlind.com"
ODOO_API_KEY = "AQUÍ_TU_API_KEY"  # <- la copiarás desde Odoo

class VendorBill(BaseModel):
    partner_name: str
    amount: float
    invoice_date: str
    ref: str

@app.get("/")
def root():
    return {"status": "ok", "message": "Bridge conectado con Odoo"}

@app.post("/vendor_bills")
def create_vendor_bill(bill: VendorBill):
    url = f"{ODOO_URL}/jsonrpc"
    headers = {"Content-Type": "application/json"}
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [
                ODOO_DB,
                ODOO_USER,
                ODOO_API_KEY,
                "account.move",
                "create",
                [{
                    "move_type": "in_invoice",
                    "partner_id": bill.partner_name,
                    "invoice_date": bill.invoice_date,
                    "invoice_line_ids": [(0, 0, {
                        "name": bill.ref,
                        "quantity": 1,
                        "price_unit": bill.amount,
                        "account_id": 600000  # Ejemplo de cuenta contable
                    })]
                }]
            ],
        },
        "id": 1,
    }

    response = requests.post(url, json=payload, headers=headers)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Error al conectar con Odoo")

    return response.json()
