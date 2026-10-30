# TIME插件开发示例

这是可直接导入的最小工作台工具包。自有示例代码采用MIT，作者tl456。

## 文件作用

- manifest.json：工具ID、中文介绍、版本、输入JSON Schema、配置Schema及超时。
- tool.py：`run(arguments, context)`执行入口；返回JSON可序列化的对象。
- LICENSE：分发和二次开发时保留版权与许可。

## 导入和使用

1. 将本目录文件置于ZIP顶层；工具空间→导入工具ZIP。
2. 先点击测试，参数填`{}`。示例返回`{"time":"2026-10-29T10:00:00+08:00","source":"TIME 示例插件"}`，实际时间以运行主机为准。
3. 配置format可选iso8601（日期/时间/时区）或clock（时分秒）。
4. 启用插件并在对话中勾选。原生函数调用及传统JSON都由工作台统一适配；不要在插件里再调用聊天接口。

## 让人或AI改造

先描述功能、输入、输出、错误、文件/网络副作用，然后同步修改manifest与tool.py。新工具要使用唯一的小写ID，例如`demo_units`，不要与内置工具重名。

参数示例：`parameters.properties.value={"type":"number"}`，再设`required:["value"]`；在run中用`arguments['value']`取值。可配置选项放settings_schema，运行时从`context.get('settings', {})`读取。示例值只用于说明，不当作用户真实测量值。

返回如`{"value":5,"unit":"mm","source":"用户输入"}`。失败可抛出ValueError说明真实原因；不要编造成功结果、重试次数或来源。不能返回datetime/Path/bytes对象，先转换为字符串或适当结构。

## 测试建议

分别测试默认配置、clock配置、缺字段、错误类型、合法边界、超时以及返回JSON。修改已导入文件会导致完整性检查拒绝，应修改原始开发副本、提高version、重新打包导入。

## 执行边界

插件是当前Windows账户下的可信Python代码；permissions是用途声明，不是系统隔离。只导入可信代码。不要将密钥写入代码、示例、日志或ZIP；不要读取企业文档作开发素材。需要第三方包时先说明依赖与离线分发许可；运行期间不应自行pip安装。
