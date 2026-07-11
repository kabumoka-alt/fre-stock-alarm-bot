scheduling build on Metal builder "builder-jiuzbg"
fetched snapshot sha256:8525ca941e80c778a5a41bcdf7e1ddbe7c7144894dd21a8a58fe03675bc88537 (42 kB bytes)
fetching snapshot
40.8 KB
256ms
unpacking archive
170 KB
3ms
using build driver railpack-v0.30.1
                   
╭─────────────────╮
│ Railpack 0.30.1 │
╰─────────────────╯
 
  ↳ Detected Python
  ↳ Using pip
  ↳ Found worker command in Procfile
            
  Packages  
  ──────────
  python  │  3.13.14  │  railpack default (3.13)
            
  Steps     
  ──────────
  ▸ install
    $ python -m venv /app/.venv
    $ pip install -r requirements.txt
            
  Deploy    
  ──────────
    $ python stock_pump_bot_v30.py
 

load build definition from ./railpack-plan.json
0ms

copy /mise/installs, /mise/shims cached
0ms

install mise packages: python cached
0ms

copy /app, /app/.venv, / /app cached
0ms

pip install -r requirements.txt cached
0ms

copy requirements.txt cached
0ms

python -m venv /app/.venv cached
0ms

copy /root/.local/state/mise, /etc/mise/config.toml, /usr/local/bin/mise cached
0ms

exporting to docker image format
683ms
containerimage.config.digest: sha256:ca207f7e3a3da1159d1c121c13b3a524f72011367a650416977bf105c64a9724
containerimage.digest: sha256:a242ff1da9a8776788c61fe88d340b39f39582b1c45c0bf76b034e3ae09e361e
containerimage.descriptor: eyJtZWRpYVR5cGUiOiJhcHBsaWNhdGlvbi92bmQub2NpLmltYWdlLm1hbmlmZXN0LnYxK2pzb24iLCJkaWdlc3QiOiJzaGEyNTY6YTI0MmZmMWRhOWE4Nzc2Nzg4YzYxZmU4OGQzNDBiMzlmMzk1ODJiMWM0NWMwYmY3NmIwMzRlM2FlMDllMzYxZSIsInNpemUiOjIwMDcsImFubm90YXRpb25zIjp7Im9yZy5vcGVuY29udGFpbmVycy5pbWFnZS5jcmVhdGVkIjoiMjAyNi0wNy0xMVQwNzoxNTo1NloifSwicGxhdGZvcm0iOnsiYXJjaGl0ZWN0dXJlIjoiYW1kNjQiLCJvcyI6ImxpbnV4In19
image push
153.1 MB
