"""Bounded, observable query preparation; never executes a model's tool calls."""
from __future__ import annotations
import asyncio
import json
import re
import time
from collections import Counter
from .config import DEFAULTS, settings
from . import providers


def defaults():
    return {"prompt": DEFAULTS["rag_query_prompt"], "enabled": True}


def validate_result(raw, original):
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    value = json.loads(text)
    if not isinstance(value, dict) or not isinstance(value.get("query"), str):
        raise ValueError("提取结果需要包含query字符串的JSON对象")
    query = value["query"].strip()
    if not query or len(query) > 2000:
        raise ValueError("提取问句为空或超长")
    def numeric_literals(text):
        # Compare complete literals and their counts, not substring presence:
        # 2 is not preserved merely because 2026 remains. Keep explicit signs,
        # decimal fractions and scientific exponents; repeated conditions may
        # not silently disappear. Only whitespace/sign glyphs are normalized.
        pattern = r"[+\-\u2212\uff0b\uff0d]?\s*(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+\-\u2212\uff0b\uff0d]?\d+)?"
        signs = str.maketrans({"\u2212": "-", "\uff0b": "+", "\uff0d": "-"})
        return Counter(re.sub(r"\s+", "", value).translate(signs).casefold()
                       for value in re.findall(pattern, text))
    if numeric_literals(original) != numeric_literals(query):
        raise ValueError("提取结果改变了原问题中的数字条件")
    def engineering_literals(text):
        ascii_parts = re.findall(r"[A-Za-z0-9]+(?:[._:/+-][A-Za-z0-9]+)*", text)
        identifiers = [p for p in ascii_parts if any(c.isalpha() for c in p) and (any(c.isdigit() for c in p) or any(c in p for c in '-_/:') or (len(p)>1 and p.isupper()))]
        units = re.findall(r"\d+(?:\.\d+)?\s*(?:[kM]?N\s*/\s*mm|[kMGmun]?Hz|[kM]?N|mm|cm|km|m|MPa|kPa|Pa|rpm|°C|℃|%|毫米|厘米|米|千牛|牛顿|赫兹|兆帕|千帕)(?![A-Za-z])", text)
        directions = re.findall(r"(?<![A-Za-z])[XYZxyz](?:向|轴|方向)?(?![A-Za-z])|纵向|横向|垂向|径向|轴向", text)
        standalone_units = re.findall(r"(?<![A-Za-z])(?:[kM]?N\s*/\s*mm|[kMGmun]?Hz|[kM]?N|mm|cm|km|MPa|kPa|Pa|rpm|℃|毫米|厘米|千牛|牛顿|赫兹|兆帕|千帕)(?![A-Za-z])", text)
        result = {"id:" + p.casefold() for p in identifiers}
        result.update("unit:" + re.sub(r"\s+", "", p) for p in units + standalone_units)
        result.update("axis:" + re.sub(r"(?:方向|向|轴)$", "", p.casefold()) for p in directions)
        return result
    if engineering_literals(original) != engineering_literals(query):
        raise ValueError("提取结果改变了型号、方向或数值单位条件")
    for word in ("不能", "不要", "不含", "不包括", "除外"):
        if word in original and word not in query:
            raise ValueError("提取结果丢失否定条件")
    keywords = value.get("keywords", [])
    if not isinstance(keywords, list):
        raise ValueError("keywords需要字符串数组")
    # Only source-literal terms are accepted; a model cannot invent a part ID.
    keywords = list(dict.fromkeys(k.strip() for k in keywords if isinstance(k, str) and 0 < len(k.strip()) <= 100 and k.strip().casefold() in original.casefold()))[:12]
    return query, keywords


async def prepare(original, opts=None, *, prompt=None, force=False, stop=None):
    cfg = settings()
    opts = {**cfg, **(opts or {})}
    original = str(original).strip()
    result = {"original": original, "query": original[:2000], "keywords": [], "status": "disabled",
              "model": opts["model"] if "model" in opts else opts["default_model"], "provider": opts.get("provider", "local"),
              "fallback": False, "error": "", "source": "original_question", "seconds": 0}
    if not original or (not force and not opts.get("rag_query_enabled", True)):
        return result
    if stop is not None and stop.is_set():
        raise asyncio.CancelledError()
    started = time.monotonic()
    instruction = prompt if prompt is not None else opts.get("rag_query_prompt", DEFAULTS["rag_query_prompt"])
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 12000:
        raise ValueError("检索提示词需要非空文本且最多12000字符")
    from .context import estimate, take_text
    window = min(8192, opts.get("context", 8192))
    input_budget = window - estimate(instruction) - 600
    if input_budget < 128:
        result.update(status="fallback", fallback=True, error="提取提示词超过本次可用预算，使用原问题检索")
        return result
    body = {"model": result["model"], "think": False, "reasoning_effort": "off",
            "inference_device": opts.get("inference_device", "auto"),
            "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": take_text(original, input_budget)}],
            "options": {"num_ctx": min(8192, opts.get("context", 8192)), "num_predict": 384, "temperature": 0,
                        "inference_device": opts.get("inference_device", "auto"), "reasoning_effort": "off"}}
    iterator = providers.stream_model(result["provider"], body)
    async def collect():
        text = ""
        async for packet in iterator:
            if stop is not None and stop.is_set():
                raise asyncio.CancelledError()
            message = packet.get("message", {})
            if message.get("tool_calls"):
                raise ValueError("关键词提取阶段不执行工具")
            text += message.get("content") or ""
            if len(text) > 8000:
                raise ValueError("关键词提取结果超长")
        return text
    pending = asyncio.create_task(collect())
    deadline = time.monotonic() + 25
    try:
        while not pending.done():
            if stop is not None and stop.is_set():
                raise asyncio.CancelledError()
            if time.monotonic() >= deadline:
                raise TimeoutError("关键词提取超时")
            await asyncio.wait({pending}, timeout=.1)
        raw = await pending
        query, words = validate_result(raw, original)
        result.update(query=query, keywords=words, status="extracted", source="model_keyword_extraction")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # Only a category, never an upstream request/header/prompt dump.
        result.update(status="fallback", fallback=True, error="关键词提取未完成或格式/条件不符，使用原问题检索（" + type(error).__name__ + "）")
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await iterator.aclose()
        result["seconds"] = round(time.monotonic() - started, 3)
    return result
