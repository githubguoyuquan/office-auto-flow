# PPTX 提取内核

从独立工具迁入原生表格解析、模板输出及字段别名算法，提供工作台无界面接口。
本模块无需网络或第三方 Go 包，Go 1.24 以上可构建。

```
go test ./...
go build -trimpath -o pptx-extractor .
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o pptx-extractor.exe .
```

每次进程从标准输入读取一条 UTF-8 JSON 请求，标准输出返回 JSON Lines 事件；退出码 0 为成功，1 为失败。

模板分析请求：`{"action":"analyze_template","template":"/path/template.xlsx"}`。
返回 `template` 事件中的 `headers`、`mapping`、`fields` 供工作台展示。

提取请求：`{"action":"extract","inputs":["/path/input.pptx"],"template":"/path/template.xlsx","output_dir":"/path/output","rules":[]}`。
可选 `mapping` 按模板列指定字段索引，`-1` 沿用模板同名字段值；规则为 `{"source":"别名","target":2}`。
未指定 `output_dir` 时沿用首个 PPTX 目录。输出重名递增，不覆盖已有文件。
返回 `progress`、`file`、`done` 事件；失败返回 `error`。单文件解析失败继续处理其余文件。

目前仅完成内核迁入。工作台入口、设置界面、子进程管理和安装打包将在后续模块接入。
