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

func fixture(t *testing.T, slides map[string]string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "中文 样本.pptx")
	f, e := os.Create(p)
	if e != nil {
		t.Fatal(e)
	}
	z := zip.NewWriter(f)
	files := map[string]string{
		"ppt/presentation.xml":            `<p:presentation xmlns:p="p" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="second"/><p:sldId id="257" r:id="first"/></p:sldIdLst></p:presentation>`,
		"ppt/_rels/presentation.xml.rels": `<Relationships><Relationship Id="first" Target="slides/slide1.xml"/><Relationship Id="second" Target="/ppt/slides/slide2.xml"/></Relationships>`,
	}
	for k, v := range slides {
		files["ppt/slides/"+k] = v
	}
	for k, v := range files {
		w, e := z.Create(k)
		if e != nil {
			t.Fatal(e)
		}
		_, _ = io.WriteString(w, v)
	}
	if e := z.Close(); e != nil {
		t.Fatal(e)
	}
	_ = f.Close()
	return p
}
func sampleTable() string {
	var b strings.Builder
	b.WriteString(`<p:sld xmlns:p="p" xmlns:a="a"><a:p><a:r><a:t>TSOW270702</a:t></a:r></a:p><a:tbl>`)
	for _, row := range [][]string{{"", "面料", "color", "使用部位"}, {"1", "0009#", "A", "大身"}, {"2", "=1+1", "B", "里布"}} {
		b.WriteString(`<a:tr>`)
		for _, v := range row {
			b.WriteString(`<a:tc><a:txBody><a:p><a:r><a:t>` + esc(v) + `</a:t></a:r></a:p></a:txBody></a:tc>`)
		}
		b.WriteString(`</a:tr>`)
	}
	b.WriteString(`</a:tbl></p:sld>`)
	return b.String()
}
func TestExtractionOrderAndNoOverwrite(t *testing.T) {
	input := fixture(t, map[string]string{"slide1.xml": sampleTable(), "slide2.xml": `<slide/>`})
	first, d, e := convert(input)
	if e != nil {
		t.Fatal(e)
	}
	if len(d.Pages) != 1 || d.Pages[0].Number != 2 || d.Count != 2 || d.Pages[0].Code != "TSOW270702" {
		t.Fatalf("incorrect extraction: %+v", d)
	}
	before, _ := os.ReadFile(first)
	second, _, e := convert(input)
	if e != nil {
		t.Fatal(e)
	}
	if first == second {
		t.Fatal("overwrote result")
	}
	after, _ := os.ReadFile(first)
	if !bytes.Equal(before, after) {
		t.Fatal("modified first result")
	}
	z, e := zip.OpenReader(first)
	if e != nil {
		t.Fatal(e)
	}
	defer z.Close()
	for _, f := range z.File {
		r, _ := f.Open()
		b, _ := io.ReadAll(r)
		r.Close()
		if _, e := parse(b); e != nil {
			t.Fatalf("invalid XML %s: %v", f.Name, e)
		}
		if f.Name == "xl/worksheets/sheet1.xml" {
			s := string(b)
			if strings.Contains(s, "<f>") || !strings.Contains(s, "=1+1") || !strings.Contains(s, "0009#") {
				t.Fatal("formula or identifier corruption")
			}
		}
	}
}
func TestRejectUnsupportedAndCorrupt(t *testing.T) {
	input := fixture(t, map[string]string{"slide1.xml": `<slide/>`, "slide2.xml": `<slide/>`})
	if _, e := extract(input); e == nil {
		t.Fatal("no table accepted")
	}
	if e := os.WriteFile(input, []byte("not a pptx"), 0600); e != nil {
		t.Fatal(e)
	}
	if _, e := extract(input); e == nil {
		t.Fatal("corrupt file accepted")
	}
	merged := strings.Replace(sampleTable(), "<a:tc>", `<a:tc gridSpan="2">`, 1)
	input = fixture(t, map[string]string{"slide1.xml": merged, "slide2.xml": `<slide/>`})
	data, e := extract(input)
	if e != nil || len(data.Pages) == 0 || len(data.Warnings) == 0 {
		t.Fatal("merged table was not retained with a warning", e)
	}
}

