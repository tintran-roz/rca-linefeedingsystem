#!/usr/bin/env python3
"""
================================================================
RCA BEEPER MIDDLEWARE v3.3 — PRODUCTION STABLE
================================================================
Fixes:
  1. ThreadingHTTPServer — xử lý đa luồng, không block
  2. ESP32 push chạy thread riêng — không block request handler
  3. RCS call có retry + full exception handling
  4. Background thread có watchdog tự restart
  5. Request handler có try/except toàn bộ
  6. Socket timeout mặc định cho mọi connection
  7. Graceful degradation khi RCS/ESP32 offline
================================================================
"""
import os,sys,json,uuid,time,logging,threading,socket,traceback
from datetime import datetime
from http.server import HTTPServer,BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.request import Request,urlopen
from urllib.error import URLError,HTTPError
from urllib.parse import urlparse

# Global socket timeout — prevents hanging connections
socket.setdefaulttimeout(15)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(name)-10s] %(levelname)-5s %(message)s",datefmt="%H:%M:%S")
log=logging.getLogger("RCA")

# ==================== THREADED HTTP SERVER ====================
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a new thread — prevents blocking"""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 50

    def handle_error(self, request, client_address):
        """Override to prevent server crash on client errors"""
        log.debug("Connection error from %s (ignored)", client_address)

# ==================== CONFIG ====================
CFG_FILE=os.path.join(os.path.dirname(os.path.abspath(__file__)),"config.json")
def load_config():
    try:
        if os.path.exists(CFG_FILE):
            with open(CFG_FILE,"r",encoding="utf-8") as f: return json.load(f)
    except Exception as e:
        log.error("Config load error: %s",e)
    return {"rcs":{"host":"127.0.0.1","port":8182},"server":{"port":8990},"timing":{},"stations":[],"kitting_layout":[]}
CFG=load_config()

# ==================== SAFE HTTP CALL ====================
def safe_http_post(url, payload, timeout=10):
    """Thread-safe HTTP POST with full error handling — never raises"""
    try:
        body = json.dumps(payload).encode("utf-8")
        req = Request(url, data=body, headers={"Content-Type":"application/json"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except HTTPError as e:
        log.warning("HTTP %s from %s", e.code, url[:60])
        return {"code":str(e.code),"message":str(e.reason)}
    except URLError as e:
        log.warning("Connect failed: %s → %s", url[:50], str(e.reason)[:40])
        return {"code":"-2","message":"Connect failed"}
    except socket.timeout:
        log.warning("Timeout: %s", url[:60])
        return {"code":"-1","message":"Timeout"}
    except Exception as e:
        log.warning("HTTP error: %s → %s", url[:50], str(e)[:40])
        return {"code":"-3","message":str(e)[:60]}

# ==================== RCS CLIENT ====================
class RCS:
    def __init__(s,c):
        s.base="http://{}:{}/rcms/services/rest/hikRpcService".format(c.get("host","127.0.0.1"),c.get("port",8182))
        s.cc=c.get("client_code","RCA_BEEPER");s.tc=c.get("token_code","");s.to=c.get("timeout",10)
        log.info("RCS: %s",s.base)
    def _rc(s): return "BPR{}{}".format(datetime.now().strftime("%Y%m%d%H%M%S"),uuid.uuid4().hex[:6].upper())
    def _bp(s,rc=None): return {"reqCode":rc or s._rc(),"reqTime":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"clientCode":s.cc,"tokenCode":s.tc}
    def gen_task(s,task_typ,positions,priority="1",data_field=""):
        rc=s._rc();p=dict(s._bp(rc));p["taskTyp"]=task_typ;p["positionCodePath"]=positions;p["priority"]=priority
        if data_field: p["data"]=data_field
        log.info("genTask: type=%s pos=%d",task_typ,len(positions))
        r=safe_http_post("{}/genAgvSchedulingTask".format(s.base),p,s.to)
        c=str(r.get("code","-1"))
        return {"success":c=="0","code":c,"message":r.get("message",""),"task_id":str(r.get("data","")) if r.get("data") else "","req_code":rc}
    def cancel_task(s,tc,force="1"):
        p=dict(s._bp());p["taskCode"]=tc;p["forceCancel"]=force
        r=safe_http_post("{}/cancelTask".format(s.base),p,s.to)
        return {"success":str(r.get("code","-1"))=="0","message":r.get("message","")}
    def query_task_status(s,tcs):
        p=dict(s._bp());p["taskCodes"]=tcs
        r=safe_http_post("{}/queryTaskStatus".format(s.base),p,s.to)
        return {"success":str(r.get("code","-1"))=="0","data":r.get("data",[])}

# ==================== STATION MANAGER ====================
IDLE="idle";CREATED="created";EXECUTING="executing";COMPLETE="complete";ERROR="error"

class Manager:
    def __init__(s,rcs,timing):
        s.rcs=rcs;s.timing=timing;s.stations={};s.task_map={};s.events=[];s.lock=threading.Lock()

    def register(s,cfg):
        s.stations[cfg["station_id"]]={"cfg":cfg,"state":IDLE,"task_id":None,"agv":None,
            "last_call":None,"last_complete":None,"last_error":"",
            "calls":0,"ok":0,"fail":0,"esp_ip":None,"esp_online":False,"esp_hb":None}

    def _evt(s,sid,event,ok=True,detail=""):
        try:
            with s.lock:
                s.events.append({"time":datetime.now().strftime("%H:%M:%S"),"sid":sid,"event":event,"ok":ok,"detail":detail[:80]})
                if len(s.events)>500: s.events=s.events[-500:]
            log.log(logging.INFO if ok else logging.WARNING,"[%s] %s - %s",sid,event,detail[:80])
        except Exception: pass

    def _ss(s,st,state,msg=""):
        try:
            old=st["state"];st["state"]=state
            if state==ERROR: st["last_error"]=msg
            s._evt(st["cfg"]["station_id"],"{} -> {}".format(old,state),detail=msg)
        except Exception: pass

    def handle_press(s,station_id,esp_ip=""):
        try:
            st=s.stations.get(station_id)
            if not st: return {"accepted":False,"state":"error","message":"Unknown: "+station_id}
            if not st["cfg"].get("enabled",True): return {"accepted":False,"state":"error","message":"Disabled"}
            if esp_ip: st["esp_ip"]=esp_ip
            st["esp_online"]=True;st["esp_hb"]=time.time();st["calls"]+=1
            if st["state"]!=IDLE:
                ms={CREATED:"Da goi AMR - dang cho",EXECUTING:"AMR dang van chuyen",COMPLETE:"Vua xong",ERROR:st["last_error"]}
                st["fail"]+=1;s._evt(station_id,"REJECTED",False,"state="+st["state"])
                return {"accepted":False,"state":st["state"],"message":ms.get(st["state"],"Busy")}
            cfg=st["cfg"];positions=cfg.get("task_positions",[])
            if not positions:
                st["fail"]+=1; return {"accepted":False,"state":"error","message":"No positions"}
            s._ss(st,CREATED,"Calling RCS");st["last_call"]=time.time()
            r=s.rcs.gen_task(task_typ=cfg.get("task_type","MF01"),positions=positions,
                priority=cfg.get("task_priority","1"),data_field=json.dumps({"stationId":station_id}))
            if r["success"]:
                st["task_id"]=r["task_id"];st["ok"]+=1
                if r["task_id"]: s.task_map[r["task_id"]]=station_id
                s._ss(st,CREATED,"Task: "+r["task_id"]);s._push_async(st)
                return {"accepted":True,"state":"created","message":"Task {} created".format(r["task_id"])}
            else:
                st["fail"]+=1;err="RCS [{}]: {}".format(r["code"],r["message"]);s._ss(st,ERROR,err);s._push_async(st)
                return {"accepted":False,"state":"error","message":err}
        except Exception as e:
            log.error("handle_press error: %s",traceback.format_exc())
            return {"accepted":False,"state":"error","message":"Internal error"}

    def get_state(s,sid,esp_ip=""):
        try:
            st=s.stations.get(sid)
            if not st: return {"state":"error","message":"Unknown"}
            if esp_ip: st["esp_ip"]=esp_ip
            st["esp_online"]=True;st["esp_hb"]=time.time()
            ms={IDLE:"San sang",CREATED:"Task {} - cho AMR".format(st["task_id"] or ""),
                EXECUTING:"AMR {} dang chay".format(st["agv"] or ""),COMPLETE:"Hoan thanh!",ERROR:st["last_error"]}
            return {"state":st["state"],"message":ms.get(st["state"],"")}
        except Exception:
            return {"state":"error","message":"Internal error"}

    def handle_callback(s,data):
        try:
            tc=data.get("taskCode","");method=data.get("method","");robot=data.get("robotCode","");custom=data.get("data","")
            sid=s.task_map.get(tc,"")
            if not sid and custom:
                try: sid=json.loads(custom).get("stationId","")
                except: pass
            if not sid:
                for st in s.stations.values():
                    if st["task_id"]==tc: sid=st["cfg"]["station_id"];break
            if not sid: return {"code":"0","message":"OK"}
            st=s.stations.get(sid)
            if not st: return {"code":"0","message":"OK"}
            if method=="start": st["agv"]=robot;s._ss(st,EXECUTING,"AMR {} started".format(robot));s._push_async(st)
            elif method=="outbin":
                if st["state"]!=EXECUTING: st["agv"]=robot;s._ss(st,EXECUTING,"AMR {} moving".format(robot));s._push_async(st)
            elif method=="end": s._ss(st,COMPLETE,"Done by AMR {}".format(robot));st["last_complete"]=time.time();s._push_async(st)
            elif method=="cancel": s._ss(st,ERROR,"Cancelled");s._push_async(st)
            return {"code":"0","message":"successful","reqCode":data.get("reqCode","")}
        except Exception as e:
            log.error("Callback error: %s",e)
            return {"code":"0","message":"OK"}

    # ---------- NON-BLOCKING ESP32 PUSH ----------
    def _push_async(s,st):
        """Push state to ESP32 in separate thread — never blocks request handler"""
        if not st["esp_ip"]: return
        sid=st["cfg"]["station_id"]
        ip=st["esp_ip"]
        try:
            t=threading.Thread(target=s._do_push,args=(sid,ip),daemon=True)
            t.start()
        except Exception: pass

    def _do_push(s,sid,ip):
        """Actual push — runs in separate thread"""
        try:
            d=s.get_state(sid)
            safe_http_post("http://{}/api/push-state".format(ip),d,timeout=3)
        except Exception: pass

    # ---------- ADMIN ----------
    def force_reset(s,sid):
        try:
            st=s.stations.get(sid)
            if not st: return {"success":False,"message":"Not found"}
            if st["task_id"] and st["task_id"] in s.task_map: del s.task_map[st["task_id"]]
            s._ss(st,IDLE,"Admin reset");st["task_id"]=None;st["agv"]=None;st["last_error"]="";s._push_async(st)
            return {"success":True,"message":"Reset OK"}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def cancel_current(s,sid):
        try:
            st=s.stations.get(sid)
            if not st: return {"success":False,"message":"Not found"}
            if st["task_id"]:
                # Cancel in background thread to not block
                tc=st["task_id"]
                threading.Thread(target=lambda: s.rcs.cancel_task(tc),daemon=True).start()
            return s.force_reset(sid)
        except Exception as e:
            return {"success":False,"message":str(e)}

    # ---------- BACKGROUND LOOP WITH WATCHDOG ----------
    def start_background(s):
        """Start background loop with auto-restart on crash"""
        def watchdog():
            while True:
                try:
                    log.info("Background manager starting")
                    s._bg_loop_inner()
                except Exception as e:
                    log.error("Background loop crashed: %s — restarting in 5s",e)
                    log.error(traceback.format_exc())
                    time.sleep(5)
        t=threading.Thread(target=watchdog,daemon=True)
        t.start()
        return t

    def _bg_loop_inner(s):
        tm=s.timing;ch=tm.get("complete_hold_sec",10);eh=tm.get("error_hold_sec",15)
        tt=tm.get("task_timeout_sec",600);hbt=tm.get("heartbeat_timeout_sec",30)
        piv=tm.get("task_poll_interval_sec",30);lp=time.time()
        while True:
            now=time.time()
            for st in list(s.stations.values()):  # list() copy prevents dict mutation issues
                try:
                    if st["state"]==COMPLETE and st["last_complete"] and now-st["last_complete"]>=ch:
                        if st["task_id"] and st["task_id"] in s.task_map:
                            try: del s.task_map[st["task_id"]]
                            except: pass
                        s._ss(st,IDLE,"Auto-reset");st["task_id"]=None;st["agv"]=None;s._push_async(st)
                    elif st["state"]==ERROR and st["last_call"] and now-st["last_call"]>=eh:
                        s._ss(st,IDLE,"Error reset");st["task_id"]=None;st["agv"]=None;s._push_async(st)
                    elif st["state"] in (CREATED,EXECUTING) and st["last_call"] and now-st["last_call"]>=tt:
                        st["fail"]+=1;s._ss(st,ERROR,"Timeout {}s".format(tt));s._push_async(st)
                    if st["esp_hb"]: st["esp_online"]=(now-st["esp_hb"])<hbt
                except Exception as e:
                    log.debug("BG station error: %s",e)
            # Poll RCS
            if now-lp>=piv:
                lp=now
                try: s._poll()
                except Exception as e: log.debug("Poll error: %s",e)
            time.sleep(1)

    def _poll(s):
        active=[st["task_id"] for st in s.stations.values()
                if st["state"] in (CREATED,EXECUTING) and st["task_id"]]
        if not active: return
        r=s.rcs.query_task_status(active)
        if not r["success"] or not r["data"]: return
        for t in r["data"]:
            try:
                tc=t.get("taskCode","");ts=t.get("taskStatus","");agv=t.get("agvCode","")
                sid=s.task_map.get(tc)
                if not sid: continue
                st=s.stations.get(sid)
                if not st: continue
                if ts=="2" and st["state"]==CREATED:
                    st["agv"]=agv;s._ss(st,EXECUTING,"AMR {} (polled)".format(agv));s._push_async(st)
                elif ts=="9" and st["state"]!=COMPLETE:
                    s._ss(st,COMPLETE,"Done (polled)");st["last_complete"]=time.time();s._push_async(st)
                elif ts=="5" and st["state"] not in (IDLE,ERROR):
                    s._ss(st,ERROR,"Cancelled (polled)");s._push_async(st)
            except Exception: pass

    # ---------- DATA ----------
    def station_data(s,sid):
        st=s.stations.get(sid)
        if not st: return None
        return {"id":sid,"name":st["cfg"]["station_name"],"state":st["state"],"task":st["task_id"],
            "agv":st["agv"],"err":st["last_error"] if st["state"]==ERROR else "",
            "calls":st["calls"],"ok":st["ok"],"fail":st["fail"],
            "ip":st["esp_ip"],"online":st["esp_online"],"kitting":st["cfg"].get("kitting","")}

    def overview(s):
        ss=list(s.stations.values())
        return {"total":len(ss),"online":sum(1 for x in ss if x["esp_online"]),
            "idle":sum(1 for x in ss if x["state"]==IDLE),
            "active":sum(1 for x in ss if x["state"] in (CREATED,EXECUTING)),
            "errors":sum(1 for x in ss if x["state"]==ERROR),
            "calls":sum(x["calls"] for x in ss),
            "ok_total":sum(x["ok"] for x in ss),"fail_total":sum(x["fail"] for x in ss)}

    def recent(s,n=30): return list(reversed(s.events[-n:]))

# ==================== INIT ====================
rcs=RCS(CFG.get("rcs",{}))
mgr=Manager(rcs,CFG.get("timing",{}))
for sc in CFG.get("stations",[]): mgr.register(sc)
log.info("Loaded %d stations",len(mgr.stations))

# ==================== HTTP HANDLER ====================
class H(BaseHTTPRequestHandler):
    timeout = 30  # Per-request timeout

    def log_message(s,f,*a): pass

    def _json(s,d,code=200):
        try:
            b=json.dumps(d,ensure_ascii=False).encode("utf-8")
            s.send_response(code);s.send_header("Content-Type","application/json; charset=utf-8")
            s.send_header("Access-Control-Allow-Origin","*");s.send_header("Content-Length",len(b))
            s.end_headers();s.wfile.write(b)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception as e: log.debug("Response error: %s",e)

    def _html(s,h):
        try:
            b=h.encode("utf-8");s.send_response(200)
            s.send_header("Content-Type","text/html; charset=utf-8")
            s.send_header("Access-Control-Allow-Origin","*");s.send_header("Content-Length",len(b))
            s.end_headers();s.wfile.write(b)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception as e: log.debug("Response error: %s",e)

    def _body(s):
        try:
            n=int(s.headers.get("Content-Length",0))
            if n>0 and n<1000000:  # Max 1MB
                return json.loads(s.rfile.read(n).decode("utf-8"))
        except Exception as e:
            log.debug("Body parse error: %s",e)
        return {}

    def _ip(s):
        try: return s.client_address[0]
        except: return ""

    def do_GET(s):
        try:
            p=urlparse(s.path).path
            if p=="/": s._html(dashboard())
            elif p.startswith("/api/v1/beeper/") and p.endswith("/state"):
                sid=p.replace("/api/v1/beeper/","").replace("/state","")
                s._json(mgr.get_state(sid,s._ip()))
            elif p=="/api/v1/admin/stations":
                s._json([mgr.station_data(sid) for sid in mgr.stations if mgr.station_data(sid)])
            elif p=="/api/v1/admin/overview": s._json(mgr.overview())
            elif p=="/api/v1/admin/events": s._json(mgr.recent(50))
            elif p=="/health":
                o=mgr.overview();s._json({"status":"ok","stations":o["total"],"online":o["online"],
                    "uptime":int(time.time()-START_TIME),"threads":threading.active_count()})
            else: s._json({"error":"Not found"},404)
        except Exception as e:
            log.error("GET error: %s",e)
            try: s._json({"error":"Internal error"},500)
            except: pass

    def do_POST(s):
        try:
            p=urlparse(s.path).path;d=s._body()
            if p=="/api/v1/beeper/press":
                s._json(mgr.handle_press(d.get("station_id",""),s._ip()))
            elif p in ("/api/v1/rcs/agvCallback","/api/v1/rcs/agvCallbackService/agvCallback"):
                s._json(mgr.handle_callback(d))
            elif p.startswith("/api/v1/admin/reset/"): s._json(mgr.force_reset(p.split("/")[-1]))
            elif p.startswith("/api/v1/admin/cancel/"): s._json(mgr.cancel_current(p.split("/")[-1]))
            else: s._json({"error":"Not found"},404)
        except Exception as e:
            log.error("POST error: %s",e)
            try: s._json({"error":"Internal error"},500)
            except: pass

    def do_OPTIONS(s):
        try:
            s.send_response(204);s.send_header("Access-Control-Allow-Origin","*")
            s.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
            s.send_header("Access-Control-Allow-Headers","Content-Type");s.end_headers()
        except: pass

# ==================== DASHBOARD ====================
def dashboard():
    try:
        o=mgr.overview();events=mgr.recent(15);rc=CFG.get("rcs",{});layout=CFG.get("kitting_layout",[])
        SC={"idle":"#3d4f5f","created":"#f39c12","executing":"#2e86de","complete":"#27ae60","error":"#e74c3c"}
        BG={"idle":"#1a2332","created":"#4a3800","executing":"#0a2a4a","complete":"#0a3a1a","error":"#3a0a0a"}
        LBL={"idle":"IDLE","created":"CALLING","executing":"AMR ▶","complete":"DONE ✓","error":"ERROR ✗"}

        def sbox(sid):
            d=mgr.station_data(sid)
            if not d: return '<div class="sn empty">'+sid+'</div>'
            st=d["state"];sc=SC.get(st,"#3d4f5f");bg=BG.get(st,"#1a2332");lbl=LBL.get(st,"?")
            dot='<span class="dot-on"></span>' if d["online"] else '<span class="dot-off"></span>'
            extra=""
            if d["agv"]: extra='<div class="sx">AMR '+str(d["agv"])+'</div>'
            elif d["task"]: extra='<div class="sx">'+str(d["task"])[:16]+'</div>'
            elif d["err"]: extra='<div class="sx" style="color:#e74c3c">'+str(d["err"])[:20]+'</div>'
            return '''<div class="sn" style="border-color:{sc};background:{bg}" onclick="showMenu('{sid}')" title="{name}">
<div class="sn-hdr">{dot}<b>{sid}</b></div>
<div class="sn-state" style="color:{sc}">{lbl}</div>
{extra}
<div class="sn-stats">{calls} | <span style="color:#27ae60">✓{ok_}</span> <span style="color:#e74c3c">✗{fail}</span></div>
</div>'''.format(sc=sc,bg=bg,sid=sid,name=d["name"],dot=dot,lbl=lbl,extra=extra,calls=d["calls"],ok_=d["ok"],fail=d["fail"])

        def kbox(kit):
            kid=kit["kitting_id"];kname=kit["kitting_name"];lines=kit["lines"]
            active=0;total=0
            for line in lines:
                for sid in line:
                    d=mgr.station_data(sid)
                    if d: total+=1
                    if d and d["state"] in (CREATED,EXECUTING): active+=1
            lines_html=""
            for line in lines:
                nodes="".join(['<div class="arrow">&rarr;</div>'+sbox(sid) for sid in line])
                lines_html+='<div class="flow-line">{}</div>'.format(nodes)
            return '''<div class="kit-group">
<div class="kit-box"><div class="kit-name">{kname}</div><div class="kit-id">{kid}</div>
<div class="kit-stats">{total} trạm | {active} active</div></div>
<div class="kit-lines">{lines}</div></div>'''.format(kname=kname,kid=kid,total=total,active=active,lines=lines_html)

        flow_html="".join([kbox(k) for k in layout])
        rows=""
        for ev in events:
            c="#27ae60" if ev["ok"] else "#e74c3c"
            rows+='<tr style="color:{}"><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(c,ev["time"],ev["sid"],ev["event"],ev["detail"][:55])
        if not events: rows='<tr><td colspan="4" style="color:#3d4f5f">No events</td></tr>'

        uptime=int(time.time()-START_TIME);uh=uptime//3600;um=(uptime%3600)//60

        return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RCA Beeper Dashboard</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0e17;color:#c8d6e5;min-height:100vh}}
.hdr{{background:linear-gradient(135deg,#0d1525,#1a1a3e);padding:14px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #1e3a5f;flex-wrap:wrap;gap:8px}}
.hdr h1{{font-size:1.1em;font-weight:800;color:#fff;letter-spacing:2px}}.hdr .sub{{color:#576574;font-size:.7em}}
.pills{{display:flex;gap:6px;flex-wrap:wrap}}
.pill{{padding:4px 12px;border-radius:12px;font-size:.7em;font-weight:700;text-align:center;min-width:50px}}
.pill .pv{{font-size:1.3em;display:block}}
.p-w{{background:#1a2332;color:#fff}}.p-g{{background:#0a2e1a;color:#27ae60}}.p-b{{background:#0a1a2e;color:#2e86de}}
.p-y{{background:#2e2a0a;color:#f39c12}}.p-r{{background:#2e0a0a;color:#e74c3c}}
.cnt{{padding:12px 16px;max-width:1600px;margin:0 auto}}
.sec-title{{font-size:.7em;color:#576574;text-transform:uppercase;letter-spacing:2px;margin:14px 0 8px;padding-bottom:4px;border-bottom:1px solid #1e2738}}
.kit-group{{margin-bottom:14px;background:#0d1117;border:1px solid #1e2738;border-radius:10px;padding:12px;display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap}}
.kit-box{{background:linear-gradient(135deg,#1a3a5f,#0d2240);border:2px solid #2e86de;border-radius:8px;padding:10px 14px;min-width:110px;text-align:center;flex-shrink:0}}
.kit-name{{font-weight:800;color:#fff;font-size:.9em}}.kit-id{{font-family:monospace;font-size:.6em;color:#576574}}
.kit-stats{{font-size:.6em;color:#2e86de;margin-top:3px;font-weight:600}}
.kit-lines{{flex:1}}
.flow-line{{display:flex;align-items:center;margin:3px 0;gap:2px;flex-wrap:nowrap}}
.arrow{{color:#3d4f5f;font-size:.85em;font-weight:700;padding:0 1px;min-width:16px;text-align:center}}
.sn{{border:2px solid #3d4f5f;border-radius:6px;padding:5px 7px;min-width:95px;cursor:pointer;transition:all .15s;text-align:center;flex-shrink:0}}
.sn:hover{{transform:scale(1.05);box-shadow:0 0 10px rgba(46,134,222,.3)}}
.sn-hdr{{font-size:.7em;display:flex;align-items:center;justify-content:center;gap:3px}}
.sn-hdr b{{color:#fff;font-size:.9em}}.sn-state{{font-family:monospace;font-size:.72em;font-weight:800;margin:2px 0;letter-spacing:.5px}}
.sx{{font-size:.58em;color:#576574;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:88px}}
.sn-stats{{font-size:.55em;color:#576574}}
.dot-on{{width:5px;height:5px;border-radius:50%;background:#27ae60;box-shadow:0 0 4px #27ae60;display:inline-block}}
.dot-off{{width:5px;height:5px;border-radius:50%;background:#e74c3c;display:inline-block}}
.legend{{display:flex;gap:10px;margin:6px 0;flex-wrap:wrap}}
.leg-item{{display:flex;align-items:center;gap:4px;font-size:.62em}}.leg-dot{{width:10px;height:10px;border-radius:2px}}
.menu-overlay{{display:none;position:fixed;top:0;left:0;width:100%;height:100%;z-index:100}}
.menu{{position:fixed;background:#1a2332;border:1px solid #2e86de;border-radius:8px;padding:8px;z-index:101;display:none;min-width:140px;box-shadow:0 4px 20px rgba(0,0,0,.5)}}
.menu a{{display:block;padding:6px 12px;color:#c8d6e5;text-decoration:none;font-size:.78em;border-radius:4px;font-weight:600}}
.menu a:hover{{background:#2e86de20}}.menu .m-title{{color:#576574;font-size:.65em;padding:4px 12px;text-transform:uppercase;letter-spacing:1px}}
table{{width:100%;border-collapse:collapse;font-size:.66em;margin-top:4px}}
th{{text-align:left;padding:3px 5px;color:#576574;border-bottom:1px solid #1e2738;font-size:.7em;text-transform:uppercase}}
td{{padding:3px 5px;border-bottom:1px solid #0d111780;font-family:monospace}}
.ft{{text-align:center;padding:8px;font-size:.6em;color:#3d4f5f;margin-top:10px}}
@media(max-width:900px){{.kit-group{{flex-direction:column}}.flow-line{{overflow-x:auto}}.sn{{min-width:80px}}}}
</style></head><body>
<div class="hdr"><div><h1>RCA BEEPER DASHBOARD</h1>
<div class="sub">Line Feeding v3.3 &mdash; RCS @ {host}:{port} &mdash; Uptime: {uh}h{um}m &mdash; Threads: {threads}</div></div>
<div class="pills">
<div class="pill p-w"><span class="pv">{total}</span>Stations</div>
<div class="pill p-g"><span class="pv">{online}</span>Online</div>
<div class="pill p-b"><span class="pv">{idle}</span>Idle</div>
<div class="pill p-y"><span class="pv">{active}</span>Active</div>
<div class="pill p-r"><span class="pv">{errors}</span>Error</div>
<div class="pill p-w"><span class="pv">{calls}</span>Calls</div>
</div></div>
<div class="cnt">
<div class="legend">
<div class="leg-item"><div class="leg-dot" style="background:#3d4f5f"></div>Idle</div>
<div class="leg-item"><div class="leg-dot" style="background:#f39c12"></div>Calling</div>
<div class="leg-item"><div class="leg-dot" style="background:#2e86de"></div>AMR Running</div>
<div class="leg-item"><div class="leg-dot" style="background:#27ae60"></div>Complete</div>
<div class="leg-item"><div class="leg-dot" style="background:#e74c3c"></div>Error</div>
</div>
<div class="sec-title">Kitting → Station Flow</div>
{flow}
<div class="sec-title">Recent Events</div>
<table><tr><th>Time</th><th>Station</th><th>Event</th><th>Detail</th></tr>{rows}</table>
<div class="ft">RCA Beeper Middleware v3.3 — Zero Dependencies — Python {pyver}</div>
</div>
<div class="menu-overlay" id="menuOverlay" onclick="hideMenu()"></div>
<div class="menu" id="ctxMenu"><div class="m-title" id="menuTitle">Station</div>
<a style="color:#2e86de" href="#" onclick="doAct('reset')">↺ Reset to IDLE</a>
<a style="color:#e74c3c" href="#" onclick="doAct('cancel')">✗ Cancel Task</a></div>
<script>
var curSid='';
function showMenu(sid){{curSid=sid;var m=document.getElementById('ctxMenu'),o=document.getElementById('menuOverlay');
document.getElementById('menuTitle').textContent=sid;m.style.display='block';o.style.display='block';
var e=event||window.event;m.style.left=Math.min(e.clientX,innerWidth-160)+'px';m.style.top=Math.min(e.clientY,innerHeight-100)+'px'}}
function hideMenu(){{document.getElementById('ctxMenu').style.display='none';document.getElementById('menuOverlay').style.display='none'}}
function doAct(a){{hideMenu();if(!confirm(a+' '+curSid+'?'))return;
fetch('/api/v1/admin/'+a+'/'+curSid,{{method:'POST'}}).then(function(r){{return r.json()}}).then(function(d){{alert(d.message||JSON.stringify(d));location.reload()}}).catch(function(e){{alert(e)}})}}
setTimeout(function(){{location.reload()}},4000);
</script></body></html>'''.format(
            host=rc.get("host","?"),port=rc.get("port","?"),flow=flow_html,rows=rows,
            uh=uh,um=um,threads=threading.active_count(),pyver=sys.version.split()[0],**o)
    except Exception as e:
        log.error("Dashboard render error: %s",traceback.format_exc())
        return "<html><body><h1>Dashboard Error</h1><pre>{}</pre></body></html>".format(str(e))

# ==================== MAIN ====================
START_TIME=time.time()

def main():
    port=CFG.get("server",{}).get("port",8990);host=CFG.get("server",{}).get("host","0.0.0.0")
    for i,a in enumerate(sys.argv):
        if a=="--port" and i+1<len(sys.argv): port=int(sys.argv[i+1])

    # Start background with watchdog
    mgr.start_background()

    # Use ThreadedHTTPServer — handles concurrent requests
    srv=ThreadedHTTPServer((host,port),H)
    srv.timeout = 30  # Server-level timeout

    log.info("")
    log.info("============================================")
    log.info("  RCA BEEPER MIDDLEWARE v3.3 (STABLE)")
    log.info("  http://%s:%s",host,port)
    log.info("  Stations: %d  |  Kitting: %d",len(mgr.stations),len(CFG.get("kitting_layout",[])))
    log.info("  RCS: %s",rcs.base)
    log.info("  Server: ThreadedHTTPServer (multi-thread)")
    log.info("  Python: %s",sys.version.split()[0])
    log.info("============================================")
    log.info("")

    try: srv.serve_forever()
    except KeyboardInterrupt: log.info("Shutting down...");srv.shutdown()

if __name__=="__main__": main()
