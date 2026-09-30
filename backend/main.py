import os
import json
import time
import uuid
import ssl
import hashlib
import hmac
import asyncio
import pathlib
import psycopg2
import bcrypt
import jwt
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Header, Depends, UploadFile, File, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from threading import Lock
from typing import Optional, List

JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
OWNER_EMAIL = os.environ["SEED_OWNER_EMAIL"].strip().lower()
OWNER_FULL_NAME = os.getenv("SEED_OWNER_FULL_NAME", "System Administrator")
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "3"))
JWT_EXPIRY_MINUTES = JWT_EXPIRY_HOURS * 60
TOKEN_TTL_SECONDS = JWT_EXPIRY_HOURS * 3600
ONBOARD_API_KEY = os.environ["ONBOARD_API_KEY"]
AUTH_TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "1800"))
SERVICE_TOKEN_KEY = os.environ["SERVICE_TOKEN_KEY"]
LDAP_DOMAIN = os.environ["LDAP_DOMAIN"]
LDAP_SERVER = os.environ["LDAP_SERVER"]
LDAP_PORT = int(os.getenv("LDAP_PORT", "636"))
LDAP_AUTH_ENABLED = os.getenv("LDAP_AUTH_ENABLED", "true").lower() in ("1", "true", "yes", "on")
LDAP_CA_CERT_FILE = os.getenv("LDAP_CA_CERT_FILE", "/app/certs/ad-ca.pem")

security = HTTPBearer(auto_error=False)

app = FastAPI(title="Enterprise SOC & ITSM Platform API")

if LDAP_AUTH_ENABLED and not os.path.isfile(LDAP_CA_CERT_FILE):
    raise RuntimeError("LDAP CA certificate file is missing; configure LDAP_CA_CERT_FILE")

CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "900"))
RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv("RATE_LIMIT_MAX_ATTEMPTS", "5"))
RATE_LIMIT_LOCK = Lock()
RATE_LIMIT_BUCKETS = {}

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Avatar / media storage (persisted via ./uploads volume)
# ---------------------------------------------------------------------------
MEDIA_DIR = "/app/uploads"
AVATAR_DIR = os.path.join(MEDIA_DIR, "avatars")
os.makedirs(AVATAR_DIR, exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

DB_HOST = os.getenv("DB_HOST", "jira-db")
DB_NAME = os.getenv("DB_NAME", "soc_jira")
DB_USER = os.getenv("DB_USER", "soc_jira")
DB_PASS = os.environ["DB_PASS"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        cursor_factory=RealDictCursor
    )

# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.chat_rooms: dict = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for d in dead:
            self.disconnect(d)

    async def chat_join(self, ticket_id: int, websocket: WebSocket):
        room = self.chat_rooms.setdefault(int(ticket_id), {"sockets": {}})
        queue = room["sockets"].setdefault(websocket, asyncio.Queue(maxsize=500))
        return queue

    def chat_leave(self, ticket_id: int, websocket: WebSocket):
        room = self.chat_rooms.get(int(ticket_id))
        if room:
            room["sockets"].pop(websocket, None)
            if not room["sockets"]:
                self.chat_rooms.pop(int(ticket_id), None)

    @staticmethod
    def jsonable(obj):
        """Recursively convert non-JSON-serializable values (datetime, etc.)."""
        if isinstance(obj, dict):
            return {k: ConnectionManager.jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [ConnectionManager.jsonable(v) for v in obj]
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return obj

    async def chat_publish(self, ticket_id: int, message: dict):
        room = self.chat_rooms.get(int(ticket_id))
        if not room:
            return
        msg = ConnectionManager.jsonable(message)
        for queue in list(room["sockets"].values()):
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                try:
                    await asyncio.wait_for(queue.get(), timeout=2)
                    queue.put_nowait(msg)
                except Exception:
                    pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        token = message.get("token") if isinstance(message, dict) else None
        if not token:
            await websocket.close(code=4401)
            return
        payload = decode_jwt(token)
        get_full_user_from_payload(payload)
    except Exception:
        await websocket.close(code=4401)
        return
    manager.active_connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)

# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------
class TicketUpdate(BaseModel):
    status: Optional[str] = None
    assigned_user: Optional[str] = None
    assigned_user_id: Optional[int] = None
    user_avatar: Optional[str] = None
    current_activity: Optional[str] = None
    active_team: Optional[List[str]] = None
    severity: Optional[str] = None

class TicketCreate(BaseModel):
    title: str
    description: str
    category: Optional[str] = "INCIDENT_MGMT"
    severity: Optional[str] = "MEDIUM"
    assigned_user: Optional[str] = None
    user_avatar: Optional[str] = None
    active_team: Optional[List[str]] = []
    current_activity: Optional[str] = "Triage"
    source: Optional[str] = "manual"

class WebhookAlert(BaseModel):
    title: Optional[str] = None
    message: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[str] = "HIGH"
    source: Optional[str] = "external"
    rule: Optional[str] = None
    src_ip: Optional[str] = None

class UserProfile(BaseModel):
    name: str
    email: str
    role: Optional[str] = ""
    dept: Optional[str] = ""
    avatar: Optional[str] = ""

class RBACUserCreate(BaseModel):
    email: str
    full_name: str
    password: Optional[str] = ""
    role_id: Optional[int] = None
    department_id: Optional[int] = None
    avatar: Optional[str] = None

class RBACUserUpdate(BaseModel):
    full_name: Optional[str] = None
    role_id: Optional[int] = None
    department_id: Optional[int] = None
    is_active: Optional[bool] = None
    avatar: Optional[str] = None

# ---------------------------------------------------------------------------
# Analyst pool / simple load-balanced assignment
# ---------------------------------------------------------------------------
ANALYST_POOL = [
    {"name": "Analyst One", "avatar": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=100"},
    {"name": "Analyst Two", "avatar": "https://images.unsplash.com/photo-1517841905240-472988babdf9?w=100"},
    {"name": "Analyst Three", "avatar": "https://images.unsplash.com/photo-1500648767791-00dcc994a43e?w=100"},
]

def pick_available_analyst(cur):
    cur.execute(
        "SELECT assigned_user, COUNT(*) as open_count FROM tickets "
        "WHERE status != 'MITIGATED' AND assigned_user IS NOT NULL "
        "GROUP BY assigned_user;"
    )
    load = {row['assigned_user']: row['open_count'] for row in cur.fetchall()}
    return min(ANALYST_POOL, key=lambda a: load.get(a['name'], 0))


def enrich_ticket(conn, ticket):
    """Attach reporter identity (name/email/department) to a ticket row."""
    if not ticket:
        return ticket
    cur = conn.cursor()
    cur.execute(
        "SELECT u.full_name AS reporter_name, u.email AS reporter_email, "
        "       d.name AS reporter_department_name, d.code AS reporter_department_code "
        "FROM users u "
        "LEFT JOIN departments d ON d.id = u.department_id "
        "WHERE u.id = %s;",
        (ticket.get('owner_id'),),
    )
    info = cur.fetchone()
    cur.close()
    out = dict(ticket)
    if info:
        out['reporter_name'] = info['reporter_name']
        out['reporter_email'] = info['reporter_email']
        out['reporter_department_name'] = info['reporter_department_name']
        out['reporter_department_code'] = info['reporter_department_code']
    else:
        out.update({
            'reporter_name': None,
            'reporter_email': None,
            'reporter_department_name': None,
            'reporter_department_code': None,
        })
    return out

# ---------------------------------------------------------------------------
# Startup: run RBAC migration + user_profiles table
# ---------------------------------------------------------------------------
RBAC_MIGRATION_SQL = """

-- Enum type (safe)
DO $$ BEGIN
    CREATE TYPE permission_module AS ENUM (
        'dashboard', 'tickets', 'incidents', 'requests',
        'changes', 'assets', 'configuration', 'knowledge',
        'users', 'roles', 'reports', 'system'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Departments
CREATE TABLE IF NOT EXISTS departments (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    code VARCHAR(20) NOT NULL UNIQUE,
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO departments (name, code, description) VALUES
    ('SOC', 'SOC', 'Security Operations Center'),
    ('NOC', 'NOC', 'Network Operations Center'),
    ('IT Support', 'IT_SUPPORT', 'IT Service Desk & Support'),
    ('Asset Management', 'ASSET_MGMT', 'Hardware & Software Asset Management'),
    ('Change Management', 'CHANGE_MGMT', 'Change Advisory Board & Management')
ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name, description=EXCLUDED.description, updated_at=NOW();

-- Roles
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    rank INT NOT NULL DEFAULT 0,
    description TEXT,
    is_system_role BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO roles (name, rank, description, is_system_role) VALUES
    ('Owner / Super Admin', 100, 'Full platform access', TRUE),
    ('SOC Analyst', 70, 'Handles incident detection & triage', TRUE),
    ('NOC Analyst', 65, 'Monitors network operations', TRUE),
    ('IT Support Agent', 50, 'Manages service requests & tickets', TRUE),
    ('Viewer', 10, 'Read-only access to dashboards', TRUE)
ON CONFLICT (name) DO UPDATE SET rank=EXCLUDED.rank, description=EXCLUDED.description, updated_at=NOW();

-- Permissions
CREATE TABLE IF NOT EXISTS permissions (
    id SERIAL PRIMARY KEY,
    code VARCHAR(80) NOT NULL UNIQUE,
    description TEXT,
    module VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO permissions (code, description, module) VALUES
    ('dashboard.view', 'View the main dashboard & metrics', 'dashboard'),
    ('tickets.view', 'View tickets', 'tickets'),
    ('tickets.create', 'Create new tickets', 'tickets'),
    ('tickets.update', 'Update ticket status & details', 'tickets'),
    ('tickets.assign', 'Assign tickets to analysts', 'tickets'),
    ('tickets.delete', 'Delete / close tickets', 'tickets'),
    ('incidents.view', 'View incident management module', 'incidents'),
    ('incidents.manage', 'Full incident lifecycle management', 'incidents'),
    ('soc.access', 'Access SOC operations', 'incidents'),
    ('requests.view', 'View request management module', 'requests'),
    ('requests.manage', 'Manage service requests', 'requests'),
    ('changes.view', 'View change management module', 'changes'),
    ('changes.approve', 'Approve or reject change requests', 'changes'),
    ('assets.view', 'View asset management module', 'assets'),
    ('assets.manage', 'Manage asset inventory', 'assets'),
    ('configuration.view', 'View configuration management', 'configuration'),
    ('configuration.manage', 'Manage CI/CD & config items', 'configuration'),
    ('knowledge.view', 'View knowledge base', 'knowledge'),
    ('knowledge.create', 'Create knowledge base articles', 'knowledge'),
    ('knowledge.edit', 'Edit knowledge base articles', 'knowledge'),
    ('users.view', 'View user list', 'users'),
    ('users.create', 'Create new user accounts', 'users'),
    ('users.edit', 'Edit user profiles & assignments', 'users'),
    ('users.deactivate', 'Deactivate user accounts', 'users'),
    ('roles.view', 'View roles & permissions', 'roles'),
    ('roles.manage', 'Manage roles & permission assignments', 'roles'),
    ('reports.view', 'View reports & analytics', 'reports'),
    ('reports.export', 'Export reports', 'reports'),
    ('system.settings', 'Manage system-wide settings', 'system'),
    ('system.webhooks', 'Manage webhook integrations', 'system')
ON CONFLICT (code) DO NOTHING;

-- Role-Permission mapping
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INT NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p
WHERE r.name = 'Owner / Super Admin'
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p WHERE r.name = 'SOC Analyst'
  AND p.code IN ('dashboard.view','tickets.view','tickets.create','tickets.update','tickets.assign',
                 'incidents.view','incidents.manage','soc.access','knowledge.view','knowledge.create','reports.view')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p WHERE r.name = 'NOC Analyst'
  AND p.code IN ('dashboard.view','tickets.view','tickets.create','tickets.update',
                 'requests.view','requests.manage','knowledge.view','reports.view')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p WHERE r.name = 'IT Support Agent'
  AND p.code IN ('dashboard.view','tickets.view','tickets.create','tickets.update',
                 'requests.view','requests.manage','assets.view','knowledge.view','reports.view')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p WHERE r.name = 'Viewer'
  AND p.code IN ('dashboard.view','tickets.view','reports.view')
ON CONFLICT DO NOTHING;

-- Users (RBAC)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash TEXT,
    full_name VARCHAR(255) NOT NULL,
    avatar TEXT,
    department_id INT REFERENCES departments(id) ON DELETE SET NULL,
    role_id INT REFERENCES roles(id) ON DELETE SET NULL,
    is_active BOOLEAN DEFAULT TRUE,
    is_owner BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Stage 9: Purge all artificial test accounts (zero-trust cleanup)
DELETE FROM users WHERE email IN (
    'soc.analyst@test.com', 'noc.analyst@test.com',
    'it.support@test.com', 'viewer@test.com'
);

-- Stage 12+: Comprehensive dummy-account purge handled by the dedicated
-- PURGE_SQL statement (avoids psycopg2 skipping statements in giant SQL strings).

-- Nullable FK columns on tickets (backward compatible)
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS owner_id INT REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS owner_department_id INT REFERENCES departments(id) ON DELETE SET NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS assigned_user_id INT REFERENCES users(id) ON DELETE SET NULL;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tickets_owner_id ON tickets(owner_id);
CREATE INDEX IF NOT EXISTS idx_tickets_owner_dept ON tickets(owner_department_id);
CREATE INDEX IF NOT EXISTS idx_tickets_assigned_uid ON tickets(assigned_user_id);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role_id);
CREATE INDEX IF NOT EXISTS idx_users_dept ON users(department_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- Stage 8: Per-user permission overrides (additive grants beyond role)
CREATE TABLE IF NOT EXISTS user_permission_overrides (
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission_id INT NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted BOOLEAN DEFAULT TRUE,
    granted_by INT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, permission_id)
);
CREATE INDEX IF NOT EXISTS idx_upo_user ON user_permission_overrides(user_id);

-- Stage 9: Revoked tokens table (for session invalidation / blacklisting)
CREATE TABLE IF NOT EXISTS revoked_tokens (
    id SERIAL PRIMARY KEY,
    jti VARCHAR(64) NOT NULL UNIQUE,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revoked_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rt_jti ON revoked_tokens(jti);
CREATE INDEX IF NOT EXISTS idx_rt_user ON revoked_tokens(user_id);

-- user_profiles (legacy)
CREATE TABLE IF NOT EXISTS user_profiles (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    avatar TEXT,
    role VARCHAR(255),
    dept VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
DO $$ BEGIN
    ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS avatar TEXT DEFAULT '';
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

"""

# Stage 12+: Purge all dummy / synthetic test accounts. Kept in its own
# statement because psycopg2 can silently skip statements buried inside the
# giant RBAC_MIGRATION_SQL string.
PURGE_SQL = """
DELETE FROM users WHERE is_owner = FALSE AND (
       email LIKE 'auto1787@%'
   OR email LIKE 'auto%@onboard.com'
   OR email LIKE 'soc_stage8%'
   OR email IN ('soc.analyst@test.com', 'noc.analyst@test.com', 'it.support@test.com', 'viewer@test.com')
   OR email LIKE '%@test.com'
   OR email LIKE '%@onboard.com'
   OR email LIKE 'newuser%');
"""

OWNER_SEED_SQL = """
INSERT INTO users (email, password_hash, full_name, role_id, is_active, is_owner)
SELECT %s, NULL, %s, r.id, TRUE, TRUE
FROM roles r WHERE r.name = 'Owner / Super Admin'
ON CONFLICT (email) DO UPDATE SET
    role_id = EXCLUDED.role_id,
    is_owner = TRUE,
    is_active = TRUE,
    updated_at = NOW();
"""

# Stage 12+/14: Seed realistic team accounts. password_hash stays NULL so each
# member completes first-login password setup (zero-trust).
TEAM_SEED_SQL = """
DO $$
DECLARE
    soc_dept   INT; noc_dept INT; it_dept INT;
    soc_role   INT; noc_role INT; it_role INT;
    team RECORD;
BEGIN
    SELECT id INTO soc_dept FROM departments WHERE code = 'SOC';
    SELECT id INTO noc_dept FROM departments WHERE code = 'NOC';
    SELECT id INTO it_dept  FROM departments WHERE code = 'IT_SUPPORT';
    SELECT id INTO soc_role FROM roles WHERE name = 'SOC Analyst';
    SELECT id INTO noc_role FROM roles WHERE name = 'NOC Analyst';
    SELECT id INTO it_role  FROM roles WHERE name = 'IT Support Agent';

    IF soc_dept IS NULL OR noc_dept IS NULL OR it_dept IS NULL
       OR soc_role IS NULL OR noc_role IS NULL OR it_role IS NULL THEN
        RAISE NOTICE 'Team seeding skipped: department or role lookup failed';
        RETURN;
    END IF;

    -- SOC Analyst team (8)
    FOR team IN SELECT * FROM (VALUES
        ('khadija.idrissi@soc.local', 'Khadija Idrissi'),
        ('yassine.amrani@soc.local',  'Yassine Amrani'),
        ('salma.bennis@soc.local',    'Salma Bennis'),
        ('omar.tazi@soc.local',       'Omar Tazi'),
        ('imane.rahmani@soc.local',   'Imane Rahmani'),
        ('amine.boukhari@soc.local',  'Amine Boukhari'),
        ('rania.bennani@soc.local',   'Rania Bennani'),
        ('hamza.ziani@soc.local',     'Hamza Ziani')
    ) AS t(email, full_name) LOOP
        INSERT INTO users (email, password_hash, full_name, role_id, department_id, is_active)
        VALUES (team.email, NULL, team.full_name, soc_role, soc_dept, TRUE)
        ON CONFLICT (email) DO UPDATE SET
            full_name = EXCLUDED.full_name,
            role_id = soc_role,
            department_id = soc_dept,
            is_active = TRUE,
            updated_at = NOW();
    END LOOP;

    -- IT Support team (1 existing + 3 new)
    FOR team IN SELECT * FROM (VALUES
        ('lina.sefrioui@soc.local',   'Lina Sefrioui'),
        ('houda.mansour@soc.local',   'Houda Mansour'),
        ('tariq.benjelloun@soc.local','Tariq Benjelloun'),
        ('soukaina.filali@soc.local', 'Soukaina Filali')
    ) AS t(email, full_name) LOOP
        INSERT INTO users (email, password_hash, full_name, role_id, department_id, is_active)
        VALUES (team.email, NULL, team.full_name, it_role, it_dept, TRUE)
        ON CONFLICT (email) DO UPDATE SET
            full_name = EXCLUDED.full_name,
            role_id = it_role,
            department_id = it_dept,
            is_active = TRUE,
            updated_at = NOW();
    END LOOP;

    -- NOC team (1 existing + 4 new)
    FOR team IN SELECT * FROM (VALUES
        ('karim.mansouri@soc.local',  'Karim Mansouri'),
        ('mehdi.chraibi@soc.local',   'Mehdi Chraibi'),
        ('sofia.alami@soc.local',     'Sofia Alami'),
        ('nabil.berrada@soc.local',   'Nabil Berrada'),
        ('othmane.naciri@soc.local',  'Othmane Naciri')
    ) AS t(email, full_name) LOOP
        INSERT INTO users (email, password_hash, full_name, role_id, department_id, is_active)
        VALUES (team.email, NULL, team.full_name, noc_role, noc_dept, TRUE)
        ON CONFLICT (email) DO UPDATE SET
            full_name = EXCLUDED.full_name,
            role_id = noc_role,
            department_id = noc_dept,
            is_active = TRUE,
            updated_at = NOW();
    END LOOP;
END $$;
"""

# ---------------------------------------------------------------------------
# Stage 15 (Principal): Self-service 'User' / 'Helpdesk User' roles
# ---------------------------------------------------------------------------
PRINCIPAL_ROLE_SQL = """
INSERT INTO roles (name, rank, description, is_system_role) VALUES
    ('User', 10, 'Standard self-service user (default for AD-synced accounts)', TRUE),
    ('Helpdesk User', 45, 'Self-service user with IT helpdesk access', TRUE)
ON CONFLICT (name) DO UPDATE SET rank=EXCLUDED.rank, description=EXCLUDED.description, updated_at=NOW();

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p WHERE r.name = 'Helpdesk User'
  AND p.code IN ('dashboard.view','tickets.view','tickets.create','tickets.update','tickets.assign',
                 'requests.view','requests.manage','assets.view','knowledge.view','knowledge.create','reports.view')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p WHERE r.name = 'User'
  AND p.code IN ('dashboard.view','tickets.view','tickets.create','requests.view')
ON CONFLICT DO NOTHING;
"""

# Stage 15: Passwordless single-use login tokens (30 min default, sha256 hashed)
AUTH_TOKENS_SQL = """
CREATE TABLE IF NOT EXISTS auth_tokens (
    id SERIAL PRIMARY KEY,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    username VARCHAR(255) NOT NULL,
    target_user_id INT REFERENCES users(id) ON DELETE CASCADE,
    created_by INT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_hash ON auth_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(target_user_id);
"""

# Stage 15: Real-time ticket chat messages (in-app conversation per ticket)
CHAT_MESSAGES_SQL = """
CREATE TABLE IF NOT EXISTS ticket_messages (
    id SERIAL PRIMARY KEY,
    ticket_id INT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    sender_id INT REFERENCES users(id) ON DELETE SET NULL,
    sender_name VARCHAR(255),
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tm_ticket ON ticket_messages(ticket_id);
CREATE INDEX IF NOT EXISTS idx_tm_created ON ticket_messages(created_at);
"""

@app.on_event("startup")
def run_startup_migrations():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Unassigned queue: add 'UNASSIGNED' to the ticket_status enum (PG12+ supports
        # ADD VALUE inside a transaction; never used in the same transaction).
        conn.autocommit = True
        cur.execute("ALTER TYPE ticket_status ADD VALUE IF NOT EXISTS 'UNASSIGNED';")
        conn.autocommit = False
        cur.execute(RBAC_MIGRATION_SQL)
        conn.commit()
        # Stage 12+: purge dummy accounts + seed realistic team accounts in
        # dedicated statements (psycopg2 may skip statements buried in giant SQL)
        cur.execute(PURGE_SQL)
        conn.commit()
        cur.execute(OWNER_SEED_SQL, (OWNER_EMAIL, OWNER_FULL_NAME))
        conn.commit()
        cur.execute(TEAM_SEED_SQL)
        conn.commit()
        # Stage 15: self-service roles + passwordless + ticket chat tables
        cur.execute(PRINCIPAL_ROLE_SQL)
        conn.commit()
        cur.execute(AUTH_TOKENS_SQL)
        conn.commit()
        cur.execute(CHAT_MESSAGES_SQL)
        conn.commit()
        cur.close()
        conn.close()
        print("[STARTUP] RBAC migration + team seeding complete — all tables ready")
    except Exception as e:
        print(f"[STARTUP] Migration error: {e}")
        raise

# ---------------------------------------------------------------------------
# Auth: helpers
# ---------------------------------------------------------------------------
class AuthLogin(BaseModel):
    email: str
    password: str

class AuthRegister(BaseModel):
    email: str
    password: str
    full_name: str
    role_id: Optional[int] = None
    department_id: Optional[int] = None

class AuthPasswordSetup(BaseModel):
    email: Optional[str] = None
    password: str
    token: Optional[str] = None

class TokenGenerateBody(BaseModel):
    username: str
    ttl: Optional[int] = None

class TokenLoginBody(BaseModel):
    token: str

class TempAccessBody(BaseModel):
    username: str
    password: str

class ChatPostBody(BaseModel):
    message: str

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def key_matches(provided: Optional[str], expected: Optional[str]) -> bool:
    if provided is None or expected is None:
        return False
    try:
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


def mask_email(value: Optional[str]) -> str:
    if not value:
        return "***"
    local, sep, domain = str(value).strip().lower().partition("@")
    if not sep:
        return f"{local[:2]}***" if len(local) > 2 else "**"
    return f"{local[:2]}***@{domain}"


def get_client_ip(request: Request, x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For")) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit_failure(scope: str, account: str, request: Request, x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For")) -> None:
    ip = get_client_ip(request, x_forwarded_for)
    key = f"{scope}:{account.lower()}:{ip}"
    now = time.time()
    with RATE_LIMIT_LOCK:
        bucket = RATE_LIMIT_BUCKETS.setdefault(key, [])
        bucket[:] = [ts for ts in bucket if now - ts < RATE_LIMIT_WINDOW_SECONDS]
        bucket.append(now)
        if len(bucket) >= RATE_LIMIT_MAX_ATTEMPTS:
            raise HTTPException(status_code=429, detail="Too many failed attempts; try again later")


def rate_limit_reset(scope: str, account: str, request: Request, x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For")) -> None:
    ip = get_client_ip(request, x_forwarded_for)
    key = f"{scope}:{account.lower()}:{ip}"
    with RATE_LIMIT_LOCK:
        RATE_LIMIT_BUCKETS.pop(key, None)


def ldap_authenticate(username: str, password: str) -> bool:
    """Bind against Active Directory with the user's own credentials over LDAPS.

    Self-signed certs are tolerated. The bind name is derived like an AD UPN:
    an input containing '@' is used verbatim, otherwise <username>@<LDAP_DOMAIN>.
    Returns False on any failure so callers can fall back to a local password.
    """
    if not username or not password:
        return False
    try:
        from ldap3 import Server, Tls, Connection
        bind_name = str(username).strip()
        if not bind_name:
            return False
        if "@" not in bind_name:
            bind_name = f"{bind_name}@{LDAP_DOMAIN}"
        if not os.path.isfile(LDAP_CA_CERT_FILE):
            raise RuntimeError("LDAP CA certificate file is missing")
        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=LDAP_CA_CERT_FILE)
        srv = Server(host=LDAP_SERVER, port=LDAP_PORT, use_ssl=True, tls=tls, connect_timeout=8)
        conn = Connection(srv, user=bind_name, password=password, auto_bind=True, receive_timeout=8)
        try:
            return bool(conn.bound)
        finally:
            try:
                conn.unbind()
            except Exception:
                pass
    except Exception:
        return False

def validate_password_complexity(password: str):
    """Stage 9: Enforce 12+ chars, uppercase, lowercase, digit, special char."""
    import re
    errors = []
    if len(password) < 12:
        errors.append("at least 12 characters")
        errors.append("at least one uppercase letter")
    if not re.search(r'[a-z]', password):
        errors.append("at least one lowercase letter")
    if not re.search(r'\d', password):
        errors.append("at least one digit")
    if not re.search(r'[!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]', password):
        errors.append("at least one special character (!@#$%^&*...)")
    if errors:
        raise HTTPException(status_code=400, detail=f"Password too weak: need {', '.join(errors)}")

def create_jwt(user_id: int, email: str, ttl_seconds: Optional[int] = None) -> str:
    jti = uuid.uuid4().hex[:32]
    effective_ttl = ttl_seconds if ttl_seconds else TOKEN_TTL_SECONDS
    payload = {
        "sub": str(user_id),
        "email": email,
        "jti": jti,
        "iat": int(time.time()),
        "exp": int(time.time()) + effective_ttl,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def session_ttl_for(user) -> int:
    """Department/role-based session lifetime in seconds.

    Super Admin (platform owner): 1 hour
    SOC users                    : 1 hour
    NOC / IT_SUPPORT users       : 30 minutes (default for all other departments)
    """
    email = str(user.get('email', '')).lower()
    if user.get('is_owner') or email == OWNER_EMAIL:
        return 3600
    dept = str(user.get('department_code') or '').upper()
    if dept == 'SOC':
        return 3600
    return 1800

def create_passwordless_jwt(target_user_id: int, email: str, ttl_seconds: int, scope: str = "passwordless") -> str:
    """Stage 15: short-lived single-use magic-login token (scope=passwordless)."""
    jti = uuid.uuid4().hex[:32]
    payload = {
        "sub": str(target_user_id),
        "email": email,
        "jti": jti,
        "scope": scope,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def resolve_username_to_email(cur, username: str):
    """Map a login identifier to a users.email:
       - contains '@'  -> used verbatim
       - otherwise     -> <username>@<LDAP_DOMAIN>, then fuzzy LIKE '<username>@%'"""
    if not username:
        return None
    username = str(username).strip()
    if not username:
        return None
    if "@" in username:
        return username.lower()
    candidate = f"{username}@{LDAP_DOMAIN}".lower()
    cur.execute("SELECT 1 FROM users WHERE email = %s;", (candidate,))
    if cur.fetchone():
        return candidate
    cur.execute("SELECT email FROM users WHERE email LIKE %s LIMIT 1;", (f"{username}@%",))
    row = cur.fetchone()
    return row['email'] if row else None


def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def is_token_revoked(cur, jti: str, user_id: int) -> bool:
    """Check whether a jti is individually blacklisted or force-revoked for the user."""
    if jti:
        cur.execute("SELECT 1 FROM revoked_tokens WHERE jti = %s;", (jti,))
        if cur.fetchone():
            return True
    cur.execute("SELECT 1 FROM revoked_tokens WHERE jti = %s AND user_id = %s;",
                (f'force-revoke-{user_id}', user_id))
    return cur.fetchone() is not None


def fetch_permission_codes(cur, role_id):
    cur.execute("""
        SELECT p.code FROM permissions p
        JOIN role_permissions rp ON rp.permission_id = p.id
        WHERE rp.role_id = %s;
    """, (role_id,))
    return [r['code'] for r in cur.fetchall()]

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return decode_jwt(credentials.credentials)

# ---------------------------------------------------------------------------
# Stage 7: Full user context + permission enforcement
# ---------------------------------------------------------------------------
def load_full_user(user_id: int):
    """Load the full user row (with permissions, dept, role, is_owner) from DB.
    Returns None if the user does not exist or is deactivated."""
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.email, u.full_name, u.avatar, u.is_active, u.is_owner,
               r.id as role_id, r.name as role_name, r.rank as role_rank,
               d.id as department_id, d.name as department_name, d.code as department_code
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE u.id = %s AND u.is_active = TRUE;
    """, (user_id,))
    user = cur.fetchone()
    if not user:
        cur.close(); conn.close()
        return None

    cur.execute("""
        SELECT p.code FROM permissions p
        JOIN role_permissions rp ON rp.permission_id = p.id
        WHERE rp.role_id = %s;
    """, (user['role_id'],))
    permissions = {r['code'] for r in cur.fetchall()}

    # Stage 8: Apply per-user permission overrides (additive only)
    cur.execute("""
        SELECT p.code, upo.granted FROM user_permission_overrides upo
        JOIN permissions p ON p.id = upo.permission_id
        WHERE upo.user_id = %s;
    """, (user_id,))
    for row in cur.fetchall():
        if row['granted']:
            permissions.add(row['code'])
        else:
            permissions.discard(row['code'])

    cur.close(); conn.close()

    user['permissions'] = permissions
    return user


def get_full_user_from_payload(current: dict):
    """Stage 15: full get_full_user logic driven by an already-decoded JWT payload.
    Used by the WebSocket chat endpoint and the service-key token generation path."""
    user_id = int(current['sub'])

    # Stage 9: reject revoked tokens (blacklist check)
    jti = current.get('jti')
    conn = get_db_connection(); cur = conn.cursor()
    if jti:
        cur.execute("SELECT 1 FROM revoked_tokens WHERE jti = %s;", (jti,))
        revoked = cur.fetchone()
        if revoked:
            cur.close(); conn.close()
            raise HTTPException(status_code=401, detail="Session has been revoked")
    # Also check force-revoke marker (all-session invalidation)
    cur.execute("SELECT 1 FROM revoked_tokens WHERE jti = %s AND user_id = %s;",
                (f'force-revoke-{user_id}', user_id))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=401, detail="All sessions have been revoked")
    cur.close(); conn.close()

    user = load_full_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found or deactivated")
    return user


def get_full_user(current=Depends(get_current_user)):
    """Load the full user row (with permissions, dept, role, is_owner) from DB."""
    return get_full_user_from_payload(current)


def require_permission(permission_code: str):
    """Dependency factory — returns a dependency that enforces a single permission code."""
    def _dep(user=Depends(get_full_user)):
        if user['is_owner']:
            return user
        if permission_code not in user['permissions']:
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: requires '{permission_code}'"
            )
        return user
    return _dep


def require_any_permission(*permission_codes):
    """Dependency factory — user needs at least ONE of the listed permissions."""
    def _dep(user=Depends(get_full_user)):
        if user['is_owner']:
            return user
        if not user['permissions'].intersection(set(permission_codes)):
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: requires one of {list(permission_codes)}"
            )
        return user
    return _dep


GLOBAL_TICKET_RANK = 45  # Helpdesk (45), IT Support (50), NOC (65), SOC (70), Owner (100)


def is_ticket_admin(user):
    """IT / NOC / SOC personnel, Helpdesk agents and Owners get global ticket visibility."""
    if user['is_owner']:
        return True
    try:
        return int(user.get('role_rank') or 0) >= GLOBAL_TICKET_RANK
    except (TypeError, ValueError):
        return False


def check_ticket_access(user, ticket):
    """
    Stage 7 dept rule (extended for role-based access control):
      - Owners and IT/NOC/SOC/Helpdesk roles have global read/write access.
      - Users can access tickets they created themselves (owner_id).
      - Users can access tickets within their own department.
      - Users can also access tickets specifically assigned to them.
    Returns True/False.
    """
    if is_ticket_admin(user):
        return True
    if ticket.get('owner_id') and ticket['owner_id'] == user['id']:
        return True
    if ticket.get('owner_department_id') and ticket['owner_department_id'] == user['department_id']:
        return True
    if ticket.get('assigned_user_id') and ticket['assigned_user_id'] == user['id']:
        return True
    return False

# ---------------------------------------------------------------------------
# Core Routes
# ---------------------------------------------------------------------------
@app.get("/health")
def health_check():
    return {"status": "ok", "service": "Enterprise ITSM & SOC API", "ws_clients": len(manager.active_connections)}

@app.get("/api/tickets")
def get_tickets(
    category: Optional[str] = None,
    user=Depends(require_permission("tickets.view"))
):
    conn = get_db_connection(); cur = conn.cursor()
    if category and category != 'ALL':
        cur.execute(
            """
            SELECT t.*, u.full_name AS reporter_name, u.email AS reporter_email,
                   d.name AS reporter_department_name, d.code AS reporter_department_code
            FROM tickets t
            LEFT JOIN users u ON u.id = t.owner_id
            LEFT JOIN departments d ON d.id = u.department_id
            WHERE t.category = %s ORDER BY t.id DESC;
            """,
            (category,),
        )
    else:
        cur.execute(
            """
            SELECT t.*, u.full_name AS reporter_name, u.email AS reporter_email,
                   d.name AS reporter_department_name, d.code AS reporter_department_code
            FROM tickets t
            LEFT JOIN users u ON u.id = t.owner_id
            LEFT JOIN departments d ON d.id = u.department_id
            ORDER BY t.id DESC;
            """
        )
    all_tickets = cur.fetchall(); cur.close(); conn.close()

    # Stage 7: filter by department / assignment (global visibility for IT/NOC/SOC)
    if is_ticket_admin(user):
        return all_tickets

    accessible = []
    for t in all_tickets:
        if check_ticket_access(user, t):
            accessible.append(t)
    return accessible

@app.post("/api/tickets", status_code=201)
async def create_ticket(
    ticket: TicketCreate,
    user=Depends(require_permission("tickets.create"))
):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO tickets (title, description, category, status, severity,
                              assigned_user, user_avatar, active_team,
                              current_activity, source, owner_id, owner_department_id,
                              created_at, updated_at)
        VALUES (%s, %s, %s, 'UNASSIGNED', %s, %s, %s, %s::jsonb, %s, %s, %s, %s, NOW(), NOW())
        RETURNING *;
        """,
        (
            ticket.title,
            ticket.description,
            ticket.category or "INCIDENT_MGMT",
            ticket.severity,
            ticket.assigned_user or "Unassigned",
            ticket.user_avatar,
            json.dumps(ticket.active_team or []),
            ticket.current_activity,
            ticket.source,
            user['id'],
            user['department_id'],
        ),
    )
    new_ticket = cur.fetchone()
    conn.commit()
    enriched = enrich_ticket(conn, new_ticket)
    cur.close()
    conn.close()
    await manager.broadcast({"type": "ticket_created", "data": enriched})
    return enriched

@app.patch("/api/tickets/{ticket_id}")
async def update_ticket(
    ticket_id: int,
    update: TicketUpdate,
    user=Depends(require_permission("tickets.update"))
):
    conn = get_db_connection(); cur = conn.cursor()
    # Stage 7: check access to this specific ticket
    cur.execute("SELECT * FROM tickets WHERE id = %s;", (ticket_id,))
    ticket = cur.fetchone()
    if not ticket:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    if not check_ticket_access(user, ticket):
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Access denied: ticket belongs to another department")

    # RBAC: status changes, assignment and team changes are IT/NOC/SOC + admin actions.
    privileged = any(x is not None for x in (
        update.status, update.assigned_user, update.assigned_user_id, update.active_team))
    if privileged and not is_ticket_admin(user):
        cur.close(); conn.close()
        raise HTTPException(
            status_code=403,
            detail="Only IT/NOC/SOC personnel, Helpdesk agents or admins can change status, assignment, or team")

    # Normalize/validate enum-backed fields so bad values never 500
    STATUS_ALIASES = {"IN_PROGRESS": "IN_INVESTIGATION", "RESOLVED": "MITIGATED"}
    VALID_STATUSES = {"OPEN", "IN_INVESTIGATION", "MITIGATED", "CLOSED", "UNASSIGNED"}
    VALID_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    if update.status is not None:
        update.status = STATUS_ALIASES.get(update.status.upper(), update.status)
        if update.status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status '{update.status}'")
    if update.severity is not None and update.severity not in VALID_SEVERITIES:
        raise HTTPException(status_code=400, detail=f"Invalid severity '{update.severity}'")

    fields = []
    values = []
    if update.status is not None:
        fields.append("status = %s"); values.append(update.status)
    if update.assigned_user is not None:
        fields.append("assigned_user = %s"); values.append(update.assigned_user)
    if update.assigned_user_id is not None:
        fields.append("assigned_user_id = %s"); values.append(update.assigned_user_id)
    if update.user_avatar is not None:
        fields.append("user_avatar = %s"); values.append(update.user_avatar)
    if update.current_activity is not None:
        fields.append("current_activity = %s"); values.append(update.current_activity)
    if update.active_team is not None:
        fields.append("active_team = %s::jsonb"); values.append(json.dumps(update.active_team))
    if update.severity is not None:
        fields.append("severity = %s"); values.append(update.severity)
    if not fields:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="No fields provided for update")
    fields.append("updated_at = NOW()"); values.append(ticket_id)
    query = f"UPDATE tickets SET {', '.join(fields)} WHERE id = %s RETURNING *;"
    cur.execute(query, tuple(values))
    updated_ticket = cur.fetchone()
    conn.commit()
    enriched = enrich_ticket(conn, updated_ticket) if updated_ticket else None
    cur.close(); conn.close()
    if not updated_ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    await manager.broadcast({"type": "ticket_updated", "data": enriched})
    return enriched


@app.post("/api/tickets/{ticket_id}/claim")
async def claim_ticket(
    ticket_id: int,
    user=Depends(require_any_permission("tickets.update", "tickets.assign"))
):
    """Self-service claim: assign the ticket to the currently logged-in IT/SOC staff member."""
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id = %s;", (ticket_id,))
    ticket = cur.fetchone()
    if not ticket:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    if not check_ticket_access(user, ticket):
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Access denied: ticket belongs to another department")
    if not is_ticket_admin(user):
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Only IT/NOC/SOC personnel, Helpdesk agents or admins can claim tickets")
    if ticket.get('assigned_user_id') and ticket['assigned_user_id'] != user['id']:
        cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="Ticket is already assigned to another analyst")

    claimant_name = user.get('full_name') or user.get('email') or 'Staff'
    cur.execute(
        """
        UPDATE tickets
        SET assigned_user = %s, assigned_user_id = %s, user_avatar = %s,
            status = CASE WHEN status IN ('UNASSIGNED', 'OPEN') THEN 'IN_INVESTIGATION' ELSE status END,
            current_activity = %s, updated_at = NOW()
        WHERE id = %s
        RETURNING *;
        """,
        (claimant_name, user['id'], user.get('avatar') or '', f"Claimed by {claimant_name} - in progress", ticket_id),
    )
    claimed = cur.fetchone()
    conn.commit()
    enriched = enrich_ticket(conn, claimed) if claimed else claimed
    cur.close(); conn.close()
    await manager.broadcast({"type": "ticket_updated", "data": enriched})
    return enriched


# ---------------------------------------------------------------------------
# Stage 15: Real-time ticket chat (REST + WebSocket)
# ---------------------------------------------------------------------------
@app.get("/api/tickets/{ticket_id}/chat")
def get_ticket_chat(
    ticket_id: int,
    after_id: Optional[int] = None,
    user=Depends(require_any_permission("tickets.view", "tickets.update")),
):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id = %s;", (ticket_id,))
    ticket = cur.fetchone()
    if not ticket:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    if not check_ticket_access(user, ticket):
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Access denied: ticket belongs to another department")
    if after_id:
        cur.execute("""
            SELECT * FROM ticket_messages
            WHERE ticket_id = %s AND id > %s
            ORDER BY id ASC;
        """, (ticket_id, after_id))
    else:
        cur.execute("""
            SELECT * FROM ticket_messages
            WHERE ticket_id = %s
            ORDER BY id ASC;
        """, (ticket_id,))
    messages = cur.fetchall(); cur.close(); conn.close()
    return messages


@app.post("/api/tickets/{ticket_id}/chat", status_code=201)
async def post_ticket_chat(
    ticket_id: int,
    body: ChatPostBody,
    user=Depends(require_any_permission("tickets.update", "tickets.create")),
):
    message_text = (body.message or "").strip()
    if not message_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(message_text) > 4000:
        raise HTTPException(status_code=400, detail="Message too long (max 4000 characters)")

    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id = %s;", (ticket_id,))
    ticket = cur.fetchone()
    if not ticket:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    if not check_ticket_access(user, ticket):
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Access denied: ticket belongs to another department")

    cur.execute("""
        INSERT INTO ticket_messages (ticket_id, sender_id, sender_name, message)
        VALUES (%s, %s, %s, %s)
        RETURNING *;
    """, (ticket_id, user['id'], user['full_name'], message_text))
    message = cur.fetchone()
    conn.commit(); cur.close(); conn.close()

    await manager.chat_publish(ticket_id, {"type": "chat_message", "ticket_id": ticket_id, "data": message})
    return message


@app.websocket("/api/tickets/{ticket_id}/chat/ws")
async def ticket_chat_websocket(websocket: WebSocket, ticket_id: int):
    """Room-scoped chat socket. Auth is provided in the first JSON frame."""
    await websocket.accept()
    try:
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        token_value = auth_msg.get("token") if isinstance(auth_msg, dict) else None
        if not token_value:
            await websocket.close(code=4401)
            return
        payload = decode_jwt(token_value)
        user = get_full_user_from_payload(payload)
    except Exception:
        await websocket.close(code=4401)
        return

    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id = %s;", (ticket_id,))
    ticket = cur.fetchone()
    cur.close(); conn.close()
    if not ticket:
        await websocket.close(code=4404)
        return
    if not check_ticket_access(user, ticket):
        await websocket.close(code=4403)
        return

    queue = await manager.chat_join(ticket_id, websocket)
    await websocket.send_json({"type": "connected", "ticket_id": ticket_id, "user_id": user['id']})
    try:
        while True:
            await websocket.receive_text()
            if queue and not queue.empty():
                await websocket.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        manager.chat_leave(ticket_id, websocket)

@app.post("/api/webhooks/alert", status_code=201)
async def ingest_alert(alert: WebhookAlert, x_webhook_token: Optional[str] = Header(None)):
    if not x_webhook_token or not key_matches(x_webhook_token, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing webhook token")
    conn = get_db_connection(); cur = conn.cursor()
    analyst = pick_available_analyst(cur)
    title = alert.title or alert.rule or "Automated Security Alert"
    description = alert.description or alert.message or "Alert ingested from external SIEM/firewall source."
    if alert.src_ip:
        description += f" (Source IP: {alert.src_ip})"
    cur.execute(
        """INSERT INTO tickets (title, description, category, status, severity,
              assigned_user, user_avatar, active_team, current_activity, source, created_at, updated_at)
           VALUES (%s, %s, 'INCIDENT_MGMT', 'OPEN', %s, %s, %s, '[]'::jsonb, %s, %s, NOW(), NOW())
           RETURNING *;""",
        (title, description, alert.severity, analyst["name"], analyst["avatar"], "Auto-Triage", alert.source),
    )
    new_ticket = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    await manager.broadcast({"type": "ticket_created", "data": new_ticket})
    return new_ticket

# ---------------------------------------------------------------------------
# Legacy User Profile endpoints (backward compat)
# ---------------------------------------------------------------------------
@app.post("/api/user/avatar", status_code=200)
async def upload_user_avatar(file: UploadFile = File(...), current=Depends(get_full_user)):
    """Upload + persist the current user's avatar. Returns a /media path so the
    image stays visible across refreshes and account switches."""
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Only JPG, PNG, WEBP or GIF images are allowed")
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be under 2 MB.")
    fname = f"u{current['id']}_{uuid.uuid4().hex[:10]}{allowed[file.content_type]}"
    with open(os.path.join(AVATAR_DIR, fname), "wb") as fh:
        fh.write(content)
    url = f"/media/avatars/{fname}"
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE users SET avatar = %s, updated_at = NOW() WHERE id = %s;",
                (url, current['id']))
    cur.execute("""
        INSERT INTO user_profiles (email, name, avatar, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (email) DO UPDATE SET avatar = EXCLUDED.avatar, updated_at = NOW();
    """, (current['email'], current['full_name'], url))
    conn.commit(); cur.close(); conn.close()
    return {"avatar": url}

@app.put("/api/user/profile")
def upsert_user_profile(profile: UserProfile, current=Depends(get_full_user)):
    # Users can update their own profile; admins can update any
    if current['email'] != profile.email and not current['is_owner'] and 'users.edit' not in current['permissions']:
        raise HTTPException(status_code=403, detail="Access denied: cannot update other users' profiles")
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_profiles (email, name, role, dept, avatar, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (email) DO UPDATE SET name=EXCLUDED.name, role=EXCLUDED.role,
            dept=EXCLUDED.dept, avatar=EXCLUDED.avatar, updated_at=NOW()
        RETURNING *;
    """, (profile.email, profile.name, profile.role, profile.dept, profile.avatar))
    user = cur.fetchone()
    # Mirror display name + avatar onto the users table so the login/refresh
    # payload and nav bar always reflect the latest profile picture.
    cur.execute("""
        UPDATE users SET full_name = %s, avatar = COALESCE(%s, avatar), updated_at = NOW()
        WHERE email = %s;
    """, (profile.name, profile.avatar, profile.email))
    conn.commit(); cur.close(); conn.close()
    return user

@app.get("/api/user/profile/{email}")
def get_user_profile(email: str, current=Depends(get_full_user)):
    # Users can view their own profile; admins can view any
    if current['email'] != email and not current['is_owner'] and 'users.view' not in current['permissions']:
        raise HTTPException(status_code=403, detail="Access denied: cannot view other users' profiles")
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM user_profiles WHERE email = %s;", (email,))
    user = cur.fetchone(); cur.close(); conn.close()
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")
    return user

# ---------------------------------------------------------------------------
# RBAC Endpoints: Departments
# ---------------------------------------------------------------------------
@app.get("/api/departments")
def list_departments(user=Depends(require_permission("dashboard.view"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM departments WHERE is_active = TRUE ORDER BY id;")
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows

@app.get("/api/departments/{dept_id}")
def get_department(dept_id: int, user=Depends(require_permission("dashboard.view"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM departments WHERE id = %s;", (dept_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Department not found")
    return row

# ---------------------------------------------------------------------------
# RBAC Endpoints: Roles & Permissions
# ---------------------------------------------------------------------------
@app.get("/api/roles")
def list_roles(user=Depends(require_permission("roles.view"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM roles ORDER BY rank DESC;")
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows

@app.get("/api/roles/{role_id}")
def get_role(role_id: int, user=Depends(require_permission("roles.view"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM roles WHERE id = %s;", (role_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Role not found")
    return row

@app.get("/api/roles/{role_id}/permissions")
def get_role_permissions(role_id: int, user=Depends(require_permission("roles.view"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT p.* FROM permissions p
        JOIN role_permissions rp ON rp.permission_id = p.id
        WHERE rp.role_id = %s ORDER BY p.module, p.code;
    """, (role_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows

@app.get("/api/permissions")
def list_permissions(user=Depends(require_permission("roles.view"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM permissions ORDER BY module, code;")
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows

# ---------------------------------------------------------------------------
# RBAC Endpoints: Users
# ---------------------------------------------------------------------------
@app.get("/api/users")
def list_users(user=Depends(get_full_user)):
    """User Directory endpoint - any authenticated user can list the platform roster."""
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.email, u.full_name, u.avatar, u.is_active, u.is_owner,
               u.created_at, u.updated_at,
               r.id as role_id, r.name as role_name, r.rank as role_rank,
               d.id as department_id, d.name as department_name, d.code as department_code
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN departments d ON d.id = u.department_id
        ORDER BY u.is_owner DESC, r.rank DESC, u.full_name;
    """)
    rows = cur.fetchall(); cur.close(); conn.close()

    # Enrich with derived AD / name fields for the directory view (no schema change)
    for r in rows:
        ws = (r['full_name'] or '').strip().split()
        r['display_name'] = r['full_name']
        r['first_name'] = ws[0] if ws else ''
        r['last_name'] = ws[-1] if len(ws) > 1 else ''
        local, sep, dom = (r['email'] or '').partition('@')
        r['ad_domain'] = dom or None
        r['sam_account_name'] = local or None
        r['upn'] = r['email']
        r['role'] = r['role_name']
    return rows

@app.get("/api/users/{user_id}")
def get_user(user_id: int, user=Depends(require_permission("users.view"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.email, u.full_name, u.avatar, u.is_active, u.is_owner,
               u.created_at, u.updated_at,
               r.name as role_name, r.rank as role_rank,
               d.name as department_name, d.code as department_code
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE u.id = %s;
    """, (user_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return row

@app.post("/api/users", status_code=201)
def create_rbac_user(user: RBACUserCreate, current=Depends(require_permission("users.create"))):
    if user.password:
        validate_password_complexity(user.password)
    conn = get_db_connection(); cur = conn.cursor()
    # Stage 8: Hash password if provided; default to Viewer role if none specified
    pw_hash = hash_password(user.password) if user.password else ""
    role_id = user.role_id
    if not role_id:
        cur.execute("SELECT id FROM roles WHERE name = 'Viewer';")
        vr = cur.fetchone()
        role_id = vr['id'] if vr else None
    cur.execute("""
        INSERT INTO users (email, password_hash, full_name, role_id, department_id, avatar)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, email, full_name, is_active, is_owner, created_at;
    """, (user.email, pw_hash, user.full_name, role_id, user.department_id, user.avatar))
    created = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return created

@app.put("/api/users/{user_id}")
def update_rbac_user(user_id: int, update: RBACUserUpdate, current=Depends(require_permission("users.edit"))):
    conn = get_db_connection(); cur = conn.cursor()
    fields, values = [], []
    if update.full_name is not None:
        fields.append("full_name = %s"); values.append(update.full_name)
    if update.role_id is not None:
        fields.append("role_id = %s"); values.append(update.role_id)
    if update.department_id is not None:
        fields.append("department_id = %s"); values.append(update.department_id)
    if update.is_active is not None:
        fields.append("is_active = %s"); values.append(update.is_active)
    if update.avatar is not None:
        fields.append("avatar = %s"); values.append(update.avatar)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    fields.append("updated_at = NOW()"); values.append(user_id)
    cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = %s RETURNING id, email, full_name;", tuple(values))
    updated = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    return updated

@app.get("/api/user/rbac/{email}")
def get_user_rbac(email: str, current=Depends(get_full_user)):
    # Users can look up their own RBAC info; admins can look up anyone
    if current['email'] != email and not current['is_owner'] and 'users.view' not in current['permissions']:
        raise HTTPException(status_code=403, detail="Access denied: cannot view other users' RBAC info")
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.email, u.full_name, u.avatar, u.is_active, u.is_owner,
               r.id as role_id, r.name as role_name, r.rank as role_rank,
               d.id as department_id, d.name as department_name, d.code as department_code
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE u.email = %s;
    """, (email,))
    user = cur.fetchone()
    if not user:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    cur.execute("""
        SELECT p.code, p.description, p.module FROM permissions p
        JOIN role_permissions rp ON rp.permission_id = p.id
        WHERE rp.role_id = %s ORDER BY p.module, p.code;
    """, (user['role_id'],))
    perms = cur.fetchall(); cur.close(); conn.close()
    return {**user, "permissions": [p['code'] for p in perms], "permission_details": perms}

# ---------------------------------------------------------------------------
# Stage 8: Admin Management Endpoints
# ---------------------------------------------------------------------------
class AdminRoleUpdate(BaseModel):
    role_id: int
    department_id: Optional[int] = None

class AdminPermOverride(BaseModel):
    permission_code: str
    granted: bool = True

class AdminStatusUpdate(BaseModel):
    is_active: bool

class OnboardPayload(BaseModel):
    email: str
    department_code: Optional[str] = ""
    role_name: Optional[str] = ""

@app.get("/api/admin/users")
def admin_list_users(user=Depends(require_permission("users.view"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.email, u.full_name, u.avatar, u.is_active, u.is_owner,
               u.created_at, u.updated_at,
               r.id as role_id, r.name as role_name, r.rank as role_rank,
               d.id as department_id, d.name as department_name, d.code as department_code,
               (SELECT COUNT(*) FROM user_permission_overrides upo WHERE upo.user_id = u.id) as override_count
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN departments d ON d.id = u.department_id
        ORDER BY u.is_owner DESC, r.rank DESC, u.full_name;
    """)
    users = cur.fetchall()

    # Attach effective permissions for each user
    for u in users:
        cur.execute("""
            SELECT p.code FROM permissions p
            JOIN role_permissions rp ON rp.permission_id = p.id
            WHERE rp.role_id = %s;
        """, (u['role_id'],))
        role_perms = {r['code'] for r in cur.fetchall()}

        cur.execute("""
            SELECT p.code, upo.granted FROM user_permission_overrides upo
            JOIN permissions p ON p.id = upo.permission_id
            WHERE upo.user_id = %s;
        """, (u['id'],))
        for row in cur.fetchall():
            if row['granted']:
                role_perms.add(row['code'])
            else:
                role_perms.discard(row['code'])

        u['effective_permissions'] = sorted(role_perms)

    cur.close(); conn.close()
    return users


@app.get("/api/admin/users/{user_id}")
def admin_get_user(user_id: int, user=Depends(require_permission("users.view"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.email, u.full_name, u.avatar, u.is_active, u.is_owner,
               u.created_at, u.updated_at,
               r.id as role_id, r.name as role_name, r.rank as role_rank,
               d.id as department_id, d.name as department_name, d.code as department_code
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE u.id = %s;
    """, (user_id,))
    u = cur.fetchone()
    if not u:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    # Role permissions
    cur.execute("""
        SELECT p.code, p.description, p.module FROM permissions p
        JOIN role_permissions rp ON rp.permission_id = p.id
        WHERE rp.role_id = %s;
    """, (u['role_id'],))
    role_perms = {r['code']: r for r in cur.fetchall()}

    # Overrides
    cur.execute("""
        SELECT p.code, p.description, p.module, upo.granted FROM user_permission_overrides upo
        JOIN permissions p ON p.id = upo.permission_id
        WHERE upo.user_id = %s;
    """, (user_id,))
    overrides = cur.fetchall()

    effective = set(role_perms.keys())
    for o in overrides:
        if o['granted']:
            effective.add(o['code'])
        else:
            effective.discard(o['code'])

    cur.close(); conn.close()
    return {
        **u,
        "role_permissions": list(role_perms.keys()),
        "overrides": overrides,
        "effective_permissions": sorted(effective),
    }


@app.patch("/api/admin/users/{user_id}/role")
def admin_update_role(user_id: int, body: AdminRoleUpdate, user=Depends(require_permission("users.edit"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id, is_owner FROM users WHERE id = %s;", (user_id,))
    target = cur.fetchone()
    if not target:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if target['is_owner'] and not user['is_owner']:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Cannot modify the Owner account")

    fields = ["role_id = %s", "updated_at = NOW()"]
    values = [body.role_id]
    if body.department_id is not None:
        fields.append("department_id = %s")
        values.append(body.department_id)
    values.append(user_id)
    cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = %s RETURNING id, email, full_name;", tuple(values))
    updated = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return updated


@app.patch("/api/admin/users/{user_id}/permissions")
def admin_update_permissions(user_id: int, body: AdminPermOverride, user=Depends(require_permission("users.edit"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id, is_owner FROM users WHERE id = %s;", (user_id,))
    target = cur.fetchone()
    if not target:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if target['is_owner'] and not user['is_owner']:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Cannot modify the Owner account")

    # Resolve permission_id
    cur.execute("SELECT id FROM permissions WHERE code = %s;", (body.permission_code,))
    perm = cur.fetchone()
    if not perm:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail=f"Permission '{body.permission_code}' not found")

    if body.granted:
        cur.execute("""
            INSERT INTO user_permission_overrides (user_id, permission_id, granted, granted_by)
            VALUES (%s, %s, TRUE, %s)
            ON CONFLICT (user_id, permission_id) DO UPDATE SET granted = TRUE, granted_by = EXCLUDED.granted_by;
        """, (user_id, perm['id'], user['id']))
    else:
        cur.execute("""
            INSERT INTO user_permission_overrides (user_id, permission_id, granted, granted_by)
            VALUES (%s, %s, FALSE, %s)
            ON CONFLICT (user_id, permission_id) DO UPDATE SET granted = FALSE, granted_by = EXCLUDED.granted_by;
        """, (user_id, perm['id'], user['id']))

    conn.commit(); cur.close(); conn.close()
    return {"message": f"Permission '{body.permission_code}' {'granted' if body.granted else 'revoked'} for user {user_id}"}


@app.patch("/api/admin/users/{user_id}/status")
def admin_update_status(user_id: int, body: AdminStatusUpdate, user=Depends(require_permission("users.edit"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id, is_owner FROM users WHERE id = %s;", (user_id,))
    target = cur.fetchone()
    if not target:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if target['is_owner']:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Cannot deactivate the Owner account")

    cur.execute("UPDATE users SET is_active = %s, updated_at = NOW() WHERE id = %s RETURNING id, email, full_name, is_active;",
                (body.is_active, user_id))
    updated = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return updated

# ---------------------------------------------------------------------------
# Auth: endpoints
# ---------------------------------------------------------------------------
@app.post("/api/auth/login")
def auth_login(body: AuthLogin, request: Request, x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For")):
    conn = get_db_connection(); cur = conn.cursor()
    email = resolve_username_to_email(cur, body.email)
    if not email:
        cur.close(); conn.close();
        rate_limit_failure("login", (body.email or "").strip().lower(), request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    cur.execute("""
        SELECT u.id, u.email, u.full_name, u.password_hash, u.avatar, u.is_active, u.is_owner,
               r.id as role_id, r.name as role_name, r.rank as role_rank,
               d.id as department_id, d.name as department_name, d.code as department_code
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE u.email = %s;
    """, (email,))
    user = cur.fetchone()

    if not user:
        cur.close(); conn.close();
        rate_limit_failure("login", email, request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user['is_active']:
        cur.close(); conn.close();
        rate_limit_failure("login", email, request, x_forwarded_for)
        raise HTTPException(status_code=403, detail="Account is deactivated")

    ldap_ok = ldap_authenticate(body.email, body.password) if LDAP_AUTH_ENABLED else False
    if not ldap_ok:
        if not user['password_hash'] or not verify_password(body.password, user['password_hash']):
            cur.close(); conn.close();
            rate_limit_failure("login", email, request, x_forwarded_for)
            detail = "Invalid Active Directory credentials" if LDAP_AUTH_ENABLED else "Invalid email or password"
            raise HTTPException(status_code=401, detail=detail)

    rate_limit_reset("login", email, request, x_forwarded_for)
    ttl = session_ttl_for(user)
    token = create_jwt(user['id'], user['email'], ttl)
    expires_at = int(time.time()) + ttl
    cur.execute("DELETE FROM revoked_tokens WHERE jti = %s;", (f"force-revoke-{user['id']}",))
    permissions = fetch_permission_codes(cur, user['role_id'])
    conn.commit(); cur.close(); conn.close()

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": ttl,
        "expires_at": expires_at,
        "user": {
            "id": user['id'],
            "email": user['email'],
            "full_name": user['full_name'],
            "avatar": user['avatar'],
            "is_owner": user['is_owner'],
            "role_id": user['role_id'],
            "role_name": user['role_name'],
            "role_rank": user['role_rank'],
            "department_id": user['department_id'],
            "department_name": user['department_name'],
            "department_code": user['department_code'],
            "permissions": permissions,
        }
    }


@app.post("/api/auth/temp-access")
def auth_temp_access(body: TempAccessBody):
    """Self-service 30-minute temporary session.

    Verifies the AD password via an LDAPS bind (no local-password fallback),
    then issues a short-lived standard session JWT for the matching platform
    account. Used by the 'Request 30-Min Temporary Access' flow on the login page."""
    if not ldap_authenticate(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid Active Directory credentials")

    conn = get_db_connection(); cur = conn.cursor()
    email = resolve_username_to_email(cur, body.username)
    if not email:
        cur.close(); conn.close()
        raise HTTPException(status_code=401, detail="Account is not provisioned for SecOps")
    cur.execute("""
        SELECT u.id, u.email, u.full_name, u.avatar, u.is_active, u.is_owner,
               r.id as role_id, r.name as role_name, r.rank as role_rank,
               d.id as department_id, d.name as department_name, d.code as department_code
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE u.email = %s;
    """, (email,))
    user = cur.fetchone()
    if not user:
        cur.close(); conn.close()
        raise HTTPException(status_code=401, detail="Account is not provisioned for SecOps")
    if not user['is_active']:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Account is deactivated")

    temp_ttl = AUTH_TOKEN_TTL_SECONDS  # 30 minutes
    token = create_jwt(user['id'], user['email'], temp_ttl)
    expires_at = int(time.time()) + temp_ttl

    cur.execute("DELETE FROM revoked_tokens WHERE jti = %s;", (f"force-revoke-{user['id']}",))
    permissions = fetch_permission_codes(cur, user['role_id'])
    conn.commit(); cur.close(); conn.close()

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": temp_ttl,
        "expires_at": expires_at,
        "temp_access": True,
        "user": {
            "id": user['id'],
            "email": user['email'],
            "full_name": user['full_name'],
            "avatar": user['avatar'],
            "is_owner": user['is_owner'],
            "role_id": user['role_id'],
            "role_name": user['role_name'],
            "role_rank": user['role_rank'],
            "department_id": user['department_id'],
            "department_name": user['department_name'],
            "department_code": user['department_code'],
            "permissions": permissions,
        }
    }
@app.post("/api/auth/refresh")
def auth_refresh(current=Depends(get_current_user)):
    """Stage 12+: Accept a valid (unexpired, non-revoked) token and return a fresh
    department-capped token for active users. The previous token is revoked."""
    user_id = int(current['sub'])
    jti = current.get('jti')
    conn = get_db_connection(); cur = conn.cursor()

    # Reject revoked / force-revoked tokens
    if is_token_revoked(cur, jti, user_id):
        cur.close(); conn.close()
        raise HTTPException(status_code=401, detail="Session has been revoked")

    cur.execute("""
        SELECT u.id, u.email, u.full_name, u.avatar, u.is_active, u.is_owner,
               r.id as role_id, r.name as role_name, r.rank as role_rank,
               d.id as department_id, d.name as department_name, d.code as department_code
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE u.id = %s;
    """, (user_id,))
    user = cur.fetchone()
    if not user:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if not user['is_active']:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Account is deactivated")

    # Revoke the old token (single-use refresh semantics)
    if jti:
        cur.execute("""
            INSERT INTO revoked_tokens (jti, user_id) VALUES (%s, %s)
            ON CONFLICT (jti) DO NOTHING;
        """, (jti, user_id))

    ttl = session_ttl_for(user)
    new_token = create_jwt(user['id'], user['email'], ttl)
    expires_at = int(time.time()) + ttl
    permissions = fetch_permission_codes(cur, user['role_id'])
    conn.commit(); cur.close(); conn.close()

    return {
        "access_token": new_token,
        "token_type": "bearer",
        "expires_in": ttl,
        "expires_at": expires_at,
        "user": {
            "id": user['id'],
            "email": user['email'],
            "full_name": user['full_name'],
            "avatar": user['avatar'],
            "is_owner": user['is_owner'],
            "role_id": user['role_id'],
            "role_name": user['role_name'],
            "role_rank": user['role_rank'],
            "department_id": user['department_id'],
            "department_name": user['department_name'],
            "department_code": user['department_code'],
            "permissions": permissions,
        }
    }


@app.post("/api/auth/setup-password")
def auth_setup_password(body: AuthPasswordSetup, request: Request, x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For")):
    """Set a first password only when a valid, unconsumed passwordless token proves account ownership."""
    validate_password_complexity(body.password)
    requested_email = (body.email or "").strip().lower()
    token_value = (body.token or "").strip()
    if not token_value and not requested_email:
        raise HTTPException(status_code=400, detail="Invalid or expired setup token")
    if not token_value:
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")

    try:
        payload = decode_jwt(token_value)
    except HTTPException:
        rate_limit_failure("setup-password", requested_email or "unknown", request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")

    if payload.get("scope") != "passwordless":
        rate_limit_failure("setup-password", requested_email or str(payload.get("email") or "unknown"), request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")

    user_id = int(payload["sub"])
    expected_email = str(payload.get("email") or "").lower()
    if requested_email and requested_email != expected_email:
        rate_limit_failure("setup-password", expected_email, request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")

    conn = get_db_connection(); cur = conn.cursor()
    token_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
    cur.execute("""
        SELECT id, target_user_id, expires_at, consumed_at
        FROM auth_tokens
        WHERE token_hash = %s AND target_user_id = %s
          AND consumed_at IS NULL AND expires_at > NOW()
        FOR UPDATE;
    """, (token_hash, user_id))
    token_row = cur.fetchone()
    if not token_row:
        cur.close(); conn.close()
        rate_limit_failure("setup-password", expected_email, request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")

    cur.execute("SELECT id, email, password_hash FROM users WHERE id = %s FOR UPDATE;", (user_id,))
    user = cur.fetchone()
    if not user or user['email'].lower() != expected_email:
        cur.close(); conn.close()
        rate_limit_failure("setup-password", expected_email, request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")
    if user['password_hash']:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Invalid or expired setup token")

    hashed = hash_password(body.password)
    cur.execute("""
        UPDATE auth_tokens
        SET consumed_at = NOW()
        WHERE id = %s AND consumed_at IS NULL RETURNING id;
    """, (token_row['id'],))
    if not cur.fetchone():
        cur.close(); conn.close()
        rate_limit_failure("setup-password", expected_email, request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")

    cur.execute("""
        UPDATE users
        SET password_hash = %s, updated_at = NOW()
        WHERE id = %s AND password_hash IS NULL RETURNING id;
    """, (hashed, user_id))
    updated = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    if not updated:
        rate_limit_failure("setup-password", expected_email, request, x_forwarded_for)
        raise HTTPException(status_code=400, detail="Invalid or expired setup token")
    rate_limit_reset("setup-password", expected_email, request, x_forwarded_for)
    return {"message": "Password set successfully. Please log in."}


@app.post("/api/auth/token-login")
def auth_token_login(body: TokenLoginBody, request: Request, x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For")):
    """Consume a passwordless token atomically and return a normal session token."""
    try:
        payload = decode_jwt(body.token)
    except HTTPException:
        rate_limit_failure("token-login", "unknown", request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if payload.get("scope") != "passwordless":
        rate_limit_failure("token-login", str(payload.get("email") or "unknown"), request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = int(payload['sub'])
    account = str(payload.get("email") or "unknown").lower()
    token_hash = hashlib.sha256(body.token.encode("utf-8")).hexdigest()

    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        DELETE FROM auth_tokens
        WHERE token_hash = %s AND target_user_id = %s AND consumed_at IS NULL AND expires_at > NOW()
        RETURNING target_user_id;
    """, (token_hash, user_id))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        rate_limit_failure("token-login", account, request, x_forwarded_for)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    conn.commit(); cur.close(); conn.close()
    user = load_full_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("DELETE FROM revoked_tokens WHERE jti = %s;", (f"force-revoke-{user['id']}",))
    session_token = create_jwt(user['id'], user['email'])
    expires_at = int(time.time()) + TOKEN_TTL_SECONDS
    permissions = fetch_permission_codes(cur, user['role_id'])
    rate_limit_reset("token-login", account, request, x_forwarded_for)
    conn.commit(); cur.close(); conn.close()

    return {
        "access_token": session_token,
        "token_type": "bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "expires_at": expires_at,
        "user": {
            "id": user['id'],
            "email": user['email'],
            "full_name": user['full_name'],
            "avatar": user['avatar'],
            "is_owner": user['is_owner'],
            "role_id": user['role_id'],
            "role_name": user['role_name'],
            "role_rank": user['role_rank'],
            "department_id": user['department_id'],
            "department_name": user['department_name'],
            "department_code": user['department_code'],
            "permissions": permissions,
        }
    }


# ---------------------------------------------------------------------------
# Stage 15: Passwordless (magic-link / one-time token) authentication
# ---------------------------------------------------------------------------
@app.post("/api/auth/generate-token")
def auth_generate_token(
    body: TokenGenerateBody,
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Issue a single-use, TTL-capped passwordless login token."""
    caller = None
    if credentials:
        payload = decode_jwt(credentials.credentials) if credentials.credentials else None
        if payload:
            caller = get_full_user_from_payload(payload)

    authorized = bool(caller and (caller['is_owner'] or 'users.create' in caller['permissions']))
    has_service_key = key_matches(x_service_key, SERVICE_TOKEN_KEY) if x_service_key else False
    has_api_key = key_matches(x_api_key, ONBOARD_API_KEY) if x_api_key else False
    if not authorized and not has_service_key and not has_api_key:
        raise HTTPException(status_code=403, detail="Not authorized to generate login tokens")

    conn = get_db_connection(); cur = conn.cursor()
    email = resolve_username_to_email(cur, body.username)
    if not email:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    cur.execute("SELECT id, email, full_name, is_active FROM users WHERE email = %s;", (email,))
    target = cur.fetchone()
    if not target or not target['is_active']:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found or deactivated")

    ttl = body.ttl or AUTH_TOKEN_TTL_SECONDS
    ttl = max(60, min(int(ttl), AUTH_TOKEN_TTL_SECONDS))
    token = create_passwordless_jwt(target['id'], target['email'], ttl)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    caller_id = caller['id'] if caller else None
    cur.execute("""
        INSERT INTO auth_tokens (token_hash, username, target_user_id, created_by, expires_at)
        VALUES (%s, %s, %s, %s, NOW() + (%s || ' seconds')::interval);
    """, (token_hash, str(body.username).strip(), target['id'], caller_id, ttl))
    conn.commit(); cur.close(); conn.close()

    return {
        "token": token,
        "token_type": "passwordless",
        "expires_in": ttl,
        "expires_at": int(time.time()) + ttl,
        "target_user_id": target['id'],
        "email": target['email'],
        "full_name": target['full_name'],
    }


@app.post("/api/auth/register", status_code=201)
def auth_register(body: AuthRegister, current=Depends(require_permission("users.create"))):
    validate_password_complexity(body.password)
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email = %s;", (body.email,))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="Email already registered")

    # Stage 8: Zero-trust — default to Viewer role if not specified
    role_id = body.role_id
    if not role_id:
        cur.execute("SELECT id FROM roles WHERE name = 'Viewer';")
        vr = cur.fetchone()
        role_id = vr['id'] if vr else None

    hashed = hash_password(body.password)
    cur.execute("""
        INSERT INTO users (email, password_hash, full_name, role_id, department_id)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, email, full_name;
    """, (body.email, hashed, body.full_name, role_id, body.department_id))
    created = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return {"message": "Account created (zero-trust: Viewer role)", "user": created}

@app.get("/api/auth/me")
def auth_me(current=Depends(get_current_user)):
    # Stage 9: check revoked tokens
    jti = current.get('jti')
    user_id = int(current['sub'])
    conn = get_db_connection(); cur = conn.cursor()
    if jti:
        cur.execute("SELECT 1 FROM revoked_tokens WHERE jti = %s;", (jti,))
        if cur.fetchone():
            cur.close(); conn.close()
            raise HTTPException(status_code=401, detail="Session has been revoked")
    cur.execute("SELECT 1 FROM revoked_tokens WHERE jti = %s AND user_id = %s;",
                (f'force-revoke-{user_id}', user_id))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=401, detail="All sessions have been revoked")

    cur.execute("""
        SELECT u.id, u.email, u.full_name, u.avatar, u.is_active, u.is_owner,
               r.id as role_id, r.name as role_name, r.rank as role_rank,
               d.id as department_id, d.name as department_name, d.code as department_code
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN departments d ON d.id = u.department_id
        WHERE u.id = %s;
    """, (user_id,))
    user = cur.fetchone()
    if not user:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    cur.execute("""
        SELECT p.code, p.description, p.module FROM permissions p
        JOIN role_permissions rp ON rp.permission_id = p.id
        WHERE rp.role_id = %s ORDER BY p.module, p.code;
    """, (user['role_id'],))
    perms = cur.fetchall(); cur.close(); conn.close()

    return {
        "id": user['id'],
        "email": user['email'],
        "full_name": user['full_name'],
        "avatar": user['avatar'],
        "is_active": user['is_active'],
        "is_owner": user['is_owner'],
        "role_id": user['role_id'],
        "role_name": user['role_name'],
        "role_rank": user['role_rank'],
        "department_id": user['department_id'],
        "department_name": user['department_name'],
        "department_code": user['department_code'],
        "permissions": [p['code'] for p in perms],
        "permission_details": perms,
    }

@app.post("/api/auth/logout")
def auth_logout(current=Depends(get_current_user)):
    """Stage 9: Revoke the current session's token."""
    jti = current.get('jti')
    if jti:
        conn = get_db_connection(); cur = conn.cursor()
        user_id = int(current['sub'])
        cur.execute("""
            INSERT INTO revoked_tokens (jti, user_id) VALUES (%s, %s)
            ON CONFLICT (jti) DO NOTHING;
        """, (jti, user_id))
        conn.commit(); cur.close(); conn.close()
    return {"message": "Logged out"}


@app.post("/api/admin/users/{user_id}/revoke-sessions")
def admin_revoke_sessions(user_id: int, current=Depends(require_permission("users.edit"))):
    """Stage 9: Owner-only — revoke all active sessions for a given user."""
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id, is_owner FROM users WHERE id = %s;", (user_id,))
    target = cur.fetchone()
    if not target:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if target['is_owner'] and not current['is_owner']:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Cannot revoke the Owner's sessions")

    # Insert force-revoke marker; get_full_user checks this on every request
    cur.execute("""
        INSERT INTO revoked_tokens (jti, user_id)
        VALUES (%s, %s)
        ON CONFLICT (jti) DO NOTHING;
    """, (f"force-revoke-{user_id}", user_id))
    conn.commit(); cur.close(); conn.close()
    return {"message": f"All sessions for user {user_id} have been revoked"}


@app.post("/api/admin/users/onboard-webhook")
def admin_onboard_webhook(
    payload: OnboardPayload,
    x_api_key: Optional[str] = Header(None),
):
    """Stage 9: Automated onboarding — secured by X-Api-Key header."""
    if not x_api_key or not key_matches(x_api_key, ONBOARD_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Api-Key")

    email = payload.email
    department_code = payload.department_code
    role_name = payload.role_name

    if not email:
        raise HTTPException(status_code=400, detail="email is required")

    conn = get_db_connection(); cur = conn.cursor()

    # Resolve department
    dept_id = None
    if department_code:
        cur.execute("SELECT id FROM departments WHERE code = %s;", (department_code,))
        dept_row = cur.fetchone()
        if not dept_row:
            cur.close(); conn.close()
            raise HTTPException(status_code=400, detail=f"Department '{department_code}' not found")
        dept_id = dept_row['id']

    # Resolve role
    role_id = None
    if role_name:
        cur.execute("SELECT id FROM roles WHERE name = %s;", (role_name,))
        role_row = cur.fetchone()
        if not role_row:
            cur.close(); conn.close()
            raise HTTPException(status_code=400, detail=f"Role '{role_name}' not found")
        role_id = role_row['id']

    # Zero-trust: if no role specified, default to Viewer
    if not role_id:
        cur.execute("SELECT id FROM roles WHERE name = 'Viewer';")
        vr = cur.fetchone()
        role_id = vr['id'] if vr else None

    # Check if user already exists
    cur.execute("SELECT id FROM users WHERE email = %s;", (email,))
    existing = cur.fetchone()
    if existing:
        # Update department/role if provided
        updates, vals = [], []
        if dept_id:
            updates.append("department_id = %s"); vals.append(dept_id)
        if role_id:
            updates.append("role_id = %s"); vals.append(role_id)
        if updates:
            updates.append("updated_at = NOW()"); vals.append(existing['id'])
            cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = %s RETURNING id, email, full_name;", tuple(vals))
            updated = cur.fetchone()
            conn.commit(); cur.close(); conn.close()
            return {"message": "User updated via onboarding", "user": updated}
        cur.close(); conn.close()
        return {"message": "User already exists, no changes needed", "user_id": existing['id']}

    # Create new user with null password_hash (requires password setup on first login)
    name_part = email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
    cur.execute("""
        INSERT INTO users (email, password_hash, full_name, role_id, department_id, is_active)
        VALUES (%s, NULL, %s, %s, %s, TRUE)
        RETURNING id, email, full_name;
    """, (email, name_part, role_id, dept_id))
    created = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return {"message": "User onboarded (zero-trust: must set password on first login)", "user": created}
@app.patch("/api/admin/users/{user_id}/status")
def admin_update_status(user_id: int, body: AdminStatusUpdate, user=Depends(require_permission("users.edit"))):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT id, is_owner FROM users WHERE id = %s;", (user_id,))
    target = cur.fetchone()
    if not target:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if target['is_owner']:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Cannot deactivate the Owner account")

    cur.execute("UPDATE users SET is_active = %s, updated_at = NOW() WHERE id = %s RETURNING id, email, is_active;", (body.is_active, user_id))
    updated = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return updated

@app.post("/api/onboard", status_code=201)
def onboard_user(payload: OnboardPayload, x_onboard_key: Optional[str] = Header(None)):
    if not x_onboard_key or not key_matches(x_onboard_key, ONBOARD_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid onboarding key")

    conn = get_db_connection(); cur = conn.cursor()
    email = payload.email.strip().lower()

    # Resolve Department
    dept_id = None
    if payload.department_code:
        cur.execute("SELECT id FROM departments WHERE code = %s;", (payload.department_code.upper(),))
        d = cur.fetchone()
        if d:
            dept_id = d['id']

    # Resolve Role
    role_id = None
    if payload.role_name:
        cur.execute("SELECT id FROM roles WHERE name = %s;", (payload.role_name,))
        r = cur.fetchone()
        if r:
            role_id = r['id']

    if not role_id:
        cur.execute("SELECT id FROM roles WHERE name = 'User';")
        r = cur.fetchone()
        role_id = r['id'] if r else None

    local_part = email.split("@")[0]
    full_name = local_part.replace(".", " ").title()

    cur.execute("""
        INSERT INTO users (email, password_hash, full_name, role_id, department_id, is_active)
        VALUES (%s, NULL, %s, %s, %s, TRUE)
        ON CONFLICT (email) DO UPDATE SET
            role_id = COALESCE(EXCLUDED.role_id, users.role_id),
            department_id = COALESCE(EXCLUDED.department_id, users.department_id),
            updated_at = NOW()
        RETURNING id, email, full_name;
    """, (email, full_name, role_id, dept_id))

    user = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return {"message": "User onboarded successfully", "user": user}