func TestMergedColorAndVerticalVariant(t *testing.T) {
	cell := func(value, attrs string) string {
		return `<a:tc` + attrs + `><a:txBody><a:p><a:r><a:t>` + esc(value) + `</a:t></a:r></a:p></a:txBody></a:tc>`
	}
	var slide strings.Builder
	slide.WriteString(`<p:sld xmlns:p="p" xmlns:a="a"><a:p><a:r><a:t>TSOW270702</a:t></a:r></a:p><a:tbl>`)
	for _, row := range [][][2]string{
		{{"", ""}, {"辅料编号", ""}, {"COLOR", ` gridSpan="2"`}, {"", ` hMerge="1"`}, {"SIZE", ""}, {"数量", ""}, {"MEMO", ""}},
		{{"3", ` rowSpan="2"`}, {"TTX3085", ` rowSpan="2"`}, {"D/GREY", ""}, {"金色", ""}, {"-", ` rowSpan="2"`}, {"1", ` rowSpan="2"`}, {"风纪扣", ` rowSpan="2"`}},
		{{"", ` vMerge="1"`}, {"", ` vMerge="1"`}, {"NAVY", ""}, {"银色", ""}, {"", ` vMerge="1"`}, {"", ` vMerge="1"`}, {"", ` vMerge="1"`}},
	} {
		slide.WriteString("<a:tr>")
		for _, c := range row {
			slide.WriteString(cell(c[0], c[1]))
		}
		slide.WriteString("</a:tr>")
	}
	slide.WriteString("</a:tbl></p:sld>")
	input := fixture(t, map[string]string{"slide1.xml": slide.String(), "slide2.xml": `<slide/>`})
	data, err := extract(input)
	if err != nil || len(data.Pages) != 1 {
		t.Fatal(data, err)
	}
	rows := layoutRows(data.Pages[0])
	if len(rows) != 3 || rows[1][5] != "TTX3085" || rows[2][5] != "TTX3085" || rows[1][6] != "D/GREY / 金色" || rows[2][6] != "NAVY / 银色" {
		t.Fatal(rows)
	}
}

func TestExternalPPTXRegression(t *testing.T) {
	input := os.Getenv("PPTX_TEST_FILE")
	if input == "" {
		t.Skip("set PPTX_TEST_FILE to test a customer presentation")
	}
	data, err := extract(input)
	if err != nil {
		t.Fatal(err)
	}
	templatePath := os.Getenv("PPTX_TEMPLATE_FILE")
	if templatePath == "" {
		templatePath = templateFixture(t, nil)
	}
	tpl, err := loadTemplate(templatePath)
	if err != nil {
		t.Fatal(err)
	}
	book, err := tpl.build(data)
	if err != nil {
		t.Fatal(err)
	}
	if len(book) == 0 || len(data.Pages) == 0 {
		t.Fatal("no editable output generated")
	}
	cells := outputCells(t, book)
	if len(cells) == 0 {
		t.Fatal("generated worksheet contains no cells")
	}
	if _, added := cells[col(len(tpl.Headers)+1)+"1"]; added {
		t.Fatal("output added a column absent from the template")
	}
	outputRows := 0
	for _, page := range data.Pages {
		outputRows += len(layoutRows(page)) - 1
	}
	t.Logf("pages=%d output_rows=%d source_items=%d warnings=%d workbook_bytes=%d template_columns=%d", len(data.Pages), outputRows, data.Count, len(data.Warnings), len(book), len(tpl.Headers))
	for _, warning := range data.Warnings {
		t.Log(warning)
	}
	missingCodes := 0
	duplicates := 0
	emptyItems := 0
	seenRows := map[string]bool{}
	for _, page := range data.Pages {
		if page.Code == "" {
			missingCodes++
		}
		for _, row := range layoutRows(page)[1:] {
			key := row[0] + "\x1f" + strings.Join(row[2:], "\x1f")
			if seenRows[key] {
				duplicates++
			}
			seenRows[key] = true
			if strings.Join(row[2:10], "") == "" {
				emptyItems++
			}
		}
	}
	t.Logf("pages_without_style_code=%d duplicate_rows=%d empty_item_rows=%d", missingCodes, duplicates, emptyItems)
}

func TestStyleCodeFormats(t *testing.T) {
	for _, code := range []string{"TSOW270702", "TTCYFWH27803", "TTCYFOW27801"} {
		if codeRE.FindString(code) != code {
			t.Fatal("style code not recognized:", code)
		}
	}
}

