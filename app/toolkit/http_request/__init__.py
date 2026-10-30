"""Bounded HTTP requests; a tool call makes one request without write retries."""
from __future__ import annotations
import json
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit
import urllib.request
import urllib.error
from ...config import settings
from ...file_operations import reserve

class _Text(HTMLParser):
    def __init__(self): super().__init__();self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ("script","style"): self.skip+=1
        if tag in ("p","br","div","li","tr","h1","h2","h3"): self.parts.append("\n")
    def handle_endtag(self,tag):
        if tag in ("script","style"):self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)

class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if req.get_method() not in ("GET","HEAD"):
            raise ValueError("写请求返回重定向，未自动重新发送；请核对目标地址")
        if urlsplit(newurl).netloc!=urlsplit(req.full_url).netloc:
            if any(k.lower() in ("authorization","cookie","proxy-authorization") for k in req.headers):
                raise ValueError("跨主机重定向携带凭据，未转发；请直接请求目标地址")
        if urlsplit(newurl).scheme not in ("http","https"): raise ValueError("只允许HTTP/HTTPS重定向")
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def run(args,scope=None):
    from ..common import configuration
    cfg=configuration("http_request")["values"]
    url=args["url"];parsed=urlsplit(url)
    if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请使用不含用户名密码的HTTP/HTTPS地址")
    method=args.get("method","GET").upper()
    headers=dict(args.get("headers",{}))
    if any("\r" in str(k)+str(v) or "\n" in str(k)+str(v) for k,v in headers.items()): raise ValueError("请求头不能含换行")
    # Stored bearer credential stays out of model input and tool result.
    if cfg.get("bearer_token"):
        host=cfg.get("credential_host","")
        if parsed.hostname.casefold()!=host.casefold():
            raise ValueError("此工具配置的凭据只用于指定主机，请核对credential_host")
        headers["Authorization"]="Bearer "+cfg["bearer_token"]
    data=None
    if "json" in args:
        data=json.dumps(args["json"],ensure_ascii=False).encode()
        headers.setdefault("Content-Type","application/json")
    elif "body" in args:data=args["body"].encode("utf-8")
    if data is not None and len(data)>2*1024*1024:raise ValueError("请求体最大2MiB")
    timeout=min(120,max(1,int(cfg.get("timeout",30))))
    max_bytes=min(100,max(1,int(cfg.get("max_response_mb",10))))*1024*1024
    mode=cfg.get("proxy_mode","system")
    proxy=urllib.request.ProxyHandler() if mode=="system" else urllib.request.ProxyHandler({})
    opener=urllib.request.build_opener(proxy,_Redirect())
    request=urllib.request.Request(url,data=data,headers=headers,method=method)
    try: response=opener.open(request,timeout=timeout)
    except urllib.error.HTTPError as error: response=error
    except (urllib.error.URLError,TimeoutError,OSError) as error:
        raise ValueError("网络请求失败或超时；没有自动重试。写请求是否已执行需向目标服务核实。") from error
    with response:
        deadline=time.monotonic()+timeout
        chunks=[]; size=0
        while size<=max_bytes:
            if time.monotonic()>deadline: raise ValueError("读取响应超过总时限，未自动重试")
            block=response.read1(min(65536,max_bytes+1-size))
            if not block: break
            chunks.append(block);size+=len(block)
        raw=b"".join(chunks)
        truncated=len(raw)>max_bytes
        raw=raw[:max_bytes]
        result={"status":response.code,"method":method,"host":urlsplit(response.geturl()).hostname,
                "content_type":response.headers.get("Content-Type",""),"bytes":len(raw),"truncated":truncated}
        if args.get("save_directory"):
            if truncated: raise ValueError("下载超过响应大小设置，未保存不完整文件")
            path,stream=reserve(args["save_directory"],args.get("save_name","download.bin"))
            try:
                with stream:stream.write(raw)
            except BaseException:
                path.unlink(missing_ok=True);raise
            result.update(path=str(path),saved=True)
            return result
        charset=response.headers.get_content_charset() or "utf-8"
        try: text=raw.decode(charset)
        except (LookupError,UnicodeError):text=raw.decode("utf-8",errors="replace")
        if args.get("extract_text",True) and "html" in result["content_type"].lower():
            parser=_Text();parser.feed(text);text="".join(parser.parts)
        limit=min(60000,max(100,int(args.get("max_chars",12000))))
        sensitive = [cfg.get("bearer_token", "")]
        sensitive.extend(str(v) for k,v in headers.items() if k.lower() in ("authorization","proxy-authorization","cookie","x-api-key","api-key"))
        for secret in sorted(set(sensitive), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[已隐藏凭据]")
        result["text"]=text[:limit];result["truncated"]=truncated or len(text)>limit
        return result
