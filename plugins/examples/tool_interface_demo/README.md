# 工具接口测试与二次开发

本工具仅处理输入，不读写宿主文件、不联网。自有代码 MIT · tl456。

1. 下载示例 ZIP，在工具工作空间导入并启用。
2. 测试 action=echo，text=你好；应返回带前缀的文字。设置 prefix 可以修改前缀。
3. 测试 {"action":"calculate","operation":"multiply","a":6,"b":7}，应返回42。
4. 测试 action=fail，确认界面显示失败并有审计记录，不影响后续调用。
5. 导出工具，换到空白工作空间重新导入，重复测试。导出只含源码/说明/manifest，不携带已保存配置、密钥或运行记录。
6. 修改manifest中的id为自己的唯一ID，name为中文名称，再调整参数Schema与tool.py。
7. run(arguments, context)返回JSON可序列化数据。不要用eval执行运算表达式。捕获明确可恢复错误，不把执行失败伪装成成功。
8. 工具上架前，至少验证正常参数、缺失参数、边界值、失败与导入导出。目录结构避免config/data/logs/cache等运行数据。

默认入口tool.py，运行超时最多30秒；示例可控错误是验收用途。
