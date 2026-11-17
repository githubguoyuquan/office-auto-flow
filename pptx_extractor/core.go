package main

import (
	"archive/zip"
	"bytes"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

type node struct {
	Name     xml.Name
	Attr     []xml.Attr
	Text     string
	Children []*node
}

func parse(data []byte) (*node, error) {
	d := xml.NewDecoder(bytes.NewReader(data))
	root := &node{}
	stack := []*node{root}
	for {
		t, e := d.Token()
		if e == io.EOF {
			break
		}
		if e != nil {
			return nil, e
		}
		parent := stack[len(stack)-1]
		switch v := t.(type) {
		case xml.StartElement:
			if len(stack) > 128 {
				return nil, errors.New("XML 层级过深")
			}
			n := &node{Name: v.Name, Attr: v.Attr}
			parent.Children = append(parent.Children, n)
			stack = append(stack, n)
		case xml.EndElement:
			stack = stack[:len(stack)-1]
		case xml.CharData:
			parent.Text += string(v)
		}
	}
	return root, nil
}
func (n *node) attr(k string) string {
	for _, a := range n.Attr {
		if a.Name.Local == k {
			return a.Value
		}
	}
	return ""
}
func (n *node) find(k string) (out []*node) {
	for _, c := range n.Children {
		if c.Name.Local == k {
			out = append(out, c)
		}
		out = append(out, c.find(k)...)
	}
	return
}
func (n *node) direct(k string) (out []*node) {
	for _, c := range n.Children {
		if c.Name.Local == k {
			out = append(out, c)
		}
	}
	return
}
func textRuns(n *node) string {
	if n.Name.Local == "t" {
		return n.Text
	}
	if n.Name.Local == "br" {
		return "\n"
	}
	var b strings.Builder
	for _, c := range n.Children {
		b.WriteString(textRuns(c))
	}
	return b.String()
}
func cellText(n *node) string {
	var p []string
	for _, v := range n.find("p") {
		p = append(p, textRuns(v))
	}
	return strings.TrimSpace(strings.Join(p, "\n"))
}

type table struct {
	Kind   string
	Rows   [][]string
	Fields []int
}
type page struct {
	Number    int
	Code      string
	Tables    []table
	Designer  string
	Year      string
	Season    string
	Tags      [][]string
	ExtraMeta map[string]string
	TagExtras []map[string]string
}
type extracted struct {
	Source   string
	Pages    []page
	Warnings []string
	Count    int
}

var codeRE = regexp.MustCompile(`[A-Z]{4,8}[0-9]{4,8}`)
var numberRE = regexp.MustCompile(`^(0|[1-9][0-9]*)(\.[0-9]+)?$`)

func norm(s string) string { return strings.ToUpper(strings.Join(strings.Fields(s), "")) }
func sourceField(raw, kind string, rules []aliasRule) int {
	key := norm(raw)
	if v, ok := aliasTarget(raw, rules); ok {
		return v
	}
	if kind == "TAG" {
		switch key {
		case "":
			return 13
		case "TAGCOLOR":
			return 14
		case "A":
			return 15
		case "B":
			return 16
		case "C":
			return 17
		case "D":
			return 18
		}
	}
	switch key {
	case "模特编号", "款号", "款式编号", "STYLENO":
		return 0
	case "面料", "面料编号", "面料名称":
		return 2
	case "使用部位", "使用位置", "部位":
		return 4
	case "辅料编号", "辅料编码", "辅料":
		return 5
	case "SIZE", "尺寸":
		return 7
	case "数量", "用量", "QTY":
		return 8
	case "MEMO", "备注":
		return 9
	case "디자이너", "设计师":
		return 10
	case "연도", "年度", "年份":
		return 11
	case "시즌", "季节":
		return 12
	case "TAGCOLOR":
		return 14
	}
	if raw == "color" || (kind == "面料" && key == "COLOR") {
		return 3
	}
	if raw == "COLOR" || (kind == "辅料" && key == "COLOR") {
		return 6
	}
	if key == "颜色" || key == "色号" {
		if kind == "面料" {
			return 3
		}
		if kind == "辅料" {
			return 6
		}
	}
	return -1
}

type inlineField struct{ Label, Value string }

func inlineFields(text string) []inlineField {
	var fields []inlineField
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		index := strings.IndexAny(line, ":：")
		if index > 0 && index < len(line)-1 {
			separatorWidth := 1
			if strings.HasPrefix(line[index:], "：") {
				separatorWidth = len("：")
			}
			label, value := strings.TrimSpace(line[:index]), strings.TrimSpace(line[index+separatorWidth:])
			if label != "" && value != "" && len([]rune(label)) <= 40 {
				fields = append(fields, inlineField{label, value})
				continue
			}
		}
		if len(fields) > 0 {
			fields[len(fields)-1].Value += "\n" + line
		}
	}
	return fields
}

