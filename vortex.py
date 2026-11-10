#!/usr/bin/env python3
"""
VORTEX CODING - Toolkit  (READ-ONLY diagnostics + file tools)
=============================================================
One tool, VIN-keyed, for a BMW/MINI coding service. It reads the car over
ENET (DoIP) for identity/diagnostics, organises your real coding backups,
helps you quote, and makes job cards.

SAFETY LINE: it only ever sends diagnostic READ requests (UDS 0x22/0x19) and,
with explicit confirmation, Clear-DTC (0x14) - the same routine faultclear
every scan tool does. It does NOT code, flash, touch security access, or
generate FSCs. Making/restoring coding backups stays in E-Sys / BimmerCode;
this tool catalogues them.

SETUP:  pip install doipclient udsoncan
COMMANDS:
  read      live identity (VIN, versions, modules, faults)
  check     pre-code safety gate: read + firmware guidance + battery + backup check
  battery   battery voltage only
  istep     software/version identifiers (for E-Sys PSdZData matching)
  faults    read DTCs; --clear to clear fault memory (asks first)
  diff      snapshot a module's data ids; compare before/after (record only)
  backup    add | scan | list | find   (catalogue real .ncd/BimmerCode backups)
  features  codable-feature cheat sheet for a head unit
  invoice   branded HTML job card from a saved job
  export    turn a read JSON into a quote-tool prefill

STATUS: validated against the simulated gateway in sim/ (see tests/).
Live vehicle testing in progress - module addresses and data identifiers
may need tuning per model.
"""
import argparse, json, logging, os, shutil, sys
from datetime import datetime

# ---- optional car libraries (only needed for read/check/battery/istep/faults/diff) ----
def _load_car():
    try:
        from doipclient import DoIPClient
        from doipclient.connectors import DoIPClientUDSConnector
        from udsoncan.client import Client
        from udsoncan import configs, DidCodec
        import udsoncan.exceptions as uexc
        return DoIPClient, DoIPClientUDSConnector, Client, configs, DidCodec, uexc
    except ImportError:
        sys.exit("This command needs the car libraries. Run:  pip install doipclient udsoncan")

DIDS = {0xF190:"VIN",0xF191:"HW part no",0xF193:"HW version",0xF194:"SW number",
        0xF195:"SW version",0xF197:"System name",0xF1A0:"Vehicle data"}
# candidate DIDs that on some BMW ECUs hold battery voltage (confirm per car)
VOLT_DIDS = [0xDD51,0xDD01,0x4010]
COMMON = {0x10:"Gateway/ZGW",0x40:"Body (BDC/FEM)",0x60:"Head unit",0x63:"Head unit alt",
          0x12:"DME engine",0x18:"Kombi cluster"}

def _codec(DidCodec):
    class Raw(DidCodec):
        def encode(self,v): return bytes(v)
        def decode(self,p): return p
        def __len__(self): raise DidCodec.ReadAllRemainingData
    return Raw()

def _decode(raw):
    try:
        t=bytes(raw).decode("ascii").strip("\x00 ").strip()
        if t and all(32<=ord(c)<127 for c in t): return t
    except Exception: pass
    return bytes(raw).hex().upper()

def _connect(ip, logical, fatal=True):
    DoIPClient,Conn,Client,configs,DidCodec,_=_load_car()
    try:
        doip=DoIPClient(ip,logical)
    except (ConnectionRefusedError, TimeoutError, OSError) as e:
        if not fatal: raise
        sys.exit(f"[!] Could not connect to the car at {ip} (module 0x{logical:02X}). "
                 f"Check ignition, ENET cable and the 169.254.x.x link.  ({e})")
    cfg=dict(configs.default_client_config); cfg["request_timeout"]=3
    cfg["data_identifiers"]={"default":_codec(DidCodec)}
    c=Client(Conn(doip),config=cfg); c.open()
    return doip,c

def _find_car(ip):
    DoIPClient,*_=_load_car()
    if ip: return ip,0x10
    print("[*] Listening for the car's DoIP announcement (ignition on, cable in)...")
    try:
        addr,ann=DoIPClient.await_vehicle_announcement(timeout=10)
        car_ip=addr[0] if isinstance(addr,(tuple,list)) else addr
        logical=getattr(ann,"logical_address",0x10)
        return car_ip,logical
    except Exception:
        # fallback: blocking broadcast discovery (returns quickly if a car answers)
        try:
            res=DoIPClient.get_entity()
            return res[0],res[1]
        except Exception as e:
            sys.exit(f"[!] No car found. Check ignition/cable/169.254.x.x link, or pass --ip.  ({e})")

