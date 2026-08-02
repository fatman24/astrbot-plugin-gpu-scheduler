import requests, time

BASE = 'http://127.0.0.1:6185/api/v1'
r = requests.post(f'{BASE}/auth/login', json={'username':'astrbot','password':'ZEr@1201390032'})
token = r.json()['data']['token']
h = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

# Step 1: Check current state
print('=== Current State ===')
r = requests.get(f'{BASE}/providers', headers=h)
for p in r.json()['data']['providers']:
    if p['provider_type'] == 'chat_completion':
        print(f"  {p['id']:45s} enabled={p['enable']}")

# Step 2: Switch to DeepSeek
print('\n=== Switching to DeepSeek ===')
ollama_ids = ['ollama/qwen3.6:35b', 'ollama/nemotron-cascade-2:latest']
for pid in ollama_ids:
    r = requests.patch(f'{BASE}/providers/{pid}/enabled', headers=h, json={'enabled': False})
    print(f'  PATCH {pid} enabled=False -> {r.status_code}')

# Step 3: Check after
time.sleep(1)
print('\n=== After Switch ===')
r = requests.get(f'{BASE}/providers', headers=h)
for p in r.json()['data']['providers']:
    if p['provider_type'] == 'chat_completion':
        print(f"  {p['id']:45s} enabled={p['enable']}")

# Step 4: Switch back
print('\n=== Restoring Ollama ===')
for pid in ollama_ids:
    r = requests.patch(f'{BASE}/providers/{pid}/enabled', headers=h, json={'enabled': True})
    print(f'  PATCH {pid} enabled=True -> {r.status_code}')
