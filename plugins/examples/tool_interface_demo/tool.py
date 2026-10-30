"""MIT · tl456. Minimal import/export teaching example; no eval or host IO."""
import math

def run(arguments, context):
    # arguments was validated by the manifest JSON Schema.
    # context.settings contains this tool's saved settings, never all tools' keys.
    action=arguments["action"]
    if action=="echo":
        return {"text":context.get("settings",{}).get("prefix","示例：")+arguments.get("text","")}
    if action=="fail":
        # Deliberate stable failure for testing UI and audit error handling.
        raise ValueError("示例工具的可控错误")
    if action!="calculate": raise ValueError("未知动作")
    a,b=arguments.get("a"),arguments.get("b")
    if not isinstance(a,(int,float)) or not isinstance(b,(int,float)) or not math.isfinite(a) or not math.isfinite(b):
        raise ValueError("请提供有限数字 a 和 b")
    operation=arguments.get("operation")
    if operation=="add": value=a+b
    elif operation=="subtract": value=a-b
    elif operation=="multiply": value=a*b
    elif operation=="divide":
        if b==0: raise ValueError("除数不能为0")
        value=a/b
    else: raise ValueError("请选择四则运算操作")
    return {"operation":operation,"a":a,"b":b,"result":value}
