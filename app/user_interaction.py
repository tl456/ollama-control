"""Persistent current-turn questions and steering; normal outbox stays separate."""
from __future__ import annotations
import asyncio
import copy
import time
from . import store, jobs

MAX_STEERS=20
MAX_STEER_CHARS=20000
SIGNALS={}
STEERING_PENDING=set()

def _queue():
    from . import turn_queue
    return turn_queue

def _get(db,conversation,turn):
    q=_queue()
    value=q._decode(db.execute("SELECT * FROM chat_turns WHERE id=?",(turn,)).fetchone())
    if value["conversation_id"]!=conversation:raise ValueError("任务不属于此对话")
    return value

def _notify(turn):
    signal=SIGNALS.get(turn)
    if signal and not signal[0].is_closed():
        signal[0].call_soon_threadsafe(signal[1].set)

def answer(conversation,turn,question_id,answer="",skip=False):
    if not isinstance(answer,str) or len(answer)>12000:raise ValueError("回答最多12000字符")
    if not isinstance(skip,bool):raise ValueError("skip需要布尔值")
    if not skip and not answer.strip():raise ValueError("请填写回答或选择跳过")
    q=_queue()
    with store.LOCK,store.connect() as db:
        value=_get(db,conversation,turn)
        question=value.get("user_question",{})
        if question.get("id")!=question_id:raise ValueError("问题已变化，请刷新后再回答")
        desired="skipped" if skip else "answered"
        if question.get("status")!="pending":
            if question.get("status")==desired and question.get("answer","")==("" if skip else answer):
                return copy.deepcopy(question)
            raise ValueError("此问题已处理，未覆盖原回答")
        if value["status"]!="running" or value.get("cancel_requested"):raise ValueError("任务已经结束或停止，不能继续回答")
        question.update(status=desired,answer="" if skip else answer)
        q._write(db,value)
    q.emit(turn,{"type":"user_question","question":copy.deepcopy(question)})
    _notify(turn)
    return copy.deepcopy(question)

def steer(conversation,turn,content,client_request_id):
    if not isinstance(content,str) or not content.strip() or len(content)>4000:raise ValueError("调整内容需要1至4000字符")
    if not isinstance(client_request_id,str) or not 8<=len(client_request_id)<=128:raise ValueError("调整提交编号无效")
    q=_queue();question=None
    with store.LOCK,store.connect() as db:
        value=_get(db,conversation,turn)
        items=value.setdefault("steering",[])
        old=next((x for x in items if x["client_request_id"]==client_request_id),None)
        if old:
            if old["content"]!=content:raise ValueError("同一提交编号不能用于不同内容")
            return {"items":copy.deepcopy(items)}
        if value["status"]!="running" or value.get("cancel_requested"):raise ValueError("只能调整正在执行的任务；普通发送会进入队列")
        if len(items)>=MAX_STEERS or sum(len(x["content"]) for x in items)+len(content)>MAX_STEER_CHARS:
            raise ValueError("本轮调整记录已达上限，请等待完成后发送新消息")
        items.append({"id":store.uid(),"client_request_id":client_request_id,"content":content,"status":"pending"})
        question=value.get("user_question")
        if question and question.get("status")=="pending":
            question.update(status="skipped",answer="")
        q._write(db,value)
    STEERING_PENDING.add(turn)
    q.emit(turn,{"type":"steering","items":copy.deepcopy(items)})
    if question:q.emit(turn,{"type":"user_question","question":copy.deepcopy(question)})
    _notify(turn)
    return {"items":copy.deepcopy(items)}

def has_steering(scope):
    return scope.get("_turn_id") in STEERING_PENDING

def consume(scope):
    turn=scope.get("_turn_id")
    if not turn or turn not in STEERING_PENDING:return []
    q=_queue()
    with store.LOCK,store.connect() as db:
        value=_get(db,scope["conversation_id"],turn)
        items=value.get("steering",[])
        pending=[copy.deepcopy(x) for x in items if x["status"]=="pending"]
        for item in items:
            if item["status"]=="pending":item["status"]="applied"
        if pending:q._write(db,value)
        STEERING_PENDING.discard(turn)
    if pending:q.emit(turn,{"type":"steering","items":copy.deepcopy(items)})
    return pending

async def ask(args,scope,stop):
    turn=scope.get("_turn_id")
    if not turn or stop is None:raise ValueError("询问用户仅能在正在执行的聊天中使用")
    q=_queue()
    question={"id":store.uid(),"question":args["question"],"options":list(args.get("options",[])),"status":"pending","answer":""}
    with store.LOCK,store.connect() as db:
        value=_get(db,scope["conversation_id"],turn)
        if value["status"]!="running":raise ValueError("任务已结束，不能再提问")
        value["user_question"]=question;q._write(db,value)
    signal=asyncio.Event();SIGNALS[turn]=(asyncio.get_running_loop(),signal)
    job=jobs.JOBS.get(jobs.CURRENT_JOB.get())
    if job:jobs.suspend(job)
    q.emit(turn,{"type":"user_question","question":copy.deepcopy(question)})
    q.emit(turn,{"type":"phase","text":"等待你的回答；模型资源已让出，可继续其他对话"})
    try:
        while True:
            if stop.is_set():raise asyncio.CancelledError()
            signal.clear()
            current=q.get(turn,scope["conversation_id"]).get("user_question",{})
            if current.get("id")!=question["id"]:raise ValueError("当前问题已被替换")
            if current.get("status")!="pending":break
            awakened=asyncio.create_task(signal.wait());cancelled=asyncio.create_task(stop.wait())
            try:
                await asyncio.wait({awakened,cancelled},timeout=2,return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (awakened,cancelled):
                    if not task.done():task.cancel()
                await asyncio.gather(awakened,cancelled,return_exceptions=True)
        if job:
            q.emit(turn,{"type":"phase","text":"已收到，等待可用模型资源后继续"})
            await jobs.acquire(job)
        if stop.is_set():raise asyncio.CancelledError()
        return {"question_id":question["id"],"status":current["status"],"answer":current.get("answer","")}
    except asyncio.CancelledError:
        with store.LOCK,store.connect() as db:
            value=_get(db,scope["conversation_id"],turn)
            current=value.get("user_question",{})
            if current.get("id")==question["id"] and current.get("status")=="pending":
                current.update(status="skipped",answer="")
                q._write(db,value)
        q.emit(turn,{"type":"user_question","question":copy.deepcopy(current)})
        raise
    finally:
        SIGNALS.pop(turn,None)

def finish_turn(turn):
    STEERING_PENDING.discard(turn)
    SIGNALS.pop(turn,None)
