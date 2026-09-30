# SOC ITSM Platform

A multi-container SOC ticketing and incident response management platform featuring custom RBAC, automated alert ingestion via n8n, Wazuh SIEM integration, and SafeLine WAF defense.

## 🚀 System Overview
- **Frontend**: React application for incident ticketing & management interface.
- **Backend**: FastAPI core providing REST APIs, authentication, and ticket workflows.
- **Database**: PostgreSQL with persistent storage.
- **Caching & Queue**: Redis for task management.
- **Security & Network**: SafeLine WAF / Nginx reverse proxy with LDAP directory authentication.
- **Automation**: n8n workflows processing incoming security alerts (Wazuh, Telegram, APIs).

## 🛠️ Quickstart

```bash
# Copy environment template
cp .env.example .env

# Launch container stack
docker compose up -d --build
## 📚 Documentation
- [Architecture & Topology](docs/ARCHITECTURE.md)
- [API & Webhook Integrations](docs/API_INTEGRATION.md)
- [RBAC & Authentication Matrix](docs/RBAC_MATRIX.md)
- [Setup & Deployment Guide](SETUP.md)
- [Security Policy](SECURITY.md)
