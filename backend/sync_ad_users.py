#!/usr/bin/env python3
"""
Active Directory sync + IAM cleanup for the SOC & ITSM platform.

WHAT IT DOES
------------
1. Connects to the corporate LDAPS directory (self-signed cert tolerated).
2. Enumerates enabled AD user accounts.
3. Maps each AD user to a platform account:
     - role     : 'Helpdesk User' when the account belongs to an AD group listed
                  in LDAP_HELPDESK_GROUPS, otherwise 'User'.
     - dept     : matched by AD `department` attribute against departments.code/name.
     - email    : AD `mail`, else <sAMAccountName>@<LDAP_DOMAIN>.
     - password : left NULL (zero-trust) - first-login setup or passwordless login.
4. Cleanup (the aggressive part):
     - DELETES every existing platform user EXCEPT the protected owner
       (owner@example.com), then re-creates/updates the AD users.

SAFETY
------
Defaults to DRY-RUN. Use `--apply` to actually delete + write.
Passwords are NEVER read or written.

ENV
---
  LDAP_SERVER           host of the LDAPS server          (REQUIRED, no default)
  LDAP_PORT             default 636
  LDAP_BIND_DN          AD service-account bind DN        (REQUIRED, no default)
  LDAP_SERVICE_PASSWORD bind password (env, or prompted interactively)
  LDAP_BASE_DN          search base DN                    (REQUIRED, no default)
  LDAP_FILTER           default (objectClass=user)
  LDAP_DOMAIN           AD domain for default emails      (REQUIRED, no default)
  LDAP_HELPDESK_GROUPS  comma-separated AD group DNs -> 'Helpdesk User' role
  LDAP_DEFAULT_ROLE     default 'User'
  LDAP_IGNORE_SAM       comma-separated sAMAccountNames to skip (default ldap_service)
  LDAP_ADMIN_GROUP_DN   optional: if set, members are 'User' role (max rank for AD)
  PROTECTED_EMAIL       owner that is never deleted       (REQUIRED, no default)
  DB_HOST/DB_NAME/DB_USER  database connection defaults match main.py
  DB_PASS               database password                 (REQUIRED, no default)

USAGE
-----
  python sync_ad_users.py [--apply] [--resolve-groups]
"""
import os
import sys
import ssl
import getpass
import psycopg2
from psycopg2.extras import RealDictCursor

DB_HOST = os.getenv("DB_HOST", "jira-db")
DB_NAME = os.getenv("DB_NAME", "soc_jira")
DB_USER = os.getenv("DB_USER", "soc_jira")
DB_PASS = os.environ["DB_PASS"]
DB_SSLMODE = os.getenv("DB_SSLMODE", "")

LDAP_SERVER = os.environ["LDAP_SERVER"]
LDAP_PORT = int(os.getenv("LDAP_PORT", "636"))
LDAP_BIND_DN = os.environ["LDAP_BIND_DN"]
LDAP_SERVICE_PASSWORD = os.getenv("LDAP_SERVICE_PASSWORD", "")
LDAP_BASE_DN = os.environ["LDAP_BASE_DN"]
LDAP_FILTER = os.getenv("LDAP_FILTER", "(objectClass=user)")
LDAP_DOMAIN = os.environ["LDAP_DOMAIN"]
LDAP_HELPDESK_GROUPS = {g.strip().lower() for g in os.getenv("LDAP_HELPDESK_GROUPS", "").split(",") if g.strip()}
LDAP_DEFAULT_ROLE = os.getenv("LDAP_DEFAULT_ROLE", "User")
LDAP_IGNORE_SAM = {s.strip().lower() for s in os.getenv("LDAP_IGNORE_SAM", "ldap_service").split(",") if s.strip()}
PROTECTED_EMAIL = os.getenv("PROTECTED_EMAIL", os.getenv("SEED_OWNER_EMAIL", "owner@example.com")).lower()
LDAP_CA_CERT_FILE = os.getenv("LDAP_CA_CERT_FILE", "/app/certs/ad-ca.pem")

AD_ATTRS = [
    "sAMAccountName", "displayName", "cn", "givenName", "sn",
    "mail", "userPrincipalName", "department", "memberOf",
    "userAccountControl", "objectClass",
]

# Common converted attributes for computing default display names
DEFAULT_AVATAR = ""


def mask_email(value):
    if not value:
        return "***"
    local, sep, domain = str(value).strip().lower().partition("@")
    if not sep:
        return local[:2] + "***" if len(local) > 2 else "**"
    return f"{local[:2]}***@{domain}"


