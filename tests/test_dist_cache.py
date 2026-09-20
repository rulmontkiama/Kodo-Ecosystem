"""L'interface en cache ne doit jamais masquer celle d'un DMG plus récent."""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = r'''
import json, os, sys
sys.path.insert(0, %r)
import kodo_base, server_pos
home = os.environ["HOME"]
cache = os.path.join(home, "Library", "Caches", "KodoPOS")
os.makedirs(os.path.join(cache, "dist", "assets"), exist_ok=True)
open(os.path.join(cache, "dist", "index.html"), "w").write("<html></html>")
vf = os.path.join(cache, "version.json")
res = {}
res["sans_version"] = server_pos.is_cache_dist_current(vf)
for name, v in (("ancienne", "1.0.0"), ("egale", kodo_base.BASE_VERSION), ("plus_recente", "9.9.9")):
    json.dump({"version": v}, open(vf, "w"))
    res[name] = server_pos.is_cache_dist_current(vf)
json.dump({"version": "1.0.0"}, open(vf, "w"))
res["dist_ancien_ignore"] = os.path.join(cache, "dist") != server_pos.get_dist_dir()
json.dump({"version": kodo_base.BASE_VERSION}, open(vf, "w"))
res["dist_recent_utilise"] = server_pos.get_dist_dir() == os.path.join(cache, "dist")
print(json.dumps(res))
''' % ROOT


def test_cache_dist_ancien_ignore():
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, HOME=home, KODO_DB_PATH=os.path.join(home, "t.db"))
        out = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True,
                             text=True, cwd=home, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res == {"sans_version": False, "ancienne": False, "egale": True, "plus_recente": True,
                   "dist_ancien_ignore": True, "dist_recent_utilise": True}
