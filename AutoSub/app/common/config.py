from qfluentwidgets import QConfig, ConfigItem, OptionsConfigItem, OptionsValidator, Theme, qconfig
from pathlib import Path
import os
import sys

def get_roots():
    """Get application and resource roots."""
    if getattr(sys, 'frozen', False):
        # PyInstaller: 
        # app_root: where the exe is (for external models/configs)
        # res_root: where the bundled files are (_internal)
        app_root = Path(sys.executable).parent
        
        if hasattr(sys, '_MEIPASS'):
            res_root = Path(sys._MEIPASS)
        else:
            # Onedir mode: check for _internal
            internal = app_root / '_internal'
            if internal.exists():
                res_root = internal
            else:
                res_root = app_root
                
        return app_root, res_root
    else:
        # Dev: project root
        root = Path(__file__).parent.parent.parent
        return root, root

# 定义常量
APP_ROOT, RES_ROOT = get_roots()
CONFIG_FILE = APP_ROOT / "config.json"

# Models should be external (editable/downloadable by user)
MODEL_PATH = APP_ROOT / "models"
# Binaries are bundled inside _internal in frozen mode, but downloaded ones go to app root
INTERNAL_BIN_PATH = RES_ROOT / "bin"
BIN_PATH = APP_ROOT / "bin"

