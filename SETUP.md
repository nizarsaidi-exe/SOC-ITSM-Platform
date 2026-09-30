# Setup

## First-time setup
1. Copy the templates: cp .env.example .env and cp backend/.env.example backend/.env
2. Fill every value. Generate each secret with: openssl rand -hex 32
   (JWT_SECRET, DB_PASS, POSTGRES_PASSWORD, REDIS_PASSWORD, WEBHOOK_SECRET,
   ONBOARD_API_KEY, SERVICE_TOKEN_KEY, N8N_ENCRYPTION_KEY)
3. Put your directory's CA certificate at backend/certs/ad-ca.pem
4. Create the n8n proxy login (both files are gitignored):

       pw=$(openssl rand -hex 32)
       printf 'username=n8n\npassword=%s\n' "$pw" > frontend/.n8n.credentials
       printf 'n8n:%s\n' "$(openssl passwd -apr1 "$pw")" > frontend/.n8n.htpasswd
       chmod 600 frontend/.n8n.credentials
       chmod 644 frontend/.n8n.htpasswd

5. Lock the env files: chmod 600 .env backend/.env
6. Start everything: docker compose up -d --build

## Owner first login
The owner account is created at startup from SEED_OWNER_EMAIL with no password.

1. Generate a single-use setup token with the service key:

       curl -X POST http://localhost:3000/api/auth/generate-token \
         -H "X-Service-Key: YOUR_SERVICE_TOKEN_KEY" \
         -H "Content-Type: application/json" \
         -d '{"username":"owner@example.com"}'

2. Send that token to /api/auth/setup-password with the email and a new
   password (12+ characters, mixed case, digit and symbol).

## Rotating secrets
- Change the value in .env, then run: docker compose up -d
- Rotating JWT_SECRET logs everyone out.
- For DB_PASS, also run ALTER USER inside Postgres.
- Never commit .env.

## Notes
- Postgres, Redis and the API are not published to the host.
- Test users must not use @test.com or @onboard.com (purged on startup).
