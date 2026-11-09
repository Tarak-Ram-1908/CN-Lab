@echo off
REM Self-signed certificate for optional TLS (needs openssl, e.g. from Git for Windows).
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=classboard" -keyout key.pem -out cert.pem
echo Created cert.pem and key.pem.  Start the server with:
echo   python run_server.py --tls-cert cert.pem --tls-key key.pem
pause
