# 《Nikon Expert 系统介绍》Word 文档的源文件

数字或内容有变化时，按下面的步骤重新生成 `docs/Nikon Expert 系统介绍.docx`：

1. `python3 diagrams.py`：生成 mindmap / dataflow / qa 三张图的 HTML
2. 用 Chrome 无界面模式截图（2 倍清晰度），再裁掉白边，得到 `img/*_c.png`：
   `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --hide-scrollbars --user-data-dir=/tmp/chrome-prof --force-device-scale-factor=2 --window-size=1700,1080 --screenshot=img/mindmap.png file://$PWD/mindmap.html`
   （dataflow 用 1640×560，qa 用 1100×1000）
3. `npm install docx@9.5.1`，然后 `node make_doc.js "../Nikon Expert 系统介绍.docx"`

文字内容都在 `make_doc.js` 里。图里的文字在 `diagrams.py` 里，和网页版 `docs/system_overview.html` 是同一套内容，改的时候两边一起改。
