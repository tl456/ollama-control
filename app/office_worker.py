"""Office COM worker. Operates one staged document; never commits a user's file."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys

def color(value):
    if isinstance(value, int):
        return value
    text = str(value).lstrip("#")
    if len(text) != 6:
        raise ValueError("颜色需要 #RRGGBB")
    r, g, b = bytes.fromhex(text)
    return r | g << 8 | b << 16

def font(target, spec):
    for key, prop in (("font", "Name"), ("size", "Size"), ("bold", "Bold"), ("italic", "Italic")):
        if key in spec:
            setattr(target, prop, spec[key])
    if "color" in spec:
        try:
            target.Color.RGB = color(spec["color"])
        except AttributeError:
            target.Color = color(spec["color"])

def point(spec, key, default):
    value = float(spec.get(key, default))
    if not 0 <= value <= 100000:
        raise ValueError(key + "尺寸/位置无效（单位磅，1厘米约28.35磅）")
    return value

def picture_size(spec):
    from PIL import Image
    with Image.open(spec["image_path"]) as im:
        width, height = im.size
    w = point(spec, "width", min(360, width * .75))
    h = point(spec, "height", w * height / width)
    return w, h

def word_range(doc, spec):
    if "paragraph" in spec:
        return doc.Paragraphs(int(spec["paragraph"])).Range
    if "bookmark" in spec:
        return doc.Bookmarks(spec["bookmark"]).Range
    return doc.Content

def word_edit(doc, s):
    op = s["op"]
    if op in ("insert_text", "insert_picture", "page_break"):
        if "after_text" in s:
            r = doc.Content.Duplicate
            if not r.Find.Execute(s["after_text"]):
                raise ValueError("未找到 after_text")
            r.Collapse(0)
            r.InsertParagraphAfter()
            r.Collapse(0)
        elif "paragraph" in s:
            r = doc.Paragraphs(int(s["paragraph"])).Range
            r.Collapse(0)
        elif "bookmark" in s:
            r = doc.Bookmarks(s["bookmark"]).Range
            r.Collapse(0)
        else:
            r = doc.Range(doc.Content.End - 1, doc.Content.End - 1)
        if op == "insert_text":
            start = r.Start
            text = s.get("text", "") + ("\r" if s.get("newline", True) else "")
            r.InsertAfter(text)
            changed = doc.Range(start, start + len(text))
            if "style" in s:
                changed.Style = s["style"]
            font(changed.Font, s)
        elif op == "page_break":
            r.InsertBreak(7)
        else:
            shape = doc.InlineShapes.AddPicture(s["image_path"], False, True, r)
            shape.LockAspectRatio = -1
            shape.Width, shape.Height = picture_size(s)
            shape.Range.ParagraphFormat.Alignment = {"left":0,"center":1,"right":2}.get(s.get("align", "center"),1)
            if s.get("caption"):
                end = doc.Range(shape.Range.End, shape.Range.End)
                end.InsertAfter("\r" + s["caption"] + "\r")
            if s.get("wrap") and s["wrap"]!="inline":
                floating=shape.ConvertToShape()
                floating.WrapFormat.Type={"square":0,"tight":1,"through":2,"front":3,"top_bottom":4,"behind":5}[s["wrap"]]
    elif op == "replace_text":
        r = word_range(doc, s)
        find = r.Find
        find.ClearFormatting()
        find.Replacement.ClearFormatting()
        if not s.get("find"):
            raise ValueError("查找文字不能为空")
        if not find.Execute(s["find"],False,False,False,False,False,True,0,False,s.get("text",""),2):
            raise ValueError("未找到待替换文字")
    elif op == "delete_paragraph":
        doc.Paragraphs(int(s["paragraph"])).Range.Delete()
    elif op == "format":
        r = word_range(doc, s)
        font(r.Font, s)
        if "style" in s:
            r.Style = s["style"]
        for key, prop in (("space_before","SpaceBefore"),("space_after","SpaceAfter"),("line_spacing","LineSpacing")):
            if key in s:
                setattr(r.ParagraphFormat, prop, float(s[key]))
        if "align" in s:
            r.ParagraphFormat.Alignment = {"left":0,"center":1,"right":2,"justify":3}[s["align"]]
        if s.get("list") == "bullet":
            r.ListFormat.ApplyBulletDefault()
        elif s.get("list") == "number":
            r.ListFormat.ApplyNumberDefault()
    elif op == "page_setup":
        setup = doc.PageSetup
        if "orientation" in s:
            setup.Orientation = 1 if s["orientation"] == "landscape" else 0
        for key, prop in (("top","TopMargin"),("bottom","BottomMargin"),("left","LeftMargin"),("right","RightMargin")):
            if key in s:
                setattr(setup, prop, point(s,key,72))
    elif op == "header_footer":
        sec = doc.Sections(int(s.get("section",1)))
        if "header" in s:
            sec.Headers(1).Range.Text = s["header"]
        if "footer" in s:
            sec.Footers(1).Range.Text = s["footer"]
        if s.get("page_numbers"):
            sec.Footers(1).PageNumbers.Add()
    elif op == "add_table":
        rows = s.get("values", [])
        if not rows or not isinstance(rows[0],list):
            raise ValueError("values需要二维表格")
        table = doc.Tables.Add(doc.Range(doc.Content.End-1,doc.Content.End-1),len(rows),max(map(len,rows)))
        for i,row in enumerate(rows,1):
            for j,val in enumerate(row,1):
                table.Cell(i,j).Range.Text = str(val)
        table.Borders.Enable = True
    elif op == "table_cell":
        cell = doc.Tables(int(s["table"])).Cell(int(s["row"]),int(s["column"]))
        if "text" in s:
            cell.Range.Text = s["text"]
        font(cell.Range.Font,s)
        if "fill" in s:
            cell.Shading.BackgroundPatternColor = color(s["fill"])
    elif op == "table_format":
        table = doc.Tables(int(s["table"]))
        if "style" in s:
            table.Style = s["style"]
        if "width" in s:
            table.PreferredWidthType, table.PreferredWidth = 3,float(s["width"])
        if "borders" in s:
            table.Borders.Enable = bool(s["borders"])
        font(table.Range.Font,s)
    else:
        raise ValueError("Word不支持操作：" + op)

def excel_edit(book, s):
    from pythoncom import Missing
    op = s["op"]
    sheet = book.Worksheets(s.get("sheet",1))
    if op == "add_sheet":
        target = book.Worksheets.Add(Missing,book.Worksheets(book.Worksheets.Count))
        target.Name = s["name"]
        return
    if op == "rename_sheet":
        sheet.Name = s["name"]; return
    if op == "copy_sheet":
        sheet.Copy(Missing,book.Worksheets(book.Worksheets.Count))
        if "name" in s:
            book.Worksheets(book.Worksheets.Count).Name = s["name"]
        return
    if op == "delete_sheet":
        if book.Worksheets.Count <= 1:
            raise ValueError("至少保留一个工作表")
        sheet.Delete(); return
    if op == "insert_picture":
        w,h = picture_size(s)
        cell = sheet.Range(s.get("range","A1"))
        pic = sheet.Shapes.AddPicture(s["image_path"],False,True,point(s,"left",cell.Left),point(s,"top",cell.Top),w,h)
        pic.LockAspectRatio = -1
        pic.Placement = {"move_size":1,"move":2,"free":3}.get(s.get("placement","move"),2)
        return
    r = sheet.Range(s.get("range","A1"))
    if r.CountLarge > 100000:
        raise ValueError("单次区域最多100000单元格")
    if op == "write":
        values=s.get("values")
        if isinstance(values,list):
            if not values or any(not isinstance(row,list) for row in values):
                raise ValueError("values需要二维数组")
            if len({len(row) for row in values}) != 1:
                raise ValueError("每行列数需要一致")
            r = r.Cells(1,1).Resize(len(values),len(values[0]))
            r.Value = tuple(tuple(row) for row in values)
        else:
            r.Value = values
    elif op == "formula":
        r.Formula = s["formula"]
    elif op == "format":
        font(r.Font,s)
        if "fill" in s:
            r.Interior.Color = color(s["fill"])
        if "number_format" in s:
            r.NumberFormat = s["number_format"]
        if "align" in s:
            r.HorizontalAlignment = {"left":-4131,"center":-4108,"right":-4152}[s["align"]]
        if "wrap" in s:
            r.WrapText = bool(s["wrap"])
        if "borders" in s:
            r.Borders.LineStyle = 1 if s["borders"] else -4142
        if "row_height" in s:
            r.RowHeight = float(s["row_height"])
        if "column_width" in s:
            r.ColumnWidth = float(s["column_width"])
        if s.get("autofit"):
            r.Columns.AutoFit()
    elif op in ("insert_rows","delete_rows","insert_columns","delete_columns"):
        target = r.EntireRow if "rows" in op else r.EntireColumn
        target.Insert() if op.startswith("insert") else target.Delete()
    elif op == "merge":
        nonempty = sum(v not in (None,"") for row in (r.Value if isinstance(r.Value,tuple) else ((r.Value,),)) for v in row)
        if nonempty > 1 and not s.get("allow_data_loss",False):
            raise ValueError("合并区域有多个值；请先处理数据或明确allow_data_loss")
        r.Merge()
    elif op == "unmerge":
        r.UnMerge()
    elif op == "clear":
        r.ClearContents()
    elif op == "sort":
        r.Sort(sheet.Range(s.get("key",r.Cells(1,1).Address)),2 if s.get("descending") else 1,Missing,Missing,1,Missing,1,1 if s.get("header",True) else 2)
    elif op == "filter":
        r.AutoFilter(int(s.get("field",1)),s.get("criteria",""))
    elif op == "freeze":
        sheet.Activate()
        win=book.Windows(1)
        win.FreezePanes=False
        win.SplitRow=int(s.get("rows",1)); win.SplitColumn=int(s.get("columns",0))
        win.FreezePanes=True
    elif op == "conditional_format":
        cond=r.FormatConditions.Add(1,{"greater":5,"less":6,"equal":3,"between":1}[s.get("operator","greater")],str(s["value"]),str(s.get("value2","")))
        if "fill" in s: cond.Interior.Color=color(s["fill"])
        font(cond.Font,s)
    elif op == "validation":
        r.Validation.Delete()
        r.Validation.Add(3,1,1,s["choices"])
    elif op == "print_setup":
        setup=sheet.PageSetup
        setup.PrintArea=s.get("range","")
        setup.Orientation=2 if s.get("orientation")=="landscape" else 1
        setup.Zoom=False; setup.FitToPagesWide=int(s.get("pages_wide",1)); setup.FitToPagesTall=False
    else:
        raise ValueError("Excel不支持操作：" + op)

def ppt_edit(pres,s):
    op=s["op"]
    if op=="theme":
        theme=Path(s["theme_path"])
        if not theme.is_absolute() or theme.suffix.lower()!=".thmx" or not theme.is_file():
            raise ValueError("theme_path需要本机有效thmx主题文件")
        pres.ApplyTheme(str(theme)); return
    if op=="add_slide":
        pres.Slides.Add(int(s.get("slide",pres.Slides.Count+1)),int(s.get("layout",12))); return
    if op=="page_setup":
        pres.PageSetup.SlideWidth=point(s,"width",960); pres.PageSetup.SlideHeight=point(s,"height",540); return
    slide=pres.Slides(int(s.get("slide",1)))
    if op=="copy_slide":
        slide.Duplicate(); return
    if op=="delete_slide":
        slide.Delete(); return
    if op=="layout":
        slide.Layout=int(s["layout"]); return
    if op=="background":
        slide.FollowMasterBackground=0
        slide.Background.Fill.Solid(); slide.Background.Fill.ForeColor.RGB=color(s["fill"]); return
    if op=="insert_picture":
        w,h=picture_size(s)
        shape=slide.Shapes.AddPicture(s["image_path"],False,True,point(s,"left",36),point(s,"top",36),w,h)
        shape.LockAspectRatio=-1
        if "crop_left" in s: shape.PictureFormat.CropLeft=float(s["crop_left"])
        if "crop_right" in s: shape.PictureFormat.CropRight=float(s["crop_right"])
        if "crop_top" in s: shape.PictureFormat.CropTop=float(s["crop_top"])
        if "crop_bottom" in s: shape.PictureFormat.CropBottom=float(s["crop_bottom"])
        return
    if op=="add_text":
        shape=slide.Shapes.AddTextbox(1,point(s,"left",36),point(s,"top",36),point(s,"width",600),point(s,"height",100))
        shape.TextFrame.TextRange.Text=s.get("text","")
        font(shape.TextFrame.TextRange.Font,s); return
    if op=="add_table":
        values=s["values"]
        shape=slide.Shapes.AddTable(len(values),max(map(len,values)),point(s,"left",36),point(s,"top",100),point(s,"width",600),point(s,"height",200))
        for i,row in enumerate(values,1):
            for j,val in enumerate(row,1):
                shape.Table.Cell(i,j).Shape.TextFrame.TextRange.Text=str(val)
        return
    if op in ("align","distribute"):
        indices=tuple(int(i) for i in s["shapes"])
        r=slide.Shapes.Range(indices)
        if op=="align":
            r.Align({"left":0,"center":1,"right":2,"top":3,"middle":4,"bottom":5}[s["align"]],bool(s.get("relative_to_slide",False)))
        else:
            r.Distribute(0 if s.get("direction","horizontal")=="horizontal" else 1,bool(s.get("relative_to_slide",False)))
        return
    shape=slide.Shapes(s.get("shape",1))
    if op=="delete_shape":
        shape.Delete()
    elif op in ("text","format"):
        if "text" in s: shape.TextFrame.TextRange.Text=s["text"]
        font(shape.TextFrame.TextRange.Font,s)
        if "align" in s:
            shape.TextFrame.TextRange.ParagraphFormat.Alignment={"left":1,"center":2,"right":3,"justify":4}[s["align"]]
        for key,prop in (("space_before","SpaceBefore"),("space_after","SpaceAfter")):
            if key in s: setattr(shape.TextFrame.TextRange.ParagraphFormat,prop,float(s[key]))
    elif op=="position":
        for key,prop in (("left","Left"),("top","Top"),("width","Width"),("height","Height"),("rotation","Rotation")):
            if key in s: setattr(shape,prop,float(s[key]))
    elif op=="layer":
        shape.ZOrder({"front":0,"back":1,"forward":2,"backward":3}[s["direction"]])
    elif op=="table_cell":
        text=shape.Table.Cell(int(s["row"]),int(s["column"])).Shape.TextFrame.TextRange
        if "text" in s: text.Text=s["text"]
        font(text.Font,s)
    else:
        raise ValueError("PPT不支持操作："+op)

def inspect(doc,kind,args):
    start=max(1,int(args.get("offset",1))); limit=max(1,min(100,int(args.get("limit",30))))
    if kind=="word":
        count=doc.Paragraphs.Count
        result={"paragraph_count":count,"page_count":doc.ComputeStatistics(2),"table_count":doc.Tables.Count,
                "paragraphs":[{"paragraph":i,"text":str(doc.Paragraphs(i).Range.Text)[:2000],"style":str(doc.Paragraphs(i).Style)} for i in range(start,min(count+1,start+limit))]}
        if "table" in args:
            t=doc.Tables(int(args["table"]))
            result["cells"]=[[str(t.Cell(r,c).Range.Text).rstrip("\r\x07")[:2000] for c in range(1,min(t.Columns.Count,30)+1)] for r in range(1,min(t.Rows.Count,100)+1)]
        return result
    if kind=="excel":
        sheets=[{"index":i,"name":doc.Worksheets(i).Name,"rows":doc.Worksheets(i).UsedRange.Rows.Count,"columns":doc.Worksheets(i).UsedRange.Columns.Count} for i in range(1,min(doc.Worksheets.Count,200)+1)]
        result={"sheets":sheets}
        if "range" in args:
            r=doc.Worksheets(args.get("sheet",1)).Range(args["range"])
            if r.CountLarge>10000: raise ValueError("读取区域最多10000单元格")
            result.update(values=r.Value,formulas=r.Formula,range=r.Address)
        return result
    total=doc.Slides.Count
    slides=[]
    for i in range(start,min(total+1,start+limit)):
        slide=doc.Slides(i); shapes=[]
        for j in range(1,min(slide.Shapes.Count,100)+1):
            shape=slide.Shapes(j)
            row={"shape":j,"name":shape.Name,"left":shape.Left,"top":shape.Top,"width":shape.Width,"height":shape.Height}
            if shape.HasTextFrame: row["text"]=str(shape.TextFrame.TextRange.Text)[:2000]
            if shape.HasTable: row["table"]={"rows":shape.Table.Rows.Count,"columns":shape.Table.Columns.Count}
            shapes.append(row)
        slides.append({"slide":i,"layout":slide.Layout,"shapes":shapes})
    return {"slide_count":total,"slides":slides,"width":doc.PageSetup.SlideWidth,"height":doc.PageSetup.SlideHeight}


def emf_png(blob,filename,width,height):
    """Rasterize Office's documented EMF output through Windows GDI."""
    import ctypes as c
    from ctypes import wintypes as w
    from PIL import Image
    class Header(c.Structure):
        _fields_=[("size",w.DWORD),("width",w.LONG),("height",w.LONG),("planes",w.WORD),("bits",w.WORD),("compression",w.DWORD),("image_size",w.DWORD),("xppm",w.LONG),("yppm",w.LONG),("used",w.DWORD),("important",w.DWORD)]
    g=c.windll.gdi32
    g.SetEnhMetaFileBits.argtypes=[w.UINT,c.c_void_p];g.SetEnhMetaFileBits.restype=c.c_void_p
    g.CreateCompatibleDC.argtypes=[c.c_void_p];g.CreateCompatibleDC.restype=c.c_void_p
    g.CreateDIBSection.argtypes=[c.c_void_p,c.c_void_p,w.UINT,c.POINTER(c.c_void_p),c.c_void_p,w.DWORD];g.CreateDIBSection.restype=c.c_void_p
    g.SelectObject.argtypes=[c.c_void_p,c.c_void_p];g.SelectObject.restype=c.c_void_p
    g.PlayEnhMetaFile.argtypes=[c.c_void_p,c.c_void_p,c.POINTER(w.RECT)]
    g.DeleteObject.argtypes=[c.c_void_p];g.DeleteEnhMetaFile.argtypes=[c.c_void_p];g.DeleteDC.argtypes=[c.c_void_p]
    width=int(width);height=int(height)
    if not 1<=width<=4096 or not 1<=height<=8192: raise ValueError("预览像素范围无效")
    raw=c.create_string_buffer(bytes(blob)); emf=g.SetEnhMetaFileBits(len(blob),raw)
    if not emf: raise ValueError("Office未返回有效页面图像")
    dc=bitmap=old=None
    try:
        dc=g.CreateCompatibleDC(None); pixels=c.c_void_p()
        header=Header(c.sizeof(Header),width,-height,1,32,0,width*height*4,0,0,0,0)
        bitmap=g.CreateDIBSection(dc,c.byref(header),0,c.byref(pixels),None,0)
        if not bitmap or not pixels.value: raise ValueError("无法创建页面预览位图")
        old=g.SelectObject(dc,bitmap)
        c.memset(pixels,255,width*height*4)
        rect=w.RECT(0,0,width,height)
        if not g.PlayEnhMetaFile(dc,emf,c.byref(rect)): raise ValueError("Office页面渲染失败")
        image=Image.frombytes("RGB",(width,height),c.string_at(pixels,width*height*4),"raw","BGRX")
        image.save(filename);image.close()
    finally:
        if dc and old:g.SelectObject(dc,old)
        if bitmap:g.DeleteObject(bitmap)
        if dc:g.DeleteDC(dc)
        g.DeleteEnhMetaFile(emf)

