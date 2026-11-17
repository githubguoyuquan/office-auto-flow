// Headless bridge for the local workbench. Stdout is JSON Lines only.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

type request struct {
	Action    string      `json:"action"`
	Inputs    []string    `json:"inputs"`
	Template  string      `json:"template"`
	Mapping   []int       `json:"mapping"`
	Rules     []aliasRule `json:"rules"`
	OutputDir string      `json:"output_dir"`
}

func run(req request, emit func(any) error) error {
	if err := validateRules(req.Rules); err != nil {
		return err
	}
	if req.Action != "analyze_template" && req.Action != "extract" {
		return errors.New("不支持的操作。")
	}
	if req.Template == "" {
		return errors.New("请选择 XLSX 模板。")
	}
	tpl, err := loadTemplateWithRules(req.Template, req.Rules)
	if err != nil {
		return err
	}
	if req.Action == "analyze_template" {
		return emit(map[string]any{"type": "template", "headers": tpl.Headers, "mapping": tpl.Mapping, "fields": fieldNames})
	}
	if req.Mapping != nil {
		if len(req.Mapping) != len(tpl.Headers) {
			return errors.New("列匹配数量与模板不一致。")
		}
		for _, field := range req.Mapping {
			if field < -1 || field >= len(fieldNames) {
				return errors.New("列匹配字段无效。")
			}
		}
		tpl.Mapping = req.Mapping
	}
	if len(req.Inputs) == 0 {
		return errors.New("请选择 PPTX 文件。")
	}
	if len(req.Inputs) > 1000 {
		return errors.New("每批最多选择 1000 个文件。")
	}
	outputDir := req.OutputDir
	if outputDir == "" {
		outputDir = filepath.Dir(req.Inputs[0])
	}
	info, err := os.Stat(outputDir)
	if err != nil || !info.IsDir() {
		return errors.New("输出目录不存在或不可访问。")
	}
	var combined extracted
	success, failed := 0, 0
	seen := map[string]bool{}
	for index, input := range req.Inputs {
		abs, err := filepath.Abs(input)
		if err != nil {
			return err
		}
		if seen[abs] {
			continue
		}
		seen[abs] = true
		if err := emit(map[string]any{"type": "progress", "current": index + 1, "total": len(req.Inputs), "input": input}); err != nil {
			return err
		}
		var data extracted
		if !strings.EqualFold(filepath.Ext(input), ".pptx") {
			err = errors.New("请选择 .pptx 文件。")
		} else {
			data, err = extractWithRules(input, req.Rules)
		}
		if err != nil {
			failed++
			if err := emit(map[string]any{"type": "file", "input": input, "ok": false, "error": err.Error()}); err != nil {
				return err
			}
			continue
		}
		success++
		combined.Pages = append(combined.Pages, data.Pages...)
		combined.Count += data.Count
		if err := emit(map[string]any{"type": "file", "input": input, "ok": true, "count": data.Count, "warnings": data.Warnings}); err != nil {
			return err
		}
	}
	if success == 0 || len(combined.Pages) == 0 {
		return errors.New("没有可导出的面辅料数据，请检查文件及目标表格。")
	}
	content, err := tpl.build(combined)
	if err != nil {
		return err
	}
	output, err := saveWorkbook(filepath.Join(outputDir, "output.pptx"), "面辅料汇总", content)
	if err != nil {
		return err
	}
	return emit(map[string]any{"type": "done", "output": output, "success": success, "failed": failed, "count": combined.Count})
}

func main() {
	encoder := json.NewEncoder(os.Stdout)
	emit := func(value any) error { return encoder.Encode(value) }
	err := func() (err error) {
		defer func() {
			if value := recover(); value != nil {
				err = fmt.Errorf("处理失败：%v", value)
			}
		}()
		decoder := json.NewDecoder(io.LimitReader(os.Stdin, 4*1024*1024))
		decoder.DisallowUnknownFields()
		var req request
		if err := decoder.Decode(&req); err != nil {
			return err
		}
		return run(req, emit)
	}()
	if err != nil {
		_ = emit(map[string]any{"type": "error", "error": err.Error()})
		os.Exit(1)
	}
}
