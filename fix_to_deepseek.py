import requests, json

BASE = 'http://127.0.0.1:6185/api/v1'
r = requests.post(BASE + '/auth/login',
    json={'username': 'astrbot', 'password': 'ZEr@1201390032'})
t = r.json()['data']['token']
h = {'Authorization': 'Bearer ' + t, 'Content-Type': 'application/json'}

for pid in ['ollama/qwen3.6:35b', 'ollama/nemotron-cascade-2:latest']:
    r = requests.patch(BASE + '/providers/' + pid + '/enabled',
        headers=h, json={'enabled': True})
    print(pid, r.status_code)

with open('data/gpu_scheduler_state.json', 'w') as f:
    json.dump({'mode': 'ollama', 'updated': '2026-07-25T15:24:00'}, f)
print('state updated')

# Verify
r = requests.get(BASE + '/providers', headers=h)
print()
for p in r.json()['data']['providers']:
    if p['provider_type'] == 'chat_completion':
        print(('ON ' if p['enable'] else 'OFF') + ' ' + p['id'])