def word_page_images(doc,folder,start,count):
    doc.Repaginate()
    doc.ActiveWindow.View.Type=3
    pages=doc.ActiveWindow.Panes(1).Pages
    total=pages.Count
    chosen=list(range(start,min(total+1,start+count)))
    if not chosen:raise ValueError("页码超出文档范围")
    for index in chosen:
        page=pages.Item(index)
        blob=bytes(int(b)&255 for b in page.EnhMetaFileBits)
        emf_png(blob,str(folder/f"page-{index}.png"),1440,round(1440*page.Height/page.Width))
    return {"total_pages":total,"pages":chosen,"method":"Office 页面图像"}

def excel_range_image(book,args,folder):
    """Office CopyPicture fallback, restoring the pre-existing clipboard object."""
    import ctypes as c
    import pythoncom
    import win32clipboard
    import win32con
    from ctypes import wintypes as w
    sheet=book.Worksheets(args.get("sheet",1))
    address=args.get("range") or sheet.PageSetup.PrintArea
    area=sheet.Range(address) if address else sheet.UsedRange
    if area.Areas.Count!=1 or area.CountLarge>10000:
        raise ValueError("区域预览需要单一连续区域，最多10000单元格；请指定range")
    width=float(area.Width); height=float(area.Height)
    if width<=0 or height<=0: raise ValueError("预览区域为空")
    status=c.windll.ole32.OleInitialize(None)
    if status not in (0,1): raise ValueError("无法初始化Office图像剪贴板")
    old=None
    sequence=None
    try:
        old=pythoncom.OleGetClipboard()
        sheet.Activate()
        area.CopyPicture(1,-4147)
        sequence=c.windll.user32.GetClipboardSequenceNumber()
        win32clipboard.OpenClipboard()
        try:
            c.windll.user32.GetClipboardData.argtypes=[w.UINT]
            c.windll.user32.GetClipboardData.restype=c.c_void_p
            handle=c.windll.user32.GetClipboardData(win32con.CF_ENHMETAFILE)
            if not handle: raise ValueError("Excel未返回区域图像")
            g=c.windll.gdi32
            g.GetEnhMetaFileBits.argtypes=[c.c_void_p,w.UINT,c.c_void_p]
            g.GetEnhMetaFileBits.restype=w.UINT
            size=g.GetEnhMetaFileBits(handle,0,None)
            if not 0<size<=32*1024*1024: raise ValueError("Excel区域图像大小异常")
            buffer=c.create_string_buffer(size)
            if g.GetEnhMetaFileBits(handle,size,buffer)!=size: raise ValueError("Excel区域图像读取失败")
            data=buffer.raw
        finally:
            win32clipboard.CloseClipboard()
        scale=min(2,1440/width,4096/height)
        emf_png(data,str(folder/"page-1.png"),max(1,round(width*scale)),max(1,round(height*scale)))
        return {"total_pages":1,"pages":[1],"method":"Office工作表区域图片（未按打印分页）","range":area.Address}
    finally:
        try:
            if sequence is not None and c.windll.user32.GetClipboardSequenceNumber()==sequence:
                pythoncom.OleSetClipboard(old)
                pythoncom.OleFlushClipboard()
        finally:
            old=None
            c.windll.ole32.OleUninitialize()
