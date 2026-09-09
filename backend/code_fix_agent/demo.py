"""场景3：自动代码修复 — Agent 读破损代码 → 跑测试 → 修复 → 验证。"""

import json, os, subprocess, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import AnthropicLLM, LoopTracer, run_loop

BUGGY_DIR = os.path.join(os.path.dirname(__file__), "buggy_project")

READ_FILE_TOOL = {
    "name": "read_file",
    "description": "读取项目中的一个源文件。支持: pricing.py, test_pricing.py",
    "input_schema": {"type": "object", "properties": {"filename": {"type": "string", "description": "pricing.py 或 test_pricing.py"}}, "required": ["filename"]},
}
WRITE_FILE_TOOL = {
    "name": "write_file",
    "description": "将修复后的内容写入文件。必须确保写入后不引入新错误。",
    "input_schema": {"type": "object", "properties": {"filename": {"type": "string", "description": "文件名"}, "content": {"type": "string", "description": "文件完整内容"}}, "required": ["filename", "content"]},
}
RUN_TEST_TOOL = {
    "name": "run_tests",
    "description": "执行 buggy_project 目录下的测试套件，返回通过/失败状态和错误信息。",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}
TOOLS = [READ_FILE_TOOL, WRITE_FILE_TOOL, RUN_TEST_TOOL]


def tool_impl(name: str, args: dict) -> str:
    if name == "read_file":
        fname = args["filename"]
        path = os.path.join(BUGGY_DIR, fname)
        if not os.path.exists(path):
            return json.dumps({"error": f"文件不存在: {fname}"})
        with open(path) as f:
            return json.dumps({"filename": fname, "content": f.read()}, ensure_ascii=False)
    if name == "write_file":
        fname = args["filename"]
        path = os.path.join(BUGGY_DIR, fname)
        with open(path, "w") as f:
            f.write(args["content"])
        return json.dumps({"filename": fname, "written": True, "size": len(args["content"])})
    if name == "run_tests":
        result = subprocess.run(
            [sys.executable, os.path.join(BUGGY_DIR, "test_pricing.py")],
            capture_output=True, text=True, cwd=BUGGY_DIR, timeout=10,
        )
        passed = result.returncode == 0
        return json.dumps({"passed": passed, "stdout": result.stdout[:500], "stderr": result.stderr[:500]})
    return json.dumps({"error": f"未知工具: {name}"})


if __name__ == "__main__":
    print("=" * 60)
    print("▶ 场景3: 自动代码修复")
    print("=" * 60)

    # 先跑一次测试确认有 bug
    print("\n📋 修复前测试:")
    r = subprocess.run(
        [sys.executable, os.path.join(BUGGY_DIR, "test_pricing.py")],
        capture_output=True, text=True, cwd=BUGGY_DIR, timeout=10,
    )
    print(r.stdout)

    llm = AnthropicLLM()
    tracer = run_loop(
        f"你需要修复 buggy_project/ 下的 pricing.py。先读 pricing.py 和 test_pricing.py，"
        f"运行测试看到失败，分析每个失败的原因，逐个修复，每次修复后跑测试验证。"
        f"修复到所有测试通过为止。最多尝试 5 轮修复。",
        TOOLS, tool_impl, llm=llm, max_iterations=15,
    )

    # 修复后验证
    print("\n📋 修复后测试:")
    r2 = subprocess.run(
        [sys.executable, os.path.join(BUGGY_DIR, "test_pricing.py")],
        capture_output=True, text=True, cwd=BUGGY_DIR, timeout=10,
    )
    print(r2.stdout)

    tracer.print_summary()
    tracer.save(os.path.join(os.path.dirname(__file__), "..", "traces", "code_fix.json"))
