# System Architecture & Network Topology

## Component Flow
1. **Traffic Entry**: External web traffic passes through SafeLine WAF / Nginx reverse proxy.
2. **Authentication**: SOC-DC-01 runs Windows Server 2025 Core with Active Directory Domain Services. FastAPI Core authenticates directory users over encrypted LDAPS on port 636 and maps identities to RBAC roles. SafeLine WAF protects the application ingress to FastAPI.
3. **Alert Pipeline**:
   - Wazuh SIEM triggers an alert payload.
   - n8n receives the webhook and parses incoming JSON data.
   - n8n triggers the backend API (`/api/tickets`) to automatically create an incident ticket.
   - The SOC SOAR Bot routes low/medium-confidence detections and Wazuh webhooks to `#soc-alerts`, high-confidence IP blocking actions to `#soc-blocks`, and sanitized workflow/API errors to `#soc-pipeline-errors`.
   - Notifications may also dispatch via Telegram Bot API / email.

## End-to-End SOAR Alert Lifecycle

```mermaid
sequenceDiagram
   autonumber
   participant Agent as Wazuh Agent (Windows Server 2025 Core)
   participant Wazuh as Wazuh SIEM
   participant n8n as n8n Orchestrator
   participant Slack as Slack SOC SOAR Bot
   participant SafeLine as SafeLine WAF
   participant FortiGate as FortiGate VM
   participant API as FastAPI Core
   participant AD as Active Directory (LDAPS :636)
   participant DB as PostgreSQL
   participant React as React ITSM

   Agent->>Wazuh: Send locally collected agent logs
   Wazuh->>n8n: HTTP POST alert webhook
   n8n->>n8n: Parse payload, enrich data, evaluate confidence and severity

   alt Low/medium confidence
      n8n->>Slack: Dispatch formatted alert to #soc-alerts
      n8n->>API: Create or update incident ticket
      API->>AD: Validate directory identity and map RBAC roles over LDAPS
      AD-->>API: Return identity and role information
      API->>DB: Persist and route incident record
      DB-->>API: Return ticket record
      API-->>React: Publish ticket update
   else High confidence / critical
      n8n->>SafeLine: Request WAF IP block
      SafeLine-->>n8n: Return block action result
      n8n->>FortiGate: Request perimeter IP block
      FortiGate-->>n8n: Return block action result
      n8n->>Slack: Dispatch block notice to #soc-blocks
      n8n->>API: Escalate and route incident ticket
      API->>AD: Validate directory identity and map RBAC roles over LDAPS
      AD-->>API: Return identity and role information
      API->>DB: Persist escalation and block-action references
      DB-->>API: Return updated ticket record
      API-->>React: Publish escalated ticket update
   end

   opt Execution error / exception at any workflow step
      n8n->>n8n: Capture sanitized execution error and stack context
      n8n->>Slack: Dispatch failure log to #soc-pipeline-errors
   end
```

## Deployment Systems

| System | OS / Platform | Hardware / License | Components |
| --- | --- | --- | --- |
| **SOC-Core-01** | **Ubuntu 22.04 LTS** | 2 vCPU, 4 GB RAM | Docker Engine, FastAPI Core, React Frontend, PostgreSQL, Redis, n8n. |
| **SOC-Wazuh-01** | **Ubuntu 22.04 LTS** | 2 vCPU, 4 GB RAM | Wazuh Manager 4.x, Indexer, Dashboard. |
| **SOC-SafeLine-01** | **Ubuntu 22.04 LTS** | 2 vCPU, 4 GB RAM | SafeLine WAF / Reverse Proxy. |
| **SOC-DC-01** | **Windows Server 2025 Core (No GUI)** | 2 vCPU, 4 GB RAM | Active Directory Domain Services (AD DS), LDAPS (Port 636), DNS, Wazuh Agent installed locally. |
| **Network & Perimeter** | **FortiGate VM & Centreon** | Running under evaluation/free lab licenses | Perimeter firewalling and monitoring. |

There is no separate Windows client VM; the Wazuh Agent runs directly on
SOC-DC-01.

![System Architecture Topology](../assets/architecture-topology.png)