# 读取本对话附件

只读本对话已发送附件。先 list 查编号；ZIP 用 list 查条目，再 read 精确 member。返回片段范围和下一段；全文计算可用 Python 原件副本。不会读取任意本机路径。

参数定义见manifest.json，执行实现见本目录；公共文件解析服务在toolkit/common.py，不依赖其他工具。输入输出可用工具空间测试核对。自有代码MIT，tl456。