func inlineTable(rows [][]string, rules []aliasRule) ([][]string, bool) {
	if len(rows) == 0 {
		return rows, false
	}
	for _, cell := range rows[0] {
		if len(inlineFields(cell)) == 0 && sourceField(cell, "", rules) >= 0 {
			return rows, false // Keep the ordinary header-row layout.
		}
	}
	known := false
	var headers []string
	positions := map[string]int{}
	var values [][]string
	for _, row := range rows {
		var pairs []inlineField
		for _, cell := range row {
			pairs = append(pairs, inlineFields(cell)...)
		}
		if len(pairs) == 0 {
			continue
		}
		for _, pair := range pairs {
			if sourceField(pair.Label, "", rules) >= 0 {
				known = true
			}
			key := norm(pair.Label)
			if _, exists := positions[key]; !exists {
				positions[key] = len(headers)
				headers = append(headers, pair.Label)
				for i := range values {
					values[i] = append(values[i], "")
				}
			}
		}
		data := make([]string, len(headers))
		for _, pair := range pairs {
			index := positions[norm(pair.Label)]
			if data[index] == "" {
				data[index] = pair.Value
			} else if data[index] != pair.Value {
				data[index] += " / " + pair.Value
			}
		}
		values = append(values, data)
	}
	if !known || len(values) == 0 {
		return rows, false
	}
	return append([][]string{headers}, values...), true
}

