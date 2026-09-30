#!/bin/bash
# backup_database.sh
# Creates a backup of the current PostgreSQL database before RBAC migration

set -e

BACKUP_DIR="$(cd "$(dirname "$0")" && pwd)/backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/soc_jira_backup_${TIMESTAMP}.sql"

echo "🔄 Creating database backup..."

# Create backups directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# Execute backup from Docker container
docker exec jira-db pg_dump -U soc_jira -d soc_jira > "$BACKUP_FILE"

# Compress backup
gzip "$BACKUP_FILE"
COMPRESSED_FILE="${BACKUP_FILE}.gz"

echo "✅ Database backup created: $COMPRESSED_FILE"
echo "   Size: $(du -h "$COMPRESSED_FILE" | cut -f1)"

# List recent backups
echo ""
echo "📋 Recent backups:"
ls -lh "$BACKUP_DIR"/*.gz 2>/dev/null | tail -5 || echo "   No previous backups found"

echo ""
echo "To restore from backup:"
echo "  gunzip $COMPRESSED_FILE"
echo "  docker exec -i jira-db psql -U soc_jira -d soc_jira < $BACKUP_FILE"
