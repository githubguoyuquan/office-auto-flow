package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestBridgeBatchAndNoOverwrite(t *testing.T) {
	input := fixture(t, map[string]string{"slide1.xml": sampleTable(), "slide2.xml": `<slide/>`})
	directory := t.TempDir()
	req := request{Action: "extract", Inputs: []string{input, filepath.Join(directory, "bad.pptx"), input}, Template: templateFixture(t, nil), OutputDir: directory}
	var last map[string]any
	emit := func(value any) error { last = value.(map[string]any); return nil }
	if err := run(req, emit); err != nil {
		t.Fatal(err)
	}
	if last["type"] != "done" || last["success"] != 1 || last["failed"] != 1 || last["count"] != 2 {
		t.Fatal(last)
	}
	first := last["output"].(string)
	if _, err := os.Stat(first); err != nil {
		t.Fatal(err)
	}
	if err := run(req, emit); err != nil {
		t.Fatal(err)
	}
	if first == last["output"] {
		t.Fatal("existing output overwritten")
	}
}

func TestBridgeTemplateAndInvalidMapping(t *testing.T) {
	req := request{Action: "analyze_template", Template: templateFixture(t, nil)}
	var event map[string]any
	if err := run(req, func(value any) error { event = value.(map[string]any); return nil }); err != nil {
		t.Fatal(err)
	}
	if event["type"] != "template" || len(event["headers"].([]string)) == 0 {
		t.Fatal(event)
	}
	req.Action = "extract"
	req.Mapping = []int{999}
	if err := run(req, func(any) error { return nil }); err == nil {
		t.Fatal("invalid mapping accepted")
	}
}
