# 第三方组件与许可导航

本项目的自有代码采用[MIT许可证](LICENSE)，版权属于tl456及相应贡献者。这个授权范围不把第三方代码、字体、Python、Ollama、显卡运行库或模型转换成本项目的原创内容或统一MIT授权。

## 仓库内的前端资源

网页使用本地资源，以免每次打开界面再从CDN加载。下面列出原始许可文件；更新或再次分发时应保留版权及适用声明。

| 组件 | 仓库中的原许可 | 来源与用途 |
|---|---|---|
| Marked | [marked-LICENSE.txt](web/vendor/marked-LICENSE.txt)，含MIT及其他原始声明 | [Marked](https://github.com/markedjs/marked)，Markdown解析 |
| DOMPurify | [dompurify-LICENSE.txt](web/vendor/dompurify-LICENSE.txt) | [DOMPurify](https://github.com/cure53/DOMPurify)，HTML净化；以所附原文为准 |
| highlight.js | [highlightjs-cdn-assets-LICENSE.txt](web/vendor/highlightjs-cdn-assets-LICENSE.txt)，BSD 3-Clause | [highlight.js](https://github.com/highlightjs/highlight.js)，代码高亮 |
| KaTeX与随附字体 | [katex-LICENSE.txt](web/vendor/katex-LICENSE.txt)，MIT | [KaTeX](https://github.com/KaTeX/KaTeX)、[字体项目](https://github.com/KaTeX/katex-fonts)，数学排版 |
| Mermaid | [mermaid-LICENSE.txt](web/vendor/mermaid-LICENSE.txt)，MIT | [Mermaid](https://github.com/mermaid-js/mermaid)，图表渲染 |

固定资源版本、上游取得地址和当时摘要记录在[provenance.json](web/vendor/provenance.json)与[enhancements-provenance.json](web/vendor/enhancements-provenance.json)。这些记录是来源线索，不替代原许可证，也不是对未来相同URL内容的保证。压缩资源中的原始版权标记应保留；升级时应核对对应上游包及其依赖声明。

`packaging/public-licenses/numpy-2.5.4/LICENSE.txt`保存构建所需的NumPy原许可，包含其附带组件的声明；不得只保留本项目MIT而删除这些内容。

## 安装依赖和离线运行资源

源码仓库不包含个人Python环境、已下载模型或整套Ollama/GPU运行库。通过依赖安装或构建流程取得的资源分别受其适用许可约束：

- **Python及PyPI依赖**：Python采用其[官方许可](https://docs.python.org/3/license.html)；FastAPI、Uvicorn、HTTPX、PyWebView、PyWin32、Pillow、RapidOCR/ONNX Runtime、NumPy、Tokenizers、文档解析库及其传递依赖保留各包的许可和版权。依赖列表不是统一MIT授权声明。
- **Ollama**：以[官方仓库](https://github.com/ollama/ollama)和对应发行包的LICENSE及附带声明为准。官方分发中的NVIDIA、AMD、Intel或其他运行组件可能有独立条款，不能将它们重新标为本项目MIT。
- **Qwen模型与分词资源**：参考[Qwen官方发布](https://huggingface.co/Qwen)、[Qwen3.5模型页](https://ollama.com/library/qwen3.5)及[Qwen3-Embedding模型页](https://ollama.com/library/qwen3-embedding)。各尺寸、版本、权重、投影与分词资源应核对对应发布物的许可和来源，不由工作台MIT代替。
- **Microsoft Office与WebView2**：属于外部系统组件，Office不随本项目分发。使用和再分发条件由其提供方决定；本项目不能授予相应产品许可。

完整离线附件应随发行物保留第三方原许可、来源和完整性清单。构建者应核对实际版本及所用来源，不能以本页导航代替一次具体发行物的合规核对。第三方名称用于说明集成，不表示其为本项目提供背书。
