#!/bin/bash
# Fix permissions on bind-mounted plugins directory
# Host dir may be owned by a different uid than container's metabase user
chown -R metabase:metabase /app/plugins 2>/dev/null || true

# Drop privileges and run Metabase
exec gosu metabase java -jar /app/metabase.jar "$@"
