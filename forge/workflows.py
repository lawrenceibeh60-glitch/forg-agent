"""Persistent, allowlisted visual workflows for Forge.

Workflow steps are declarative and intentionally limited to read-only workspace
inspection and security review. They cannot execute shell commands or external
side effects.
"""
from __future__ import annotations
import json, os, re, sqlite3, time, uuid
from pathlib import Path
from threading import RLock

_LOCK = RLock()
_DB = Path(os.getenv("FORGE_WORKFLOW_DB", os.getenv("TASK_DB_PATH", "data/tasks.sqlite3")))
ACTIONS = {"map_workspace", "search_workspace", "security_scan", "list_memory"}
MAX_STEPS = 20

def _db():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(_DB), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("""CREATE TABLE IF NOT EXISTS forge_workflows(
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, name TEXT NOT NULL,
        steps_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS forge_workflow_runs(
        id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, session_id TEXT NOT NULL,
        status TEXT NOT NULL, result_json TEXT NOT NULL, created_at REAL NOT NULL,
        finished_at REAL)""")
    return db

def _sid(sid):
    if not isinstance(sid,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",sid):
        raise ValueError("invalid session id")

def _validate(name, steps):
    name = str(name or "").strip()
    if not name or len(name)>120: raise ValueError("workflow name is required and must be <=120 characters")
    if not isinstance(steps,list) or not steps or len(steps)>MAX_STEPS:
        raise ValueError(f"workflow must contain between 1 and {MAX_STEPS} steps")
    cleaned=[]
    for i, step in enumerate(steps):
        if not isinstance(step,dict) or step.get("action") not in ACTIONS:
            raise ValueError(f"step {i+1} uses an unsupported action")
        args=step.get("args") or {}
        if not isinstance(args,dict): raise ValueError(f"step {i+1} args must be an object")
        allowed={"query","limit"}
        if set(args)-allowed: raise ValueError(f"step {i+1} contains unsupported arguments")
        query=str(args.get("query",""))[:500]
        limit=max(1,min(int(args.get("limit",30)),100))
        cleaned.append({"id":str(step.get("id") or uuid.uuid4().hex[:8])[:40],
                        "name":str(step.get("name") or step["action"])[:100],
                        "action":step["action"],"args":{"query":query,"limit":limit}})
    return name,cleaned

def save(session_id,name,steps):
    _sid(session_id); name,steps=_validate(name,steps); now=time.time(); wid=uuid.uuid4().hex
    with _LOCK,_db() as db:
        db.execute("INSERT INTO forge_workflows(id,session_id,name,steps_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                   (wid,session_id,name,json.dumps(steps),now,now)); db.commit()
    return get(session_id,wid)

def list_for_session(session_id):
    _sid(session_id)
    with _LOCK,_db() as db:
        rows=db.execute("SELECT * FROM forge_workflows WHERE session_id=? ORDER BY updated_at DESC",(session_id,)).fetchall()
    return [_decode(r) for r in rows]

def _decode(row):
    if row is None:return None
    d=dict(row); d["steps"]=json.loads(d.pop("steps_json")); return d

def get(session_id,workflow_id):
    _sid(session_id)
    with _LOCK,_db() as db:
        row=db.execute("SELECT * FROM forge_workflows WHERE id=? AND session_id=?",(workflow_id,session_id)).fetchone()
    return _decode(row)

def delete(session_id,workflow_id):
    _sid(session_id)
    with _LOCK,_db() as db:
        cur=db.execute("DELETE FROM forge_workflows WHERE id=? AND session_id=?",(workflow_id,session_id)); db.commit()
    return cur.rowcount>0

def run(session_id,workflow_id,tools_module,intelligence_module):
    workflow=get(session_id,workflow_id)
    if not workflow: raise ValueError("workflow not found in this session")
    run_id=uuid.uuid4().hex; started=time.time(); results=[]; status="completed"
    with _LOCK,_db() as db:
        db.execute("INSERT INTO forge_workflow_runs(id,workflow_id,session_id,status,result_json,created_at) VALUES(?,?,?,?,?,?)",
                   (run_id,workflow_id,session_id,"running","[]",started)); db.commit()
    try:
        for i,step in enumerate(workflow["steps"]):
            action,args=step["action"],step["args"]
            if action=="map_workspace":
                output=intelligence_module.project_map(session_id,tools_module)
            elif action=="search_workspace":
                output=intelligence_module.search_workspace(session_id,args.get("query",""),tools_module,args.get("limit",30))
            elif action=="security_scan":
                output=intelligence_module.risk_report(session_id,tools_module)
            elif action=="list_memory":
                output={"items":intelligence_module.list_memory(session_id,args.get("query",""),args.get("limit",30))}
            else:
                raise ValueError("unsupported workflow action")
            results.append({"step":i+1,"id":step["id"],"name":step["name"],"action":action,"status":"completed","output":output})
            with _LOCK,_db() as db:
                db.execute("UPDATE forge_workflow_runs SET result_json=? WHERE id=?",(json.dumps(results,ensure_ascii=False)[:200000],run_id)); db.commit()
    except Exception as exc:
        status="failed"
        results.append({"step":len(results)+1,"status":"failed","error":type(exc).__name__+": "+str(exc)[:300]})
    finished=time.time()
    with _LOCK,_db() as db:
        db.execute("UPDATE forge_workflow_runs SET status=?,result_json=?,finished_at=? WHERE id=?",
                   (status,json.dumps(results,ensure_ascii=False)[:200000],finished,run_id)); db.commit()
    return {"run_id":run_id,"workflow_id":workflow_id,"status":status,"steps_completed":sum(1 for r in results if r.get("status")=="completed"),"results":results}
