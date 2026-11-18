package main

import (
	"archive/zip"
	"bytes"
	"errors"
	"fmt"
	"io"
	"path"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

type excelTemplate struct {
	Parts    map[string][]byte
	Sheet    string
	Header   string
	Mapping  []int
	Styles   []int
	RowAttrs string
	Headers  []string
}

var sheetDataRE = regexp.MustCompile(`(?s)<sheetData(?:\s[^>]*)?>.*?</sheetData>`)
var headerRowRE = regexp.MustCompile(`(?s)<row\b[^>]*\br="1"[^>]*>.*?</row>`)
var dimensionRE = regexp.MustCompile(`<dimension\b[^>]*/>`)
var filterRE = regexp.MustCompile(`<autoFilter\b[^>]*/>`)

func columnNumber(ref string) int {
	n := 0
	for _, r := range ref {
		if r < 'A' || r > 'Z' {
			break
		}
		n = n*26 + int(r-'A'+1)
	}
	return n
}
func templateValue(c *node, shared []string) string {
	if c.attr("t") == "inlineStr" {
		return textRuns(c)
	}
	v := c.find("v")
	if len(v) == 0 {
		return ""
	}
	s := v[0].Text
	if c.attr("t") == "s" {
		i, e := strconv.Atoi(s)
		if e == nil && i >= 0 && i < len(shared) {
			return shared[i]
		}
		return ""
	}
	return s
}
func loadTemplate(file string) (*excelTemplate, error) { return loadTemplateWithRules(file, nil) }
func loadTemplateWithRules(file string, rules []aliasRule) (*excelTemplate, error) {
	if !strings.EqualFold(filepath.Ext(file), ".xlsx") {
		return nil, errors.New("请选择 .xlsx 格式模板。")
	}
	z, e := zip.OpenReader(file)
	if e != nil {
		return nil, errors.New("无法读取模板，请确认是未加密的 .xlsx 文件。")
	}
	defer z.Close()
	t := &excelTemplate{Parts: map[string][]byte{}}
	total := 0
	for _, f := range z.File {
		if f.UncompressedSize64 > 16<<20 {
			return nil, errors.New("模板过大，请删除无关图片和数据。")
		}
		r, e := f.Open()
		if e != nil {
			return nil, e
		}
		b, e := io.ReadAll(io.LimitReader(r, (16<<20)+1))
		r.Close()
		if e != nil {
			return nil, e
		}
		total += len(b)
		if total > 32<<20 || len(b) > 16<<20 {
			return nil, errors.New("模板过大，请精简后重试。")
		}
		t.Parts[f.Name] = b
	}
	wb, e := parse(t.Parts["xl/workbook.xml"])
	if e != nil {
		return nil, e
	}
	sheets := wb.find("sheet")
	if len(sheets) != 1 {
		return nil, errors.New("请上传仅包含一个工作表的模板，第一行为表头，第二行为数据样式。")
	}
	relation := ""
	for _, a := range sheets[0].Attr {
		if a.Name.Local == "id" && strings.Contains(a.Name.Space, "relationships") {
			relation = a.Value
		}
	}
	rels, e := parse(t.Parts["xl/_rels/workbook.xml.rels"])
	if e != nil {
		return nil, e
	}
	for _, r := range rels.find("Relationship") {
		if r.attr("Id") == relation && r.attr("TargetMode") != "External" {
			target := r.attr("Target")
			if strings.HasPrefix(target, "/") {
				t.Sheet = strings.TrimPrefix(target, "/")
			} else {
				t.Sheet = path.Join("xl", target)
			}
		}
	}
	raw := t.Parts[t.Sheet]
	if len(raw) == 0 {
		return nil, errors.New("未找到模板工作表。")
	}
	doc, e := parse(raw)
	if e != nil {
		return nil, e
	}
	for _, tag := range []string{"mergeCell", "f", "tablePart", "drawing", "legacyDrawing", "conditionalFormatting", "dataValidation"} {
		if len(doc.find(tag)) > 0 {
			return nil, fmt.Errorf("模板包含暂不支持的结构（%s）。请使用普通单层表头、无合并单元格和公式的模板。", tag)
		}
	}
	var shared []string
	if b := t.Parts["xl/sharedStrings.xml"]; len(b) > 0 {
		n, e := parse(b)
		if e != nil {
			return nil, e
		}
		for _, si := range n.find("si") {
			shared = append(shared, textRuns(si))
		}
	}
	rows := doc.find("row")
	var header, body *node
	for _, r := range rows {
		if r.attr("r") == "1" {
			header = r
		}
		if r.attr("r") == "2" {
			body = r
		}
	}
	if header == nil {
		return nil, errors.New("模板第 1 行需包含表头。")
	}
	if body == nil {
		body = &node{}
	}
	headers := map[int]string{}
	maxCol := 0
	for _, c := range header.direct("c") {
		i := columnNumber(c.attr("r"))
		if i > 16384 {
			return nil, errors.New("模板列数超过支持范围。")
		}
		headers[i] = strings.TrimSpace(templateValue(c, shared))
		if headers[i] != "" && i > maxCol {
			maxCol = i
		}
	}
	if maxCol == 0 {
		return nil, errors.New("模板第 1 行没有可识别的表头文字。")
	}
	aliases := map[string]int{"款号": 0, "款式编号": 0, "款式号": 0, "产品款号": 0, "模特编号": 0, "STYLE": 0, "STYLENO": 0, "序号": 1, "编号": 1, "NO": 1, "面料": 2, "面料编号": 2, "面料编码": 2, "面料名称": 2, "FABRIC": 2, "面料颜色": 3, "面料色号": 3, "FABRICCOLOR": 3, "使用部位": 4, "部位": 4, "用途": 4, "辅料编号": 5, "辅料编码": 5, "辅料": 5, "辅料名称": 5, "辅料颜色": 6, "辅料色号": 6, "TRIMCOLOR": 6, "SIZE": 7, "尺寸": 7, "规格": 7, "数量": 8, "用量": 8, "QTY": 8, "QUANTITY": 8, "MEMO": 9, "备注": 9, "说明": 9,
		"设计师": 10, "디자이너": 10, "年份": 11, "年度": 11, "연도": 11, "季节": 12, "시즌": 12, "TAG序号": 13, "TAGNO": 13, "TAGCOLOR": 14, "TAG颜色": 14, "TAG色号": 14, "TAGA": 15, "TAGB": 16, "TAGC": 17, "TAGD": 18}
	for i := 1; i <= maxCol; i++ {
		h := headers[i]
		key := strings.TrimSuffix(norm(h), ".")
		field, ok := aliases[key]
		// Preserve the original template's explicit color/COLOR distinction.
		if h == "color" {
			field = 3
			ok = true
		}
		if h == "COLOR" {
			field = 6
			ok = true
		}
		if custom, found := aliasTarget(h, rules); found {
			field, ok = custom, true
		}
		if !ok {
			field = -1
		}
		t.Mapping = append(t.Mapping, field)
		t.Headers = append(t.Headers, h)
	}
	t.Styles = make([]int, maxCol)
	for _, c := range body.direct("c") {
		i := columnNumber(c.attr("r"))
		if i > 0 && i <= maxCol {
			t.Styles[i-1], _ = strconv.Atoi(c.attr("s"))
		}
	}
	for _, a := range body.Attr {
		if a.Name.Local == "ht" || a.Name.Local == "customHeight" || a.Name.Local == "s" || a.Name.Local == "customFormat" {
			t.RowAttrs += fmt.Sprintf(` %s="%s"`, a.Name.Local, esc(a.Value))
		}
	}
	t.Header = string(headerRowRE.Find(raw))
	if t.Header == "" || !sheetDataRE.Match(raw) {
		return nil, errors.New("模板 XML 结构暂不支持，请用 Excel 或 WPS 另存为 .xlsx 后重试。")
	}
	return t, nil
}
func (t *excelTemplate) build(data extracted) ([]byte, error) {
	var sheet strings.Builder
	sheet.WriteString("<sheetData>")
	sheet.WriteString(t.Header)
	rowNum := 2
	for _, p := range data.Pages {
		for rowIndex, src := range layoutRows(p)[1:] {
			extras := extraValues(p, rowIndex)
			if rowNum > 1048576 {
				return nil, errors.New("汇总超过 Excel 行数上限，请分批转换。")
			}
			fmt.Fprintf(&sheet, `<row r="%d"%s>`, rowNum, t.RowAttrs)
			for ci, field := range t.Mapping {
				v := ""
				// Blank template columns are layout space, never output fields.
				if ci < len(t.Headers) && strings.TrimSpace(t.Headers[ci]) == "" {
					field = -1
				} else if field >= 0 && field < len(src) {
					v = src[field]
				} else if field < 0 && ci < len(t.Headers) {
					v = extras[norm(t.Headers[ci])]
				}
				numeric := (field == 1 || field == 8 || field == 13) && numberRE.MatchString(v) && len(v) <= 15
				sheet.WriteString(cell(col(ci+1)+strconv.Itoa(rowNum), v, t.Styles[ci], numeric))
			}
			sheet.WriteString("</row>")
			rowNum++
		}
	}
	sheet.WriteString("</sheetData>")
	raw := t.Parts[t.Sheet]
	modified := sheetDataRE.ReplaceAllFunc(raw, func([]byte) []byte { return []byte(sheet.String()) })
	dimension := fmt.Sprintf("A1:%s%d", col(len(t.Headers)), rowNum-1)
	dimXML := []byte(`<dimension ref="` + dimension + `"/>`)
	if dimensionRE.Match(modified) {
		modified = dimensionRE.ReplaceAll(modified, dimXML)
	} else {
		// Dimension follows sheetPr when present, otherwise the worksheet opening.
		anchor := regexp.MustCompile(`(?s)<sheetPr\b[^>]*(?:/>|>.*?</sheetPr>)`).FindIndex(modified)
		if anchor == nil {
			anchor = regexp.MustCompile(`<worksheet\b[^>]*>`).FindIndex(modified)
		}
		if anchor != nil {
			end := anchor[1]
			next := append([]byte(nil), modified[:end]...)
			next = append(next, dimXML...)
			modified = append(next, modified[end:]...)
		}
	}
	modified = filterRE.ReplaceAll(modified, []byte(`<autoFilter ref="`+dimension+`"/>`))
	var b bytes.Buffer
	z := zip.NewWriter(&b)
	for name, content := range t.Parts {
		if name == t.Sheet {
			content = modified
		}
		w, e := z.Create(name)
		if e != nil {
			return nil, e
		}
		if _, e = w.Write(content); e != nil {
			return nil, e
		}
	}
	if e := z.Close(); e != nil {
		return nil, e
	}
	return b.Bytes(), nil
}

func extraValues(p page, rowIndex int) map[string]string {
	result := map[string]string{}
	for name, value := range p.ExtraMeta {
		result[norm(name)] = strings.ReplaceAll(value, "\n", "")
	}
	for _, t := range p.Tables {
		if rowIndex+1 >= len(t.Rows) {
			continue
		}
		for j, name := range t.Rows[0] {
			if strings.TrimSpace(name) == "" || j >= len(t.Rows[rowIndex+1]) {
				continue
			}
			field := sourceField(name, t.Kind, nil)
			if j < len(t.Fields) {
				field = t.Fields[j]
			}
			if field < 0 {
				result[norm(name)] = strings.ReplaceAll(t.Rows[rowIndex+1][j], "\n", "")
			}
		}
	}
	if len(p.TagExtras) > 0 {
		idx := rowIndex
		if len(p.TagExtras) == 1 {
			idx = 0
		}
		if idx < len(p.TagExtras) {
			for name, value := range p.TagExtras[idx] {
				result[norm(name)] = strings.ReplaceAll(value, "\n", "")
			}
		}
	}
	return result
}