def _read_dids(uds, dids=DIDS):
    _,_,_,_,_,uexc=_load_car()
    out={}
    for did,name in dids.items():
        try: out[name]=_decode(uds.read_data_by_identifier_first(did))
        except (uexc.NegativeResponseException,uexc.TimeoutException,uexc.InvalidResponseException): continue
        except TimeoutError: continue
        except OSError: break      # DoIP negative acknowledge: module not fitted / unreachable
        except Exception: continue
    return out

# ---------------- car commands ----------------
def cmd_read(a):
    ip,gw=_find_car(a.ip)
    print(f"[+] Car at {ip} (gw 0x{gw:02X})")
    doip,uds=_connect(ip,gw)
    ident=_read_dids(uds)
    vin=ident.get("VIN","UNKNOWN")
    res={"read_at":datetime.now().isoformat(timespec="seconds"),"gateway_ip":ip,"vin":vin,"gateway":ident,"modules":{}}
    print(f"    VIN: {vin}")
    for k,v in ident.items():
        if k!="VIN": print(f"    {k}: {v}")
    try: doip.close()
    except Exception: pass
    if a.scan:
        print("[*] Probing common modules...")
        for addr,label in COMMON.items():
            try:
                d2,u2=_connect(ip,addr,fatal=False); info=_read_dids(u2)
                if info: res["modules"][f"{addr:02X}"]={"label":label,**info}; print(f"    [{addr:02X}] {label}: {info.get('SW version','responded')}")
                d2.close()
            except Exception: continue
    out=a.out or f"vortex_read_{''.join(c for c in vin if c.isalnum()) or 'unknown'}.json"
    json.dump(res,open(out,"w"),indent=2); print(f"[+] Saved -> {out}")

def _battery(uds):
    for did in VOLT_DIDS:
        try:
            raw=bytes(uds.read_data_by_identifier_first(did))
            if len(raw)>=2:
                v=int.from_bytes(raw[:2],"big")
                for scale in (1000.0,100.0,10.0):
                    volts=v/scale
                    if 8.0<=volts<=16.0: return round(volts,2)
        except Exception: continue
    return None

def cmd_battery(a):
    ip,gw=_find_car(a.ip); _,uds=_connect(ip,gw)
    v=_battery(uds)
    if v is None: print("[i] Battery voltage DID not found on this car - confirm the DID on a live vehicle.")
    else:
        flag="OK" if v>=12.5 else ("LOW - charger on before any flash" if v>=12.0 else "TOO LOW - do not flash")
        print(f"[+] Battery: {v} V  -> {flag}")

def cmd_istep(a):
    ip,gw=_find_car(a.ip); _,uds=_connect(ip,gw)
    ident=_read_dids(uds)
    print("[*] Version identifiers (use to match E-Sys PSdZData / confirm firmware):")
    for k,v in ident.items(): print(f"    {k}: {v}")
    print("[i] Full I-step comes from the FA in E-Sys; these identifiers confirm firmware level.")

def cmd_faults(a):
    ip,gw=_find_car(a.ip); doip,uds=_connect(ip,gw)
    try:
        r=uds.get_dtc_by_status_mask(0xFF)
        dtcs=getattr(r.service_data,"dtcs",[]) if r else []
        if not dtcs: print("[+] No stored faults.")
        else:
            print(f"[+] {len(dtcs)} fault(s):")
            for d in dtcs: print(f"    {d.id:06X}  status {d.status.get_byte_as_int():02X}")
    except Exception as e:
        print(f"[!] Could not read faults: {e}"); dtcs=[]
    if a.clear:
        if input("Clear fault memory? (standard diagnostic reset) [y/N] ").strip().lower()=="y":
            try: uds.clear_dtc(0xFFFFFF); print("[+] Fault memory cleared.")
            except Exception as e: print(f"[!] Clear failed: {e}")

