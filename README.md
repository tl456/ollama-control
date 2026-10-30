# 本地智能工作台

面向 Windows 的轻量 AI 工作台：**界面 → Python 服务 → Ollama**，也可连接其他 Ollama 主机和 OpenAI 兼容云模型。默认工业深色，支持聊天、资料读取、知识库、Markdown 记忆和可选小工具。

当前版本：**1.6.0**。作者：**tl456**。自有代码采用 [MIT](LICENSE)；Ollama、模型、字体和其他依赖保留各自许可，见[第三方许可导航](THIRD_PARTY_NOTICES.md)。

## 能做什么

- 本地聊天、Markdown 显示、折叠状态记忆与提问导航；同一对话可排队发送、调整当前任务方向，也可回答模型中途发出的选项或自定义问题。
- TXT、MD、Word、Excel、PPT 文字提取，未知扩展按文本检测；图片 OCR 或交给支持图片的模型。
- 知识库分类、异步索引、Markdown 编辑与预览；检索时可查看关键词、来源片段和原始记录。
- 一份由 AI 或人工维护的 Markdown 记忆，支持可配置的保存与注入限额。
- 查询时间、文件检索和分段读取；新建文本、文件操作、Office COM 排版与图片、网络请求；可选 Python 辅助、工具导入导出及技能中心。
- 模型下载与信息、上下文和设备偏好、资源状态、受控模型 API 网关及分页日志。

Python 与插件拥有当前 Windows 用户的文件和网络权限，**不是安全沙箱**。共享界面使用同一个工作空间。使用前请阅读 [安全说明](SECURITY.md)、[隐私说明](PRIVACY.md)和[免责说明](DISCLAIMER.md)。

## 普通用户：使用发行包

