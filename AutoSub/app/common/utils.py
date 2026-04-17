# -*- coding: utf-8 -*-
import os
import subprocess
import wave
import math
from array import array
from pathlib import Path
from app.common.config import BIN_PATH, INTERNAL_BIN_PATH, APP_ROOT

def get_ffmpeg_path():
    """ 获取 ffmpeg 路径 """
    # 1. 优先检查 BIN_PATH (用户目录)
    user_ffmpeg = BIN_PATH / "ffmpeg.exe"
    if user_ffmpeg.exists():
        return str(user_ffmpeg.absolute())

    # 2. 检查 INTERNAL_BIN_PATH (打包资源)
    bundled_ffmpeg = INTERNAL_BIN_PATH / "ffmpeg.exe"
    if bundled_ffmpeg.exists():
        return str(bundled_ffmpeg.absolute())

    # 3. 兼容旧路径：bin/Faster-Whisper-XXL/ffmpeg.exe（基于 APP_ROOT 避免依赖工作目录）
    old_ffmpeg = APP_ROOT / "bin" / "Faster-Whisper-XXL" / "ffmpeg.exe"
    if old_ffmpeg.exists():
        return str(old_ffmpeg.absolute())
    
    # 4. 备选：系统路径
    return "ffmpeg"

def extract_audio(video_path, audio_path=None):
    """
    从视频中提取音频 (WAV格式, 16kHz, 单声道)
    """
    input_path = Path(video_path)

    if not audio_path:
        if input_path.suffix.lower() == ".wav":
            try:
                with wave.open(str(input_path), "rb") as wf:
                    if (
                        wf.getframerate() == 16000
                        and wf.getnchannels() == 1
                        and wf.getsampwidth() == 2
                    ):
                        print(f"[Utils] 复用现成 WAV 音频: {input_path}")
                        return str(input_path)
            except Exception:
                pass

            audio_path = str(input_path.with_name(f"{input_path.stem}_extract.wav"))
        else:
            audio_path = str(input_path.with_suffix(".wav"))

    if str(Path(audio_path).resolve()) == str(input_path.resolve()):
        audio_path = str(input_path.with_name(f"{input_path.stem}_extract.wav"))
    
    ffmpeg_exe = get_ffmpeg_path()
    
    # 构造命令
    # -i: 输入
    # -ar 16000: 采样率 16k (Whisper 要求)
    # -ac 1: 单声道
    # -vn: 禁用视频
    # -y: 覆盖已存在文件
    cmd = [
        ffmpeg_exe,
        "-i", video_path,
        "-ar", "16000",
        "-ac", "1",
        "-vn",
        "-y",
        audio_path
    ]
    
    print(f"[Utils] 正在提取音频: {' '.join(cmd)}")
    
    # 隐藏控制台窗口 (Windows)
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    
    try:
        process = subprocess.run(
            cmd, 
            startupinfo=startupinfo, 
            capture_output=True, 
            text=True, 
            encoding='utf-8',
            errors='ignore',
            timeout=300
        )
        
        if process.returncode != 0:
            print(f"[Utils] FFmpeg 错误: {process.stderr}")
            return None
            
        print(f"[Utils] 音频提取成功: {audio_path}")
        return audio_path
        
    except Exception as e:
        print(f"[Utils] 提取音频失败: {e}")
        return None