func unwrapInlineValues(rows [][]string, kind string, rules []aliasRule) {
	for ri := 1; ri < len(rows); ri++ {
		for ci, value := range rows[ri] {
			pairs := inlineFields(value)
			if len(pairs) != 1 || ci >= len(rows[0]) {
				continue
			}
			label := rows[0][ci]
			field, inlineField := sourceField(label, kind, rules), sourceField(pairs[0].Label, kind, rules)
			if norm(label) == norm(pairs[0].Label) || (field >= 0 && field == inlineField) {
				rows[ri][ci] = pairs[0].Value
			}
		}
	}
}
func extract(input string) (extracted, error) { return extractWithRules(input, nil) }
func extractWithRules(input string, rules []aliasRule) (out extracted, err error) {
	out.Source = filepath.Base(input)
	if !strings.EqualFold(filepath.Ext(input), ".pptx") {
		return out, errors.New("请选择 .pptx 文件。")
	}
	z, e := zip.OpenReader(input)
	if e != nil {
		return out, fmt.Errorf("无法读取 PPTX。请检查文件是否损坏、加密或尚未下载完成。\n%w", e)
	}
	defer z.Close()
	entries := map[string]*zip.File{}
	for _, f := range z.File {
		entries[f.Name] = f
	}
	var consumed int64
	read := func(name string) (*node, error) {
		f := entries[name]
		if f == nil {
			return nil, fmt.Errorf("PPTX 缺少 %s", name)
		}
		if f.UncompressedSize64 > 32<<20 {
			return nil, errors.New("单页内容过大，请拆分 PPTX 后重试。")
		}
		r, e := f.Open()
		if e != nil {
			return nil, e
		}
		defer r.Close()
		b, e := io.ReadAll(io.LimitReader(r, (32<<20)+1))
		if e != nil {
			return nil, e
		}
		consumed += int64(len(b))
		if len(b) > 32<<20 || consumed > 128<<20 {
			return nil, errors.New("文档内容过大，请拆分 PPTX 后重试。")
		}
		return parse(b)
	}
	pres, presErr := read("ppt/presentation.xml")
	rels, relsErr := read("ppt/_rels/presentation.xml.rels")
	targets := map[string]string{}
	if relsErr == nil {
		for _, r := range rels.find("Relationship") {
			if r.attr("TargetMode") != "External" {
				t := r.attr("Target")
				if strings.HasPrefix(t, "/") {
					t = strings.TrimPrefix(t, "/")
				} else {
					t = path.Join("ppt", t)
				}
				targets[r.attr("Id")] = t
			}
		}
	}
	var ordered []string
	if presErr == nil && relsErr == nil {
		for _, s := range pres.find("sldId") {
			relation := ""
			for _, a := range s.Attr {
				if a.Name.Local == "id" && strings.Contains(a.Name.Space, "relationships") {
					relation = a.Value
				}
			}
			ordered = append(ordered, targets[relation])
		}
	} else {
		out.Warnings = append(out.Warnings, "演示文稿索引不完整，已扫描包内可读取的幻灯片。")
	}
	// A damaged index can reference a missing slide while other slides remain usable.
	var discovered []string
	for name := range entries {
		base := strings.TrimPrefix(name, "ppt/slides/")
		if base != name && !strings.Contains(base, "/") && strings.HasPrefix(base, "slide") && strings.HasSuffix(base, ".xml") {
			discovered = append(discovered, name)
		}
	}
	sort.Slice(discovered, func(i, j int) bool {
		a, ea := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(discovered[i], "ppt/slides/slide"), ".xml"))
		b, eb := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(discovered[j], "ppt/slides/slide"), ".xml"))
		if ea == nil && eb == nil {
			return a < b
		}
		return discovered[i] < discovered[j]
	})
	seenSlides := map[string]bool{}
	validOrder := make([]string, 0, len(ordered)+len(discovered))
	for _, name := range ordered {
		if name == "" || entries[name] == nil {
			out.Warnings = append(out.Warnings, "索引中的幻灯片缺失，已跳过。")
			continue
		}
		if !seenSlides[name] {
			seenSlides[name] = true
			validOrder = append(validOrder, name)
		}
	}
	for _, name := range discovered {
		if !seenSlides[name] {
			validOrder = append(validOrder, name)
			seenSlides[name] = true
		}
	}
	if len(ordered) > 1000 || len(discovered) > 1000 {
		return out, errors.New("页数超过 1000，请拆分后重试。")
	}
	for i, name := range validOrder {
		doc, e := read(name)
		if e != nil {
			out.Warnings = append(out.Warnings, fmt.Sprintf("第 %d 页无法读取，已跳过：%v", i+1, e))
			continue
		}
		p := page{Number: i + 1}
		explicitCode := false
		for _, para := range doc.find("p") {
			text := textRuns(para)
			if code := codeRE.FindString(text); code != "" && p.Code == "" {
				p.Code = code
			}
			for _, pair := range inlineFields(text) {
				switch sourceField(pair.Label, "", rules) {
				case 0:
					if !explicitCode && pair.Value != "" {
						p.Code = pair.Value
						explicitCode = true
					}
				case 10:
					if p.Designer == "" {
						p.Designer = pair.Value
					}
				case 11:
					if p.Year == "" {
						p.Year = pair.Value
					}
				case 12:
					if p.Season == "" {
						p.Season = pair.Value
					}
				}
			}
		}
		kinds := map[string]bool{}
		for _, tbl := range doc.find("tbl") {
			var rows [][]string
			merged := false
			for ri, tr := range tbl.direct("tr") {
				var row []string
				for ci, tc := range tr.direct("tc") {
					value := cellText(tc)
					if tc.attr("vMerge") == "1" && ri > 0 && ci < len(rows[ri-1]) {
						value = rows[ri-1][ci]
					}
					if tc.attr("hMerge") == "1" && ri == 0 && ci > 0 {
						value = row[ci-1]
					}
					if tc.attr("gridSpan") != "" || tc.attr("rowSpan") != "" || tc.attr("hMerge") == "1" || tc.attr("vMerge") == "1" {
						merged = true
					}
					row = append(row, value)
				}
				rows = append(rows, row)
			}
			if len(rows) == 0 {
				continue
			}
			width := len(rows[0])
			for _, row := range rows[1:] {
				if len(row) > width {
					width = len(row)
				}
			}
			if width > len(rows[0]) {
				for len(rows[0]) < width {
					rows[0] = append(rows[0], fmt.Sprintf("未命名列%d", len(rows[0])+1))
				}
			}
			for ri := 1; ri < len(rows); ri++ {
				for len(rows[ri]) < width {
					rows[ri] = append(rows[ri], "")
				}
			}
			rows, _ = inlineTable(rows, rules)
			h := map[int]bool{}
			for _, label := range rows[0] {
				if f := sourceField(label, "", rules); f >= 0 {
					h[f] = true
				}
			}
			kind := ""
			isTag := h[14] && (len(rows[0]) >= 3 || h[15] || h[16] || h[17] || h[18])
			if isTag {
				unwrapInlineValues(rows, "TAG", rules)
				fields := make([]int, len(rows[0]))
				for j, label := range rows[0] {
					fields[j] = sourceField(label, "TAG", rules)
				}
				for _, src := range rows[1:] {
					tag := make([]string, 6)
					extra := map[string]string{}
					for j, v := range src {
						if j >= len(fields) {
							break
						}
						if f := fields[j]; f >= 13 && f <= 18 {
							tag[f-13] = v
						}
						if fields[j] < 0 && strings.TrimSpace(rows[0][j]) != "" {
							extra[rows[0][j]] = v
						}
					}
					p.Tags = append(p.Tags, tag)
					p.TagExtras = append(p.TagExtras, extra)
				}
				continue
			}
			if (h[0] || h[10] || h[11] || h[12]) && !h[2] && !h[5] {
				unwrapInlineValues(rows, "", rules)
				for _, label := range rows[0] {
					if code := codeRE.FindString(strings.TrimSpace(label)); code != "" {
						p.Code = code
						break
					}
				}
				for ci, label := range rows[0] {
					field := sourceField(label, "", rules)
					if field == 0 && ci+1 < len(rows[0]) && sourceField(rows[0][ci+1], "", rules) < 0 {
						if code := strings.TrimSpace(rows[0][ci+1]); code != "" {
							p.Code = code
						}
					}
					if len(rows) < 2 || ci >= len(rows[1]) {
						continue
					}
					if field < 0 && strings.TrimSpace(label) != "" && strings.TrimSpace(rows[1][ci]) != "" {
						if p.ExtraMeta == nil {
							p.ExtraMeta = map[string]string{}
						}
						p.ExtraMeta[label] = rows[1][ci]
					}
					switch field {
					case 0:
						if p.Code == "" || codeRE.FindString(rows[1][ci]) != "" {
							p.Code = strings.TrimSpace(rows[1][ci])
						}
					case 10:
						p.Designer = strings.TrimSpace(rows[1][ci])
					case 11:
						p.Year = strings.TrimSpace(rows[1][ci])
					case 12:
						p.Season = strings.TrimSpace(rows[1][ci])
					}
				}
				continue
			}
			if h[2] || h[3] || h[4] {
				kind = "面料"
			}
			if h[5] || h[6] || h[7] || h[8] || h[9] {
				kind = "辅料"
			}
			if kind == "" {
				continue
			}
			unwrapInlineValues(rows, kind, rules)
			if merged {
				out.Warnings = append(out.Warnings, fmt.Sprintf("第 %d 页%s表含合并单元格，已展开并保留内容，请核对。", i+1, kind))
			}
			for _, row := range rows {
				for _, v := range row {
					if len([]rune(v)) > 16000 {
						return out, errors.New("单元格文字过长，请缩短后重试。")
					}
				}
			}
			fields := make([]int, len(rows[0]))
			for j, label := range rows[0] {
				fields[j] = sourceField(label, kind, rules)
			}
			for _, src := range rows[1:] {
				for j, field := range fields {
					if j >= len(src) || strings.TrimSpace(src[j]) == "" {
						continue
					}
					switch field {
					case 0:
						if p.Code == "" {
							p.Code = src[j]
						}
					case 10:
						p.Designer = src[j]
					case 11:
						p.Year = src[j]
					case 12:
						p.Season = src[j]
					}
				}
			}
			p.Tables = append(p.Tables, table{Kind: kind, Rows: rows, Fields: fields})
			kinds[kind] = true
			out.Count += len(rows) - 1
		}
		if len(p.Tables) > 0 {
			out.Pages = append(out.Pages, p)
			if len(kinds) < 2 {
				out.Warnings = append(out.Warnings, fmt.Sprintf("第 %d 页仅找到一种目标表格，请核对。", i+1))
			}
		}
	}
	if len(out.Pages) == 0 {
		return out, errors.New("未找到面料或辅料表。\n请确认表头包含“面料 / 使用部位”或“辅料编号 / 数量”。\n图片中的表格暂不支持。")
	}
	return out, nil
}
func esc(s string) string { var b bytes.Buffer; _ = xml.EscapeText(&b, []byte(s)); return b.String() }
func col(n int) string {
	s := ""
	for n > 0 {
		n--
		s = string(rune('A'+n%26)) + s
		n /= 26
	}
	return s
}

