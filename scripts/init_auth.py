#!/usr/bin/env python3
"""Run from repository root. Never commit generated auth.json."""
import getpass, hashlib, json, os, secrets
from pathlib import Path
root=Path(__file__).resolve().parents[1]
target=root/"auth.json"
if target.exists():
    raise SystemExit("auth.json exists; refusing to overwrite")
username=input("Username: ").strip()
password=getpass.getpass("Password (at least 12 characters): ")
if not username or ":" in username or len(password)<12:
    raise SystemExit("Invalid username or password too short")
if password != getpass.getpass("Repeat password: "):
    raise SystemExit("Passwords do not match")
salt=secrets.token_hex(16)
a={"username":username,"salt":salt,"password_hash":hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(salt),200000).hex(),"health_token":secrets.token_urlsafe(32)}
fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,"w") as f: json.dump(a,f)
for p in [target,root/"data",root/"charts"]:
    if p!=target: p.mkdir(exist_ok=True)
    if os.geteuid()==0: os.chown(p,10001,10001)
print("Created local credentials. Container UID 10001 needs access to auth.json/data/charts.")