def render(doc,kind,args,folder):
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=True)
    start=max(1,int(args.get("page",1))); count=max(1,min(10,int(args.get("page_count",3))))
    if kind=="powerpoint":
        total=doc.Slides.Count
        pages=list(range(start,min(total+1,start+count)))
        if not pages: raise ValueError("页码超出文档范围")
        for page in pages:
            filename=folder/f"page-{page}.png"
            doc.Slides(page).Export(str(filename),"PNG",1440,round(1440*doc.PageSetup.SlideHeight/doc.PageSetup.SlideWidth))
            with filename.open("rb") as stream: signature=stream.read(24)
            if signature.startswith(b"%TSD-Header"):
                raise ValueError("Office导出的预览图受到本机透明加密保护，浏览器无法直接读取")
            if not signature.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("PowerPoint未输出可读取的PNG预览图")
        return {"total_pages":total,"pages":pages}
    if kind=="word" and args.get("render_method","native")=="native":
        return word_page_images(doc,folder,start,count)
    if kind=="excel" and args.get("preview_mode")=="range":
        return excel_range_image(doc,args,folder)
    pdf=folder/"document.pdf"
    if kind=="word":
        doc.Repaginate()
        total=doc.ComputeStatistics(2)
        if start>total: raise ValueError("页码超出文档范围")
        end=min(total,start+count-1)
        doc.ExportAsFixedFormat(str(pdf),17,False,0,3,start,end,0,False)
        pages=list(range(start,end+1))
    else:
        sheet=doc.Worksheets(args.get("sheet",1))
        if args.get("range"):
            sheet.PageSetup.PrintArea=args["range"]
        sheet.ExportAsFixedFormat(0,str(pdf),0,False,False,start,start+count-1,False)
        pages=[]
        total=None
    import pypdfium2 as pdfium
    with pdf.open("rb") as stream:
        header=stream.read(24)
    if header.startswith(b"%TSD-Header"):
        raise ValueError("Office导出的PDF受到本机透明加密保护，标准PDF渲染器无法读取")
    if not header.startswith(b"%PDF-"):
        raise ValueError("Office未输出可读取的标准PDF")
    with pdfium.PdfDocument(str(pdf)) as rendered:
        for i in range(min(len(rendered),count)):
            page_no=pages[i] if pages else start+i
            page=rendered[i]
            bitmap=page.render(scale=min(2,1440/max(1,page.get_width())))
            image=bitmap.to_pil()
            image.save(folder/f"page-{page_no}.png")
            image.close(); bitmap.close(); page.close()
        if not pages: pages=list(range(start,start+min(len(rendered),count)))
    return {"total_pages":total,"pages":pages,"pdf_name":"document.pdf","partial":True}

