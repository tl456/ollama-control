# -*- coding: utf-8 -*-
"""Original MIT tl456 adapter executed by the installed NX journal runtime (Python 3.6+).
Uses public NX Open / UF APIs. It never saves the supplied source part.
"""
import json
import os
import sys

def inspect(request):
    import NXOpen
    import NXOpen.UF
    session = NXOpen.Session.GetSession()
    # NX12 Python enum NotSet is documented as "load none of the components".
    # Do not follow absolute paths stored in the source assembly.
    session.Parts.LoadOptions.ComponentsToLoad = NXOpen.LoadOptions.LoadComponents.NotSet
    source = request["path"]
    suffix = os.path.splitext(source)[1].lower()
    status = None
    if suffix in (".step", ".stp"):
        importer = session.DexManager.CreateStep214Importer()
        try:
            importer.InputFile = source
            importer.OutputFile = os.path.join(os.path.dirname(source), "translated.prt")
            importer.SettingsFile = os.path.join(request["nx_directory"], "STEP214UG", "step214ug.def")
            importer.ImportTo = NXOpen.Step214Importer.ImportToOption.NewPart
            importer.ProcessHoldFlag = True
            importer.Commit()
        finally:
            importer.Destroy()
        source = os.path.join(os.path.dirname(source), "translated.prt")
    part, status = session.Parts.OpenDisplay(source)
    warnings = []
    if status is not None:
        try:
            for i in range(min(20,status.NumberUnloadedParts)):
                warnings.append(status.GetStatusDescription(i))
        finally:
            status.Dispose()
    if part is None:
        raise ValueError("NX未返回Part")
    bodies = list(part.Bodies)
    size = max(1,min(100,int(request.get("page_size",50))))
    page = max(1,int(request.get("page",1)))
    uf = NXOpen.UF.UFSession.GetUFSession()
    items = []
    for index in range((page-1)*size,min(len(bodies),page*size)):
        body = bodies[index]
        item = {"index":index+1,"name":body.Name[:200],"solid":bool(body.IsSolidBody),
                "faces":len(body.GetFaces()),"edges":len(body.GetEdges())}
        try:
            item["bounding_box"] = list(uf.ModlGeneral.AskBoundingBox(body.Tag))
        except Exception:
            item["bounding_box"] = None
            warnings.append("实体{}包围盒未取得".format(index+1))
        items.append(item)
    components=[]
    root=part.ComponentAssembly.RootComponent
    if root is not None:
        for child in list(root.GetChildren())[:100]:
            components.append({"name":child.DisplayName[:200],"suppressed":bool(child.IsSuppressed)})
    units = "mm" if part.PartUnits == NXOpen.BasePart.Units.Millimeters else "inch" if part.PartUnits == NXOpen.BasePart.Units.Inches else "unknown"
    return {"ok":True,"backend":"nx","interface":"NX Open / UF","units":units,
            "bodies":items,"total_bodies":len(bodies),"page":page,"page_size":size,
            "assembly_children":components,"assembly_scope":"首层最多100项；不递归加载外部文件",
            "warnings":warnings[:20],"not_obtained":["材料密度","质量"],
            "scope":"当前Part的已加载实体；包围盒单位同Part；未把缺少几何当作零体积"}

def main():
    request_path = sys.argv[-1]
    with open(request_path,"r",encoding="utf-8-sig") as stream:
        request=json.load(stream)
    try:
        result=inspect(request)
    except Exception as error:
        result={"ok":False,"error":str(error)[:400],"error_type":type(error).__name__}
    with open(request["output"],"w",encoding="utf-8") as stream:
        json.dump(result,stream,ensure_ascii=True,allow_nan=False)

if __name__=="__main__":
    main()
