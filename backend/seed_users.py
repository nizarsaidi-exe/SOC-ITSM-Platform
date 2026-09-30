#!/usr/bin/env python3
"""
seed_users.py
Purpose: Seed RBAC database with Owner account and test users
Run AFTER migration: 001_create_rbac_schema.sql

Usage:
  python3 backend/seed_users.py
"""

import os
import sys
import psycopg2
from psycopg2.extras import RealDictCursor
import bcrypt

# Database connection parameters
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME", "soc_jira")
DB_USER = os.getenv("DB_USER", "soc_jira")
DB_PASS = os.environ["DB_PASS"]
DB_PORT = os.getenv("DB_PORT", "5432")

# Test data — all secrets come from the environment (no defaults).
OWNER_EMAIL = os.environ["SEED_OWNER_EMAIL"]
OWNER_PASSWORD = os.environ["SEED_OWNER_PASSWORD"]

TEST_USERS = [
    {
        "email": os.environ["TEST_USER_SOC_EMAIL"],
        "password": os.environ["TEST_USER_SOC_PASSWORD"],
        "full_name": os.getenv("TEST_USER_SOC_FULL_NAME", "SOC Analyst"),
        "department": os.getenv("TEST_USER_SOC_DEPARTMENT", "SOC"),
        "role": os.getenv("TEST_USER_SOC_ROLE", "SOC Analyst"),
    },
    {
        "email": os.environ["TEST_USER_NOC_EMAIL"],
        "password": os.environ["TEST_USER_NOC_PASSWORD"],
        "full_name": os.getenv("TEST_USER_NOC_FULL_NAME", "NOC Analyst"),
        "department": os.getenv("TEST_USER_NOC_DEPARTMENT", "NOC"),
        "role": os.getenv("TEST_USER_NOC_ROLE", "NOC Analyst"),
    },
    {
        "email": os.environ["TEST_USER_IT_SUPPORT_EMAIL"],
        "password": os.environ["TEST_USER_IT_SUPPORT_PASSWORD"],
        "full_name": os.getenv("TEST_USER_IT_SUPPORT_FULL_NAME", "IT Support Agent"),
        "department": os.getenv("TEST_USER_IT_SUPPORT_DEPARTMENT", "IT Support"),
        "role": os.getenv("TEST_USER_IT_SUPPORT_ROLE", "IT Support Agent"),
    },
]

def hash_password(password: str) -> str:
    """Hash password using bcrypt"""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def mask_email(value: str) -> str:
    if not value:
        return "***"
    local, sep, domain = str(value).strip().lower().partition("@")
    if not sep:
        return local[:2] + "***" if len(local) > 2 else "**"
    return f"{local[:2]}***@{domain}"

def get_db_connection():
    """Create and return database connection"""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            cursor_factory=RealDictCursor
        )
        return conn
    except Exception as e:
        print(f"❌ Failed to connect to database: {e}")
        sys.exit(1)

def seed_owner_account(conn):
    """Create Owner / Super Admin account"""
    print(f"\n📝 Creating Owner account for {mask_email(OWNER_EMAIL)}")

    cur = conn.cursor()

    try:
        # Get Owner role ID
        cur.execute("SELECT id FROM roles WHERE name = 'Owner / Super Admin'")
        role_result = cur.fetchone()
        if not role_result:
            print("❌ Owner / Super Admin role not found!")
            return False

        owner_role_id = role_result['id']

        # Get IT Administration department ID (default dept for Owner)
        cur.execute("SELECT id FROM departments WHERE code = 'IT_ADMIN'")
        dept_result = cur.fetchone()
        if not dept_result:
            print("❌ IT Administration department not found!")
            return False

        owner_dept_id = dept_result['id']

        # Hash password
        password_hash = hash_password(OWNER_PASSWORD)

        # Check if Owner already exists
        cur.execute("SELECT id FROM users WHERE email = %s", (OWNER_EMAIL,))
        existing = cur.fetchone()

        if existing:
            print(f"⚠️  Owner account already exists (ID: {existing['id']})")
            print("   If you need to reset the Owner account, use the approved admin reset flow.")
            return True

        # Create Owner account
        cur.execute(
            """
            INSERT INTO users (email, password_hash, full_name, department_id, role_id, is_active, is_owner)
            VALUES (%s, %s, %s, %s, %s, true, true)
            RETURNING id, email, full_name, role_id, department_id
            """,
            (OWNER_EMAIL, password_hash, "System Administrator", owner_dept_id, owner_role_id)
        )

        user = cur.fetchone()
        conn.commit()

        print("✅ Owner account created:")
        print(f"   ID: {user['id']}")
        print(f"   Email: {mask_email(user['email'])}")
        print(f"   Name: {user['full_name']}")
        print(f"   Department ID: {user['department_id']}")
        print(f"   Role ID: {user['role_id']} (Owner / Super Admin)")
        print("   Secret values are kept out of stdout logs.")

        cur.close()
        return True

    except Exception as e:
        print(f"❌ Error creating Owner account: {e}")
        conn.rollback()
        return False

