"""校验 docs/project_atlas.html 的 ATLAS_DATA 与真实源码一致。

背景：project_atlas.html 是生成物，模块规格里的文件路径、函数/类名与行号靠手工维护，
必然随代码漂移。本脚本把「文档引用即代码事实」变成可执行检查：
  1. 每个 modules[].files[].path 在项目根下真实存在；
  3. 每个 l2_contracts[].name 能在对应文件里找到「定义行」（def/class/常量/JS 函数式声明），
     且 |定义行 - 标注行| ≤ 10；
  4. 模块间引用（upstream/downstream/readsState/writesState）与 states 的
     writers/readers 必须指向真实存在的模块 id（以 / 或 @ 开头的外部引用跳过）；
  5. tasks/concepts 的 refs 同第 4 条。
用法：.venv/bin/python scripts/verify_atlas.py   （exit 0 = 全部通过）
"""
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
HTML = PROJECT / "docs" / "project_atlas.html"
MARK_OPEN = '<script id="atlas-data" type="application/json">'
MARK_CLOSE = "</script>"
MAX_DRIFT = 10

PY_DEF = r"^\s*(?:async\s+)?def\s+{sym}\b"
PY_CLASS = r"^\s*class\s+{sym}\b"
PY_CONST = r"^{sym}\s*[:=]"
JS_DEF = r"^\s*(?:export\s+)?(?:async\s+)?(?:function|const|let|class)\s+{sym}\b"


def load_data() -> dict:
    text = HTML.read_text(encoding="utf-8")
    start = text.index(MARK_OPEN) + len(MARK_OPEN)
    end = text.index(MARK_CLOSE, start)
    return json.loads(text[start:end])


def definition_lines(source: str, symbol: str) -> list[int]:
    patterns = [p.format(sym=re.escape(symbol))
                for p in (PY_DEF, PY_CLASS, PY_CONST, JS_DEF)]
    rx = re.compile("|".join(f"(?:{p})" for p in patterns), re.MULTILINE)
    return [i + 1 for i, line in enumerate(source.splitlines()) if rx.search(line)]


def main() -> int:
    errors: list[str] = []
    stats = {"files": 0, "symbols": 0}

    data = load_data()
    # 项目根一律取脚本所在仓库，不读 meta.abs_root——那是 vscode 深链的本机展示字段
    # （开源后不同人 clone 到不同路径），仓库里存占位值即可，校验不依赖它。
    root = PROJECT

    cache: dict[str, str] = {}
    module_ids = {m["id"] for m in data["modules"]}
    layer_ids = {l["id"] for l in data.get("layers", [])}
    state_names = {s["name"] for s in data.get("states", [])}

    def check_ref(owner: str, ref):
        if not isinstance(ref, str) or ref.startswith(("/", "@")):
            return
        if ref.startswith("S."):
            if ref not in state_names:
                errors.append(f"[{owner}] 引用未登记的状态字段：{ref}")
            return
        if ref.startswith("Post."):
            return  # 领域字段不建索引，仅作展示语义
        if ref not in module_ids:
            errors.append(f"[{owner}] 引用不存在的模块：{ref}")

    for mod in data["modules"]:
        mid = mod["id"]
        if mod.get("layer") and mod["layer"] not in layer_ids:
            errors.append(f"[{mid}] layer 不在 layers 清单：{mod['layer']}")
        for f in mod.get("files", []):
            stats["files"] += 1
            if not (root / f["path"]).exists():
                errors.append(f"[{mid}] 文件不存在：{f['path']}")
        for key in ("upstream", "downstream", "readsState", "writesState"):
            for ref in mod.get(key, []):
                check_ref(mid, ref)
        for c in mod.get("l2_contracts", []):
            stats["symbols"] += 1
            path, symbol = c["file"], c["name"]
            line = c.get("line")
            if path not in cache:
                full = root / path
                cache[path] = full.read_text(encoding="utf-8") if full.exists() else ""
            source = cache[path]
            if not source:
                errors.append(f"[{mid}] {symbol}: 文件不存在 {path}")
                continue
            if line is None:
                continue  # 无行号锚点的概念条目：仅要求文件存在
            defs = definition_lines(source, symbol)
            if not defs:
                errors.append(f"[{mid}] {symbol}: {path} 中找不到定义（拼写错误或已删除？）")
                continue
            actual = min(defs, key=lambda d: abs(d - line))
            if abs(actual - line) > MAX_DRIFT:
                errors.append(
                    f"[{mid}] {symbol}: 标注 {path}:{line}，实际定义在 :{actual}（漂移 > {MAX_DRIFT} 行）")

    for st in data.get("states", []):
        for ref in st.get("writers", []) + st.get("readers", []):
            check_ref(f"state {st['name']}", ref)
    for task in data.get("tasks", []):
        for step in task.get("steps", []):
            for ref in step.get("refs", []):
                check_ref(f"task {task['id']}", ref)
    for concept in data.get("concepts", []):
        for w in concept.get("where", []):
            if w.get("ref"):
                check_ref(f"concept {concept['id']}", w["ref"])

    print(f"校验 project_atlas.html：{len(data['modules'])} 个模块，"
          f"{len(data.get('states', []))} 个状态字段，{len(data.get('tasks', []))} 个任务，"
          f"{len(data.get('concepts', []))} 条概念，"
          f"{stats['files']} 个文件引用，{stats['symbols']} 个接口符号")
    if errors:
        print(f"FAIL：{len(errors)} 处不一致")
        for e in errors:
            print("  -", e)
        return 1
    print("PASS：文件路径 / 符号 / 行号 / 模块引用全部与源码一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