def cmd_diff(a):
    # snapshot a module's data-identifier values to a file; compare two snapshots.
    if a.compare:
        before=json.load(open(a.compare[0])); after=json.load(open(a.compare[1]))
        keys=set(before)|set(after); changed=False
        print(f"=== diff {a.compare[0]} -> {a.compare[1]} ===")
        for k in sorted(keys):
            b,af=before.get(k),after.get(k)
            if b!=af: changed=True; print(f"  {k}: {b!r} -> {af!r}")
        if not changed: print("  (no differences)")
        return
    ip,gw=_find_car(a.ip); addr=a.module if a.module is not None else gw
    _,uds=_connect(ip,addr)
    snap=_read_dids(uds)
    label=a.label or "snapshot"
    out=a.out or f"vortex_diff_{addr:02X}_{label}.json"
    json.dump(snap,open(out,"w"),indent=2)
    print(f"[+] Saved {label} snapshot of module 0x{addr:02X} -> {out}")
    print("[i] Record only - not a restorable coding backup. Compare with:  vortex.py diff --compare before.json after.json")

# ---------------- backup manager ----------------
def _bk_index(root):
    p=os.path.join(root,"index.json")
    if os.path.exists(p):
        try: return json.load(open(p))
        except Exception: print("[!] index.json unreadable - fresh index (old kept).")
    return {"cars":{}}
def _bk_save(root,idx): os.makedirs(root,exist_ok=True); json.dump(idx,open(os.path.join(root,"index.json"),"w"),indent=2)

def cmd_backup(a):
    root=a.root; idx=_bk_index(root)
    if a.bcmd=="add":
        vin=a.vin or (json.load(open(a.from_read)).get("vin") if a.from_read else None)
        if not vin: sys.exit("[!] Need --vin or --from-read.")
        vin=vin.strip().upper(); files=a.files or []
        miss=[f for f in files if not os.path.isfile(f)]
        if miss: sys.exit("[!] Not found:\n   "+"\n   ".join(miss))
        stamp=datetime.now().strftime("%Y-%m-%d_%H%M%S"); dd=os.path.join(root,vin,stamp); n=1
        while os.path.exists(dd):                       # never write into an existing backup folder
            n+=1; dd=os.path.join(root,vin,f"{stamp}_{n}")
        stamp=os.path.basename(dd); os.makedirs(dd)
        stored=[os.path.basename(f) for f in files]
        for f in files: shutil.copy2(f,os.path.join(dd,os.path.basename(f)))
        idx["cars"].setdefault(vin,{"backups":[]})["backups"].append(
            {"date":datetime.now().isoformat(timespec="seconds"),"tool":a.tool,"note":a.note,"folder":os.path.join(vin,stamp),"files":stored})
        _bk_save(root,idx); print(f"[+] Filed {len(stored)} file(s) for {vin} -> {dd}")
    elif a.bcmd=="scan":
        if not os.path.isdir(a.dir): sys.exit(f"[!] Not a folder: {a.dir}")
        exts=(".ncd",".cafd",".fa",".svt",".bmwbackup",".zip")
        found=[os.path.join(a.dir,f) for f in os.listdir(a.dir) if f.lower().endswith(exts)]
        if not found: sys.exit("[!] No backup files there.")
        a.files=found; a.from_read=None; a.bcmd="add"; a.tool=a.tool or "scan"; a.note=a.note or "bulk scan"
        return cmd_backup(a)
    elif a.bcmd=="list":
        cars=idx["cars"]
        if not cars: print("[i] Library empty."); return
        print(f"{'VIN':<18}{'#':<4}{'Latest':<21}Last note"); print("-"*64)
        for vin,d in sorted(cars.items()):
            b=d["backups"][-1] if d["backups"] else None
            print(f"{vin:<18}{len(d['backups']):<4}{(b['date'][:19].replace('T',' ') if b else '-'):<21}{(b['note'][:26] if b else '')}")
    elif a.bcmd=="find":
        d=idx["cars"].get(a.vin.strip().upper())
        if not d: sys.exit(f"[!] No backups for {a.vin}.")
        print(f"=== {a.vin.upper()} ===  {len(d['backups'])} backup(s)\n")
        for i,b in enumerate(d["backups"],1):
            print(f"[{i}] {b['date'][:19].replace('T',' ')} ({b['tool']}) {('- '+b['note']) if b['note'] else ''}")
            print(f"    {os.path.join(root,b['folder'])}")
            for fn in b["files"]: print(f"      - {fn}")

