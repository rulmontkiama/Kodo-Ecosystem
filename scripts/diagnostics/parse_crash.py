import json, sys
data = open(sys.argv[1]).read().split('\n')
for line in data:
    if line.startswith('{"app_name"'):
        try:
            obj = json.loads(line)
            threads = obj.get("threads", [])
            for i, t in enumerate(threads):
                print(f"THREAD {i}:")
                for f in t.get("frames", [])[:5]:
                    sym = f.get("symbol", f.get("imageOffset"))
                    print(f" - {sym}")
        except:
            pass
