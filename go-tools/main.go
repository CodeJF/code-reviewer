package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"unicode"
)

// ============================================================
// Go 工具服务：给 Agent 提供代码静态分析能力
//
// Python 的 AST 分析可以用 Python 自己做，但用 Go 写有两个好处：
// 1. 面试时展示"跨语言 Agent 工具链"
// 2. Go 处理文件扫描、字符串分析比 Python 快得多
//
// 启动: cd go-tools && go run main.go
// 端口: 8787
// ============================================================

// AnalyzeRequest 接收要分析的代码或文件路径
type AnalyzeRequest struct {
	Code     string `json:"code,omitempty"`
	FilePath string `json:"file_path,omitempty"`
}

// AnalyzeResponse 返回分析结果
type AnalyzeResponse struct {
	FileName    string      `json:"file_name"`
	TotalLines  int         `json:"total_lines"`
	CodeLines   int         `json:"code_lines"`
	BlankLines  int         `json:"blank_lines"`
	CommentLines int        `json:"comment_lines"`
	Functions   []FuncInfo  `json:"functions"`
	Imports     []string    `json:"imports"`
	Issues      []Issue     `json:"issues"`
	Complexity  string      `json:"complexity"`
}

// FuncInfo 函数信息
type FuncInfo struct {
	Name      string `json:"name"`
	Line      int    `json:"line"`
	ArgCount  int    `json:"arg_count"`
	LineCount int    `json:"line_count"`
	HasReturn bool   `json:"has_return"`
	HasDocstr bool   `json:"has_docstring"`
}

// Issue 发现的问题
type Issue struct {
	Line     int    `json:"line"`
	Category string `json:"category"`
	Message  string `json:"message"`
}

func analyzePython(code string, fileName string) AnalyzeResponse {
	lines := strings.Split(code, "\n")
	resp := AnalyzeResponse{
		FileName:   fileName,
		TotalLines: len(lines),
		Functions:  []FuncInfo{},
		Imports:    []string{},
		Issues:     []Issue{},
	}

	var currentFunc *FuncInfo
	var funcStartLine int
	var funcIndent int

	for i, line := range lines {
		lineNum := i + 1
		trimmed := strings.TrimSpace(line)

		// 统计行类型
		if trimmed == "" {
			resp.BlankLines++
			continue
		}
		if strings.HasPrefix(trimmed, "#") {
			resp.CommentLines++
			continue
		}
		resp.CodeLines++

		// 提取 import
		if strings.HasPrefix(trimmed, "import ") || strings.HasPrefix(trimmed, "from ") {
			resp.Imports = append(resp.Imports, trimmed)
		}

		// 检测函数定义
		if strings.HasPrefix(trimmed, "def ") {
			// 保存上一个函数
			if currentFunc != nil {
				currentFunc.LineCount = lineNum - funcStartLine
				resp.Functions = append(resp.Functions, *currentFunc)
			}

			funcName := extractFuncName(trimmed)
			argCount := countArgs(trimmed)
			funcIndent = countIndent(line)
			funcStartLine = lineNum

			currentFunc = &FuncInfo{
				Name:     funcName,
				Line:     lineNum,
				ArgCount: argCount,
			}

			// 检查函数名规范（应该是 snake_case）
			if strings.ToLower(funcName) != funcName || containsUpperCase(funcName) {
				resp.Issues = append(resp.Issues, Issue{
					Line:     lineNum,
					Category: "style",
					Message:  fmt.Sprintf("函数名 '%s' 不符合 snake_case 规范", funcName),
				})
			}

			// 检查参数过多
			if argCount > 5 {
				resp.Issues = append(resp.Issues, Issue{
					Line:     lineNum,
					Category: "style",
					Message:  fmt.Sprintf("函数 '%s' 有 %d 个参数，建议不超过 5 个", funcName, argCount),
				})
			}
		}

		// 在函数内部检查
		if currentFunc != nil && lineNum > funcStartLine {
			currentIndent := countIndent(line)
			if trimmed != "" && currentIndent <= funcIndent && !strings.HasPrefix(trimmed, "def ") && !strings.HasPrefix(trimmed, "@") {
				currentFunc.LineCount = lineNum - funcStartLine
				resp.Functions = append(resp.Functions, *currentFunc)
				currentFunc = nil
			} else {
				if strings.Contains(trimmed, "return") {
					currentFunc.HasReturn = true
				}
				if lineNum == funcStartLine+1 && (strings.HasPrefix(trimmed, `"""`) || strings.HasPrefix(trimmed, `'''`)) {
					currentFunc.HasDocstr = true
				}
			}
		}

		// 通用检查
		if len(line) > 120 {
			resp.Issues = append(resp.Issues, Issue{
				Line:     lineNum,
				Category: "style",
				Message:  fmt.Sprintf("行长度 %d 超过 120 字符", len(line)),
			})
		}

		// 检测硬编码字符串（可能是密码/密钥）
		lowerTrimmed := strings.ToLower(trimmed)
		if (strings.Contains(lowerTrimmed, "password") || strings.Contains(lowerTrimmed, "secret") ||
			strings.Contains(lowerTrimmed, "api_key") || strings.Contains(lowerTrimmed, "token")) &&
			strings.Contains(trimmed, "=") && strings.Contains(trimmed, `"`) {
			resp.Issues = append(resp.Issues, Issue{
				Line:     lineNum,
				Category: "security",
				Message:  "疑似硬编码敏感信息",
			})
		}

		// 检测 open() 不带 with
		if strings.Contains(trimmed, "open(") && !strings.HasPrefix(trimmed, "with ") && strings.Contains(trimmed, "=") {
			resp.Issues = append(resp.Issues, Issue{
				Line:     lineNum,
				Category: "bug",
				Message:  "使用 open() 未配合 with 语句，可能导致文件句柄泄漏",
			})
		}

		// 检测 TODO/FIXME
		if strings.Contains(lowerTrimmed, "todo") || strings.Contains(lowerTrimmed, "fixme") {
			resp.Issues = append(resp.Issues, Issue{
				Line:     lineNum,
				Category: "docs",
				Message:  "存在未完成的 TODO/FIXME 标记",
			})
		}
	}

	// 保存最后一个函数
	if currentFunc != nil {
		currentFunc.LineCount = len(lines) - funcStartLine + 1
		resp.Functions = append(resp.Functions, *currentFunc)
	}

	// 检查过长函数
	for _, f := range resp.Functions {
		if f.LineCount > 50 {
			resp.Issues = append(resp.Issues, Issue{
				Line:     f.Line,
				Category: "style",
				Message:  fmt.Sprintf("函数 '%s' 有 %d 行，建议拆分（不超过 50 行）", f.Name, f.LineCount),
			})
		}
		if !f.HasDocstr {
			resp.Issues = append(resp.Issues, Issue{
				Line:     f.Line,
				Category: "docs",
				Message:  fmt.Sprintf("函数 '%s' 缺少 docstring", f.Name),
			})
		}
	}

	// 复杂度评估
	funcCount := len(resp.Functions)
	issueCount := len(resp.Issues)
	switch {
	case funcCount == 0:
		resp.Complexity = "script（无函数结构）"
	case issueCount == 0:
		resp.Complexity = "low（代码整洁）"
	case issueCount <= 3:
		resp.Complexity = "medium（有改进空间）"
	default:
		resp.Complexity = "high（需要重构）"
	}

	return resp
}