def first_attr(attrs, key):
    """Safely return the first value of an LDAP attribute ([] if attribute absent)."""
    values = attrs.get(key, [])
    return values[0] if values else ""


def db_connect():
    kwargs = {
        "host": DB_HOST,
        "database": DB_NAME,
        "user": DB_USER,
        "password": DB_PASS,
        "cursor_factory": RealDictCursor,
    }
    if DB_SSLMODE:
        kwargs["sslmode"] = DB_SSLMODE
    return psycopg2.connect(**kwargs)


def banner(conn):
    cur = conn.cursor()
    cur.execute("SELECT name, id FROM roles WHERE name IN ('User', 'Helpdesk User') ORDER BY rank;")
    rows = cur.fetchall()
    cur.close()
    if len(rows) < 2:
        print("[WARN] Expected 'User' + 'Helpdesk User' roles. Has the API started its migration yet?")
    for r in rows:
        print(f"  role: '{r['name']}' (id {r['id']})")


def fetch_dept_map(conn):
    """Return {name_lower: id, code_lower: id} lookups."""
    cur = conn.cursor()
    cur.execute("SELECT id, name, code FROM departments;")
    rows = cur.fetchall()
    cur.close()
    lookup = {}
    for r in rows:
        if r["name"]:
            lookup[r["name"].strip().lower()] = r["id"]
        if r["code"]:
            lookup[r["code"].strip().lower()] = r["id"]
    return lookup


def fetch_role_ids(conn):
    cur = conn.cursor()
    cur.execute("SELECT name, id FROM roles WHERE name IN ('User', 'Helpdesk User');")
    rows = {r["name"]: r["id"] for r in cur.fetchall()}
    cur.close()
    return rows


def disabled_account(entry_uac):
    """AD userAccountControl bit 2 (0x2) => account disabled."""
    try:
        return bool(int(entry_uac) & 0x2)
    except (TypeError, ValueError):
        return False


def ad_users(server, bind_dn, password):
    """Return list of dicts for enabled, non-ignored, non-computer AD users."""
    try:
        from ldap3 import Server, Tls, Connection
    except ImportError:
        print("[ERROR] ldap3 is not installed. Add it to requirements.txt and rebuild the API image.")
        sys.exit(3)

    if not os.path.isfile(LDAP_CA_CERT_FILE):
        raise FileNotFoundError(f"LDAP CA certificate file is missing: {LDAP_CA_CERT_FILE}")
    tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=LDAP_CA_CERT_FILE)
    srv = Server(host=server, port=LDAP_PORT, use_ssl=True, tls=tls, connect_timeout=10, get_info="ALL")
    print(f"[LDAP] connecting to ldaps://{server}:{LDAP_PORT} with required certificate validation ...")
    conn = Connection(srv, user=bind_dn, password=password, auto_bind=True, receive_timeout=15)
    print(f"[LDAP] bound as {bind_dn}")

    conn.search(search_base=LDAP_BASE_DN, search_scope="SUBTREE",
                search_filter=LDAP_FILTER, attributes=AD_ATTRS, paged_size=500)
    entries = list(conn.entries)

    users = []
    for e in entries:
        attrs = e.entry_attributes_as_dict
        sam = first_attr(attrs, "sAMAccountName")
        if not sam:
            continue
        if sam.lower() in LDAP_IGNORE_SAM:
            continue
        if sam.endswith("$"):  # computer accounts
            continue
        if disabled_account(first_attr(attrs, "userAccountControl")):
            continue
        mail = first_attr(attrs, "mail")
        email = mail if mail else f"{sam}@{LDAP_DOMAIN}".lower()
        display_name = first_attr(attrs, "displayName")
        cn = first_attr(attrs, "cn")
        given = first_attr(attrs, "givenName")
        surname = first_attr(attrs, "sn")
        full_name = display_name or cn or (f"{given} {surname}".strip()) or sam
        member_of = [str(m).strip().lower() for m in attrs.get("memberOf", []) if str(m).strip()]
        dept_attr = first_attr(attrs, "department")
        users.append({
            "email": email.lower(),
            "sam": sam,
            "full_name": full_name,
            "department_attr": dept_attr,
            "member_of": member_of,
        })
    conn.unbind()
    return users


def role_for(user, role_ids, helpdesk_role, default_role):
    """Helpdesk User if in an AD group listed in LDAP_HELPDESK_GROUPS else default."""
    if any(g in user["member_of"] for g in LDAP_HELPDESK_GROUPS):
        return helpdesk_role
    return default_role


