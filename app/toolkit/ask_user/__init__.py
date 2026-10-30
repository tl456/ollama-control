"""Chat-only asynchronous tool, handled in chat.call_tool rather than a worker."""
def run(args,scope=None):
    raise ValueError("询问用户需要正在执行的聊天。请在聊天中勾选此工具进行测试；工具测试页不会等待回答。")