class Config(QConfig):
    """ 全局配置 """
    
    # 任务配置
    # 转录方式 (本地/云端)
    transcribeMode = OptionsConfigItem(
        "Task",
        "TranscribeMode",
        "本地",
        OptionsValidator(["本地", "云端"]),
        restart=False
    )

    # 本地模型大小
    asrModel = OptionsConfigItem(
        "Task", 
        "ASRModel", 
        "base", 
        OptionsValidator(["tiny", "base", "small", "medium", "large-v2", "large-v3", "large-v3-turbo"]), 
        restart=False
    )
    
    # 视频语境
    videoContext = ConfigItem(
        "Task",
        "VideoContext",
        "",
        restart=True # Force reset on restart if needed, but QConfig logic persists. We handle clearing manually.
    )

    # 术语表路径
    glossaryPath = ConfigItem(
        "Task",
        "GlossaryPath",
        "",
        restart=False
    )
    
    # 常用语言列表 (按使用频率排序)
    LANGUAGE_LIST = [
        "Auto", 
        "Chinese", "English", "Japanese", "Korean", 
        "French", "German", "Spanish", "Russian", 
        "Portuguese", "Italian", "Thai", "Vietnamese", 
        "Indonesian", "Arabic", "Hindi", "Catalan"
    ]

    sourceLanguage = OptionsConfigItem(
        "Task", 
        "SourceLanguage", 
        "Auto", 
        OptionsValidator(LANGUAGE_LIST), 
        restart=False
    )
    
    targetLanguage = OptionsConfigItem(
        "Task", 
        "TargetLanguage", 
        "Chinese", 
        OptionsValidator([lang for lang in LANGUAGE_LIST if lang != "Auto"]), 
        restart=False
    )

    # 自动流程开关
    workflow_optimize = ConfigItem(
        "Workflow",
        "Optimize",
        True,
        restart=False
    )
    workflow_split = ConfigItem(
        "Workflow",
        "Split",
        True,
        restart=False
    )
    workflow_translate = ConfigItem(
        "Workflow",
        "Translate",
        True,
        restart=False
    )
    workflow_overflow_fix = ConfigItem(
        "Workflow",
        "OverflowFix",
        True,
        restart=False
    )
    # --- API Configuration ---

    # Cloud Transcription API
    asr_provider = OptionsConfigItem(
        "ASR", "Provider", "Whisper",
        OptionsValidator(["Whisper", "Gladia"]),
        restart=False
    )
    cloud_asr_fallback_enabled = ConfigItem("ASR", "FallbackEnabled", True, restart=False)
    cloud_asr_fallback_provider = OptionsConfigItem(
        "ASR", "FallbackProvider", "Gladia",
        OptionsValidator(["Whisper", "Gladia"]),
        restart=False
    )
    asr_api_key = ConfigItem("ASR", "ApiKey", "", restart=False)
    asr_base_url = ConfigItem("ASR", "BaseUrl", "", restart=False)
    asr_model = ConfigItem("ASR", "Model", "", restart=False)
    gladia_api_key = ConfigItem("ASR", "GladiaKey", "", restart=False)

    # Gladia 转录调参
    gladia_vocabulary_intensity = ConfigItem("Gladia", "VocabularyIntensity", 0.6, restart=False)
    gladia_local_denoise = ConfigItem("Gladia", "LocalDenoise", False, restart=False)

    # Gladia 说话人分离
    gladia_diarization = ConfigItem("Gladia", "Diarization", False, restart=False)
    gladia_max_speakers = ConfigItem("Gladia", "MaxSpeakers", 4, restart=False)

    # Translation API
    llm_api_key = ConfigItem("LLM", "ApiKey", "", restart=False)
    llm_base_url = ConfigItem("LLM", "BaseUrl", "", restart=False)
    llm_model = ConfigItem("LLM", "Model", "", restart=False)
    llm_translation_mode = OptionsConfigItem(
        "LLM",
        "Mode",
        "折中",
        OptionsValidator(["稳健", "折中", "极速", "自定义"]),
        restart=False
    )
    llm_batch_size = OptionsConfigItem(
        "LLM",
        "BatchSize",
        10,
        OptionsValidator([5, 10, 15, 20, 25, 30]),
        restart=False
    )
    llm_max_workers = ConfigItem("LLM", "MaxWorkers", 3, restart=False)
    llm_reflect = ConfigItem("LLM", "Reflect", False, restart=False)
    translation_hallucination_filter = ConfigItem("LLM", "HallucinationFilter", True, restart=False)

    # Optimization API
    optimize_api_key = ConfigItem("Optimize", "ApiKey", "", restart=False)
    optimize_base_url = ConfigItem("Optimize", "BaseUrl", "", restart=False)
    optimize_model = ConfigItem("Optimize", "Model", "", restart=False)

    # Context Enhancement API
    context_api_key = ConfigItem("Context", "ApiKey", "", restart=False)
    context_base_url = ConfigItem("Context", "BaseUrl", "", restart=False)
    context_model = ConfigItem("Context", "Model", "", restart=False)

    # Search API (Tavily/Serper for knowledge enhancement)
    search_provider = OptionsConfigItem(
        "Search", 
        "Provider", 
        "Tavily", 
        OptionsValidator(["Tavily", "Serper"]), 
        restart=False
    )
    tavily_api_key = ConfigItem("Search", "TavilyKey", "", restart=False)
    serper_api_key = ConfigItem("Search", "SerperKey", "", restart=False)
    enable_web_search = ConfigItem("Search", "Enable", False, restart=False)

    # Overflow Repair API
    fix_api_key = ConfigItem("Fix", "ApiKey", "", restart=False)
    fix_base_url = ConfigItem("Fix", "BaseUrl", "", restart=False)
    fix_model = ConfigItem("Fix", "Model", "", restart=False)
    fix_reflect_rounds = ConfigItem("Fix", "ReflectRounds", 1, restart=False)

    # Faster Whisper Configuration
    fasterWhisperDevice = OptionsConfigItem(
        "FasterWhisper",
        "Device",
        "CPU",
        OptionsValidator(["CPU", "GPU"]),
        restart=False
    )

    # --- Advanced Transcription Parameters ---
    beam_size = ConfigItem("Transcription", "BeamSize", 5, restart=False)
    best_of = ConfigItem("Transcription", "BestOf", 5, restart=False)
    patience = ConfigItem("Transcription", "Patience", 1.0, restart=False)
    repetition_penalty = ConfigItem("Transcription", "RepetitionPenalty", 1.2, restart=False)
    no_speech_threshold = ConfigItem("Transcription", "NoSpeechThreshold", 0.7, restart=False)
    log_prob_threshold = ConfigItem("Transcription", "LogProbThreshold", -1.0, restart=False)
    compression_ratio_threshold = ConfigItem("Transcription", "CompressionRatioThreshold", 2.0, restart=False)
    vad_filter = ConfigItem("Transcription", "VadFilter", True, restart=False)
    vad_threshold = ConfigItem("Transcription", "VadThreshold", 0.3, restart=False)
    vad_min_silence_duration_ms = ConfigItem("Transcription", "VadMinSilenceDurationMs", 300, restart=False)
    vad_speech_pad_ms = ConfigItem("Transcription", "VadSpeechPadMs", 400, restart=False)
    vad_min_speech_duration_ms = ConfigItem("Transcription", "VadMinSpeechDurationMs", 100, restart=False)
    condition_on_previous_text = ConfigItem("Transcription", "ConditionOnPreviousText", False, restart=False)
    transcription_hallucination_filter = ConfigItem("Transcription", "HallucinationFilter", True, restart=False)

    # Sentence Splitting Configuration
    max_word_count_cjk = ConfigItem("Split", "MaxWordCountCJK", 18, restart=False)
    max_word_count_english = ConfigItem("Split", "MaxWordCountEnglish", 12, restart=False)
    long_pause_split_sec = ConfigItem("Split", "LongPauseSplitSec", 0.5, restart=False)

    # Enable word-level timestamps
    wordLevelTimestamps = ConfigItem(
        "Task",
        "WordLevelTimestamps",
        True,
        restart=False
    )

    # Multilingual transcription
    multilingualMode    = ConfigItem("Transcription", "MultilingualMode", False, restart=False)
    secondaryLanguage   = OptionsConfigItem(
        "Transcription", "SecondaryLanguage", "Auto",
        OptionsValidator(LANGUAGE_LIST),
        restart=False
    )
    gap_fill_enabled = ConfigItem(
        "Transcription", "GapFillEnabled", True, restart=False
    )
    multilingual_gap_threshold_sec = ConfigItem(
        "Transcription", "MultilingualGapThresholdSec", 10, restart=False
    )
    # 云端转录专用：空白区间补录的时长阈值（秒），与本地阈值独立
    cloud_multilingual_gap_threshold_sec = ConfigItem(
        "Transcription", "CloudMultilingualGapThresholdSec", 10, restart=False
    )

    # Remove Punctuation
    remove_punctuation = ConfigItem(
        "Text",
        "RemovePunctuation",
        False,
        restart=False
    )

    # Overflow Settings
    overflow_mode = OptionsConfigItem(
        "Overflow",
        "Mode",
        "PR模式",
        OptionsValidator(["PR模式", "剪映模式"]),
        restart=False
    )
    
    font_size = ConfigItem("Overflow", "FontSize", 60, restart=False)
    max_line_count = ConfigItem("Overflow", "MaxLineCount", 23, restart=False)

cfg = Config()
qconfig.load(str(CONFIG_FILE), cfg)
