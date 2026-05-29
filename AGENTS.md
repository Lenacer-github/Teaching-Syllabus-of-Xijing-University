# AGENTS.md

本文件用于指导后续在本项目中工作的 AI 编程助手和开发者。请优先遵守本文件，再参考 `README.md`、`data/README.md` 和代码中的现有实现。

## 项目概览

本项目是“西京学院课程教学大纲自动化审查系统”，基于 Streamlit 构建，面向 2026 版课程教学大纲初审。系统支持上传 PDF、DOCX、DOC 文件，结合全院课程总表和各专业人才培养方案，完成合规性审查、格式审查、内容深度审查和 PDF 审查意见导出。

主要入口与模块：

- `app.py`：Streamlit 前端入口、上传流程、专业自动识别、批量审查和报告下载。
- `syllabus_review/parser.py`：PDF、DOCX、DOC 文档解析。
- `syllabus_review/rules.py`：核心审查规则，包括基本信息、毕业要求、考核方式、教学内容、课程资源、格式审查和 AI 深度审查。
- `syllabus_review/training_plan.py`：人才培养方案 PDF 解析和课程匹配。
- `syllabus_review/data_loader.py`：全院课程总表读取。
- `syllabus_review/config.py`：系统配置和专业配置读取。
- `syllabus_review/report.py`：PDF 报告生成。
- `data/course_code.xlsx` 或 `data/course_codes.xlsx`：全院统一课程总表。
- `data/programs/<专业代码>/program.yaml`：专业配置。
- `data/programs/<专业代码>/training_plan.pdf`：专业人才培养方案。

## 本地运行

推荐使用项目内虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

本地常用验证：

```bash
.venv/bin/python -m compileall app.py syllabus_review
```

如果需要针对某个大纲复现问题，优先写一个临时 Python 片段调用：

- `parse_document(...)`
- `load_programs(...)`
- `load_course_catalog(...)`
- `load_training_plan(...)`
- `run_compliance_review(...)`

不要为了调试修改正式数据文件。

## 数据与配置约定

全院课程信息应统一放在 `data/course_codes.xlsx`，系统兼容 `data/course_code.xlsx`。课程总表第一行为表头，从第二行开始每行一门课程。推荐字段见 `data/README.md`。

新增专业时：

1. 在 `data/programs/` 下新建专业目录。
2. 添加 `program.yaml`。
3. 添加 `training_plan.pdf`。
4. 不需要为每个专业单独放课程编码表。

平台课程可在 `program.yaml` 中通过 `is_platform` 和 `related_programs` 配置，审查时需要同时调用多个相关专业的人才培养方案。

## API Key 与密钥安全

项目可能使用：

- `LLAMA_CLOUD_API_KEY`
- `DEEPSEEK_API_KEY`

密钥只能通过环境变量或 Streamlit Secrets 配置，绝不能写入代码、提交到 Git，或出现在文档示例的真实值中。本地 `.streamlit/secrets.toml` 不应提交。

如果没有配置 LlamaParse，系统会使用本地解析作为 fallback。修改解析逻辑时必须保留本地 fallback，避免云端或本地无 API Key 时不可用。

## 编码与实现原则

- 保持 Python 代码类型清晰，优先复用现有 dataclass 和工具函数。
- 新规则优先放在 `syllabus_review/rules.py`，报告展示逻辑放在 `syllabus_review/report.py`。
- 规则输出要面向普通教师，语言简洁、可执行，不要暴露内部规则 ID 给最终报告正文。
- 高优先级问题可以保留必要细节；中低优先级问题应尽量合并同类项，举一处示例并提示自查相似问题。
- 尽量减少误报。新增规则前应使用用户提供的真实大纲进行复现和验证。
- 文档格式审查要放宽对备注、参考书目、项目符号、表头文字等特殊段落的判断，避免把模板允许内容误判为正文问题。
- 实践教育课程有特殊逻辑：以“周”为单位的总学时或实践学时可以按每周 30 学时换算；表 2 中实践安排较大或一个教学内容对应多个课程目标时，应按实践课程规则谨慎处理。
- 毕业要求支撑关系核对必须以人才培养方案课程矩阵为准，并在报错时只列出需要修改的差异项。

## 前端与体验约定

- 页面名称使用“西京学院课程教学大纲自动化审查系统”。
- 底部版权为“© 西京学院商学院本科教学中心 2026 powerby Streamlit”。
- 前端需兼容明暗模式，避免浅色文字在深色背景下不可见。
- 上传区分页应显示中文页码。
- 不启用内容深度审查时，单次最多 100 份；启用内容深度审查时，单次最多 10 份，并提示该功能处于测试阶段、意见仅供参考。
- 点击“刷新专业和配置”时，应清理与旧配置相关的上传和审查状态，避免界面残留造成误解。

## 报告输出约定

PDF 报告表头为“西京学院课程教学大纲专家审核意见”。

报告结构原则：

1. 基本信息
2. 意见与建议
3. 合规性审查结果
4. 格式审查结果
5. 内容深度审查结果

若用户要求精简输出，可不输出综合意见与建议。批量导出时，每门课程保持一行，意见与建议列先放简要总结，再放问题明细。

报告 PDF 应使用 A4 横向表格，表头每页重复，正文使用黑体，并注意中文字体不乱码。表格单元格内容应垂直居中。遇到长文本时需要自动换行和分页，避免 ReportLab `LayoutError`。

报告文件名建议使用：

```text
专业名称_课程代码_课程名称.pdf
```

## 测试与验证

本项目目前主要依赖样本文档回归验证。修改规则时至少完成：

1. 编译检查：`.venv/bin/python -m compileall app.py syllabus_review`
2. 用相关大纲文件运行 `run_compliance_review(...)`，确认目标误报消失或新规则生效。
3. 如果改动了 PDF 报告，至少生成一次单文件报告；如涉及批量导出，使用多份结果生成合并报告，确认不出现排版异常。
4. 如果改动了 Streamlit 前端，启动本地服务并用浏览器检查关键交互。

## Git 与提交注意事项

- 提交前运行 `git status --short`，确认只提交本次相关文件。
- 不要提交用户本地改动的数据文件，尤其是 `data/course_code.xlsx`、`data/course_codes.xlsx`，除非用户明确要求。
- 不要提交 `.venv/`、`__pycache__/`、`.streamlit/secrets.toml`、临时测试文件或下载报告。
- 提交信息使用简洁英文动词短语，例如 `Fix catalog hour comparison`。
- 如果需要 push 到 GitHub，先确认本地验证通过。

## 常见问题处理思路

- 课程编码表明明有但提示不存在：检查课程名重复、专业重复、课程代码数字/文本格式、空格和全角符号归一化。
- 人才培养方案匹配不到课程：检查 `training_plan.pdf` 解析结果、课程名称别名、书名号、引号、加号、括号和平台课程多专业逻辑。
- 毕业要求误判：优先从已识别表格结构提取，避免从全文或教学内容表中误抓“模块”“信汇”“8.”等非毕业要求内容。
- 表 4 计算问题：按“考核环节在课程目标中权重 × 考核环节在总评中占比”逐项计算，设计权重保留三位小数，并检查合计行。
- 周/天学时问题：实践教育课程可使用“周”；普通理论或实验教学内容中使用“天/周”时再提示改为具体学时。
- 格式误报：先判断文本是否属于表头、备注、参考书目、项目符号或模板特殊区域，再决定是否输出。