def analyze_audio_quality(audio_path):
    try:
        with wave.open(audio_path, "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            framerate = wf.getframerate()
            if sample_width != 2:
                return None

            max_amp = float(2 ** (8 * sample_width - 1))
            silence_threshold = max_amp * 0.02
            window_size = max(1, int(framerate * 0.5))

            # 一次性读取全部帧并用 array 批量转换
            all_frames = wf.readframes(wf.getnframes())
            if not all_frames:
                return None
            data = array("h")
            data.frombytes(all_frames)
            if n_channels > 1:
                data = data[::n_channels]

            total_samples = len(data)
            if total_samples == 0:
                return None

            # 使用批量操作代替逐样本 Python 循环
            try:
                import numpy as np
                arr = np.frombuffer(data, dtype=np.int16).astype(np.float64)
                abs_arr = np.abs(arr)
                peak = float(np.max(abs_arr))
                sum_sq = float(np.sum(arr * arr))
                clipped_samples = int(np.sum(abs_arr >= max_amp * 0.98))
                silent_samples = int(np.sum(abs_arr < silence_threshold))
                # 窗口 RMS
                n_full_windows = total_samples // window_size
                window_rms_list = []
                if n_full_windows > 0:
                    truncated = arr[:n_full_windows * window_size].reshape(n_full_windows, window_size)
                    window_rms_list = np.sqrt(np.mean(truncated ** 2, axis=1)).tolist()
                remainder = total_samples % window_size
                if remainder > 0:
                    tail = arr[n_full_windows * window_size:]
                    window_rms_list.append(float(np.sqrt(np.mean(tail ** 2))))
            except ImportError:
                # numpy 不可用时回退到纯 Python（但用 array 批量操作尽量减少开销）
                peak = 0.0
                sum_sq = 0.0
                clipped_samples = 0
                silent_samples = 0
                window_sum_sq = 0.0
                window_samples = 0
                window_rms_list = []
                for s in data:
                    abs_s = abs(s)
                    if abs_s > peak:
                        peak = abs_s
                    sum_sq += s * s
                    if abs_s >= max_amp * 0.98:
                        clipped_samples += 1
                    if abs_s < silence_threshold:
                        silent_samples += 1
                    window_sum_sq += s * s
                    window_samples += 1
                    if window_samples >= window_size:
                        rms = math.sqrt(window_sum_sq / window_samples) if window_samples else 0.0
                        window_rms_list.append(rms)
                        window_sum_sq = 0.0
                        window_samples = 0
                if window_samples > 0:
                    rms = math.sqrt(window_sum_sq / window_samples) if window_samples else 0.0
                    window_rms_list.append(rms)

        rms = math.sqrt(sum_sq / total_samples) if total_samples else 0.0
        rms_db = 20 * math.log10(rms / max_amp) if rms > 0 else -120.0
        peak_db = 20 * math.log10(peak / max_amp) if peak > 0 else -120.0
        silence_ratio = silent_samples / total_samples if total_samples else 1.0
        clipping_ratio = clipped_samples / total_samples if total_samples else 0.0

        snr_db = None
        if window_rms_list:
            window_rms_list.sort()
            idx = max(0, int(len(window_rms_list) * 0.1))
            noise_rms = window_rms_list[idx]
            if noise_rms > 0 and rms > 0:
                snr_db = 20 * math.log10(rms / noise_rms)

        return {
            "rms_db": rms_db,
            "peak_db": peak_db,
            "silence_ratio": silence_ratio,
            "snr_db": snr_db,
            "clipping_ratio": clipping_ratio
        }
    except Exception as e:
        print(f"[Utils] 音频分析失败: {e}")
        return None

def auto_tune_transcription_params(stats):
    if not stats:
        return {
            "beam_size": 3,
            "best_of": 1,
            "patience": 1.0,
            "repetition_penalty": 1.2,
            "no_speech_threshold": 0.6,
            "log_prob_threshold": -1.0,
            "compression_ratio_threshold": 2.4,
            "vad_filter": True,
            "vad_threshold": 0.4,
            "vad_min_silence_duration_ms": 500,
            "condition_on_previous_text": False,
            "gain_db": 0.0,
            "denoise": False
        }

    rms_db = stats.get("rms_db", -120.0)
    peak_db = stats.get("peak_db", -120.0)
    silence_ratio = stats.get("silence_ratio", 1.0)
    snr_db = stats.get("snr_db", None)
    clipping_ratio = stats.get("clipping_ratio", 0.0)

    low_volume = rms_db < -30
    very_low_volume = rms_db < -35
    long_silence = silence_ratio > 0.6
    noisy = snr_db is not None and snr_db < 15

    over_clipped = clipping_ratio > 0.003 or peak_db > -1.0
    beam_size = 4 if low_volume or noisy or over_clipped else 3
    # best_of 串行跑 N 遍取最优，每+1 就多一倍耗时；GPU 场景上限设为 2
    best_of = 2 if low_volume or noisy else 1
    patience = 1.2 if low_volume or noisy else 1.0
    # 噪声环境下提高重复惩罚，抑制"重复幻觉"（如"订阅订阅订阅"等循环捏造）
    repetition_penalty = 1.35 if noisy else 1.2
    # 这两个阈值直接影响幻觉率，不随音频质量调整
    # 降低它们会让 Whisper 在静音/噪声段强行生成内容，反而更差
    no_speech_threshold = 0.6
    log_prob_threshold = -1.0
    compression_ratio_threshold = 3.5 if noisy else 2.4
    # VAD 始终开启：静音片段也要过滤，关闭反而更慢
    vad_filter = True
    vad_threshold = 0.2 if low_volume or very_low_volume else 0.4
    vad_min_silence_duration_ms = 200 if long_silence or low_volume else 500
    condition_on_previous_text = False

    target_rms_db = -20.0
    max_gain_db = 12.0
    min_gain_db = -12.0
    gain_db = (target_rms_db - rms_db) if rms_db > -120 else 0.0
    gain_db = max(min_gain_db, min(max_gain_db, gain_db))
    if peak_db > -120:
        gain_db = min(gain_db, -3.0 - peak_db)
    if clipping_ratio > 0.01:
        gain_db = min(gain_db, -6.0)
    if abs(gain_db) < 0.5:
        gain_db = 0.0

    denoise = noisy

    return {
        "beam_size": beam_size,
        "best_of": best_of,
        "patience": patience,
        "repetition_penalty": repetition_penalty,
        "no_speech_threshold": no_speech_threshold,
        "log_prob_threshold": log_prob_threshold,
        "compression_ratio_threshold": compression_ratio_threshold,
        "vad_filter": vad_filter,
        "vad_threshold": vad_threshold,
        "vad_min_silence_duration_ms": vad_min_silence_duration_ms,
        "condition_on_previous_text": condition_on_previous_text,
        "gain_db": gain_db,
        "denoise": denoise,
        # 词级时间戳启用时传递给 faster-whisper，静音超过 2 秒时增强幻觉检测
        "hallucination_silence_threshold": 2.0
    }

def apply_audio_preprocess(audio_path, gain_db=0.0, denoise=False):
    if gain_db == 0.0 and not denoise:
        return audio_path

    ffmpeg_exe = get_ffmpeg_path()
    audio_path_obj = Path(audio_path)
    output_path = str(audio_path_obj.with_suffix("").as_posix() + "_proc.wav")

    filters = []
    if denoise:
        filters.append("afftdn")
    if gain_db != 0.0:
        filters.append(f"volume={gain_db:.2f}dB")
    filter_str = ",".join(filters)

    cmd = [
        ffmpeg_exe,
        "-i", audio_path,
        "-af", filter_str,
        "-ar", "16000",
        "-ac", "1",
        "-y",
        output_path
    ]

    print(f"[Utils] 音频预处理: {' '.join(cmd)}")

    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    try:
        process = subprocess.run(
            cmd,
            startupinfo=startupinfo,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=300
        )
        if process.returncode != 0:
            print(f"[Utils] 预处理失败: {process.stderr}")
            return audio_path
        print(f"[Utils] 预处理完成: {output_path}")
        return output_path
    except Exception as e:
        print(f"[Utils] 预处理失败: {e}")
        return audio_path

def fix_timestamp_overlaps(segments):
    """
    修复时间戳重叠：确保上一句结束时间不晚于下一句开始时间
    并对时间戳进行格式化（保留3位小数）
    """
    if not segments:
        return segments
        
    # 按开始时间排序
    segments.sort(key=lambda x: x.get("start", 0))
    
    # 第一次遍历：解决重叠
    for i in range(1, len(segments)):
        prev = segments[i-1]
        curr = segments[i]
        
        curr_start = curr.get("start", 0)
        
        # 强制修正上一句的结束时间
        if prev.get("end", 0) > curr_start:
            prev["end"] = curr_start
            
            # 防止负时长
            if prev["end"] < prev.get("start", 0):
                prev["end"] = prev.get("start", 0)
    
    # 第二次遍历：格式化精度，避免浮点数误差
    for s in segments:
        s["start"] = round(s.get("start", 0), 3)
        s["end"] = round(s.get("end", 0), 3)
        
    return segments