def seed_test_users(conn):
    """Create test users for demonstration"""
    print(f"\n📝 Creating test users...")

    cur = conn.cursor()

    for test_user in TEST_USERS:
        try:
            # Get role ID
            cur.execute("SELECT id FROM roles WHERE name = %s", (test_user['role'],))
            role_result = cur.fetchone()
            if not role_result:
                print(f"⚠️  Role '{test_user['role']}' not found, skipping user")
                continue

            role_id = role_result['id']

            # Get department ID
            cur.execute("SELECT id FROM departments WHERE name = %s", (test_user['department'],))
            dept_result = cur.fetchone()
            if not dept_result:
                print(f"⚠️  Department '{test_user['department']}' not found, skipping user")
                continue

            dept_id = dept_result['id']

            # Check if user already exists
            cur.execute("SELECT id FROM users WHERE email = %s", (test_user['email'],))
            existing = cur.fetchone()

            if existing:
                print(f"⚠️  User {mask_email(test_user['email'])} already exists")
                continue

            # Hash password
            password_hash = hash_password(test_user['password'])

            # Create user
            cur.execute(
                """
                INSERT INTO users (email, password_hash, full_name, department_id, role_id, is_active, is_owner)
                VALUES (%s, %s, %s, %s, %s, true, false)
                RETURNING id, email, full_name
                """,
                (test_user['email'], password_hash, test_user['full_name'], dept_id, role_id)
            )

            user = cur.fetchone()
            conn.commit()

            print(f"   ✅ {mask_email(test_user['email'])} ({test_user['full_name']})")
            print(f"      Department: {test_user['department']}, Role: {test_user['role']}")
            print("      Secret values are kept out of stdout logs.")

        except Exception as e:
            print(f"   ❌ Error creating user {mask_email(test_user['email'])}: {e}")
            conn.rollback()

    cur.close()

def verify_schema(conn):
    """Verify RBAC schema was created correctly"""
    print(f"\n🔍 Verifying RBAC schema...")

    cur = conn.cursor()

    checks = [
        ("departments", "SELECT COUNT(*) as count FROM departments"),
        ("roles", "SELECT COUNT(*) as count FROM roles"),
        ("permissions", "SELECT COUNT(*) as count FROM permissions"),
        ("role_permissions", "SELECT COUNT(*) as count FROM role_permissions"),
        ("users", "SELECT COUNT(*) as count FROM users"),
    ]

    all_good = True
    for table_name, query in checks:
        try:
            cur.execute(query)
            result = cur.fetchone()
            count = result['count']
            status = "✅" if count > 0 else "⚠️ "
            print(f"   {status} {table_name}: {count} rows")
        except Exception as e:
            print(f"   ❌ {table_name}: Error - {e}")
            all_good = False

    cur.close()
    return all_good

def main():
    print("=" * 60)
    print("RBAC USER SEEDING SCRIPT")
    print("=" * 60)

    # Connect to database
    print(f"\n🔗 Connecting to database...")
    print(f"   Host: {DB_HOST}:{DB_PORT}")
    print(f"   Database: {DB_NAME}")
    print(f"   User: {DB_USER}")

    conn = get_db_connection()
    print(f"✅ Connected")

    # Verify schema
    if not verify_schema(conn):
        print("\n❌ RBAC schema is incomplete. Run migration first:")
        print("   docker exec jira-db psql -U soc_jira -d soc_jira < /path/to/001_create_rbac_schema.sql")
        conn.close()
        sys.exit(1)

    # Seed Owner account
    if not seed_owner_account(conn):
        print("❌ Failed to create Owner account")
        conn.close()
        sys.exit(1)

    # Seed test users
    seed_test_users(conn)

    conn.close()

    print("\n" + "=" * 60)
    print("✅ SEEDING COMPLETE")
    print("=" * 60)
    print("\nYou can now test login with:")
    print(f"  Email: {mask_email(OWNER_EMAIL)}")
    print("  Password: [redacted]")
    print("\nOr with test users:")
    for user in TEST_USERS:
        print(f"  Email: {mask_email(user['email'])}")
        print("  Password: [redacted]")

if __name__ == "__main__":
    main()
