#!/bin/sh
# Ensure appuser owns the image cache directory (volume mounts override Dockerfile chown)
chown -R appuser:appgroup /app/images

# Drop privileges and exec the CMD
exec su -s /bin/sh appuser -c "$*"
