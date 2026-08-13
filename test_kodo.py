import urllib.request
import json
data = json.dumps({"name": "TestCaissier", "role": "Caissier", "pin": "9999"}).encode('utf-8')
req = urllib.request.Request("http://127.0.0.1:8765/api/users", data=data, headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req) as response:
        print(response.read().decode())
except Exception as e:
    print(e)
