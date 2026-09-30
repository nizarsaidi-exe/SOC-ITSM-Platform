# Role-Based Access Control (RBAC) Matrix

| Role | Ticket View | Ticket Edit | Assign Ticket | System Settings |
| :--- | :---: | :---: | :---: | :---: |
| **Analyst (L1)** | ✅ | ✅ | ❌ | ❌ |
| **Analyst (L2/L3)** | ✅ | ✅ | ✅ | ❌ |
| **SOC Manager / Admin** | ✅ | ✅ | ✅ | ✅ |

## Identity and Authentication Workflow

- **Directory service**: SOC-DC-01 runs Windows Server 2025 Core (No GUI) with
  Active Directory Domain Services (AD DS) and DNS.
- **Encrypted authentication**: FastAPI Core authenticates directory identities
  over LDAPS on port 636. The Windows Server's Wazuh Agent is installed locally;
  there is no separate Windows client VM.
- **Centralized RBAC mapping**: SafeLine WAF protects the application ingress to
  FastAPI Core. FastAPI maps authenticated directory identities to the platform
  roles in this matrix: L1 Analysts, L2/L3 Engineers, and SOC Managers/Admins.
