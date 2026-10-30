"""Opt-in real Office synthetic document check. Never opens user documents."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

def main():
    if not os.environ.get("LOCAL_AI_ROOT"):
        raise SystemExit("Set LOCAL_AI_ROOT to an isolated empty test directory")
    root=Path(os.environ["LOCAL_AI_ROOT"]).resolve()
    marker=root/"office-v16-test.json"
    if marker.exists(): raise SystemExit("Choose a fresh test root")
    root.mkdir(parents=True,exist_ok=True)
    from PIL import Image
    from app.office_operations import execute,preview_file
    picture=root/"synthetic.png"
    Image.new("RGB",(600,300),(35,110,160)).save(picture)
    checks={}; outputs={}
    start=time.time()
    cases={
      "word":(".docx",[{"op":"insert_text","text":"Synthetic inspection","size":20,"bold":True},{"op":"insert_text","text":"Verified sample 42."},{"op":"add_table","values":[["Item","Value"],["A","42"]]},{"op":"insert_picture","image_path":str(picture),"width":160,"caption":"Figure 1 synthetic"}]),
      "excel":(".xlsx",[{"op":"write","range":"A1","values":[["Item","Value"],["A",42],["B",8]]},{"op":"formula","range":"B4","formula":"=SUM(B2:B3)"},{"op":"format","range":"A1:B1","bold":True,"fill":"#DDEEFF"},{"op":"format","range":"B2:B4","number_format":"0.00"},{"op":"insert_picture","image_path":str(picture),"range":"D2","width":120},{"op":"print_setup","range":"A1:F12"}]),
      "powerpoint":(".pptx",[{"op":"page_setup","width":960,"height":540},{"op":"add_slide","layout":12},{"op":"add_text","text":"Synthetic inspection","size":28,"bold":True},{"op":"insert_picture","image_path":str(picture),"left":60,"top":160,"width":360},{"op":"add_table","values":[["Item","Value"],["A","42"]],"left":500,"top":160,"width":300,"height":150}])
    }
    for kind,(extension,operations) in cases.items():
        path=root/("synthetic"+extension)
        try:
            result=execute({"action":"create","path":str(path),"operations":operations,"preview":True,"page_count":2})
            checks[kind+"_created"]=path.is_file() and result["verified_saved"]
            checks[kind+"_preview"]=bool(result.get("preview",{}).get("items"))
            outputs[kind]=result
            if kind=="powerpoint":checks["powerpoint_user_windows_preserved"]=result.get("user_presentations_unchanged",True)
            if result.get("preview"):
                checks[kind+"_png"]=all(preview_file(result["preview_id"],Path(item["url"]).name).read_bytes().startswith(b"\x89PNG") for item in result["preview"]["items"])
            if kind=="word":
                edit=[{"op":"replace_text","find":"Verified sample 42.","text":"Verified sample 43."},{"op":"format","paragraph":1,"size":22,"color":"#123456"}]
            elif kind=="excel":
                edit=[{"op":"write","range":"B2","values":[[43]]},{"op":"freeze","rows":1},{"op":"conditional_format","range":"B2:B3","value":10,"fill":"#CCFFCC"}]
            else:
                edit=[{"op":"text","slide":1,"shape":1,"text":"Updated synthetic inspection"},{"op":"position","slide":1,"shape":2,"left":80}]
            changed=execute({"action":"edit","path":str(path),"operations":edit,"preview":False})
            checks[kind+"_snapshot"]=bool(changed.get("snapshot_id"))
            from hashlib import sha256
            before=sha256(path.read_bytes()).hexdigest()
            try:
                execute({"action":"edit","path":str(path),"operations":[{"op":"not_supported"}],"preview":False})
                checks[kind+"_rollback"]=False
            except ValueError:
                checks[kind+"_rollback"]=sha256(path.read_bytes()).hexdigest()==before
            inspection=execute({"action":"inspect","path":str(path),**({"range":"B2:B4"} if kind=="excel" else {})})
            outputs[kind+"_inspection"]=inspection
            if kind=="excel":
                checks[kind+"_actual_value"]=inspection["structure"]["values"][0][0]==43
            elif kind=="word":
                checks[kind+"_actual_value"]=any("43" in p["text"] for p in inspection["structure"]["paragraphs"])
            else:
                checks[kind+"_actual_value"]="Updated" in inspection["structure"]["slides"][0]["shapes"][0]["text"]
        except Exception as error:
            checks[kind+"_exception"]=False; outputs[kind+"_error"]=str(error)
        marker.write_text(json.dumps({"checks":checks,"outputs":outputs,"passed":all(checks.values()),"seconds":time.time()-start},ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"checks":checks,"passed":all(checks.values()),"seconds":time.time()-start},ensure_ascii=False))
    return 0 if all(checks.values()) else 1

if __name__=="__main__": raise SystemExit(main())
