"""Quick Schwab auth — paste the callback URL after login."""
import urllib.parse, requests, base64, json, time, os, sys, webbrowser
from dotenv import load_dotenv
load_dotenv()

app_key = os.getenv('SCHWAB_APP_KEY')
app_secret = os.getenv('SCHWAB_APP_SECRET')
callback_url = os.getenv('SCHWAB_CALLBACK_URL', 'https://127.0.0.1:8080/callback')

# Build auth URL
params = {
    'client_id': app_key,
    'redirect_uri': callback_url,
    'response_type': 'code'
}
auth_url = f"https://api.schwabapi.com/v1/oauth/authorize?{urllib.parse.urlencode(params)}"

print("\n📎 Opening Schwab login in browser...")
print(f"\n   {auth_url}\n")
webbrowser.open(auth_url)

print("After login, your browser will redirect to a URL starting with:")
print("   https://127.0.0.1:8080/callback?code=...")
print("\nThe page will show an error (that's fine). Copy the FULL URL from your browser's address bar")
print("and paste it here:\n")

url = input("Paste callback URL: ").strip()

# Extract code
parsed = urllib.parse.urlparse(url)
params = urllib.parse.parse_qs(parsed.query)
if 'code' not in params:
    print("❌ No code found in URL!")
    sys.exit(1)

code = params['code'][0]
print(f"\n🎫 Got code, exchanging for tokens...")

credentials = f'{app_key}:{app_secret}'
encoded = base64.b64encode(credentials.encode()).decode()

resp = requests.post(
    'https://api.schwabapi.com/v1/oauth/token',
    headers={
        'Authorization': f'Basic {encoded}',
        'Content-Type': 'application/x-www-form-urlencoded'
    },
    data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': callback_url
    }
)

if resp.status_code == 200:
    data = resp.json()
    token_file = os.path.join(os.path.dirname(__file__), 'connectors', '.schwab_tokens.json')
    tokens = {
        'access_token': data['access_token'],
        'refresh_token': data['refresh_token'],
        'token_expiry': time.time() + data.get('expires_in', 1800),
        'saved_at': time.strftime('%Y-%m-%dT%H:%M:%S')
    }
    with open(token_file, 'w') as f:
        json.dump(tokens, f, indent=2)
    print(f"\n✅ Tokens saved! Expires in {data.get('expires_in', '?')}s")
    print("You can now start the server: python server.py")
else:
    print(f"\n❌ Token exchange failed: {resp.status_code}")
    print(resp.text[:300])
