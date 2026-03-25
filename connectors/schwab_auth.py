"""
Schwab OAuth2 Authentication Module
Handles initial auth, token exchange, and auto-refresh.
"""

import os
import time
import json
import threading
import requests
import base64
import urllib.parse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TOKEN_FILE = os.path.join(os.path.dirname(__file__), '.schwab_tokens.json')


class SchwabAuth:
    def __init__(self):
        self.app_key = os.getenv('SCHWAB_APP_KEY')
        self.app_secret = os.getenv('SCHWAB_APP_SECRET')
        self.callback_url = os.getenv('SCHWAB_CALLBACK_URL', 'https://127.0.0.1:8080/callback')
        self.access_token = None
        self.refresh_token = None
        self.token_expiry = 0
        self.base_url = 'https://api.schwabapi.com'

        if not self.app_key or not self.app_secret:
            raise ValueError("SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set in .env")

        # Try to load saved tokens
        if self._load_tokens():
            print(f"[AUTH] Loaded saved tokens. Refreshing...")
            try:
                self._refresh()
                print(f"[AUTH] ✅ Token refreshed successfully")
                self._start_auto_refresh()
            except Exception as e:
                print(f"[AUTH] ⚠️  Refresh failed ({e}). Need new authorization.")
                self._prompt_authorization()
        else:
            self._prompt_authorization()

    def _get_auth_header(self):
        """Base64 encoded app_key:app_secret for token requests"""
        credentials = f"{self.app_key}:{self.app_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    def get_authorization_url(self):
        """Generate the URL the user needs to visit to authorize the app"""
        params = {
            'client_id': self.app_key,
            'redirect_uri': self.callback_url,
            'response_type': 'code'
        }
        return f"{self.base_url}/v1/oauth/authorize?{urllib.parse.urlencode(params)}"

    def exchange_code(self, authorization_code):
        """Exchange the authorization code for access + refresh tokens"""
        resp = requests.post(
            f"{self.base_url}/v1/oauth/token",
            headers={
                'Authorization': self._get_auth_header(),
                'Content-Type': 'application/x-www-form-urlencoded'
            },
            data={
                'grant_type': 'authorization_code',
                'code': authorization_code,
                'redirect_uri': self.callback_url
            }
        )

        if resp.status_code != 200:
            raise Exception(f"Token exchange failed: {resp.status_code} - {resp.text}")

        data = resp.json()
        self.access_token = data['access_token']
        self.refresh_token = data['refresh_token']
        self.token_expiry = time.time() + data.get('expires_in', 1800)
        self._save_tokens()
        self._start_auto_refresh()
        print(f"[AUTH] ✅ Authorization successful. Tokens saved.")

    def _refresh(self):
        """Refresh the access token using the refresh token"""
        if not self.refresh_token:
            raise Exception("No refresh token available")

        resp = requests.post(
            f"{self.base_url}/v1/oauth/token",
            headers={
                'Authorization': self._get_auth_header(),
                'Content-Type': 'application/x-www-form-urlencoded'
            },
            data={
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token
            }
        )

        if resp.status_code != 200:
            raise Exception(f"Token refresh failed: {resp.status_code} - {resp.text}")

        data = resp.json()
        self.access_token = data['access_token']
        # Schwab gives a new refresh token each time
        if 'refresh_token' in data:
            self.refresh_token = data['refresh_token']
        self.token_expiry = time.time() + data.get('expires_in', 1800)
        self._save_tokens()

    def _start_auto_refresh(self):
        """Start background thread to refresh token every 25 minutes"""
        def refresh_loop():
            while True:
                # Sleep 25 min (5 min before the 30-min expiry)
                time.sleep(25 * 60)
                try:
                    self._refresh()
                    now = datetime.now().strftime('%H:%M:%S')
                    print(f"[AUTH] 🔄 Token auto-refreshed at {now}")
                except Exception as e:
                    print(f"[AUTH] ❌ Auto-refresh failed: {e}")
                    # Retry in 2 minutes
                    time.sleep(120)
                    try:
                        self._refresh()
                        print(f"[AUTH] 🔄 Retry successful")
                    except Exception as e2:
                        print(f"[AUTH] ❌ Retry also failed: {e2}")

        t = threading.Thread(target=refresh_loop, daemon=True)
        t.start()

    def _save_tokens(self):
        """Save tokens to disk for restart survival"""
        data = {
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'token_expiry': self.token_expiry,
            'saved_at': datetime.now().isoformat()
        }
        with open(TOKEN_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def _load_tokens(self):
        """Load previously saved tokens"""
        if not os.path.exists(TOKEN_FILE):
            return False
        try:
            with open(TOKEN_FILE, 'r') as f:
                data = json.load(f)
            self.access_token = data.get('access_token')
            self.refresh_token = data.get('refresh_token')
            self.token_expiry = data.get('token_expiry', 0)
            return bool(self.refresh_token)
        except Exception:
            return False

    def _prompt_authorization(self):
        """Authorization flow with automatic local callback server"""
        import ssl
        import webbrowser
        from http.server import HTTPServer, BaseHTTPRequestHandler

        auth_url = self.get_authorization_url()
        auth_instance = self  # Reference for the handler

        # Store the captured code
        captured = {'code': None, 'error': None}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self_handler):
                """Handle the OAuth callback redirect"""
                parsed = urllib.parse.urlparse(self_handler.path)
                params = urllib.parse.parse_qs(parsed.query)

                if 'code' in params:
                    captured['code'] = urllib.parse.unquote(params['code'][0])
                    # Send success page to browser
                    self_handler.send_response(200)
                    self_handler.send_header('Content-Type', 'text/html')
                    self_handler.end_headers()
                    self_handler.wfile.write(b"""
                    <html><body style="font-family:Arial;text-align:center;padding:50px;background:#1a1a2e;color:#e0e0e0">
                    <h1 style="color:#00d4aa">&#10003; Schwab Authorization Successful!</h1>
                    <p>You can close this tab and return to your terminal.</p>
                    </body></html>""")
                else:
                    captured['error'] = 'No authorization code in callback'
                    self_handler.send_response(400)
                    self_handler.send_header('Content-Type', 'text/html')
                    self_handler.end_headers()
                    self_handler.wfile.write(b"<html><body><h1>Error: No code received</h1></body></html>")

            def log_message(self_handler, format, *args):
                """Suppress default HTTP logging"""
                pass

        # Parse callback URL to get port
        parsed_callback = urllib.parse.urlparse(self.callback_url)
        port = parsed_callback.port or 8080

        # Create HTTPS server with self-signed cert
        server = HTTPServer(('127.0.0.1', port), CallbackHandler)

        # Generate a temporary self-signed certificate
        import tempfile
        import subprocess
        cert_file = tempfile.NamedTemporaryFile(suffix='.pem', delete=False)
        key_file = tempfile.NamedTemporaryFile(suffix='.pem', delete=False)
        cert_file.close()
        key_file.close()

        try:
            subprocess.run([
                'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                '-keyout', key_file.name, '-out', cert_file.name,
                '-days', '1', '-nodes', '-batch',
                '-subj', '/CN=localhost'
            ], capture_output=True, check=True)

            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_file.name, key_file.name)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        except Exception as e:
            print(f"[AUTH] ⚠️  Could not create HTTPS server ({e}), trying HTTP fallback...")
            # Fall back - the server is already created as HTTP
            server = HTTPServer(('127.0.0.1', port), CallbackHandler)

        print("\n" + "=" * 60)
        print("  SCHWAB AUTHORIZATION")
        print("=" * 60)
        print(f"\n🌐 Local callback server started on port {port}")
        print(f"\n📎 Opening browser for Schwab login...\n")
        print(f"   If browser doesn't open, visit this URL:\n")
        print(f"   {auth_url}\n")
        print("⏳ Waiting for authorization callback...\n")

        # Open browser automatically
        webbrowser.open(auth_url)

        # Handle one request (the callback)
        server.handle_request()
        server.server_close()

        # Clean up temp certs
        try:
            os.unlink(cert_file.name)
            os.unlink(key_file.name)
        except Exception:
            pass

        if captured['error']:
            raise Exception(f"Authorization failed: {captured['error']}")

        if not captured['code']:
            raise Exception("No authorization code received")

        print(f"[AUTH] 🎫 Got authorization code, exchanging for tokens...")
        self.exchange_code(captured['code'])

    def get_headers(self):
        """Get authorization headers for API calls"""
        if not self.access_token:
            raise Exception("Not authenticated. Run authorization first.")
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Accept': 'application/json'
        }

    def is_authenticated(self):
        """Check if we have a valid access token"""
        return self.access_token is not None and time.time() < self.token_expiry

    def get(self, endpoint, params=None):
        """Make authenticated GET request to Schwab API"""
        url = f"{self.base_url}{endpoint}"
        resp = requests.get(url, headers=self.get_headers(), params=params)
        if resp.status_code == 401:
            # Try refreshing token
            print("[AUTH] Got 401, attempting token refresh...")
            self._refresh()
            resp = requests.get(url, headers=self.get_headers(), params=params)
        if resp.status_code != 200:
            raise Exception(f"API request failed: {resp.status_code} - {resp.text}")
        return resp.json()


if __name__ == '__main__':
    # Run this to do initial authorization
    auth = SchwabAuth()
    if auth.is_authenticated():
        print("\n✅ Authenticated and ready!")
        # Quick test
        try:
            data = auth.get('/marketdata/v1/quotes', params={'symbols': 'AAPL'})
            print(f"Test quote: AAPL = {data}")
        except Exception as e:
            print(f"Test failed: {e}")
