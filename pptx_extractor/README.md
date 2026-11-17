# PPTX 提取内核

从独立工具迁入原生表格解析、模板输出及字段别名算法，提供工作台无界面接口。
本模块无需网络或第三方 Go 包，Go 1.24 以上可构建。

```
go test ./...
go build -trimpath -o bin/pptx-extractor .
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o bin/pptx-extractor.exe .
```

每次进程从标准输入读取一条 UTF-8 JSON 请求，标准输出返回 JSON Lines 事件；退出码 0 为成功，1 为失败。

模板分析请求：`{"action":"analyze_template","template":"/path/template.xlsx"}`。
返回 `template` 事件中的 `headers`、`mapping`、`fields` 供工作台展示。

提取请求：`{"action":"extract","inputs":["/path/input.pptx"],"template":"/path/template.xlsx","output_dir":"/path/output","rules":[]}`。
可选 `mapping` 按模板列指定字段索引，`-1` 沿用模板同名字段值；规则为 `{"source":"别名","target":2}`。
未指定 `output_dir` 时沿用首个 PPTX 目录。输出重名递增，不覆盖已有文件。
返回 `progress`、`file`、`done` 事件；失败返回 `error`。单文件解析失败继续处理其余文件。

工作台后端已接入 `pptx_service.py`，使用当前平台的 `bin/pptx-extractor` 或 `bin/pptx-extractor.exe`。
本机编译文件不提交 Git，Windows 自动构建组件将在安装打包阶段接入。

本地 POST 接口沿用工作台令牌和来源检查：

- `/api/pptx/files`：`{"kind":"pptx"}` 多选输入，或 `{"kind":"xlsx"}` 选择模板；返回 `paths`。
- `/api/pptx/template`：模板路径与可选规则，返回模板字段和默认匹配。
- `/api/pptx/run`：批量提取请求；任务进入现有 `/api/state`，通过 `/api/stop` 停止。

提取任务与 OA 任务互斥，无需启动 Playwright。取消、超时和退出都会回收提取子进程。
输出在目标目录内的临时文件夹完成，成功后以防覆盖方式发布；失败、取消时清理暂存文件。
工作台已提供独立入口、原生文件选择、模板列匹配和别名设置。
配置保存在工作台用户目录的 `pptx-settings.json`，保留上次模板、保存目录、列匹配和别名；源 PPTX 列表每次重新选择。
更换模板会重新分析列；规则可在任务区或“应用配置 → PPTX 字段别名”管理，应用规则后自动重新分析并保存。
运行前自动保存配置；配置无效或条件不完整时不启动。Windows 安装打包尚待接入组件编译。
