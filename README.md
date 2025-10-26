# Odoo Invoice Tools (mínimo)

Backend FastAPI para Acción de ChatGPT:
- Analiza factura (PDF/JPG/PNG) con OCR si hace falta
- Busca/crea proveedor (con confirmación)
- Asigna cuenta/impuestos
- Crea factura de proveedor y adjunta archivo en Odoo

## Variables (Render o .env)
- ODOO_URL, ODOO_DB, ODOO_USER, **ODOO_PASSWORD** (API Key de Odoo o la contraseña)
- COMPANY_VAT, DEFAULT_COMPANY_ID (opcional)
- **API_KEY** (la que pondrás en la Acción como `X-API-Key`)

## Ejecutar local
pip install -r requirements.txt
uvicorn main:app --reload

## Salud
curl -s http://localhost:8000/health

## Despliegue Render (Dockerfile)
- Crea Web Service desde el repo
- Añade env vars (no subas .env)
- Health check: /health

## Acción (ChatGPT)
- Importa openapi_action.yaml
- Auth: API Key → Header `X-API-Key` con el valor de `API_KEY`