从本仓库 [Releases](https://github.com/tl456/ollama-control/releases) 获取发行文件，以该版本实际附件和完整性清单为准。

- **完整安装版**：安装 EXE 和所有 BIN 放在同一文件夹，核对摘要后运行 EXE。安装时不需要把 BIN 手工合并。
- **解压运行版**：保留完整 `LocalAIStudio` 文件夹，运行其中的 `启动.bat` 或 `LocalAIStudio.exe`。
- **源码版**：按下一节准备环境。源码仓库不包含已安装模型、Python 环境或显卡运行库。

完整离线构建支持预置 `qwen3.5:9b`、`qwen3.5:4b` 和 `qwen3-embedding:0.6b`。预置是否齐全应以发行清单为准，不能只凭界面中存在预设判断已经下载。

默认上下文为 8K。首次已知专用显存大于 10 GiB 时优先选择 9B，其余或未知选择 4B；不以大上下文试载判断性能。模型仍可能答错，工程数据需核对来源、单位、工况和公式。

默认使用 Edge / Chrome 应用窗口，不可用时使用系统浏览器；设置中可切换 WebView2 内嵌窗口或普通浏览器。Office COM 需要另行安装相应 Microsoft Office，项目不附带 Office。关闭界面不等于退出后台，请使用托盘菜单退出。Office 导出预览可能受企业透明加密限制，保存结果与预览结果分别显示。

## 开发者：从源码启动

开发和公共合成测试以 **Windows、Python 3.12、64 位**为基线。建议将源码放在自己可管理的普通目录中。

```powershell
git clone https://github.com/tl456/ollama-control.git
cd ollama-control
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 main.py
```

也可在环境准备完成后运行 `启动.bat`；只使用浏览器时运行 `浏览器启动.bat`。

1. 从 [Ollama 官网](https://ollama.com/download/windows)安装官方 Windows 版本，或把完整官方运行文件准备到 `runtime/ollama/`。保留其显卡运行库。
2. 启动工作台，在模型中心检查 Ollama 状态并下载聊天模型；知识库使用前下载所选嵌入模型。源码克隆不会自动携带模型。
3. 新建对话，先用一个短问题确认服务正常，再添加资料或选择工具。

本工作台优先管理自己的聊天和嵌入服务，分别使用 11437 和 11438。也可在设置中明确连接已有 Ollama 地址；不会接管或强制停止其他服务。

未准备官方分词文件时，界面使用标明的保守 Token 估算。需要精确本地分词时，按[构建说明](packaging/构建说明.md)准备 `runtime/tokenizers/qwen35/tokenizer.json` 和 `runtime/tokenizers/qwen3-embedding/tokenizer.json` 及对应许可。不要把未知来源的文件当作官方资源。

开发模式下 Python 辅助可使用当前解释器；冻结发行包需要独立 Python 执行运行时。依赖清单、通用冻结与离线打包步骤见[构建说明](packaging/构建说明.md)。

## 默认地址

| 入口 | 默认地址或端口 | 作用 |
|---|---|---|
| 工作台网页 | `http://127.0.0.1:8765/` | 聊天、资料、工具和设置 |
| 模型共享网关 | `127.0.0.1:11436`，默认关闭 | 原生 Ollama 与 `/v1` 接口、鉴权、排队和访问记录 |
| 自管聊天 Ollama | `127.0.0.1:11437` | 本机聊天推理 |
| 自管嵌入 Ollama | `127.0.0.1:11438` | 默认 CPU 嵌入和知识库索引 |

网页与模型网关分别开放。默认仅本机；共享前确认监听范围、密码、网关密钥及远程完整控制设置。无网页密码时直接进入，设置密码后各入口统一使用 Cookie 登录；后台重启不撤销仍有效的会话。HTTP 不加密，公网访问应另配 HTTPS / VPN，不建议直接端口映射。

使用 Open WebUI 等客户端时，填写工作台显示的共享网关地址；OpenAI 兼容地址为 `http://主机IP:11436/v1`，使用自己生成的网关密钥。网关仅提供列明的模型接口，不执行宿主文件或 Python 工具。直连本机 Ollama 原生端口不会经过工作台的网关日志与排队。

## 集中数据与升级

运行目录下：`config/` 存设置和认证资料，`data/` 存聊天、附件副本、记忆、知识库与实验结果，`models/` 存模型，`runtime/` 存运行依赖，`plugins/imported/` 存用户插件，`logs/` 和 `cache/` 存派生记录。这些目录不属于公开源码。

**正式版升级默认保留用户资料。** 覆盖程序前先正常退出并自行备份重要数据；手动“恢复出厂设置”会清除软件管理的资料，不创建备份，应在界面确认后使用。模型、运行库及外部原件不属于该清空范围。详见[升级及目录结构](docs/升级与目录结构.md)。

默认最多 30 个活动对话，满额时提示手动移入回收站；回收站默认 30 天、普通日志默认 7 天清理。工具审计独立压缩保存、按页读取，固定最长 365 天且总容量不超过 1GB，出厂初始化保留。清理并不等同于底层磁盘安全擦除。

## 验证与参与开发

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

公共 CI 使用临时合成资料，不需要云 API 密钥或真实业务文件。缺少官方分词资源的相关集成检查应明确跳过；实际 Ollama 推理、Office COM、原生窗口、安装卸载和第二台物理电脑兼容性需分别验收，不能由合成测试代替。版本结果以对应工作流和发行记录为准。

- [软件教程](docs/软件教程.md)：聊天、上下文、附件、RAG、记忆、模型和共享。
- [技能教程](docs/技能教程.md)：来源核对、Python 辅助与刚度表格示例。
- [插件开发](docs/插件开发.md)：工具格式、独立设置及执行边界。
- [贡献说明](CONTRIBUTING.md)与[更新记录](CHANGELOG.md)。

分享截图、日志、Issue 或示例前，请自行移除密钥、个人路径、文件正文及其他无权公开的信息。安全问题的报告方式见 [SECURITY.md](SECURITY.md)。
