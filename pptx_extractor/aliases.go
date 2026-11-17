package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// A rule maps one possible source column name to one canonical output field.
// Several source names may independently resolve to the same target.
type aliasRule struct {
	Source string `json:"source"`
	Target int    `json:"target"`
}

var fieldNames = []string{
	"款号 / 模特编号", "序号", "面料", "面料颜色", "使用部位",
	"辅料编号", "辅料颜色", "尺寸 / SIZE", "数量", "备注 / MEMO",
	"设计师", "年份 / 年度", "季节", "TAG 序号", "TAG COLOR",
	"TAG A", "TAG B", "TAG C", "TAG D",
}

func validateRules(rules []aliasRule) error {
	seen := map[string]int{}
	for _, rule := range rules {
		key := norm(rule.Source)
		if key == "" || len([]rune(rule.Source)) > 100 {
			return errors.New("原字段名不能为空，且不能超过 100 个字。")
		}
		if rule.Target < 0 || rule.Target >= len(fieldNames) {
			return fmt.Errorf("字段“%s”的目标不在可选范围内。", rule.Source)
		}
		if existing, ok := seen[key]; ok {
			if existing != rule.Target {
				return fmt.Errorf("原字段“%s”被设置到两个不同目标，请保留一条。", rule.Source)
			}
			return fmt.Errorf("原字段“%s”重复，请删除一条。", rule.Source)
		}
		seen[key] = rule.Target
	}
	return nil
}

func aliasTarget(raw string, rules []aliasRule) (int, bool) {
	key := norm(raw)
	for _, rule := range rules {
		if norm(rule.Source) == key {
			return rule.Target, true
		}
	}
	return -1, false
}

func settingsFile() (string, error) {
	config, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(config, "面辅料提取工具", "字段别名.json"), nil
}

func loadRulesFile(file string) ([]aliasRule, error) {
	contents, err := os.ReadFile(file)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var rules []aliasRule
	if err := json.Unmarshal(contents, &rules); err != nil {
		return nil, errors.New("字段别名设置文件已损坏，请检查或删除后重试。")
	}
	if err := validateRules(rules); err != nil {
		return nil, err
	}
	return rules, nil
}

func saveRulesFile(file string, rules []aliasRule) error {
	if err := validateRules(rules); err != nil {
		return err
	}
	contents, err := json.MarshalIndent(rules, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(file), 0700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(file), "规则-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if err := tmp.Chmod(0600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(contents); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp.Name(), file); err != nil {
		return err
	}
	return nil
}

func ruleSummary(rules []aliasRule) string {
	if len(rules) == 0 {
		return "尚未设置字段别名"
	}
	var names []string
	for _, r := range rules {
		names = append(names, strings.TrimSpace(r.Source)+" → "+fieldNames[r.Target])
	}
	return strings.Join(names, "；")
}