func TestStyleHeaderOverridesMaterialLikeCode(t *testing.T) {
	metadata := `<a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>STYLE NO.</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>TTCYFOW27801</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>设计师</a:t></a:r></a:p></a:txBody></a:tc></a:tr><a:tr><a:tc/><a:tc/><a:tc><a:txBody><a:p><a:r><a:t>张一凡</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl>`
	slide := strings.Replace(sampleTable(), "TSOW270702", "SHTW270702", 1)
	slide = strings.Replace(slide, "</p:sld>", metadata+"</p:sld>", 1)
	input := fixture(t, map[string]string{"slide1.xml": slide, "slide2.xml": `<slide/>`})
	data, err := extract(input)
	if err != nil || len(data.Pages) != 1 || data.Pages[0].Code != "TTCYFOW27801" {
		t.Fatal(data, err)
	}
}

func TestInlineLabelValueCells(t *testing.T) {
	cell := func(value string) string {
		return `<a:tc><a:txBody><a:p><a:r><a:t>` + esc(value) + `</a:t></a:r></a:p></a:txBody></a:tc>`
	}
	metadata := `<a:tbl><a:tr>` + cell("TEENIE WEENIE") + cell("推广：全渠道GIRL 打样90#") + cell("模特编号： TOMQOW27863") + cell("设计师: 孟庆慧") + `</a:tr></a:tbl>`
	trim := `<a:tbl>`
	for _, row := range [][]string{
		{"辅料编号：TTL1694", "COLOR: IVORY", "SIZE：11mm", "数量: 2", "MEMO：主唛：后领"},
		{"辅料编号: TTB1480", "COLOR：BROWN", "SIZE: 3#", "数量：1", "MEMO: 背带"},
	} {
		trim += `<a:tr>`
		for _, value := range row {
			trim += cell(value)
		}
		trim += `</a:tr>`
	}
	trim += `</a:tbl>`
	slide := strings.Replace(sampleTable(), ">0009#<", ">面料：0009#<", 1)
	slide = strings.Replace(slide, "</p:sld>", metadata+trim+"</p:sld>", 1)
	input := fixture(t, map[string]string{"slide1.xml": slide, "slide2.xml": `<slide/>`})
	data, err := extract(input)
	if err != nil || len(data.Pages) != 1 {
		t.Fatal(data, err)
	}
	p := data.Pages[0]
	if p.Code != "TOMQOW27863" || p.Designer != "孟庆慧" {
		t.Fatal("inline metadata not extracted", p.Code, p.Designer)
	}
	rows := layoutRows(p)
	if rows[1][2] != "0009#" || rows[1][5] != "TTL1694" || rows[1][6] != "IVORY" || rows[1][7] != "11mm" || rows[1][8] != "2" || rows[1][9] != "主唛：后领" || rows[2][5] != "TTB1480" {
		t.Fatal("inline item fields not extracted", rows)
	}
}

func TestInlineMetadataOutsideTable(t *testing.T) {
	labels := `<a:p><a:r><a:t>模特编号： TOMQOW27863</a:t></a:r></a:p><a:p><a:r><a:t>设计师: 孟庆慧</a:t></a:r></a:p>`
	slide := strings.Replace(sampleTable(), "</p:sld>", labels+"</p:sld>", 1)
	input := fixture(t, map[string]string{"slide1.xml": slide, "slide2.xml": `<slide/>`})
	data, err := extract(input)
	if err != nil || len(data.Pages) != 1 || data.Pages[0].Code != "TOMQOW27863" || data.Pages[0].Designer != "孟庆慧" {
		t.Fatal(data, err)
	}
}

func TestScreenshotHeaderVariants(t *testing.T) {
	for _, sample := range []struct {
		label, kind string
		field       int
	}{
		{"使用位置", "面料", 4},
		{"颜色", "面料", 3},
		{"颜色", "辅料", 6},
		{"色号", "辅料", 6},
	} {
		if got := sourceField(sample.label, sample.kind, nil); got != sample.field {
			t.Fatalf("%s in %s: got %d", sample.label, sample.kind, got)
		}
	}
}

