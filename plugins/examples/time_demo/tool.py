"""TIME示例：复制这个最小入口即可开始开发，详见同包README.md。

工作台导入ZIP后用独立Python进程调用run(arguments, context)。
请返回可JSON序列化的dict/list/数字/文本；不要print业务内容作为协议输出。
这个示例只读系统时间，不联网、不读用户文件、不修改系统设置。
权限声明用于说明用途，不会把插件变为操作系统安全沙箱。
"""
from datetime import datetime


def run(arguments, context):
    """arguments由manifest.parameters校验；context.settings来自工具配置。

    - 本例不需要参数，因此arguments应为{}。
    - format是可选配置，未填写时使用iso8601。
    - astimezone()保留当前主机实际时区，不把固定北京时间冒充系统时间。
    - 真正耗时任务应分块并设置合理timeout；勿无限循环或自动重复提交。
    """
    now = datetime.now().astimezone()
    value = now.strftime("%H:%M:%S") if context.get("settings", {}).get("format") == "clock" else now.isoformat()
    return {"time": value, "source": "TIME 示例插件"}
