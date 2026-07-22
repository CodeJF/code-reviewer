package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"regexp"
	"sort"
	"strings"
)

type AnalyzeRequest struct {
	FilePath string `json:"file_path,omitempty"`
	Text     string `json:"text,omitempty"`
}

type LogEvent struct {
	Line      int    `json:"line"`
	Level     string `json:"level"`
	Timestamp string `json:"timestamp,omitempty"`
	Message   string `json:"message"`
}

type AnalyzeResponse struct {
	Service       string         `json:"service"`
	RiskLevel     string         `json:"risk_level"`
	LineCount     int            `json:"line_count"`
	ErrorCount    int            `json:"error_count"`
	WarningCount  int            `json:"warning_count"`
	LevelCounts   map[string]int `json:"level_counts"`
	TopErrors     []TopError     `json:"top_errors"`
	ErrorKeywords []KeywordCount `json:"error_keywords"`
	MessageIDs    []string       `json:"message_ids"`
	UUIDs         []string       `json:"uuids"`
	Timeline      []LogEvent     `json:"timeline"`
	Incidents     map[string]int `json:"incidents"`
}

type TopError struct {
	Message string `json:"message"`
	Count   int    `json:"count"`
}

type KeywordCount struct {
	Keyword string `json:"keyword"`
	Count   int    `json:"count"`
}

var (
	ipPattern           = regexp.MustCompile(`\b(?:\d{1,3}\.){3}\d{1,3}\b`)
	phonePattern        = regexp.MustCompile(`\b1[3-9]\d{9}\b`)
	secretPattern       = regexp.MustCompile(`(?i)(password|passwd|pwd|token|secret|access[_-]?key|api[_-]?key)\s*[:=]\s*['"]?[^,'"\s}]+`)
	timestampPattern    = regexp.MustCompile(`\d{4}[-/]\d{2}[-/]\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?`)
	messageIDPattern    = regexp.MustCompile(`(?i)\b(?:message[_-]?id|msg[_-]?id|request[_-]?id)\b["'=:\s]+([A-Za-z0-9_-]{6,})`)
	uuidPattern         = regexp.MustCompile(`(?i)\b(?:uuid|device[_-]?id|sn)\b["'=:\s]+([A-Za-z0-9_-]{6,})`)
	errorKeywordPattern = regexp.MustCompile(`(?i)\b(error|err|fail|failed|fatal|panic|timeout|invalid|denied|refused|disconnect|offline|unauthorized|exception)\b`)
)

func redact(line string) string {
	line = ipPattern.ReplaceAllString(line, "<IP>")
	line = phonePattern.ReplaceAllString(line, "<PHONE>")
	line = secretPattern.ReplaceAllString(line, "$1=<REDACTED>")
	return line
}

func inferService(path string, text string) string {
	lower := strings.ToLower(path + "\n" + text)
	switch {
	case strings.Contains(lower, "deviceshadow"):
		return "deviceShadow"
	case strings.Contains(lower, "pushservice"):
		return "pushService"
	case strings.Contains(lower, "adminservice"):
		return "AdminService"
	case strings.Contains(lower, "cloudstorage"):
		return "cloudStorage"
	case strings.Contains(lower, "gateway"):
		return "gateway"
	default:
		return "unknown"
	}
}

func level(line string) string {
	lower := strings.ToLower(line)
	switch {
	case strings.Contains(lower, "fatal") || strings.Contains(lower, "panic"):
		return "fatal"
	case strings.Contains(lower, "error") || strings.Contains(lower, "failed") || strings.Contains(lower, "fail") || strings.Contains(lower, "timeout") || strings.Contains(lower, "invalid"):
		return "error"
	case strings.Contains(lower, "warn") || strings.Contains(lower, "retry") || strings.Contains(lower, "reconnect"):
		return "warning"
	default:
		return "info"
	}
}

func incidentType(line string, service string) string {
	lower := strings.ToLower(line)
	switch {
	case service == "gateway" && (strings.Contains(lower, "device/login") || strings.Contains(lower, "devicelogin") || strings.Contains(lower, "uuidinvalid")):
		return "device_login_failed"
	case service == "deviceShadow" && strings.Contains(lower, "mqtt") && (strings.Contains(lower, "connect") || strings.Contains(lower, "offline") || strings.Contains(lower, "disconnect") || strings.Contains(lower, "reconnect")):
		return "mqtt_connection_failed"
	case service == "deviceShadow" && (strings.Contains(lower, "payload") || strings.Contains(lower, "unmarshal") || strings.Contains(lower, "topic is invalid")):
		return "mqtt_payload_invalid"
	case strings.Contains(lower, "rpc") || strings.Contains(lower, "notify"):
		return "rpc_call_failed"
	case strings.Contains(lower, "push send error"):
		return "push_failed"
	case strings.Contains(lower, "ota") || strings.Contains(lower, "upgrade"):
		return "ota_failed"
	case service == "deviceShadow" && (strings.Contains(lower, "websocket send error") || strings.Contains(lower, "websocket connection error") || strings.Contains(lower, "websocket failed")):
		return "websocket_failed"
	case strings.Contains(lower, "env file") || strings.Contains(lower, "configuration parameter"):
		return "config_init_failed"
	case containsAny(lower, []string{"mysql", "mongo", "mongodb", "redis"}) && containsAny(lower, []string{"connection error", "ping error", "conn error", "connect refused", "connection refused", "dial tcp", "noauth", "authentication required"}):
		return "database_connection_failed"
	default:
		return ""
	}
}

func containsAny(text string, keywords []string) bool {
	for _, keyword := range keywords {
		if strings.Contains(text, keyword) {
			return true
		}
	}
	return false
}