func TestIncompletePresentationStillExportsReadableData(t *testing.T) {
	slide := strings.Replace(sampleTable(), ">color<", ">未设字段<", 1)
	input := fixture(t, map[string]string{"slide1.xml": slide}) // slide2 is referenced but absent
	data, err := extract(input)
	if err != nil || len(data.Pages) != 1 || len(data.Warnings) == 0 {
		t.Fatal(data, err)
	}
	tpl, err := loadTemplate(templateFixture(t, nil))
	if err != nil {
		t.Fatal(err)
	}
	book, err := tpl.build(data)
	if err != nil {
		t.Fatal(err)
	}
	cells := outputCells(t, book)
	if cells["C2"] != "0009#" || cells["D2"] != "" || cells["K1"] != "" || cells["K2"] != "" {
		t.Fatal(cells)
	}
}
func TestTextRuns(t *testing.T) {
	n, e := parse([]byte(`<tc><p><r><t>TX</t></r><r><t>5360</t></r><br/><r><t>花瑶绉</t></r></p><p><r><t>A色</t></r></p></tc>`))
	if e != nil {
		t.Fatal(e)
	}
	if s := cellText(n); s != "TX5360\n花瑶绉\nA色" {
		t.Fatal(s)
	}
}

func TestBatchContinuesAfterFailure(t *testing.T) {
	input := fixture(t, map[string]string{"slide1.xml": sampleTable(), "slide2.xml": `<slide/>`})
	results := convertBatch([]string{input, filepath.Join(t.TempDir(), "missing.pptx"), input}, nil)
	if len(results) != 2 || results[0].Err != nil || results[1].Err == nil {
		t.Fatalf("unexpected batch results: %+v", results)
	}
	results = convertBatch([]string{filepath.Join(t.TempDir(), "missing.pptx"), input}, nil)
	if len(results) != 2 || results[0].Err == nil || results[1].Err != nil {
		t.Fatal("failure stopped batch")
	}
}

func TestSideBySideLayout(t *testing.T) {
	p := page{Code: "TSOW270702", Tables: []table{
		{Kind: "面料", Rows: [][]string{{"", "面料", "color", "使用部位"}, {"1", "FAB", "A", "大身"}}},
		{Kind: "辅料", Rows: [][]string{{"", "辅料编号", "COLOR", "SIZE", "数量", "MEMO"}, {"1", "TRIM1", "A\n色", "", "1", "主标"}, {"2", "TRIM2", "B", "#1", "2", "挂钩"}}},
	}}
	rows := layoutRows(p)
	if len(rows) != 3 || len(rows[0]) < 10 || rows[1][2] != "FAB" || rows[1][5] != "TRIM1" || rows[1][6] != "A色" || rows[2][2] != "" || rows[2][5] != "TRIM2" || rows[2][1] != "2" || rows[2][0] != p.Code {
		t.Fatalf("wrong layout: %+v", rows)
	}
}

func TestBatchSingleWorkbook(t *testing.T) {
	first := fixture(t, map[string]string{"slide1.xml": sampleTable(), "slide2.xml": `<slide/>`})
	second := fixture(t, map[string]string{"slide1.xml": strings.ReplaceAll(sampleTable(), "TSOW270702", "TSOW270703"), "slide2.xml": `<slide/>`})
	results := convertBatch([]string{first, filepath.Join(t.TempDir(), "missing.pptx"), second}, nil)
	if len(results) != 3 || results[0].Err != nil || results[1].Err == nil || results[2].Err != nil {
		t.Fatal(results)
	}
	if results[0].Output == "" || results[0].Output != results[2].Output {
		t.Fatal("outputs are not combined")
	}
	z, e := zip.OpenReader(results[0].Output)
	if e != nil {
		t.Fatal(e)
	}
	defer z.Close()
	sheets := 0
	for _, f := range z.File {
		if strings.HasPrefix(f.Name, "xl/worksheets/") {
			sheets++
			r, _ := f.Open()
			b, _ := io.ReadAll(r)
			r.Close()
			n, e := parse(b)
			if e != nil {
				t.Fatal(e)
			}
			if len(n.find("row")) != 5 || !bytes.Contains(b, []byte("TSOW270702")) || !bytes.Contains(b, []byte("TSOW270703")) {
				t.Fatal("rows missing from summary")
			}
		}
	}
	if sheets != 1 {
		t.Fatal("expected one sheet")
	}
	files, _ := filepath.Glob(filepath.Join(filepath.Dir(first), "*.xlsx"))
	if len(files) != 1 {
		t.Fatal("extra outputs", files)
	}
	files, _ = filepath.Glob(filepath.Join(filepath.Dir(second), "*.xlsx"))
	if len(files) != 0 {
		t.Fatal("per-file output created")
	}
	again := convertBatch([]string{first, second}, nil)
	if again[0].Err != nil || again[0].Output == results[0].Output {
		t.Fatal("summary overwritten")
	}
	failed := convertBatch([]string{filepath.Join(t.TempDir(), "missing.pptx")}, nil)
	if failed[0].Err == nil || failed[0].Output != "" {
		t.Fatal("all-failed batch emitted output")
	}
}

