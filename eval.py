"""
v5: Evals — 自动化评测 AI 代码审查的质量
跑法: uv run eval.py

核心思路：
  1. 准备一批"已知答案"的代码样本
  2. 让 AI 审查每个样本
  3. 检查 AI 的输出是否符合预期
  4. 汇总通过率

Go 类比：就是 go test，只不过被测对象不是函数，是 AI
"""
import os
import sys
import json
import time
from dotenv import load_dotenv
load_dotenv(override=True)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 复用 main.py 里的 review_code 函数
from main import review_code


# ============================================================
# 第一部分：定义测试用例
#
# 每个用例 = 一段代码 + 你期望 AI 给出的判断
# 不是检查 AI 说的每个字，而是检查"关键判断对不对"
# ============================================================

TEST_CASES = [
    {
        "name": "应该发现硬编码密码",
        "code": """
password = "admin123"

def login(user, pwd):
    if pwd == password:
        return True
    return False
""",
        "checks": [
            {"type": "min_issues", "value": 1},
            {"type": "max_score", "value": 60},
            {"type": "has_severity", "value": "high"},
            {"type": "has_category", "value": "security"},
        ]
    },
    {
        "name": "应该发现文件未关闭",
        "code": """
def read_data(path):
    f = open(path)
    data = f.read()
    return data
""",
        "checks": [
            {"type": "min_issues", "value": 1},
            {"type": "has_severity", "value": "high"},
            {"type": "has_category", "value": "bug"},
        ]
    },
    {
        "name": "应该发现除零风险",
        "code": """
def divide(a, b):
    return a / b
""",
        "checks": [
            {"type": "min_issues", "value": 1},
            {"type": "has_category", "value": "bug"},
        ]
    },
    {
        "name": "干净代码应该高分",
        "code": """
def add(a: int, b: int) -> int:
    \"\"\"Return the sum of two integers.\"\"\"
    return a + b
""",
        "checks": [
            {"type": "min_score", "value": 70},
            {"type": "max_issues", "value": 2},
        ]
    },
    {
        "name": "应该发现 SQL 注入风险",
        "code": """
def get_user(db, username):
    query = f"SELECT * FROM users WHERE name = '{username}'"
    return db.execute(query)
""",
        "checks": [
            {"type": "has_severity", "value": "high"},
            {"type": "has_category", "value": "security"},
        ]
    },
]


# ============================================================
# 第二部分：检查函数
#
# 每种 check type 对应一个判断逻辑
# ============================================================

def run_check(result: dict, check: dict) -> tuple[bool, str]:
    """
    检查 AI 的审查结果是否符合预期。
    返回 (是否通过, 原因说明)
    """
    check_type = check["type"]
    expected = check["value"]
    issues = result.get("issues", [])
    score = result.get("score", 0)

    if check_type == "min_issues":
        actual = len(issues)
        passed = actual >= expected
        return passed, f"问题数 {actual} >= {expected}" if passed else f"问题数 {actual} < {expected}"

    if check_type == "max_issues":
        actual = len(issues)
        passed = actual <= expected
        return passed, f"问题数 {actual} <= {expected}" if passed else f"问题数 {actual} > {expected}"

    if check_type == "min_score":
        passed = score >= expected
        return passed, f"评分 {score} >= {expected}" if passed else f"评分 {score} < {expected}"

    if check_type == "max_score":
        passed = score <= expected
        return passed, f"评分 {score} <= {expected}" if passed else f"评分 {score} > {expected}"

    if check_type == "has_severity":
        severities = [i["severity"] for i in issues]
        passed = expected in severities
        return passed, f"包含 {expected}" if passed else f"未找到 {expected}，实际: {severities}"

    if check_type == "has_category":
        categories = [i["category"] for i in issues]
        passed = expected in categories
        return passed, f"包含 {expected}" if passed else f"未找到 {expected}，实际: {categories}"

    return False, f"未知检查类型: {check_type}"


# ============================================================
# 第三部分：运行所有测试用例
# ============================================================

def run_eval():
    total_cases = len(TEST_CASES)
    passed_cases = 0
    total_checks = 0
    passed_checks = 0
    results_log = []

    print(f"开始评测，共 {total_cases} 个用例\n")

    for i, case in enumerate(TEST_CASES):
        name = case["name"]
        print(f"[{i + 1}/{total_cases}] {name}")

        # 调 AI 审查
        start = time.time()
        try:
            result = review_code(case["code"])
        except Exception as e:
            print(f"  API 调用失败: {e}")
            results_log.append({"name": name, "status": "error", "error": str(e)})
            continue
        elapsed = time.time() - start

        print(f"  评分: {result['score']}, 问题数: {len(result['issues'])}, 耗时: {elapsed:.1f}s")

        # 逐项检查
        case_passed = True
        check_details = []
        for check in case["checks"]:
            passed, reason = run_check(result, check)
            total_checks += 1
            if passed:
                passed_checks += 1
                print(f"  [PASS] {reason}")
            else:
                case_passed = False
                print(f"  [FAIL] {reason}")
            check_details.append({"check": check, "passed": passed, "reason": reason})

        if case_passed:
            passed_cases += 1

        results_log.append({
            "name": name,
            "status": "pass" if case_passed else "fail",
            "score": result["score"],
            "issue_count": len(result["issues"]),
            "elapsed": round(elapsed, 1),
            "checks": check_details,
        })
        print()

    # 汇总
    print("=" * 50)
    print(f"用例通过率: {passed_cases}/{total_cases} ({passed_cases / total_cases * 100:.0f}%)")
    print(f"检查通过率: {passed_checks}/{total_checks} ({passed_checks / total_checks * 100:.0f}%)")
    print("=" * 50)

    # 保存详细结果
    with open("eval_results.json", "w", encoding="utf-8") as f:
        json.dump(results_log, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果已保存到: eval_results.json")

    return passed_cases == total_cases


if __name__ == "__main__":
    success = run_eval()
    sys.exit(0 if success else 1)
