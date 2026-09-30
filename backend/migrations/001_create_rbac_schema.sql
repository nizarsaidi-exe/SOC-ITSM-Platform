-- Migration: 001_create_rbac_schema.sql
-- Purpose: Add RBAC (Role-Based Access Control) tables to support authentication and authorization
-- Safe: Does not modify existing tickets table structure; only adds new tables and optional new columns
-- Reversible: Can be rolled back by dropping new tables and columns

-- ============================================================================
-- STEP 1: Create DEPARTMENTS table
-- ============================================================================
CREATE TABLE IF NOT EXISTS departments (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    code VARCHAR(20) UNIQUE,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_departments_code ON departments(code);
CREATE INDEX IF NOT EXISTS idx_departments_is_active ON departments(is_active);

-- ============================================================================
-- STEP 2: Create ROLES table
-- ============================================================================
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    rank INTEGER,
    is_system_role BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_roles_name ON roles(name);
CREATE INDEX IF NOT EXISTS idx_roles_is_system_role ON roles(is_system_role);

-- ============================================================================
-- STEP 3: Create PERMISSIONS table
-- ============================================================================
CREATE TABLE IF NOT EXISTS permissions (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    category VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_permissions_name ON permissions(name);
CREATE INDEX IF NOT EXISTS idx_permissions_category ON permissions(category);

-- ============================================================================
-- STEP 4: Create ROLE_PERMISSIONS junction table
-- ============================================================================
CREATE TABLE IF NOT EXISTS role_permissions (
    id SERIAL PRIMARY KEY,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(role_id, permission_id)
);

CREATE INDEX IF NOT EXISTS idx_role_permissions_role_id ON role_permissions(role_id);
CREATE INDEX IF NOT EXISTS idx_role_permissions_permission_id ON role_permissions(permission_id);

-- ============================================================================
-- STEP 5: Create USERS table
-- ============================================================================
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    avatar TEXT,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    role_id INTEGER NOT NULL REFERENCES roles(id),
    is_active BOOLEAN DEFAULT true,
    is_owner BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by_id INTEGER REFERENCES users(id),
    updated_by_id INTEGER REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_department_id ON users(department_id);
CREATE INDEX IF NOT EXISTS idx_users_role_id ON users(role_id);
CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);
CREATE INDEX IF NOT EXISTS idx_users_is_owner ON users(is_owner);

-- ============================================================================
-- STEP 6: Modify TICKETS table - Add foreign keys and tracking columns
-- ============================================================================

-- Add owner_id column (references users.id, nullable for existing tickets)
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES users(id);

-- Add owner_department_id column (references departments.id, nullable for existing tickets)
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS owner_department_id INTEGER REFERENCES departments(id);

-- Add created_by_id column (references users.id, nullable for existing tickets)
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS created_by_id INTEGER REFERENCES users(id);

-- Add assigned_user_id column (references users.id, nullable - for future migration away from assigned_user string)
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS assigned_user_id INTEGER REFERENCES users(id);

-- Add indexes for new columns
CREATE INDEX IF NOT EXISTS idx_tickets_owner_id ON tickets(owner_id);
CREATE INDEX IF NOT EXISTS idx_tickets_owner_department_id ON tickets(owner_department_id);
CREATE INDEX IF NOT EXISTS idx_tickets_created_by_id ON tickets(created_by_id);
CREATE INDEX IF NOT EXISTS idx_tickets_assigned_user_id ON tickets(assigned_user_id);

-- ============================================================================
-- STEP 7: Seed DEPARTMENTS
-- ============================================================================
INSERT INTO departments (name, description, code, is_active)
VALUES
    ('Security Operations Center', 'SOC team for security incident management', 'SOC', true),
    ('Network Operations Center', 'NOC team for network monitoring and management', 'NOC', true),
    ('IT Support', 'IT Support and Help Desk', 'IT_SUPPORT', true),
    ('IT Administration', 'IT Administration and Infrastructure', 'IT_ADMIN', true),
    ('Asset Management', 'Asset management and tracking', 'ASSET_MGMT', true),
    ('Change Management', 'Change management and implementation', 'CHANGE_MGMT', true),
    ('Threat Intelligence', 'Threat intelligence and research', 'THREAT_INT', true),
    ('Infrastructure Engineering', 'Infrastructure engineering and design', 'INFRA_ENG', true)
ON CONFLICT (name) DO NOTHING;

-- ============================================================================
-- STEP 8: Seed ROLES
-- ============================================================================
INSERT INTO roles (name, description, rank, is_system_role)
VALUES
    ('Owner / Super Admin', 'Global full access to all departments, modules, users, and settings', 1, true),
    ('IT Administrator', 'IT Administration and user management', 20, true),
    ('IT Support Agent', 'IT Support and help desk', 30, true),
    ('NOC Analyst', 'Network Operations Center analyst', 40, true),
    ('NOC Lead', 'Network Operations Center lead/supervisor', 35, true),
    ('SOC Analyst', 'Security Operations Center analyst', 40, true),
    ('SOC Lead', 'Security Operations Center lead/supervisor', 35, true),
    ('Service Desk Agent', 'Service desk and incident routing', 30, true),
    ('Asset Manager', 'Asset management and tracking', 40, true),
    ('Change Manager', 'Change management and approval', 40, true),
    ('Viewer', 'Read-only access to dashboards and tickets', 50, true)
ON CONFLICT (name) DO NOTHING;

-- ============================================================================
-- STEP 9: Seed PERMISSIONS (Granular, extensible)
-- ============================================================================
INSERT INTO permissions (name, description, category)
VALUES
    -- Dashboard
    ('dashboard.view', 'View dashboard and analytics', 'dashboard'),

    -- Tickets
    ('tickets.view', 'View tickets', 'tickets'),
    ('tickets.create', 'Create new tickets', 'tickets'),
    ('tickets.update', 'Update ticket details', 'tickets'),
    ('tickets.assign', 'Assign tickets to users', 'tickets'),
    ('tickets.close', 'Close/resolve tickets', 'tickets'),

    -- Incidents
    ('incidents.view', 'View incidents', 'incidents'),
    ('incidents.create', 'Create new incidents', 'incidents'),
    ('incidents.update', 'Update incident details', 'incidents'),
    ('incidents.investigate', 'Investigate incidents', 'incidents'),
    ('incidents.approve', 'Approve incident resolution', 'incidents'),

    -- Requests
    ('requests.view', 'View requests', 'requests'),
    ('requests.create', 'Create new requests', 'requests'),
    ('requests.update', 'Update request details', 'requests'),

    -- Problems
    ('problems.view', 'View problems', 'problems'),
    ('problems.create', 'Create new problems', 'problems'),
    ('problems.update', 'Update problem details', 'problems'),

    -- Changes
    ('changes.view', 'View changes', 'changes'),
    ('changes.create', 'Create new changes', 'changes'),
    ('changes.approve', 'Approve changes', 'changes'),

    -- Assets
    ('assets.view', 'View assets', 'assets'),
    ('assets.create', 'Create new assets', 'assets'),
    ('assets.update', 'Update asset details', 'assets'),

    -- Module Access
    ('soc.access', 'Access SOC module', 'access'),
    ('noc.access', 'Access NOC module', 'access'),
    ('itsupport.access', 'Access IT Support module', 'access'),
    ('asset_mgmt.access', 'Access Asset Management module', 'access'),
    ('change_mgmt.access', 'Access Change Management module', 'access'),

    -- Users Management
    ('users.view', 'View users', 'users'),
    ('users.create', 'Create new users', 'users'),
    ('users.update', 'Update user details', 'users'),
    ('users.disable', 'Disable or delete users', 'users'),

    -- Roles Management
    ('roles.view', 'View roles and permissions', 'roles'),
    ('roles.create', 'Create new roles', 'roles'),
    ('roles.update', 'Update role details', 'roles'),
    ('roles.assign', 'Assign roles to users', 'roles'),

    -- Settings
    ('settings.manage', 'Manage system settings', 'settings')
ON CONFLICT (name) DO NOTHING;

-- ============================================================================
-- STEP 10: Assign PERMISSIONS to ROLES
-- ============================================================================

-- Helper: Get role and permission IDs
DO $$
DECLARE
    owner_role_id INT;
    it_admin_role_id INT;
    it_support_role_id INT;
    noc_analyst_role_id INT;
    noc_lead_role_id INT;
    soc_analyst_role_id INT;
    soc_lead_role_id INT;
    service_desk_role_id INT;
    asset_manager_role_id INT;
    change_manager_role_id INT;
    viewer_role_id INT;
BEGIN
    SELECT id INTO owner_role_id FROM roles WHERE name = 'Owner / Super Admin';
    SELECT id INTO it_admin_role_id FROM roles WHERE name = 'IT Administrator';
    SELECT id INTO it_support_role_id FROM roles WHERE name = 'IT Support Agent';
    SELECT id INTO noc_analyst_role_id FROM roles WHERE name = 'NOC Analyst';
    SELECT id INTO noc_lead_role_id FROM roles WHERE name = 'NOC Lead';
    SELECT id INTO soc_analyst_role_id FROM roles WHERE name = 'SOC Analyst';
    SELECT id INTO soc_lead_role_id FROM roles WHERE name = 'SOC Lead';
    SELECT id INTO service_desk_role_id FROM roles WHERE name = 'Service Desk Agent';
    SELECT id INTO asset_manager_role_id FROM roles WHERE name = 'Asset Manager';
    SELECT id INTO change_manager_role_id FROM roles WHERE name = 'Change Manager';
    SELECT id INTO viewer_role_id FROM roles WHERE name = 'Viewer';

    -- OWNER / SUPER ADMIN: ALL PERMISSIONS
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT owner_role_id, id FROM permissions
    ON CONFLICT (role_id, permission_id) DO NOTHING;

    -- IT ADMINISTRATOR
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT it_admin_role_id, id FROM permissions
    WHERE name IN (
        'dashboard.view', 'users.view', 'users.create', 'users.update', 'users.disable',
        'roles.view', 'itsupport.access', 'tickets.view', 'tickets.create', 'tickets.update',
        'requests.view', 'requests.create', 'requests.update', 'settings.manage'
    )
    ON CONFLICT (role_id, permission_id) DO NOTHING;

    -- IT SUPPORT AGENT
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT it_support_role_id, id FROM permissions
    WHERE name IN (
        'dashboard.view', 'itsupport.access', 'tickets.view', 'tickets.create', 'tickets.update', 'tickets.assign',
        'requests.view', 'requests.create', 'requests.update'
    )
    ON CONFLICT (role_id, permission_id) DO NOTHING;

    -- SOC ANALYST
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT soc_analyst_role_id, id FROM permissions
    WHERE name IN (
        'dashboard.view', 'soc.access', 'tickets.view', 'tickets.create', 'tickets.update', 'tickets.assign', 'tickets.close',
        'incidents.view', 'incidents.create', 'incidents.update', 'incidents.investigate'
    )
    ON CONFLICT (role_id, permission_id) DO NOTHING;

    -- SOC LEAD
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT soc_lead_role_id, id FROM permissions
    WHERE name IN (
        'dashboard.view', 'soc.access', 'tickets.view', 'tickets.create', 'tickets.update', 'tickets.assign', 'tickets.close',
        'incidents.view', 'incidents.create', 'incidents.update', 'incidents.investigate', 'incidents.approve',
        'users.view'
    )
    ON CONFLICT (role_id, permission_id) DO NOTHING;

    -- NOC ANALYST
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT noc_analyst_role_id, id FROM permissions
    WHERE name IN (
        'dashboard.view', 'noc.access', 'tickets.view', 'tickets.create', 'tickets.update', 'tickets.assign',
        'problems.view', 'problems.create', 'problems.update', 'changes.view', 'changes.create'
    )
    ON CONFLICT (role_id, permission_id) DO NOTHING;

    -- NOC LEAD
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT noc_lead_role_id, id FROM permissions
    WHERE name IN (
        'dashboard.view', 'noc.access', 'tickets.view', 'tickets.create', 'tickets.update', 'tickets.assign',
        'problems.view', 'problems.create', 'problems.update', 'changes.view', 'changes.create', 'changes.approve',
        'users.view'
    )
    ON CONFLICT (role_id, permission_id) DO NOTHING;

    -- SERVICE DESK AGENT
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT service_desk_role_id, id FROM permissions
    WHERE name IN (
        'dashboard.view', 'tickets.view', 'tickets.create', 'tickets.update',
        'requests.view', 'requests.create', 'requests.update'
    )
    ON CONFLICT (role_id, permission_id) DO NOTHING;

    -- ASSET MANAGER
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT asset_manager_role_id, id FROM permissions
    WHERE name IN (
        'dashboard.view', 'asset_mgmt.access', 'assets.view', 'assets.create', 'assets.update',
        'tickets.view'
    )
    ON CONFLICT (role_id, permission_id) DO NOTHING;

    -- CHANGE MANAGER
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT change_manager_role_id, id FROM permissions
    WHERE name IN (
        'dashboard.view', 'change_mgmt.access', 'changes.view', 'changes.create', 'changes.approve',
        'tickets.view'
    )
    ON CONFLICT (role_id, permission_id) DO NOTHING;

    -- VIEWER
    INSERT INTO role_permissions (role_id, permission_id)
    SELECT viewer_role_id, id FROM permissions
    WHERE name IN (
        'dashboard.view', 'tickets.view', 'incidents.view', 'requests.view'
    )
    ON CONFLICT (role_id, permission_id) DO NOTHING;

END $$;

-- ============================================================================
-- STEP 11: Create OWNER user (SEED_OWNER_EMAIL)
-- Password hash for "admin123" using bcrypt (will be replaced during seeding)
-- Note: This is a placeholder - will be properly hashed during Python seeding
-- ============================================================================
-- DO NOT manually insert here - will be done by Python script with proper bcrypt hashing

-- ============================================================================
-- Migration complete
-- ============================================================================
-- Summary of changes:
-- ✓ Created departments table (8 departments seeded)
-- ✓ Created roles table (11 roles seeded)
-- ✓ Created permissions table (~35 permissions seeded)
-- ✓ Created role_permissions junction table (role->permission mappings)
-- ✓ Created users table (auth, role, department tracking)
-- ✓ Modified tickets table (added owner_id, owner_department_id, created_by_id, assigned_user_id)
-- ✓ All tables have appropriate indexes
-- ✓ All tables use SERIAL for auto-increment IDs
-- ✓ Foreign key constraints ensure referential integrity
-- ✓ Existing tickets table NOT dropped or recreated - data preserved
-- ✓ New columns on tickets are nullable - safe for existing rows
-- ============================================================================
