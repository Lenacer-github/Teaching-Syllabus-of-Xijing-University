# 西京学院商学院教学大纲自动化审查系统

这是一个基于 Streamlit 的教学大纲自动化审查系统原型，支持 PDF、DOCX、DOC 批量上传、专业规则加载、合规性审查、Markdown 报告生成和 PDF 报告下载。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## API Key 配置

可通过环境变量或 Streamlit secrets 配置：

```bash
export LLAMA_CLOUD_API_KEY="..."
export DEEPSEEK_API_KEY="..."
```

未配置 `LLAMA_CLOUD_API_KEY` 时，系统会使用 `pypdf` 做基础文本提取，适合本地演示；生产审查建议配置 LlamaParse。

## 文件格式

上传教学大纲支持：

- `.pdf`：优先使用 LlamaParse，未配置时使用 `pypdf` 基础文本提取
- `.docx`：使用 `python-docx` 解析段落和表格
- `.doc`：优先使用 LlamaParse；本机开发环境可退回到 macOS `textutil` 转文本

新增或修改专业目录后，页面左侧点击“刷新专业和配置”即可重新加载专业列表。

## 专业数据

全院课程总表统一放在：

```text
data/course_codes.xlsx
```

也兼容 `data/course_code.xlsx`，但推荐使用 `course_codes.xlsx`。

课程总表第一行为表头，从第二行开始每行一门课。建议表头为：

```text
课程代码
课程名称（中文）
课程名称（英文）
课程类别
课程性质
学分
总学时
理论学时
实践学时
开课学期
考核方式（考试考查）
```

其中，课程类别应填写：通识教育课程、公共基础课程、专业教育课程、个性化课程、实践教育课程。课程性质应填写：必修、选修。考核方式应填写：考试、考查。

每个专业一个目录：

```text
data/programs/<专业代码>/
  program.yaml
  training_plan.pdf
```

新增专业时复制 `data/programs/template`，填写 `program.yaml`，上传人才培养方案即可；课程信息从全院课程总表读取。

`training_plan.pdf` 用于提取专业专属信息：

- 课程开设学期
- 课程对毕业要求的 H/M/L 支撑矩阵
- 毕业要求及指标点完整内容

程序会优先使用 LlamaParse 解析人才培养方案。请在本地 `.streamlit/secrets.toml` 或部署平台 Secrets 中配置：

```toml
LLAMA_CLOUD_API_KEY = "..."
DEEPSEEK_API_KEY = "..."
```

本地 `.streamlit/secrets.toml` 已被 `.gitignore` 排除，不应提交到代码仓库。