func firstMatch(pattern *regexp.Regexp, text string) string {
	match := pattern.FindStringSubmatch(text)
	if len(match) < 2 {
		return ""
	}
	return match[1]
}

func addSetValue(set map[string]bool, value string) {
	if value != "" {
		set[value] = true
	}
}

func setToSortedSlice(set map[string]bool, limit int) []string {
	values := make([]string, 0, len(set))
	for value := range set {
		values = append(values, value)
	}
	sort.Strings(values)
	if limit > 0 && len(values) > limit {
		return values[:limit]
	}
	return values
}

func keywordCounts(counts map[string]int, limit int) []KeywordCount {
	items := make([]KeywordCount, 0, len(counts))
	for keyword, count := range counts {
		items = append(items, KeywordCount{Keyword: keyword, Count: count})
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].Count == items[j].Count {
			return items[i].Keyword < items[j].Keyword
		}
		return items[i].Count > items[j].Count
	})
	if limit > 0 && len(items) > limit {
		return items[:limit]
	}
	return items
}

func riskLevel(errorCount int, incidents map[string]int) string {
	if errorCount >= 5 || len(incidents) > 0 {
		return "high"
	}
	if errorCount > 0 {
		return "medium"
	}
	return "low"
}

func analyze(text string, source string) AnalyzeResponse {
	resp := AnalyzeResponse{
		Service:     inferService(source, text),
		LevelCounts: map[string]int{},
		Incidents:   map[string]int{},
	}
	top := map[string]int{}
	keywords := map[string]int{}
	messageIDs := map[string]bool{}
	uuids := map[string]bool{}
	scanner := bufio.NewScanner(strings.NewReader(text))
	lineNo := 0
	for scanner.Scan() {
		lineNo++
		line := strings.TrimSpace(redact(scanner.Text()))
		if line == "" {
			continue
		}
		resp.LineCount++
		lvl := level(line)
		resp.LevelCounts[lvl]++
		if lvl == "fatal" || lvl == "error" {
			resp.ErrorCount++
		}
		if lvl == "warning" {
			resp.WarningCount++
		}
		addSetValue(messageIDs, firstMatch(messageIDPattern, line))
		addSetValue(uuids, firstMatch(uuidPattern, line))

		inc := incidentType(line, resp.Service)
		if inc != "" {
			resp.Incidents[inc]++
		}
		if lvl == "fatal" || lvl == "error" || lvl == "warning" {
			for _, match := range errorKeywordPattern.FindAllString(line, -1) {
				keywords[strings.ToLower(match)]++
			}
			timestamp := timestampPattern.FindString(line)
			if len(resp.Timeline) < 100 {
				resp.Timeline = append(resp.Timeline, LogEvent{Line: lineNo, Level: lvl, Timestamp: timestamp, Message: line})
			}
			top[line]++
		}
	}

	for msg, count := range top {
		resp.TopErrors = append(resp.TopErrors, TopError{Message: msg, Count: count})
	}
	sort.Slice(resp.TopErrors, func(i, j int) bool {
		return resp.TopErrors[i].Count > resp.TopErrors[j].Count
	})
	if len(resp.TopErrors) > 10 {
		resp.TopErrors = resp.TopErrors[:10]
	}
	resp.ErrorKeywords = keywordCounts(keywords, 12)
	resp.MessageIDs = setToSortedSlice(messageIDs, 20)
	resp.UUIDs = setToSortedSlice(uuids, 20)
	resp.RiskLevel = riskLevel(resp.ErrorCount, resp.Incidents)
	return resp
}

func handleAnalyze(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "only POST", http.StatusMethodNotAllowed)
		return
	}
	var req AnalyzeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON: "+err.Error(), http.StatusBadRequest)
		return
	}
	text := req.Text
	source := "inline"
	if req.FilePath != "" {
		data, err := os.ReadFile(req.FilePath)
		if err != nil {
			http.Error(w, "read file: "+err.Error(), http.StatusBadRequest)
			return
		}
		text = string(data)
		source = req.FilePath
	}
	if strings.TrimSpace(text) == "" {
		http.Error(w, "file_path or text required", http.StatusBadRequest)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(analyze(text, source))
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"status":"ok","service":"sl100-go-log-tools"}`))
}

func analyzeFile(path string) (AnalyzeResponse, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return AnalyzeResponse{}, err
	}
	return analyze(string(data), path), nil
}

func printJSON(value any) error {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}

func main() {
	filePath := flag.String("file", "", "Analyze one log file and print JSON.")
	inlineText := flag.String("text", "", "Analyze inline log text and print JSON.")
	serve := flag.Bool("serve", false, "Start HTTP server.")
	port := flag.String("port", os.Getenv("PORT"), "HTTP server port.")
	flag.Parse()

	if *filePath != "" {
		result, err := analyzeFile(*filePath)
		if err != nil {
			log.Fatal(err)
		}
		if err := printJSON(result); err != nil {
			log.Fatal(err)
		}
		return
	}
	if strings.TrimSpace(*inlineText) != "" {
		if err := printJSON(analyze(*inlineText, "inline")); err != nil {
			log.Fatal(err)
		}
		return
	}

	if !*serve && flag.NFlag() > 0 {
		flag.Usage()
		os.Exit(2)
	}

	http.HandleFunc("/analyze", handleAnalyze)
	http.HandleFunc("/health", handleHealth)
	if *port == "" {
		*port = "8788"
	}
	fmt.Printf("SL100 Go log analyzer listening on http://localhost:%s\n", *port)
	log.Fatal(http.ListenAndServe(":"+*port, nil))
}
