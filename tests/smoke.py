import sys,json,tempfile,hashlib,secrets,threading,urllib.request,urllib.error
from pathlib import Path
sys.path.insert(0,"/app")
import fund_flow as f
password=secrets.token_urlsafe(24);salt=secrets.token_hex(16)
Path("/app/auth.json").write_text(json.dumps({"username":"test","salt":salt,"password_hash":hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(salt),200000).hex(),"health_token":secrets.token_urlsafe(24)}))
with tempfile.TemporaryDirectory() as td:
 f.DATA_DIR=Path(td)
 for day,vs in [("20261116",[10,12]),("20261117",[5,7])]:
  (f.DATA_DIR/("fund_flow_"+day+".json")).write_text("\n".join(json.dumps({"time":tm,"sector":"SYNTHETIC","main_net_inflow":v,"delta":None}) for tm,v in zip(["10:00:00","14:55:00"],vs)))
 r=f.period_data("week","20261118");assert r["em"]["sectors"][0]["values"]==[12,7,None]
 assert f.period_data("today","20261118")["em"] is None
 handler=f._make_handler(lambda d=None:{},lambda:{"em":[]},f._build_ajax_html(300),300)
 srv=f.http.server.HTTPServer(("127.0.0.1",0),handler)
 threading.Thread(target=srv.serve_forever,daemon=True).start()
 import base64
 headers={"Authorization":"Basic "+base64.b64encode(("test:"+password).encode()).decode()}
 for path in ["/","/api/data?period=week&date=20261118","/api/dates","/static/plotly.min.js"]:
  url="http://127.0.0.1:"+str(srv.server_port)+path
  try:urllib.request.urlopen(url);raise AssertionError("anonymous allowed")
  except urllib.error.HTTPError as e:assert e.code==401
  r=urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=10);assert r.status==200;r.read();r.close()
 srv.shutdown()
print("PASS: synthetic daily last-value/no-zero tests and anonymous401/authenticated200 routes")