def execute(payload):
    import pythoncom
    import win32com.client
    import win32process
    import psutil
    pythoncom.CoInitialize()
    app=doc=None; owned=False; kind=payload["application"]
    presentation_ids=None;result=None
    names={"word":("Word.Application","winword.exe"),"excel":("Excel.Application","excel.exe"),"powerpoint":("PowerPoint.Application","powerpnt.exe")}
    before={p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower()==names[kind][1]}
    try:
        app=win32com.client.DispatchEx(names[kind][0])
        pid=None
        for attribute in ("Hwnd","HWND"):
            try:
                hwnd=getattr(app,attribute)
                candidate=win32process.GetWindowThreadProcessId(int(hwnd() if callable(hwnd) else hwnd))[1]
                if candidate>0:
                    pid=candidate;break
            except Exception:
                continue
        if pid is None:
            fresh={p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower()==names[kind][1]}-before
            if len(fresh)==1:
                pid=fresh.pop()
            elif kind=="powerpoint" and before:
                pid=next(iter(before))
            else:
                raise ValueError("无法确认独立Office进程，未接管已有窗口；请保存并关闭该Office程序后重试")
        owned=pid not in before
        if not owned and (kind!="powerpoint" or not payload.get("macro_free")):
            raise ValueError("当前Office已运行，无法确认该文稿无宏；请保存并关闭PowerPoint后重试，工具不会修改应用全局设置")
        if not owned:
            presentation_ids=[app.Presentations(i)._oleobj_.QueryInterface(pythoncom.IID_IUnknown) for i in range(1,app.Presentations.Count+1)]
        if owned:
            Path(payload["ownership"]).write_text(json.dumps({"pid":pid,"created":psutil.Process(pid).create_time(),"name":names[kind][1]}),encoding="utf-8")
            app.AutomationSecurity=3
        if owned and kind in ("word","excel"):
            app.Visible=False
            app.DisplayAlerts=0
        if owned and kind=="excel":
            app.EnableEvents=False
            app.AskToUpdateLinks=False
        path=payload["path"]; action=payload["action"]
        if action=="create":
            doc=app.Documents.Add() if kind=="word" else app.Workbooks.Add() if kind=="excel" else app.Presentations.Add(False)
        elif kind=="word":
            doc=app.Documents.Open(path,False,action in ("inspect","preview"),False)
        elif kind=="excel":
            doc=app.Workbooks.Open(path,0,action in ("inspect","preview"))
        else:
            doc=app.Presentations.Open(path,action in ("inspect","preview"),False,False)
        if action in ("create","edit"):
            for step in payload.get("operations",[]):
                {"word":word_edit,"excel":excel_edit,"powerpoint":ppt_edit}[kind](doc,step)
            if action=="create":
                if kind=="word": doc.SaveAs2(path,16)
                elif kind=="excel": doc.SaveAs(path,51)
                else: doc.SaveAs(path,24)
            else:
                doc.Save()
            if not Path(path).is_file() or Path(path).stat().st_size==0:
                raise ValueError("Office未生成有效保存文件")
            # Close and reopen persisted bytes: inspect is not only the unsaved COM view.
            doc.Close(False) if kind!="powerpoint" else doc.Close()
            doc=None
            if kind=="word": doc=app.Documents.Open(path,False,True,False)
            elif kind=="excel": doc=app.Workbooks.Open(path,0,True)
            else: doc=app.Presentations.Open(path,True,False,False)
        result={"application":kind,"structure":inspect(doc,kind,payload),"verified_saved":action in ("create","edit")}
        if payload.get("preview_dir"):
            try:
                result["render"]=render(doc,kind,payload,payload["preview_dir"])
            except Exception as error:
                if kind=="excel" and payload.get("preview_mode","auto")=="auto":
                    try:
                        result["render"]=excel_range_image(doc,payload,Path(payload["preview_dir"]))
                        result["preview_note"]="打印页预览未生成，已显示Office工作表区域图片。"
                    except Exception as fallback:
                        result["preview_error"]="预览生成失败："+str(error)[:250]+"；区域预览："+str(fallback)[:250]
                else:
                    result["preview_error"]="预览生成失败："+str(error)[:500]
        return result
    finally:
        try:
            if doc is not None:
                if kind=="powerpoint":
                    doc.Saved=True; doc.Close()
                else: doc.Close(False)
        finally:
            try:
                if app is not None and owned: app.Quit()
                elif app is not None and presentation_ids is not None and result is not None:
                    after=[app.Presentations(i)._oleobj_.QueryInterface(pythoncom.IID_IUnknown) for i in range(1,app.Presentations.Count+1)]
                    result["user_presentations_unchanged"]=len(after)==len(presentation_ids) and all(any(old==current for current in after) for old in presentation_ids)
            finally: pythoncom.CoUninitialize()

if __name__=="__main__":
    sys.stdin.reconfigure(encoding="utf-8"); sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps({"ok":True,**execute(json.loads(sys.stdin.read()))},ensure_ascii=False,default=str))
    except Exception as error:
        print(json.dumps({"ok":False,"error":str(error)[:1500]},ensure_ascii=False))
        sys.exit(1)
