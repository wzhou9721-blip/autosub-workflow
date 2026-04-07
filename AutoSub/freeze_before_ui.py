import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PROJECT_ROOT / "logic_freeze_manifest.json"
SMOKE_CHECKLIST = PROJECT_ROOT / "smoke_checklist.txt"

KEY_FILES = [
    "app/common/thread.py",
    "app/core/llm.py",
    "app/core/transcriber.py",
    "app/view/setting_task_interface.py",
    "app/view/transcribe_interface.py",
    "app/view/translation_interface.py",
    "app/view/setting_interface.py",
    "app/common/config.py",
    "requirements.txt",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build_file_snapshot() -> list:
    snapshot = []
    for rel in KEY_FILES:
        p = PROJECT_ROOT / rel
        if p.exists() and p.is_file():
            snapshot.append({
                "file": rel,
                "sha256": sha256_file(p),
                "size": p.stat().st_size,
                "mtime": int(p.stat().st_mtime),
            })
        else:
            snapshot.append({
                "file": rel,
                "missing": True,
            })
    return snapshot


def run_regression() -> tuple[bool, str]:
    cmd = [sys.executable, str(PROJECT_ROOT / "run_regression.py")]
    try:
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return proc.returncode == 0, output.strip()
    except Exception as e:
        return False, f"运行回归失败: {e}"


def main() -> int:
    ok_regression, regression_output = run_regression()
    smoke_exists = SMOKE_CHECKLIST.exists()

    manifest = {
        "generated_at": int(time.time()),
        "python": sys.version,
        "smoke_checklist_exists": smoke_exists,
        "regression_passed": ok_regression,
        "files": build_file_snapshot(),
    }

    with open(MANIFEST_PATH, "w", encoding="utf-8") as wf:
        json.dump(manifest, wf, ensure_ascii=False, indent=2)

    print(f"[Freeze] 已生成冻结清单: {MANIFEST_PATH}")
    print(f"[Freeze] 冒烟清单存在: {smoke_exists}")
    print(f"[Freeze] 回归结果: {'PASS' if ok_regression else 'FAIL'}")
    if regression_output:
        print("[Freeze] 回归输出:")
        print(regression_output)

    if not smoke_exists:
        print("[Freeze] 缺少 smoke_checklist.txt")
        return 2
    if not ok_regression:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
