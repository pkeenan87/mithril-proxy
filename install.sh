
#!/usr/bin/env bash
# install.sh — bootstrap mithril-proxy on a fresh Raspberry Pi OS install
set -euo pipefail

INSTALL_DIR=/opt/mithril-proxy
CONFIG_DIR=/etc/mithril-proxy
LOG_DIR=/var/log/mithril-proxy
CACHE_DIR=/var/cache/mithril-proxy
SERVICE_NAME=mithril-proxy
SERVICE_USER=mithril

# --------------------------------------------------------------------------- #
# 1. System user
# --------------------------------------------------------------------------- #
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "[+] Creating system user '$SERVICE_USER'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
else
    echo "[=] User '$SERVICE_USER' already exists."
fi

# --------------------------------------------------------------------------- #
# 2. Install directory
# --------------------------------------------------------------------------- #
echo "[+] Creating install directory $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"

# Copy project files (excluding .git, __pycache__, *.pyc)
rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='*.egg-info' --exclude='.venv' --exclude='venv' \
    "$(dirname "$0")/" "$INSTALL_DIR/"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# --------------------------------------------------------------------------- #
# 3. Python virtual environment + dependencies
# --------------------------------------------------------------------------- #
echo "[+] Setting up Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# --------------------------------------------------------------------------- #
# 4. Log directory
# --------------------------------------------------------------------------- #
echo "[+] Creating log directory $LOG_DIR..."
mkdir -p "$LOG_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"

# --------------------------------------------------------------------------- #
# 4b. npm cache directory (used by npx in stdio destinations)
# --------------------------------------------------------------------------- #
echo "[+] Creating npm cache directory $CACHE_DIR..."
mkdir -p "$CACHE_DIR/.npm"
chown -R "$SERVICE_USER:$SERVICE_USER" "$CACHE_DIR"

# --------------------------------------------------------------------------- #
# 5. Config directory + env file
# --------------------------------------------------------------------------- #
echo "[+] Setting up config directory $CONFIG_DIR..."
mkdir -p "$CONFIG_DIR"

ENV_FILE="$CONFIG_DIR/env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'EOF'
# mithril-proxy environment configuration
LOG_FILE=/var/log/mithril-proxy/proxy.log
DESTINATIONS_CONFIG=/etc/mithril-proxy/destinations.yml
PATTERNS_DIR=/etc/mithril-proxy/patterns.d
PYTHONPATH=/opt/mithril-proxy/src
NPM_CONFIG_CACHE=/var/cache/mithril-proxy/.npm
EOF
    echo "[+] Created $ENV_FILE with defaults."
else
    echo "[=] $ENV_FILE already exists — not overwritten."
fi

# --------------------------------------------------------------------------- #
# 6. Destinations config
# --------------------------------------------------------------------------- #
DEST_FILE="$CONFIG_DIR/destinations.yml"
if [ ! -f "$DEST_FILE" ]; then
    cp "$INSTALL_DIR/config/destinations.yml" "$DEST_FILE"
    echo "[+] Copied destinations.yml to $DEST_FILE."
    echo "    -> Edit $DEST_FILE to add your MCP destinations, then restart the service."
else
    echo "[=] $DEST_FILE already exists — not overwritten."
fi

# --------------------------------------------------------------------------- #
# 6b. Detection patterns
# --------------------------------------------------------------------------- #
PATTERNS_DIR="$CONFIG_DIR/patterns.d"
echo "[+] Setting up detection patterns directory $PATTERNS_DIR..."
mkdir -p "$PATTERNS_DIR"

# Seed default patterns if the directory is empty (first install)
if [ -z "$(ls -A "$PATTERNS_DIR" 2>/dev/null)" ]; then
    cp "$INSTALL_DIR/config/patterns.d/"*.conf "$PATTERNS_DIR/" 2>/dev/null || true
    echo "[+] Seeded default detection patterns."
    echo "    -> Edit files in $PATTERNS_DIR to customise, then reload:"
    echo "       curl -X POST http://localhost:3000/admin/reload-patterns"
else
    echo "[=] $PATTERNS_DIR already contains patterns — not overwritten."
fi

chown -R root:"$SERVICE_USER" "$CONFIG_DIR"
chmod 640 "$ENV_FILE" "$DEST_FILE"
chmod 750 "$PATTERNS_DIR"
find "$PATTERNS_DIR" -type f -exec chmod 640 {} \;

# --------------------------------------------------------------------------- #
# 7. systemd unit file
# --------------------------------------------------------------------------- #
echo "[+] Installing systemd service..."
cp "$INSTALL_DIR/systemd/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo ""
echo "======================================================"
echo " mithril-proxy installed and started."
echo " Check status:  systemctl status $SERVICE_NAME"
echo " Logs:          journalctl -u $SERVICE_NAME -f"
echo "                tail -f $LOG_DIR/proxy.log | jq"
echo " Edit destinations: $DEST_FILE"
echo " After editing: systemctl restart $SERVICE_NAME"
echo "======================================================"
