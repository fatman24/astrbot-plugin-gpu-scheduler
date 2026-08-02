import subprocess

# Check provider states
cmd1 = """
docker exec astrbot python3 -c "
import requests
BASE='http://127.0.0.1:6185/api/v1'
r=requests.post(BASE+'/auth/login',json={'username':'astrbot','password':'ZEr@1201390032'})
t=r.json()['data']['token']
h={'Authorization':'Bearer '+t}
r=requests.get(BASE+'/providers',headers=h)
for p in r.json()['data']['providers']:
    if p['provider_type']=='chat_completion':
        s='ON' if p['enable'] else 'OFF'
        print(s,p['id'])
"
"""

# Check GPU scheduler logs
cmd2 = """docker logs astrbot --tail 100 2>&1 | grep 'GPU Scheduler\' | tail -15"""

# Check state file
cmd3 = """docker exec astrbot cat data/gpu_scheduler_state.json 2>/dev/null || echo 'no state file'"""

for label, cmd in [("=== Provider 状态 ===", cmd1), ("=== GPU Scheduler 日志 ===", cmd2), ("=== 状态文件 ===", cmd3)]:
    print(label)
    result = subprocess.run(
        ['ssh', '-o', 'StrictHostKeyChecking=no', 'root@192.168.100.115', cmd],
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr[:200])
    print()
