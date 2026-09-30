<p align="center">
  <img src="assets/logo.svg" alt="SOC ITSM Platform Banner" width="100%" />
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker"></a>
  <a href="#"><img src="https://img.shields.io/badge/FastAPI-0.100+-009688?logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="#"><img src="https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black" alt="React"></a>
  <a href="#"><img src="https://img.shields.io/badge/Wazuh-4.x-0052CC?logo=wazuh&logoColor=white" alt="Wazuh"></a>
  <a href="SECURITY.md"><img src="https://img.shields.io/badge/Security-Report_Vulnerability-red.svg" alt="Security Vulnerability"></a>
</p>

> [!IMPORTANT]
> **Production Hardening**: Always generate unique secret keys in `.env` before public deployment. Refer to [SECURITY.md](SECURITY.md) for vulnerability reporting guidelines.

---

## 🛡️ Overview

An enterprise-ready **SOC Incident Ticketing & Automation Platform**. Designed to streamline Security Operations Center workflows by bridging real-time SIEM alerts (Wazuh), automated orchestration (n8n), Web Application Firewall defense (SafeLine WAF), and role-based incident tracking (FastAPI + React).

---

## 🔥 Key Capabilities

* **Automated Incident Ingestion**: Receives real-time security alerts from Wazuh SIEM & webhook triggers via n8n pipelines.
* **Role-Based Access Control (RBAC)**: Fine-grained permissions for L1 Analysts, L2/L3 Engineers, and SOC Managers mapped via LDAP / DB.
* **WAF Enforcement**: Fronted by SafeLine WAF / Nginx reverse proxy to filter malicious HTTP/HTTPS payloads.
* **Incident Response Ticketing**: Triage statuses, severity tagging, automated assignments, and escalation workflows.
* **Notification Pipelines**: Automated instant alerts dispatched directly to Telegram channels and security teams.

---

## 🏗️ How It Works

```text
  [ External Web Traffic ]
            │
            ▼
    ┌────────────────┐
    │ SafeLine WAF   │ (HTTP Filtering & Reverse Proxy)
    └───────┬────────┘
            │
   ┌────────┴──────────────────────────┐
   │                                   │
   ▼                                   ▼
┌───────────────┐             ┌─────────────────┐
│ React Frontend│             │  FastAPI Core   │
└───────────────┘             └────────┬────────┘
                                       │
            ┌──────────────────────────┼──────────────────────────┐
            ▼                          ▼                          ▼
   ┌─────────────────┐        ┌─────────────────┐        ┌────────────────┐
   │ PostgreSQL DB   │        │   Redis Cache   │        │ LDAP / Auth    │
   └─────────────────┘        └─────────────────┘        └────────────────┘
            ▲
            │
   ┌────────┴────────┐
   │  n8n Workflows  │ ◄─── (Webhook Ingestion from Wazuh / Telegram)
   └─────────────────┘
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed component breakdowns.

---

## 🛠️ Quickstart

### 1️⃣ Clone the Repository

Clone the project from GitHub:

```bash
git clone https://github.com/nizarsaidi-exe/SOC-ITSM-Platform.git
```

Then enter the project directory:

```bash
cd SOC-ITSM-Platform
```

### 2️⃣ Configure Environment Variables

Create your local .env file from the example template:

```bash
cp .env.example .env
```

Note: Update the values in .env before deploying the platform, especially secret keys and credentials.

### 3️⃣ Build and Launch Containers

Build the Docker images and start the complete platform:

```bash
docker compose up -d --build
```

### 4️⃣ Verify Container Status

Check that all containers are running:

```bash
docker compose ps
```

### 5️⃣ View Application Logs

View logs for the complete stack:

```bash
docker compose logs -f
```

To view logs for a specific service:

```bash
docker compose logs -f <service-name>
```

### 6️⃣ Stop the Platform

Stop the running containers:

```bash
docker compose down
```

### 7️⃣ Update the Platform

Pull the latest version of the repository:

```bash
git pull
```

Then rebuild and restart the containers:

```bash
docker compose up -d --build
```

## 📚 Technical Documentation

| Document                                                 | Description                                               |
| -------------------------------------------------------- | --------------------------------------------------------- |
| 📐 [Architecture Guide](docs/ARCHITECTURE.md)            | Network design, container communication, and data flow.   |
| 🔌 [API & Webhook Integrations](docs/API_INTEGRATION.md) | Endpoint specifications, n8n trigger hooks, and payloads. |
| 🔐 [RBAC & Permissions Matrix](docs/RBAC_MATRIX.md)      | User roles, LDAP integration, and access controls.        |
| ⚙️ [Deployment & Setup Guide](SETUP.md)                  | Installation, volume persistence, and database backups.   |
| 🛡️ [Security Policy](SECURITY.md)                       | Vulnerability disclosure policy and security response.    |

## 🌐 Community & Support

### 🔐 Security Vulnerabilities

Follow the guidelines in SECURITY.md.

Important: Do not open public GitHub issues for zero-day or sensitive security vulnerabilities.

### 🐛 Bug Reports & Features

Open a GitHub Issue using the available issue templates.

### 📖 Documentation

Detailed technical guides are available inside the docs/ directory.

## ⚖️ Notice & Disclaimer

This software is provided for security monitoring, defensive operations, and educational lab environments.

The author assumes no liability for misconfigurations, unauthorized network operations, or service disruptions caused by deployment in unhardened environments.

Ensure all credentials, secret keys, certificates, and other sensitive configuration values are updated prior to production use.

## 📄 License

This project is licensed under the MIT License.