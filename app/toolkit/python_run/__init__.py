from ... import python_lab

def definition():
    return {'id': 'python_run', 'name': 'Python 辅助', 'builtin': True, 'enabled': python_lab.config()['enabled'], 'description': '本机正常 Python 辅助计算、解析和生成文件；print 输出，input 用 stdin。INPUTS 列本对话原始附件副本（非聊天截断片段），可 import openpyxl；文件保存当前目录可下载。previous_run_id 只可复用本对话历史运行。不是安全沙箱；不要访问其他文件、联网、装依赖或运行命令，除非另获明确授权。', 'permissions': ['code.execute.trusted'], 'settings_schema': python_lab.SCHEMA, 'parameters': {'type': 'object', 'properties': {'code': {'type': 'string', 'minLength': 1, 'maxLength': 20000, 'description': '正常 Python 代码；用 print 查看 INPUTS，路径由 inputs 项给出'}, 'stdin': {'type': 'string', 'maxLength': 20000, 'default': ''}, 'attachment_ids': {'type': 'array', 'items': {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}, 'maxItems': 20, 'uniqueItems': True, 'description': '可选：只复制所列已授权附件；省略则复制本分支全部附件，超过20个/100MiB须明确挑选'}, 'previous_run_id': {'type': 'string', 'pattern': '^[0-9a-f]{32}$', 'description': '可选：本对话工具结果中已有运行 id，其普通输出文件复制到 previous/'}}, 'required': ['code'], 'additionalProperties': False}}

def run(args,scope=None):
    return python_lab.execute(args,scope)
