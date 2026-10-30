# CAD 只读信息读取器

自有适配代码 MIT，作者 tl456。本工具单独导入，未自动启用；不捆绑、修改或绕过 CAD 软件及其许可证。

## 快速使用

1. 将此目录中的 manifest.json、tool.py、nx_reader.py、README.md、LICENSE 打成 ZIP；也可运行 build_plugin.py。
2. 工作台 → 工具工作空间 → 导入工具 → 启用 CAD 只读信息读取器。
3. 测试参数 `{"action":"probe"}`。安装探测不等于许可证已通过。
4. 在工具设置指定 NX 安装根目录（可留空自动探测）；不要填写企业账户或许可证密码。
5. 在对话中勾选本工具，先提供实际文件路径，再让模型调用 inspect。

调用示例（请替换为实际存在文件）：

```json
{"action":"inspect","path":"D:\\CAD-Test\\part.step","backend":"nx","page":1}
```

```json
{"action":"inspect","path":"D:\\CAD-Test\\drawing.dwg","backend":"caxa","page":1}
```

## 能力与事实边界

|后端|官方接口|已实现读取|限制|
|---|---|---|---|
|NX|NX Open + UF，通过官方 run_journal 独立批处理|PRT；STEP 先转换临时 PRT；单位、当前 Part 实体/面/边数量、包围盒、装配首层|需相应版本的 NX Open 与 STEP 转换许可；不主动递归加载外部组件；无密度不计算质量|
|CAXA|独立 CAXAInfo.DWGInfo / CAXAInfo.EBInfo 信息组件|DWG/DXF/EXB 打开、图纸数、图幅/比例、标题栏、BOM、文字及位置|格式版本与许可由 OpenFile 验证；标题栏/BOM需图纸结构可识别；不是完整几何/图层解析接口|

本版适配依据 NX 12 安装 SDK 与 CAXA CAD 2023 信息组件类型库确认的方法。其他版本在 probe 发现安装后，仍须实际读取验证。接口报错返回未取得，不把缺项当作数值零或成功。

CAXA 的 GetPaperSizeAndScale 返回值保留为官方原始字段；未核对图幅单位时不要直接解释为实体尺寸。包围盒是坐标范围，不是零件公差，也不是质量或体积。

## 原件与进程

工具先复制单个输入文件到独立临时目录，再调用 CAD；结束校验原文件 SHA-256 未改变。不会保存源文件，不连接用户当前 CAXA/NX 窗口；不会发送桌面命令或按键。外部引用不随副本复制，结果会注明这一范围。

NX 进程先以暂停状态创建，加入本工具的 Windows Job 后才运行；超时只结束该任务及其子进程。CAXA 使用信息组件的进程内 COM，不是绘图主程序。NX 读取可设置 5–120 秒，整个插件最多 150 秒；超时或许可失败不会反复自动尝试。

企业透明加密、文件版本、密码或许可证都可能使 CAD 打开失败；按原错误处理，不绕过文件保护。源文件、副本与返回文本不进入软件源码/公开安装包；向云模型使用本工具时，工具返回的信息会随对话发给所选云服务。

## 分页与开发

- `page` 从 1 开始，工具设置 `page_size` 默认 50；NX 对实体分页，CAXA 对文字分页。
- CAXA 单条文字限制 200 UTF-8 字节，超长字段给出 `truncated` / `truncated_fields` 标记；这些片段不能代替原图全文。
- 不将模型返回的“已打开”当作证据；检查 `ok`、`interface`、`source_unchanged`、`not_obtained` 和范围说明。
- 参数校验、已启用工具范围、超时和本地调用审计由工作台执行。
- `tool.py` 负责探测、原件副本、COM与进程边界；`nx_reader.py` 只在 NX 自带 Python 内执行。
- Python 插件代码本身是可信本机代码，不是强隔离沙箱。导入前可检查源码。
- 新增 CAD 版本适配时先检查官方 SDK，不猜 ProgID 或方法名；适配器返回统一结构，并增加合成测试。

## 1.6.0 验证范围

- 自动化验证覆盖参数、分页、源文件副本与 SHA-256、接口失败、进程超时边界、ZIP 可读性，以及实际插件导入→执行探测→导出→空白导入→执行。
- NX 12 实机合成 PRT：10×20×30 mm 长方体，读取到 1 实体、6 面、12 边与对应包围盒；未连接用户已打开的 NX，原件哈希未变。
- STEP 适配调用已按安装 SDK 实现，但本次合成 STEP 导出未形成可读取文件，STEP 转换链仍待目标机实际样本验收；不将运行器正常退出视为成功。
- CAXA 2023：类型库和独立信息组件已核对；公开 SDK DWG 样本与合成 DXF 的 `OpenFile` 返回失败。因此 DWG/DXF/EXB 实机成功读取仍待验收，原因未确定；不会返回虚构图纸数据。

打包命令：在工作台源码根目录执行 `python plugins/examples/cad_reader/build_plugin.py cad_reader-1.6.0.zip`。ZIP 只包含清单、原始适配源码、教程与 MIT 许可证，不包含安装 SDK、模型或运行日志。

官方资料：
- [CAXA 产品与 CRX/PDM 信息组件介绍](https://cad.caxa.com/pc/function2d)
- [Siemens NX Open 官方课程与 API 范围](https://training.plm.automation.siemens.com/ilt/iltdescription.cfm?pID=TRCT1205-GH___NX___1899.0_B1)
- 已安装 CAXA 信息组件：SDK/bin/CaxaInfo.tlb、SDK/Samples/DraftInfoSampleReader。
- 已安装 NX：UGOPEN/NXOpen/PartCollection.hxx、Step214Importer.hxx、UGOPEN/uf_modl_utilities.h。