const xmlHead = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`
const styles = `<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="3"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><sz val="16"/><name val="Microsoft YaHei"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="2"><border/><border><left style="thin"><color rgb="FF000000"/></left><right style="thin"><color rgb="FF000000"/></right><top style="thin"><color rgb="FF000000"/></top><bottom style="thin"><color rgb="FF000000"/></bottom><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="49" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1" applyNumberFormat="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="center" horizontal="center"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="center" horizontal="center" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>`

func cell(ref, value string, style int, numeric bool) string {
	if numeric {
		return fmt.Sprintf(`<c r="%s" s="%d"><v>%s</v></c>`, ref, style, value)
	}
	return fmt.Sprintf(`<c r="%s" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>`, ref, style, esc(value))
}
func layoutRows(p page) [][]string {
	rows := [][]string{{"款号", "", "面料", "color", "使用部位", "辅料编号", "COLOR", "SIZE", "数量", "MEMO"}}
	var fabrics, trims [][]string
	for _, t := range p.Tables {
		fields := []string{"面料", "COLOR", "使用部位"}
		if t.Kind == "辅料" {
			fields = []string{"辅料编号", "COLOR", "SIZE", "数量", "MEMO"}
		}
		for _, src := range t.Rows[1:] {
			dst := make([]string, len(fields))
			for i, key := range fields {
				var values []string
				for j, h := range t.Rows[0] {
					mapped := sourceField(h, t.Kind, nil)
					if j < len(t.Fields) && t.Fields[j] >= 0 {
						mapped = t.Fields[j]
					}
					want := sourceField(key, t.Kind, nil)
					if mapped == want {
						value := strings.ReplaceAll(src[j], "\n", "")
						if value != "" && (len(values) == 0 || values[len(values)-1] != value) {
							values = append(values, value)
						}
					}
				}
				dst[i] = strings.Join(values, " / ")
			}
			if t.Kind == "面料" {
				fabrics = append(fabrics, dst)
			} else {
				trims = append(trims, dst)
			}
		}
	}
	count := max(len(fabrics), len(trims), len(p.Tags))
	for i := 0; i < count; i++ {
		row := make([]string, 19)
		row[0] = p.Code
		row[1] = strconv.Itoa(i + 1)
		row[10], row[11], row[12] = p.Designer, p.Year, p.Season
		if i < len(fabrics) {
			copy(row[2:5], fabrics[i])
		}
		if i < len(trims) {
			copy(row[5:], trims[i])
		}
		if len(p.Tags) == 1 || i < len(p.Tags) {
			tagIndex := i
			if len(p.Tags) == 1 {
				tagIndex = 0
			}
			tag := p.Tags[tagIndex]
			for j := 0; j < len(tag) && j < 6; j++ {
				row[13+j] = strings.ReplaceAll(tag[j], "\n", "")
			}
		}
		rows = append(rows, row)
	}
	return rows
}
func worksheet(rows [][]string) string {
	var b strings.Builder
	b.WriteString(xmlHead + `<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView showGridLines="0" workbookViewId="0"/></sheetViews><sheetFormatPr defaultColWidth="9" defaultRowHeight="16.5"/><cols>`)
	for i, w := range []float64{14.4076923076923, 4.33846153846154, 22.5153846153846, 9, 9, 12.3, 13.8461538461538, 9.92307692307692, 8.11538461538461, 19.4384615384615} {
		fmt.Fprintf(&b, `<col min="%d" max="%d" width="%.14g" customWidth="1"/>`, i+1, i+1, w)
	}
	b.WriteString(`</cols><sheetData>`)
	for ri, row := range rows {
		fmt.Fprintf(&b, `<row r="%d" ht="30" customHeight="1">`, ri+1)
		for ci, v := range row {
			numeric := ri > 0 && (ci == 1 || ci == 8) && numberRE.MatchString(v) && len(v) <= 15
			style := 1
			if numeric {
				style = 2
			}
			if ri == 0 && ci > 0 {
				style = 3
			}
			b.WriteString(cell(col(ci+1)+strconv.Itoa(ri+1), v, style, numeric))
		}
		b.WriteString(`</row>`)
	}
	b.WriteString(`</sheetData><pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/></worksheet>`)
	return b.String()
}
func workbook(data extracted) ([]byte, error) {
	var b bytes.Buffer
	z := zip.NewWriter(&b)
	put := func(name, content string) error {
		w, e := z.Create(name)
		if e != nil {
			return e
		}
		_, e = io.WriteString(w, content)
		return e
	}
	ct := xmlHead + `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>`
	wb := xmlHead + `<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>`
	rel := xmlHead + `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>`
	rows := layoutRows(page{})
	for _, p := range data.Pages {
		rows = append(rows, layoutRows(p)[1:]...)
	}
	if len(rows) > 1048576 {
		return nil, errors.New("汇总超过 Excel 行数上限，请分批转换。")
	}
	wb += `<sheet name="面辅料汇总" sheetId="1" r:id="rId1"/>`
	rel += `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>`
	ct += `<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`
	if e := put("xl/worksheets/sheet1.xml", worksheet(rows)); e != nil {
		return nil, e
	}
	for name, content := range map[string]string{"[Content_Types].xml": ct + `</Types>`, "xl/workbook.xml": wb + `</sheets></workbook>`, "xl/_rels/workbook.xml.rels": rel + `</Relationships>`, "xl/styles.xml": xmlHead + styles, "_rels/.rels": xmlHead + `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`} {
		if e := put(name, content); e != nil {
			return nil, e
		}
	}
	if e := z.Close(); e != nil {
		return nil, e
	}
	return b.Bytes(), nil
}
func convert(input string) (output string, data extracted, err error) {
	data, err = extract(input)
	if err != nil {
		return
	}
	content, e := workbook(data)
	if e != nil {
		err = e
		return
	}
	output, err = saveWorkbook(input, strings.TrimSuffix(filepath.Base(input), filepath.Ext(input))+"_面辅料表", content)
	return
}
func saveWorkbook(input, stem string, content []byte) (output string, err error) {
	for i := 0; i < 10000; i++ {
		name := stem
		if i > 0 {
			name += fmt.Sprintf(" (%d)", i)
		}
		output = filepath.Join(filepath.Dir(input), name+".xlsx")
		f, e := os.OpenFile(output, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if errors.Is(e, os.ErrExist) {
			continue
		}
		if e != nil {
			err = fmt.Errorf("无法在原文件夹保存 Excel，请把 PPTX 复制到桌面后重试。\n%w", e)
			return
		}
		_, err = f.Write(content)
		if err == nil {
			err = f.Sync()
		}
		closeErr := f.Close()
		if err == nil {
			err = closeErr
		}
		if err != nil {
			_ = os.Remove(output)
		}
		return
	}
	err = errors.New("同名结果文件过多，请整理文件夹后重试。")
	return
}

type batchResult struct {
	Input, Output string
	Data          extracted
	Err           error
}

func convertBatch(inputs []string, progress func(int, int, string), templates ...*excelTemplate) []batchResult {
	var chosen *excelTemplate
	if len(templates) > 0 {
		chosen = templates[0]
	}
	return convertBatchWithRules(inputs, progress, chosen, nil)
}
func convertBatchWithRules(inputs []string, progress func(int, int, string), chosen *excelTemplate, rules []aliasRule) []batchResult {
	results := make([]batchResult, 0, len(inputs))
	seen := map[string]bool{}
	for _, input := range inputs {
		key, _ := filepath.Abs(input)
		if seen[key] {
			continue
		}
		seen[key] = true
		if progress != nil {
			progress(len(results)+1, len(inputs), input)
		}
		r := batchResult{Input: input}
		func() {
			defer func() {
				if v := recover(); v != nil {
					r.Err = fmt.Errorf("处理失败：%v", v)
				}
			}()
			r.Data, r.Err = extractWithRules(input, rules)
		}()
		results = append(results, r)
	}
	var combined extracted
	for _, r := range results {
		if r.Err == nil {
			combined.Pages = append(combined.Pages, r.Data.Pages...)
		}
	}
	if len(combined.Pages) > 0 {
		var content []byte
		var err error
		if chosen != nil {
			content, err = chosen.build(combined)
		} else {
			content, err = workbook(combined)
		}
		output := ""
		if err == nil {
			output, err = saveWorkbook(inputs[0], "面辅料汇总", content)
		}
		for i := range results {
			if results[i].Err == nil {
				results[i].Output = output
				results[i].Err = err
			}
		}
	}
	return results
}