func TestHighlightedHeaderAndTagTables(t *testing.T) {
	header := `<a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>模特编号</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>TSOW270702</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>디자이너</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>연도</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>시즌</a:t></a:r></a:p></a:txBody></a:tc></a:tr><a:tr>`
	for _, v := range []string{"", "", "朱静", "27FW-7P", "7P 社交 中国"} {
		header += `<a:tc><a:txBody><a:p><a:r><a:t>` + v + `</a:t></a:r></a:p></a:txBody></a:tc>`
	}
	header += `</a:tr></a:tbl>`
	tag := `<a:tbl>`
	for _, row := range [][]string{{"", "TAG COLOR", "A", "B", "C", "D"}, {"1", "KHAKI", "KHAKI", "0009#", "", ""}} {
		tag += `<a:tr>`
		for _, v := range row {
			tag += `<a:tc><a:txBody><a:p><a:r><a:t>` + v + `</a:t></a:r></a:p></a:txBody></a:tc>`
		}
		tag += `</a:tr>`
	}
	tag += `</a:tbl>`
	slide := strings.Replace(sampleTable(), "</p:sld>", header+tag+"</p:sld>", 1)
	p := fixture(t, map[string]string{"slide1.xml": slide, "slide2.xml": `<slide/>`})
	d, e := extract(p)
	if e != nil {
		t.Fatal(e)
	}
	if len(d.Pages) != 1 {
		t.Fatal(d.Pages)
	}
	rows := layoutRows(d.Pages[0])
	if len(rows) != 3 {
		t.Fatal(rows)
	}
	for _, row := range rows[1:] {
		if row[10] != "朱静" || row[11] != "27FW-7P" || row[12] != "7P 社交 中国" {
			t.Fatal("header missing", row)
		}
	}
	if rows[1][13] != "1" || rows[1][14] != "KHAKI" || rows[1][15] != "KHAKI" || rows[1][16] != "0009#" {
		t.Fatal("tag missing", rows[1])
	}
	if rows[2][14] != "KHAKI" {
		t.Fatal("shared tag color missing on next row", rows[2])
	}
	selected := &excelTemplate{Parts: map[string][]byte{}, Sheet: "xl/worksheets/sheet1.xml", Mapping: []int{0, 10, 11, 12, 13, 14, 15, 16, 17, 18}, Styles: make([]int, 10), Header: `<row r="1"></row>`}
	selected.Parts[selected.Sheet] = []byte(`<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"></row></sheetData></worksheet>`)
	output, e := selected.build(d)
	if e != nil {
		t.Fatal(e)
	}
	z, e := zip.NewReader(bytes.NewReader(output), int64(len(output)))
	if e != nil {
		t.Fatal(e)
	}
	values := map[string]string{}
	for _, f := range z.File {
		if f.Name != selected.Sheet {
			continue
		}
		r, _ := f.Open()
		raw, _ := io.ReadAll(r)
		r.Close()
		sheet, e := parse(raw)
		if e != nil {
			t.Fatal(e)
		}
		for _, c := range sheet.find("c") {
			values[c.attr("r")] = templateValue(c, nil)
		}
	}
	for cell, want := range map[string]string{"A2": "TSOW270702", "B2": "朱静", "C2": "27FW-7P", "D2": "7P 社交 中国", "E2": "1", "F2": "KHAKI", "G2": "KHAKI", "H2": "0009#", "F3": "KHAKI"} {
		if values[cell] != want {
			t.Fatalf("%s = %q, want %q", cell, values[cell], want)
		}
	}
}
