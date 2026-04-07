# -*- coding: utf-8 -*-
import os
import sys
import site
from pathlib import Path

# Import BIN_PATH to check for user-downloaded DLLs
# We need to be careful about circular imports if config imports this.
# But config doesn't import cuda_setup.
try:
    from app.common.config import BIN_PATH
except ImportError:
    BIN_PATH = None

def setup_cuda_paths():
    """
    针对 Windows 平台，将 nvidia-cublas-cu12 和 nvidia-cudnn-cu12 的 DLL 目录添加到搜索路径。
    这解决了 faster-whisper 无法找到 cublas64_12.dll 等问题。
    """
    if os.name != 'nt':
        return

    # 0. 优先检查 BIN_PATH (用户下载的 GPU 包)
    # 假设用户下载并解压到了 BIN_PATH/Faster-Whisper-XXL 或 BIN_PATH/nvidia
    if BIN_PATH:
        possible_dirs = [
            BIN_PATH / "nvidia",
            BIN_PATH / "Faster-Whisper-XXL",
            BIN_PATH / "Faster-Whisper-XXL" / "nvidia",
            BIN_PATH / "libs"
        ]
        
        for p_dir in possible_dirs:
            if p_dir.exists():
                # 递归查找 bin 目录
                for root, dirs, files in os.walk(str(p_dir)):
                    if "bin" in dirs:
                        bin_path = Path(root) / "bin"
                        try:
                            if hasattr(os, 'add_dll_directory'):
                                os.add_dll_directory(str(bin_path))
                            if str(bin_path) not in os.environ['PATH']:
                                os.environ['PATH'] = str(bin_path) + os.pathsep + os.environ['PATH']
                            print(f"[CUDA] Found and added user DLL path: {bin_path}")
                        except Exception as e:
                            print(f"[CUDA] Error adding user DLL path {bin_path}: {e}")
                    
                    # Also add the root itself if it contains DLLs (heuristic)
                    # Simple check: if any .dll file is in this dir
                    has_dll = any(f.lower().endswith('.dll') for f in files)
                    if has_dll:
                         try:
                            if hasattr(os, 'add_dll_directory'):
                                os.add_dll_directory(root)
                            if root not in os.environ['PATH']:
                                os.environ['PATH'] = root + os.pathsep + os.environ['PATH']
                            print(f"[CUDA] Found and added user DLL path (root): {root}")
                         except Exception as e:
                             print(f"[CUDA] Error adding user DLL path {root}: {e}")


    # Check if frozen (PyInstaller)
    if getattr(sys, 'frozen', False):
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        # In frozen app, DLLs are likely in base_path or a subdirectory
        # Try adding base_path to DLL search
        try:
             if hasattr(os, 'add_dll_directory'):
                 os.add_dll_directory(base_path)
             if base_path not in os.environ['PATH']:
                 os.environ['PATH'] = base_path + os.pathsep + os.environ['PATH']
             print(f"[CUDA] Frozen mode: Added {base_path} to DLL search path")
             
             # Also check for nvidia subdir if preserved
             nvidia_path = Path(base_path) / "nvidia"
             if nvidia_path.exists():
                 for sub_dir in nvidia_path.iterdir():
                    bin_path = sub_dir / "bin"
                    if bin_path.exists():
                        os.add_dll_directory(str(bin_path))
                        
             return True
        except Exception as e:
            print(f"[CUDA] Failed to setup frozen paths: {e}")
            return False

    # 获取所有 site-packages 目录
    site_dirs = site.getsitepackages()
    if hasattr(site, 'getusersitepackages'):
        site_dirs.append(site.getusersitepackages())

    # 常见目录结构：site-packages/nvidia/cublas/bin
    # 我们需要查找并添加这些 bin 目录
    added_count = 0
    for s_dir in site_dirs:
        nvidia_path = Path(s_dir) / "nvidia"
        if nvidia_path.exists():
            # 遍历 nvidia 下的所有子目录 (cublas, cudnn 等)
            for sub_dir in nvidia_path.iterdir():
                bin_path = sub_dir / "bin"
                if bin_path.exists():
                    bin_str = str(bin_path.absolute())
                    try:
                        # 1. 使用 os.add_dll_directory (Python 3.8+)
                        if hasattr(os, 'add_dll_directory'):
                            os.add_dll_directory(bin_str)
                        
                        # 2. 同时添加到 PATH 环境变量 (某些库可能需要)
                        if bin_str not in os.environ['PATH']:
                            os.environ['PATH'] = bin_str + os.pathsep + os.environ['PATH']
                            
                        print(f"[CUDA] 已成功关联 DLL 目录: {bin_str}")
                        added_count += 1
                    except Exception as e:
                        print(f"[CUDA] 关联 DLL 目录失败 {bin_str}: {e}")

    # 另外一种可能：直接在 site-packages 下
    # 比如某些版本可能结构不同，这里做一个补丁
    if added_count == 0:
        print("[CUDA] 警告: 未在 site-packages/nvidia 下找到 bin 目录，尝试搜索系统 PATH...")
        
    return added_count > 0

if __name__ == "__main__":
    setup_cuda_paths()
