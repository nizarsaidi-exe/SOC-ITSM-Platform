# Webhook & API Integrations

## Ingestion Endpoints
- **POST `/api/tickets`**: Accepts incident creation payloads from n8n / SIEM pipelines.
- **GET `/api/users`**: Retrieves user profile lists and role assignments.
- **GET `/api/user/rbac/{email}`**: Resolves active permissions for user identities.

## Integrations
- **Wazuh**: Automated rule alert forwarding.
- **n8n**: Workflow orchestration and payload transformation.
- **SafeLine WAF**: Web application inspection and filtering layer.
## 🔄 Automated Ingestion Workflow (n8n)

![n8n Incident Response Workflow](../assets/n8n-workflow-screenshot.png)