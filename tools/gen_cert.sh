#!/usr/bin/env bash
# Create a self-signed certificate for optional TLS encryption:
#   ./tools/gen_cert.sh            -> cert.pem + key.pem in the current folder
#   python run_server.py --tls-cert cert.pem --tls-key key.pem
#   (students tick "Encrypted (TLS)" or run the client with --tls)
# Needs openssl (Linux/macOS built in; on Windows it comes with Git for Windows).
set -euo pipefail
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=classboard" \
        -keyout key.pem -out cert.pem
echo "Created cert.pem and key.pem"
