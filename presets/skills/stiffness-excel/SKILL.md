---
name: stiffness-excel
description: Analyze supplied stiffness test data and prepare a traceable Excel workbook, using attachment reading and Python when authorized.
---

# 整理刚度 Excel 表格

自主观察用户提供的数据结构并选择方法，不假设所有 DAT 同格式。可以分段读取、编写正常 Python 解析和计算；无需遵循固定算法。需要全量处理时读取授权原始附件，不把聊天中的截断文字或检索片段当全文。Python 的 INPUTS 列出可用原件副本；inputs 目录不会因聊天截断而截断。ZIP 先看条目，不盲目解压或执行其中程序。

必须区分原始实测、目标/标准、计算结果和推测。关键原始数要关联附件编号、文件/ZIP 条目、记录组及字段/行；找到相同数字还不够，核对样件、方向、频率、振幅及其定义、单位、预载和试验阶段。数据不全、标准缺失或多候选时标记“待确认”，不补造数值或擅自套用别的零件标准。相对目标偏差与同一样件耐久前后变化是不同指标，没有前后配对不能判耐久结论。

派生数保留输入、公式、单位及所用来源。Python 可用 workbench.verify_text(attachment_id, quote, member="...")核对原文确实出现的位置；它仅核对文字存在，不判断条件匹配、业务含义或合格性。workbench.record_calculation 可保存计算依据，不能凭记录自身宣称已独立验证。未运行核验就写“待核验”，不自报“软件核验通过”。模型无法保证零幻觉；计算和结论须按证据复查。

生成 Excel 时建议结果、来源依据、复核/待确认分开列或分表，提供可下载文件。保存后用 Python 重新打开，核对数据行数、采用记录、写入位置和公式/结果；说明实际检查了什么及未覆盖内容。没有授权 Python 时不要假装创建了文件；遇到格式歧义先展示候选和问题，允许用户确认后继续。超时或调用上限时保存进度，说明下一轮需要继续什么。
