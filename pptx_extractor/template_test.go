package main

import (
	"archive/zip"
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func templateFixture(t *testing.T, modify func(string) string) string {
	t.Helper()
	data := extracted{Pages: []page{{Code: "EXAMPLE", Tables: []table{{Kind: "面料", Rows: [][]string{{"", "面料", "color", "使用部位"}, {"1", "OLD_DATA", "A", "大身"}}}}}}}
	b, e := workbook(data)
	if e != nil {
		t.Fatal(e)
	}
	z, e := zip.NewReader(bytes.NewReader(b), int64(len(b)))
	if e != nil {
		t.Fatal(e)
	}
	p := filepath.Join(t.TempDir(), "模板.xlsx")
	f, e := os.Create(p)
	if e != nil {
		t.Fatal(e)
	}
	out := zip.NewWriter(f)
	for _, entry := range z.File {
		r, _ := entry.Open()
		v, _ := io.ReadAll(r)
		r.Close()
		if entry.Name == "xl/worksheets/sheet1.xml" && modify != nil {
			v = []byte(modify(string(v)))
		}
		w, _ := out.Create(entry.Name)
		w.Write(v)
	}
	out.Close()
	f.Close()
	return p
}

func TestAlternativeTemplateHeaderFields(t *testing.T) {
	for _, separator := range []string{"｜", "|"} {
		header := " 不存在 " + separator + " 面料 " + separator + " 辅料编号 "
		file := templateFixture(t, func(s string) string {
			return sheetDataRE.ReplaceAllString(s, `<sheetData><row r="1">`+cell("A1", header, 3, false)+`</row></sheetData>`)
		})
		tpl, err := loadTemplate(file)
		if err != nil {
			t.Fatal(err)
		}
		if len(tpl.Headers) != 1 || tpl.Mapping[0] != 2 {
			t.Fatal(tpl.Headers, tpl.Mapping)
		}
		pages := []page{
			{Tables: []table{{Kind: "辅料", Rows: [][]string{{"辅料编号"}, {"TRIM_ONLY"}}}}},
			{Tables: []table{{Kind: "面料", Rows: [][]string{{"面料"}, {"FABRIC_FIRST"}}}, {Kind: "辅料", Rows: [][]string{{"辅料编号"}, {"TRIM_SECOND"}}}}},
		}
		content, err := tpl.build(extracted{Pages: pages})
		if err != nil {
			t.Fatal(err)
		}
		z, err := zip.NewReader(bytes.NewReader(content), int64(len(content)))
		if err != nil {
			t.Fatal(err)
		}
		for _, entry := range z.File {
			if entry.Name != tpl.Sheet {
				continue
			}
			r, _ := entry.Open()
			raw, _ := io.ReadAll(r)
			r.Close()
			doc, err := parse(raw)
			if err != nil {
				t.Fatal(err)
			}
			values := map[string]string{}
			for _, c := range doc.find("c") {
				values[c.attr("r")] = templateValue(c, nil)
			}
			if values["A1"] != header || values["A2"] != "TRIM_ONLY" || values["A3"] != "FABRIC_FIRST" {
				t.Fatalf("alternative fields: %#v", values)
			}
			if len(values) != 3 {
				t.Fatalf("extra output columns: %#v", values)
			}
		}
	}
}

func TestBlankTemplateColumnNeverBecomesImplicitSequence(t *testing.T) {
	file := templateFixture(t, func(s string) string {
		data := `<sheetData><row r="1">` + cell("A1", "款号", 3, false) + cell("B1", "", 3, false) + cell("C1", "数量", 3, false) + `</row><row r="2">` + cell("B2", "OLD", 1, false) + `</row></sheetData>`
		return sheetDataRE.ReplaceAllString(s, data)
	})
	tpl, err := loadTemplate(file)
	if err != nil {
		t.Fatal(err)
	}
	if tpl.Mapping[1] != -1 {
		t.Fatalf("blank header mapped to %d", tpl.Mapping[1])
	}
	// Simulate a mapping saved by an older client.
	tpl.Mapping[1] = 1
	content, err := tpl.build(extracted{Pages: []page{{Code: "STYLE", Tables: []table{{Kind: "辅料", Rows: [][]string{{"辅料编号", "数量", "模板外字段"}, {"TRIM", "2", "MUST_NOT_EXPORT"}}}}}}})
	if err != nil {
		t.Fatal(err)
	}
	z, err := zip.NewReader(bytes.NewReader(content), int64(len(content)))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range z.File {
		if entry.Name != tpl.Sheet {
			continue
		}
		r, err := entry.Open()
		if err != nil {
			t.Fatal(err)
		}
		raw, err := io.ReadAll(r)
		r.Close()
		if err != nil {
			t.Fatal(err)
		}
		doc, err := parse(raw)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range doc.find("c") {
			if c.attr("r") == "B2" && templateValue(c, nil) != "" {
				t.Fatal("blank template column contains generated data")
			}
		}
		if bytes.Contains(raw, []byte("MUST_NOT_EXPORT")) {
			t.Fatal("source-only field was exported")
		}
	}
}

func TestFlexibleTemplateColumns(t *testing.T) {
	for _, headers := range [][]string{{"客户款式", "数量", "采购负责人"}, {"款号", "面料", "面料颜色", "使用部位", "辅料编号", "辅料颜色", "SIZE", "数量", "MEMO", "供应商", "交期", "备注"}} {
		file := templateFixture(t, func(s string) string {
			var h strings.Builder
			h.WriteString(`<sheetData><row r="1">`)
			for i, v := range headers {
				h.WriteString(cell(col(i+1)+"1", v, 3, false))
			}
			h.WriteString(`</row><row r="2" ht="30" customHeight="1">`)
			for i := range headers {
				h.WriteString(cell(col(i+1)+"2", "OLD", 1, false))
			}
			h.WriteString(`</row></sheetData>`)
			return sheetDataRE.ReplaceAllString(s, h.String())
		})
		tpl, e := loadTemplate(file)
		if e != nil {
			t.Fatal(e)
		}
		if len(tpl.Mapping) != len(headers) {
			t.Fatal("column count restricted")
		}
		if len(headers) == 3 {
			if tpl.Mapping[0] != -1 || tpl.Mapping[1] != 8 || tpl.Mapping[2] != -1 {
				t.Fatal(tpl.Mapping)
			}
			tpl.Mapping[0] = 0
		}
		b, e := tpl.build(extracted{Pages: []page{{Code: "TSOW270702", Tables: []table{{Kind: "辅料", Rows: [][]string{{"", "辅料编号", "COLOR", "SIZE", "数量", "MEMO"}, {"1", "TRIM", "A", "#1", "2", "备注"}}}}}}})
		if e != nil {
			t.Fatal(e)
		}
		z, e := zip.NewReader(bytes.NewReader(b), int64(len(b)))
		if e != nil {
			t.Fatal(e)
		}
		for _, entry := range z.File {
			if entry.Name != tpl.Sheet {
				continue
			}
			r, _ := entry.Open()
			s, _ := io.ReadAll(r)
			r.Close()
			n, e := parse(s)
			if e != nil {
				t.Fatal(e)
			}
			if !bytes.Contains(s, []byte(fmt.Sprintf(`A1:%s2`, col(len(headers))))) {
				t.Fatal("incorrect output dimension")
			}
			if len(headers) == 3 {
				vals := map[string]string{}
				for _, c := range n.find("c") {
					vals[c.attr("r")] = templateValue(c, nil)
				}
				if vals["A2"] != "TSOW270702" || vals["B2"] != "2" || vals["C2"] != "" {
					t.Fatal("manual mapping or blank field incorrect", vals)
				}
			}
		}
	}
}

func TestHeadersOnlyTemplate(t *testing.T) {
	file := templateFixture(t, func(s string) string {
		return sheetDataRE.ReplaceAllString(s, `<sheetData><row r="1">`+cell("A1", "款号", 3, false)+`</row></sheetData>`)
	})
	tpl, e := loadTemplate(file)
	if e != nil || len(tpl.Mapping) != 1 || tpl.Mapping[0] != 0 {
		t.Fatal(tpl, e)
	}
}
func TestUploadedTemplateMapping(t *testing.T) {
	file := templateFixture(t, func(s string) string {
		s = strings.Replace(s, ">面料<", ">TEMP<", 1)
		s = strings.Replace(s, ">MEMO<", ">面料<", 1)
		return strings.Replace(s, ">TEMP<", ">MEMO<", 1)
	})
	before, _ := os.ReadFile(file)
	tpl, e := loadTemplate(file)
	if e != nil {
		t.Fatal(e)
	}
	if tpl.Mapping[2] != 9 || tpl.Mapping[9] != 2 {
		t.Fatal("header order ignored", tpl.Mapping)
	}
	input := fixture(t, map[string]string{"slide1.xml": sampleTable(), "slide2.xml": `<slide/>`})
	results := convertBatch([]string{input}, nil, tpl)
	if results[0].Err != nil {
		t.Fatal(results[0].Err)
	}
	after, _ := os.ReadFile(file)
	if !bytes.Equal(before, after) {
		t.Fatal("template modified")
	}
	z, e := zip.OpenReader(results[0].Output)
	if e != nil {
		t.Fatal(e)
	}
	defer z.Close()
	for _, f := range z.File {
		r, _ := f.Open()
		b, _ := io.ReadAll(r)
		r.Close()
		if f.Name == tpl.Sheet {
			if bytes.Contains(b, []byte("OLD_DATA")) {
				t.Fatal("sample data retained")
			}
			doc, e := parse(b)
			if e != nil {
				t.Fatal(e)
			}
			if len(doc.find("row")) != 3 {
				t.Fatal("wrong data row count")
			}
			found := false
			for _, c := range doc.find("c") {
				if c.attr("r") == "J2" && templateValue(c, nil) == "0009#" {
					found = true
				}
			}
			if !found {
				t.Fatal("reordered field not filled")
			}
		} else if !bytes.Equal(b, tpl.Parts[f.Name]) {
			t.Fatal("template package changed", f.Name)
		}
	}
}
func TestUploadedTemplateValidation(t *testing.T) {
	for _, change := range []func(string) string{
		func(s string) string {
			return strings.Replace(s, "</worksheet>", `<mergeCells><mergeCell ref="A1:B1"/></mergeCells></worksheet>`, 1)
		},
	} {
		if _, e := loadTemplate(templateFixture(t, change)); e == nil {
			t.Fatal("invalid template accepted")
		}
	}
}
