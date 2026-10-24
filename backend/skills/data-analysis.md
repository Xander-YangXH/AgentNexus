---
name: data-analysis
description: 用 Python（pandas、matplotlib）分析表格数据、做计算、画图，文件保存在本对话的工作目录里。
tools: [python_exec, list_files, read_file, write_file, share_file]
---
# 数据分析

## 步骤
1. 先用 list_files 看工作目录里有什么文件。用户在消息里直接贴了数据时，用 write_file 存成文件（如 data.csv）再分析。
2. 先小步探索：读入后打印 `df.shape`、`df.head()`、`df.dtypes`，确认列名和类型，再写正式的分析代码。
3. 每次 python_exec 都是一个新进程：变量不会保留到下一次调用，需要的中间结果写成文件。
4. 只 print 结论需要的数字和小表格，不要打印整张大表（输出超过 8000 字符会被截断）。
5. 画图：`import matplotlib.pyplot as plt`，用 `plt.savefig("chart.png", dpi=120, bbox_inches="tight")` 保存，然后立即用 share_file 交给用户（用户看不到工作目录，不交付就拿不到）。图里可以直接写中文：已经配置好中文字体，不需要查找或设置字体。不要用 read_file 读图片（二进制文件读不了），保存成功就可以交付。整理好的表格（如 result.csv）同样用 share_file 交付。
6. 代码报错时读 stderr 修正后再试；同一个错误连续出现两次，就停下来向用户说明问题。

## 回答
- 先给结论（关键数字），再简要说明计算方法；数字来自代码的实际输出，不要估算或编造。
- 沙盒没有网络，不能安装新的包；需要外部数据时请用户提供。
