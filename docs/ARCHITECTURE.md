# System Architecture & Network Topology

## Component Flow
1. **Traffic Entry**: External web traffic passes through SafeLine WAF / Nginx reverse proxy.
2. **Authentication**: Users authenticate via LDAP / Local database through FastAPI backend endpoints.
3. **Alert Pipeline**:
   - Wazuh SIEM triggers an alert payload.
   - n8n receives the webhook and parses incoming JSON data.
   - n8n triggers the backend API (`/api/tickets`) to automatically create an incident ticket.
   - Notifications dispatch via Telegram Bot API / email.