// === 辅助函数 ===

func extractFuncName(defLine string) string {
	defLine = strings.TrimPrefix(defLine, "def ")
	if idx := strings.Index(defLine, "("); idx > 0 {
		return defLine[:idx]
	}
	return defLine
}

func countArgs(defLine string) int {
	start := strings.Index(defLine, "(")
	end := strings.LastIndex(defLine, ")")
	if start < 0 || end < 0 || end <= start+1 {
		return 0
	}
	args := defLine[start+1 : end]
	if strings.TrimSpace(args) == "" {
		return 0
	}
	return len(strings.Split(args, ","))
}

func countIndent(line string) int {
	count := 0
	for _, ch := range line {
		if ch == ' ' {
			count++
		} else if ch == '\t' {
			count += 4
		} else {
			break
		}
	}
	return count
}

func containsUpperCase(s string) bool {
	for _, r := range s {
		if unicode.IsUpper(r) {
			return true
		}
	}
	return false
}

// ============================================================
// HTTP 处理
// ============================================================

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

	code := req.Code
	fileName := "inline"

	if req.FilePath != "" {
		data, err := os.ReadFile(req.FilePath)
		if err != nil {
			http.Error(w, "read file error: "+err.Error(), http.StatusBadRequest)
			return
		}
		code = string(data)
		fileName = filepath.Base(req.FilePath)
	}

	if code == "" {
		http.Error(w, "code or file_path required", http.StatusBadRequest)
		return
	}

	result := analyzePython(code, fileName)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"status":"ok","service":"go-code-analyzer"}`))
}

func main() {
	http.HandleFunc("/analyze", handleAnalyze)
	http.HandleFunc("/health", handleHealth)

	port := "8787"
	fmt.Printf("Go 代码分析服务启动: http://localhost:%s\n", port)
	fmt.Printf("  POST /analyze  — 分析 Python 代码\n")
	fmt.Printf("  GET  /health   — 健康检查\n")
	log.Fatal(http.ListenAndServe(":"+port, nil))
}
