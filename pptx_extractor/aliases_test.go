package main

import (
	"archive/zip"
	"bytes"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func withExtraColumn(name string) string {
	s := sampleTable()
	index := strings.Index(s, "</a:tr>")
	cell := `<a:tc><a:txBody><a:p><a:r><a:t>` + name + `</a:t></a:r></a:p></a:txBody></a:tc>`
	s = s[:index] + cell + s[index:]
	for _, value := range []string{"原始内容", "第二条"} {
		index = strings.Index(s[index+len(cell)+len("</a:tr>"):], "</a:tr>") + index + len(cell) + len("</a:tr>")
		c := `<a:tc><a:txBody><a:p><a:r><a:t>` + value + `</a:t></a:r></a:p></a:txBody></a:tc>`
		s = s[:index] + c + s[index:]
	}
	return s
}
func outputCells(t *testing.T, contents []byte) map[string]string {
	t.Helper()
	z, e := zip.NewReader(bytes.NewReader(contents), int64(len(contents)))
	if e != nil {
		t.Fatal(e)
	}
	result := map[string]string{}
	for _, f := range z.File {
		if f.Name != "xl/worksheets/sheet1.xml" {
			continue
		}
		r, _ := f.Open()
		b, _ := io.ReadAll(r)
		r.Close()
		root, e := parse(b)
		if e != nil {
			t.Fatal(e)
		}
		for _, c := range root.find("c") {
			result[c.attr("r")] = templateValue(c, nil)
		}
	}
	return result
}
func TestAlternativeNamesConvergeAndUnmappedOmitted(t *testing.T) {
	rules := []aliasRule{{"a", 2}, {"b", 2}, {"c", 2}}
	if e := validateRules(rules); e != nil {
		t.Fatal(e)
	}
	tpl, e := loadTemplate(templateFixture(t, nil))
	if e != nil {
		t.Fatal(e)
	}
	for _, name := range []string{"a", "b", "c"} {
		slide := strings.Replace(withExtraColumn("自定义参数"), ">面料<", ">"+name+"<", 1)
		input := fixture(t, map[string]string{"slide1.xml": slide, "slide2.xml": `<slide/>`})
		data, e := extractWithRules(input, rules)
		if e != nil {
			t.Fatal(name, e)
		}
		if len(data.Pages) != 1 || data.Pages[0].Tables[0].Fields[1] != 2 {
			t.Fatal(name, "not mapped")
		}
		book, e := tpl.build(data)
		if e != nil {
			t.Fatal(e)
		}
		cells := outputCells(t, book)
		if cells["C2"] != "0009#" || cells["K1"] != "" || cells["K2"] != "" {
			t.Fatal(name, cells)
		}
	}
}
func TestRulePersistenceAndConflicts(t *testing.T) {
	file := filepath.Join(t.TempDir(), "规则.json")
	rules := []aliasRule{{"a", 2}, {"b", 2}, {"c", 2}}
	if e := saveRulesFile(file, rules); e != nil {
		t.Fatal(e)
	}
	loaded, e := loadRulesFile(file)
	if e != nil || len(loaded) != 3 {
		t.Fatal(loaded, e)
	}
	if v, ok := aliasTarget(" B ", loaded); !ok || v != 2 {
		t.Fatal("normalization failed")
	}
	if e := validateRules([]aliasRule{{"a", 2}, {"A", 5}}); e == nil {
		t.Fatal("conflicting source accepted")
	}
	if e := os.WriteFile(file, []byte("broken"), 0600); e != nil {
		t.Fatal(e)
	}
	if _, e := loadRulesFile(file); e == nil {
		t.Fatal("broken file silently accepted")
	}
}

func TestUnmappedFieldUsesExistingTemplateColumn(t *testing.T) {
	file := templateFixture(t, func(s string) string {
		return strings.Replace(s, "</row>", cell("K1", "自定义参数", 3, false)+"</row>", 1)
	})
	tpl, e := loadTemplate(file)
	if e != nil {
		t.Fatal(e)
	}
	slide := strings.Replace(withExtraColumn("自定义参数"), ">原始内容<", ">自定义参数：原始内容<", 1)
	input := fixture(t, map[string]string{"slide1.xml": slide, "slide2.xml": `<slide/>`})
	data, e := extract(input)
	if e != nil {
		t.Fatal(e)
	}
	book, e := tpl.build(data)
	if e != nil {
		t.Fatal(e)
	}
	cells := outputCells(t, book)
	if cells["K1"] != "自定义参数" || cells["K2"] != "原始内容" || cells["L1"] != "" {
		t.Fatal(cells)
	}
}

func TestCustomAliasOverridesBuiltins(t *testing.T) {
	tpl, e := loadTemplateWithRules(templateFixture(t, nil), []aliasRule{{"color", 9}})
	if e != nil {
		t.Fatal(e)
	}
	if tpl.Mapping[3] != 9 || sourceField("A", "TAG", []aliasRule{{"A", 2}}) != 2 {
		t.Fatal("custom alias did not take precedence")
	}
}