# ---------------- features cheat sheet ----------------
FEATURES={
 "nbt":[("Sport displays","HU/Kombi"),("Mirror fold","FEM/BDC"),("Welcome lights","FRM")],
 "evo":[("Digital speedo","HU_NBT_EVO/Kombi"),("CarPlay fullscreen*","HU_NBT_EVO (if licensed)"),
        ("Cornering lights","FRM/BDC"),("Mirror fold on lock","BDC/FEM"),("Ambient lighting","BDC/light module"),
        ("Sport/M displays","Kombi/HU"),("Seatbelt chime off","ACSM/BDC"),("Comfort access tweaks","BDC"),("Start-up disclaimer off","HU")],
 "mgu7":[("Digital speed","HU_MGU/Kombi"),("CarPlay fullscreen","HU_MGU (usually factory-on)"),
         ("M-view / sport displays","Kombi/HU"),("Ambient lighting","BDC/BCP"),("Mirror fold","BDC/BCP")],
 "mgu8":[("Digital speed","HU_MGU/Kombi"),("Ambient lighting","BCP"),("Sport displays","Kombi"),
         ("Note","iDrive 8 / 2021+ progressively locked - confirm per car")],
}
def cmd_features(a):
    hu=a.hu
    if hu not in FEATURES: sys.exit("[!] --hu must be one of: nbt, evo, mgu7, mgu8")
    print(f"Codable features - {hu.upper()}{(' / '+a.chassis) if a.chassis else ''}")
    print("(confirm on the car + a current chassis cheat sheet before quoting)\n")
    for feat,mod in FEATURES[hu]: print(f"  {feat:<26} -> {mod}")
    print("\n* fullscreen applies when CarPlay is already licensed on the car.")

# ---------------- invoice ----------------
def cmd_invoice(a):
    job=json.load(open(a.job)) if a.job else {}
    cust=job.get("cust",a.cust or ""); car=job.get("car",a.car or ""); vin=job.get("vin",a.vin or "")
    svc=job.get("services",[]); total=job.get("total",a.total or 0); date=job.get("date",datetime.now().date().isoformat())
    rows="".join(f"<tr><td>{s.get('nm','')}</td><td style='text-align:right'>${s.get('pr','')}</td></tr>" for s in svc) or "<tr><td>Coding service</td><td style='text-align:right'>$"+str(total)+"</td></tr>"
    html=f"""<!doctype html><meta charset=utf-8><title>Vortex Coding - Job Card</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:620px;margin:30px auto;color:#141413;padding:0 18px}}
h1{{letter-spacing:3px;font-size:20px;margin:0}} .ember{{color:#E24E24}} .muted{{color:#777}}
table{{width:100%;border-collapse:collapse;margin:18px 0}} td{{padding:9px 4px;border-bottom:1px solid #eee}}
.tot{{font-weight:800;font-size:20px}} .box{{border:1px solid #eee;border-radius:12px;padding:16px;margin:16px 0}}
small{{color:#999}}</style>
<h1>VORTEX <span class=ember>CODING</span></h1>
<div class=muted>BMW &amp; MINI feature coding - Melbourne &amp; remote</div>
<div class=box><b>Job card</b> &nbsp; <span class=muted>{date}</span><br>
Customer: {cust}<br>Vehicle: {car}<br>VIN: {vin}</div>
<table>{rows}<tr><td class=tot>Total</td><td class=tot style='text-align:right'>${total}</td></tr></table>
<div class=box>{'&#10003; Coding backed up before work' if job.get('backup') else '&#9888; Backup status not recorded'}<br>
{'&#10003; Risks acknowledged / waiver signed' if job.get('waiver') else ''}</div>
<small>Aftermarket service, not affiliated with BMW/MINI. Print to PDF to save.</small>"""
    out=a.out or f"vortex_invoice_{(vin or 'job')}.html"
    open(out,"w").write(html); print(f"[+] Job card -> {out}  (open in a browser, Print -> Save as PDF)")