def dept_for(user, dept_map):
    key = user["department_attr"].strip().lower()
    return dept_map.get(key) if key else None


def main():
    apply = "--apply" in sys.argv

    print("=" * 72)
    print("Active Directory sync + IAM cleanup")
    print(f"  mode        : {'APPLY (destructive!)' if apply else 'DRY-RUN'}")
    print(f"  protected   : {mask_email(PROTECTED_EMAIL)}")
    print("=" * 72)

    if not LDAP_SERVICE_PASSWORD:
        LDAP_SERVICE_PASSWORD_global = getpass.getpass("LDAP bind password: ")
    else:
        LDAP_SERVICE_PASSWORD_global = LDAP_SERVICE_PASSWORD
    if not LDAP_SERVICE_PASSWORD_global:
        print("[ERROR] LDAP bind password required (env LDAP_SERVICE_PASSWORD)")
        sys.exit(2)

    users = ad_users(LDAP_SERVER, LDAP_BIND_DN, LDAP_SERVICE_PASSWORD_global)
    print(f"[LDAP] {len(users)} enabled user accounts discovered")

    try:
        conn = db_connect()
    except Exception as exc:
        print(f"[ERROR] database connection failed: {exc}")
        sys.exit(4)

    banner(conn)
    role_ids = fetch_role_ids(conn)
    helpdesk_id = role_ids.get("Helpdesk User")
    default_id = role_ids.get("User")
    if not helpdesk_id or not default_id:
        print("[ERROR] Required roles missing. Start the API container once so migrations run.")
        conn.close()
        sys.exit(4)
    dept_map = fetch_dept_map(conn)

    cur = conn.cursor()
    cur.execute("SELECT id, email, is_owner FROM users ORDER BY id;")
    existing = cur.fetchall()
    cur.close()

    to_delete = [u for u in existing if u["email"].lower() != PROTECTED_EMAIL and not u.get("is_owner")]
    owner_row = next((u for u in existing if u["email"].lower() == PROTECTED_EMAIL), None)

    print(f"\n[DB] current users      : {len(existing)}")
    print(f"[DB] to delete (not {mask_email(PROTECTED_EMAIL)}) : {len(to_delete)}")
    if not owner_row:
        print("[WARN] protected owner account does not exist in the database!")

    print(f"\n[SYNC] AD users -> platform accounts")
    planned = []
    for u in users:
        role_name = "Helpdesk User" if role_for(u, role_ids, helpdesk_id, default_id) == helpdesk_id else "User"
        dept_id = dept_for(u, dept_map)
        planned.append({"email": u["email"], "full_name": u["full_name"], "role": role_name, "dept_id": dept_id})
        print(f"  + {mask_email(u['email']):<42} {u['full_name']:<25} role={role_name:<14} dept={dept_id or '-'}")

    if apply:
        if len(to_delete) > 250:
            print(f"[SAFETY] Refusing to delete {len(to_delete)} rows in one pass; reduce scope and retry.")
            conn.close();
            sys.exit(5)
        cur = conn.cursor()
        for u in to_delete:
            print(f"[APPLY] DELETE user {mask_email(u['email'])}")
            cur.execute("DELETE FROM users WHERE id = %s AND is_owner = FALSE;", (u["id"],))
        conn.commit()

        for p in planned:
            cur.execute("""
                INSERT INTO users (email, password_hash, full_name, avatar, role_id, department_id, is_active, is_owner)
                VALUES (%s, NULL, %s, %s, %s, %s, TRUE, FALSE)
                ON CONFLICT (email) DO UPDATE SET
                    full_name = EXCLUDED.full_name,
                    avatar = EXCLUDED.avatar,
                    role_id = EXCLUDED.role_id,
                    department_id = EXCLUDED.department_id,
                    is_active = TRUE,
                    updated_at = NOW();
            """, (p["email"], p["full_name"], DEFAULT_AVATAR,
                  helpdesk_id if p["role"] == "Helpdesk User" else default_id, p["dept_id"]))
        conn.commit()
        cur.close()
        print(f"\n[APPLY] completed: deleted {len(to_delete)}, upserted {len(planned)} AD users")
    else:
        print(f"\n[DRY-RUN] no changes written. Re-run with --apply to commit.")
        if to_delete:
            print(f"  would delete {len(to_delete)} users:")
            for u in to_delete[:20]:
                print(f"    - {mask_email(u['email'])}")
            if len(to_delete) > 20:
                print(f"    ... and {len(to_delete) - 20} more")
    conn.close()


if __name__ == "__main__":
    main()
