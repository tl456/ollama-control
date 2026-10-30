# Office 操作器

作者 tl456，自有实现使用 MIT。需要 Windows、已安装并激活 Microsoft Office。所有模型共用同一参数，不按本地/云端划分功能。

## 使用方法

不确定动作字段时先 action=help，application=word/excel/powerpoint 取得该程序的精简教程。

先 action=inspect 读取真实结构，按返回编号操作；不要猜段落、表、工作表、幻灯片或形状编号。对象编号从1开始。action=create 新建现代格式；edit 修改；copy 不覆盖地复制；preview 生成预览；fonts 返回已安装字体名称。新建目录由文件操作器完成。

一次 operations 对同一个文件执行最多100个动作，全部在副本处理、保存并重新打开核验后提交。已有原件修改前创建快照；文件正在被Office打开时请先保存关闭。不自动重试写操作，不关闭用户已有Office进程。只对本工具独立实例自动清理。PowerPoint共享实例只打开本工具临时副本并关闭该副本，不改变应用全局设置；只允许可核实无宏的现代文稿（包括本工具创建且未被外部修改的文件）。加密外部文稿不能确认时请先保存关闭PowerPoint，再由独立实例读取。

尺寸/位置单位是磅：1厘米约28.35磅。颜色为 #RRGGBB。图片默认嵌入文档，width 指定宽度而不传 height 时保持原比例。预览只生成选定范围；前端展示和把图发送给模型是两个独立动作。return_images=true 返回最多3页，可产生云端外发。

## 基本请求示例

```json
{"action":"create","path":"C:\\合成资料\\报告.docx","operations":[{"op":"insert_text","text":"设备检查报告","size":20,"bold":true},{"op":"insert_text","text":"以下内容来自合成测试。"},{"op":"add_table","values":[["项目","结果"],["外观","正常"]]}],"preview":true}
```

```json
{"action":"edit","path":"C:\\合成资料\\报告.docx","operations":[{"op":"insert_picture","after_text":"设备检查报告","image_path":"C:\\合成资料\\设备.png","width":340,"caption":"图1 设备外观"}]}
```

## Word 动作

- insert_text：text，paragraph / bookmark / after_text 可指定位置；默认追加。可设置 style/font/size/bold/italic/color。
- replace_text：find + text，可选 paragraph 或 bookmark。delete_paragraph：paragraph。
- format：paragraph/bookmark可选，否则全文；font/size/bold/italic/color/style；align=left/center/right/justify；space_before/space_after/line_spacing；list=bullet/number。
- page_setup：orientation=portrait/landscape；top/bottom/left/right 页边距。
- page_break：指定paragraph后或文末插入分页。
- header_footer：section默认1；header/footer文字；page_numbers=true。
- add_table：values二维数组。table_cell：table/row/column，text及字体、fill。
- table_format：table、style、width、borders、字体。
- insert_picture：image_path或本对话attachment_id；paragraph/bookmark/after_text；width/height/align/caption。wrap默认inline，可选square/tight/through/front/top_bottom/behind，转换为浮动图像。

## Excel 动作

- add_sheet/rename_sheet/copy_sheet：sheet定位现有表，name新名称；delete_sheet保留至少1表。
- write：sheet、range（左上角或区域）、values二维数组。formula：range、formula；文字写入不自动更改既有显示格式。
- format：range；font/size/bold/italic/color/fill/number_format/align/wrap/borders/row_height/column_width/autofit。number_format只改变显示，例如0.00、0%、yyyy-mm-dd。
- insert_rows/delete_rows/insert_columns/delete_columns：range选定位置。
- merge/unmerge：range；多个非空值合并默认拒绝，只有allow_data_loss=true才明确允许丢弃其他值。
- clear：仅清内容。sort：range、key、descending、header。filter：range、field、criteria。
- freeze：sheet、rows、columns。conditional_format：range、operator=greater/less/equal/between、value/value2、fill/字体。
- validation：range、choices（逗号分隔选项）。print_setup：range、orientation、pages_wide。
- insert_picture：range定位、image_path/attachment_id、width/height、可选left/top、placement=move/move_size/free。

inspect 时提供 sheet/range 可读取单元格值和公式；最多10000格，编辑区域最多100000格。

## PPT 动作

- add_slide：slide可选插入位置；layout使用Office数值布局，12为空白。copy_slide/delete_slide/layout：slide。
- page_setup：width/height。background：slide、fill。
- add_text：slide、text、left/top/width/height和字体。
- text/format：slide、shape编号或名称、text/字体/align/space_before/space_after。
- position：slide、shape、left/top/width/height/rotation。
- align：slide、shapes编号数组、align=left/center/right/top/middle/bottom、relative_to_slide。
- distribute：slide、shapes、direction=horizontal/vertical、relative_to_slide。
- layer：slide、shape、direction=front/back/forward/backward。delete_shape：slide、shape。
- add_table：slide、values、left/top/width/height。table_cell：slide、shape、row/column、text/字体。
- insert_picture：slide、image_path/attachment_id、left/top/width/height、crop_left/right/top/bottom可选。

theme：theme_path指向已存在的本机.thmx文件，使用Office ApplyTheme。主题文件内容与字体效果以实际预览为准。

## 预览与结果

preview、page、page_count控制范围。Word通过官方Page.EnhMetaFileBits直接生成Office页面图片；Excel默认导出PDF后由PDFium渲染，失败时通过Office CopyPicture生成区域图（明确标注未按打印分页），使用剪贴板期间保存并恢复原对象；PPT直接导出PNG。Excel preview_mode可选auto/print/range。结果有 preview_id 和 preview.items；编辑保存成功而渲染失败时返回 preview_error，不能称为编辑失败或模型已经看图。

设置可控制超时、默认预览页数、缓存容量和保留天数。快照数量/容量在文件操作器设置，共用同一时间线。缓存和快照均只保留本机，不进入发行包。

工具不会判断文档事实是否正确；保存回读只证明文件已写入。需核对数值、公式及排版时，请再次inspect或preview并引用实际返回。


## 本机环境限制

若Office导出PDF或图片被企业透明加密保护，工具会报告预览失败而保留已经保存成功的文档；不把加密字节冒充PDF/PNG，不修改企业软件或绕过保护。Word标准COM页面图像可以直接渲染；Excel区域图依赖系统剪贴板可用。保存前后的原件、快照与实际预览状态分别返回。