# ---------------- export to quote tool ----------------
def cmd_export(a):
    r=json.load(open(a.from_read))
    job={"id":0,"date":r.get("read_at","")[:10],"cust":"","car":"","vin":r.get("vin",""),
         "hu":"","services":[],"total":0,"backup":False,"notes":"Prefill from live read"}
    out=a.out or "vortex_prefill.json"
    json.dump([job],open(out,"w"),indent=2)
    print(f"[+] Prefill -> {out}  (VIN {r.get('vin','')} ready for the Job tool)")

# ---------------- CLI ----------------
def main():
    ap=argparse.ArgumentParser(prog="vortex.py",description="Vortex Coding toolkit (read-only diagnostics + file tools)")
    sub=ap.add_subparsers(dest="cmd",required=True)
    def ip(p): p.add_argument("--ip",help="Gateway IP (skip to auto-discover)")
    r=sub.add_parser("read");  ip(r); r.add_argument("--scan",action="store_true"); r.add_argument("--out"); r.set_defaults(fn=cmd_read)
    c=sub.add_parser("check"); ip(c); c.add_argument("--root",default="vortex_backups"); c.set_defaults(fn=cmd_check)
    b=sub.add_parser("battery"); ip(b); b.set_defaults(fn=cmd_battery)
    i=sub.add_parser("istep"); ip(i); i.set_defaults(fn=cmd_istep)
    fa=sub.add_parser("faults"); ip(fa); fa.add_argument("--clear",action="store_true"); fa.set_defaults(fn=cmd_faults)
    df=sub.add_parser("diff"); ip(df); df.add_argument("--module",type=lambda x:int(x,0)); df.add_argument("--label"); df.add_argument("--out"); df.add_argument("--compare",nargs=2,metavar=("BEFORE","AFTER")); df.set_defaults(fn=cmd_diff)
    bk=sub.add_parser("backup"); bk.add_argument("--root",default="vortex_backups")
    bsub=bk.add_subparsers(dest="bcmd",required=True)
    ba=bsub.add_parser("add"); ba.add_argument("--vin"); ba.add_argument("--from-read",dest="from_read"); ba.add_argument("--files",nargs="+"); ba.add_argument("--tool",default="esys",choices=["esys","bimmercode","scan","other"]); ba.add_argument("--note",default="")
    bsc=bsub.add_parser("scan"); bsc.add_argument("--dir",required=True); bsc.add_argument("--vin",required=True); bsc.add_argument("--tool",default="scan"); bsc.add_argument("--note",default=""); bsc.add_argument("--files",nargs="*"); bsc.add_argument("--from-read",dest="from_read")
    bsub.add_parser("list"); bf=bsub.add_parser("find"); bf.add_argument("--vin",required=True)
    bk.set_defaults(fn=cmd_backup)
    ft=sub.add_parser("features"); ft.add_argument("--hu",required=True); ft.add_argument("--chassis"); ft.set_defaults(fn=cmd_features)
    iv=sub.add_parser("invoice"); iv.add_argument("--job"); iv.add_argument("--cust"); iv.add_argument("--car"); iv.add_argument("--vin"); iv.add_argument("--total",type=float); iv.add_argument("--out"); iv.set_defaults(fn=cmd_invoice)
    ex=sub.add_parser("export"); ex.add_argument("--from-read",dest="from_read",required=True); ex.add_argument("--out"); ex.set_defaults(fn=cmd_export)
    ap.add_argument("--verbose",action="store_true",help="show protocol library logs")
    a=ap.parse_args()
    logging.basicConfig(level=logging.WARNING if a.verbose else logging.CRITICAL)
    a.fn(a)

def cmd_check(a):
    ip,gw=_find_car(a.ip); print(f"[+] Car at {ip}")
    _,uds=_connect(ip,gw); ident=_read_dids(uds)
    vin=ident.get("VIN","UNKNOWN"); print(f"    VIN: {vin}")
    sw=ident.get("SW version") or ident.get("System name") or ""
    if sw: print(f"    Firmware/SW: {sw}")
    v=_battery(uds)
    if v is not None:
        print(f"    Battery: {v} V " + ("OK" if v>=12.5 else "- LOW, charger on before flashing"))
    # backup check
    idx=_bk_index(a.root); has=vin.strip().upper() in idx.get("cars",{})
    print("    Backup on file: " + ("YES" if has else "NO - make + file one before coding"))
    print("\n[gate] " + ("Clear to code (backup on file)." if has else "STOP: no backup filed for this VIN yet."))

if __name__=="__main__":
    main()
