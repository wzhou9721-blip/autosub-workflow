import os
import shutil
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QFileDialog
from qfluentwidgets import (
    SmoothScrollArea,
    SettingCardGroup,
    TitleLabel,
    FluentIcon as FIF,
    SwitchSettingCard,
    ComboBoxSettingCard,
    PushSettingCard,
    InfoBar,
    InfoBarPosition
)
from app.common.config import cfg, CONFIG_FILE
from app.components.collapsible_setting_section import CollapsibleSettingSection
from app.components.setting_cards import LineEditSettingCard, NumberSettingCard, PasswordSettingCard, DoubleNumberSettingCard
from app.components.faster_whisper_manager import FasterWhisperManager
from app.core.llm import llm_manager
from app.core.search import search_manager

class SettingInterface(SmoothScrollArea):
    """ 全局设置界面 """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SettingInterface")
        
        self.scrollWidget = QWidget()
        self.scrollWidget.setObjectName("scrollWidget")
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        
        self.setStyleSheet("SettingInterface, #scrollWidget { background-color: transparent; border: none; }")
        
        self.vBoxLayout = QVBoxLayout(self.scrollWidget)
        self.vBoxLayout.setContentsMargins(36, 20, 36, 36)
        self.vBoxLayout.setSpacing(20)
        
        # 标题
        self.titleLabel = TitleLabel("全局设置", self.scrollWidget)
        self.vBoxLayout.addWidget(self.titleLabel)
        
        # 初始化设置组
        self.initGroups()
        
        self.vBoxLayout.addStretch(1)

    def _addCollapsibleGroup(self, title: str, group: QWidget, expanded: bool = True):
        if isinstance(group, SettingCardGroup):
            self._stripGroupHeader(group)
        section = CollapsibleSettingSection(title, group, self.scrollWidget, expanded=expanded)
        self.vBoxLayout.addWidget(section)
        return section

    @staticmethod
    def _stripGroupHeader(group: SettingCardGroup):
        title_label = getattr(group, "titleLabel", None)
        layout = getattr(group, "vBoxLayout", None)
        if not title_label or not layout:
            return

        title_label.hide()
        if layout.count() >= 1:
            item = layout.takeAt(0)
            if item and item.widget():
                item.widget().setParent(None)

        if layout.count() >= 1:
            item = layout.itemAt(0)
            if item and item.spacerItem():
                layout.takeAt(0)

    def initGroups(self):
        # 1. 云端转录设置
        self.asrGroup = SettingCardGroup("", self.scrollWidget)

        # 提供商切换（Whisper / Gladia）
        self.asrProviderCard = ComboBoxSettingCard(
            cfg.asr_provider,
            FIF.CLOUD,
            "转录提供商",
            "Whisper = Groq 等兼容 OpenAI 格式的 Whisper API；Gladia = Gladia 专属 REST API（更适合多语种足球解说）",
            ["Whisper", "Gladia"],
            self.asrGroup
        )
        self.asrGroup.addSettingCard(self.asrProviderCard)

        self.cloudTranscriptionHallucinationFilterCard = SwitchSettingCard(
            FIF.ROBOT,
            "云端转录幻觉清理",
            "开启后自动清理云端转录中的疑似幻觉与重复片段（Whisper/Gladia 均生效）",
            configItem=cfg.transcription_hallucination_filter,
            parent=self.asrGroup
        )
        self.asrGroup.addSettingCard(self.cloudTranscriptionHallucinationFilterCard)

        # ── Whisper 专属字段 ──────────────────────────────────────────
        self.asrKeyCard = PasswordSettingCard(
            cfg.asr_api_key,
            FIF.VPN,
            "Whisper API Key",
            "请输入 Whisper 兼容 API 的密钥（Groq 等）",
            self.asrGroup
        )
        self.asrUrlCard = LineEditSettingCard(
            cfg.asr_base_url,
            FIF.LINK,
            "Whisper Base URL",
            "请输入 Whisper 兼容 API 地址（如 https://api.groq.com/openai/v1）",
            self.asrGroup
        )
        self.asrModelCard = LineEditSettingCard(
            cfg.asr_model,
            FIF.SPEED_HIGH,
            "Whisper Model Name",
            "请输入模型名称，如 whisper-large-v3-turbo",
            self.asrGroup
        )

        self.asrGroup.addSettingCard(self.asrKeyCard)
        self.asrGroup.addSettingCard(self.asrUrlCard)
        self.asrGroup.addSettingCard(self.asrModelCard)

        self.asrTestCard = PushSettingCard(
            "测试连接",
            FIF.SEND,
            "测试 Whisper API 配置",
            "测试当前 Whisper API 是否能正常连接并测量延迟",
            self.asrGroup
        )
        self.asrTestCard.clicked.connect(lambda: self._onTestConnection("asr"))
        self.asrGroup.addSettingCard(self.asrTestCard)

        # ── Gladia 专属字段 ───────────────────────────────────────────
        self.gladiaKeyCard = PasswordSettingCard(
            cfg.gladia_api_key,
            FIF.VPN,
            "Gladia API Key",
            "请输入 Gladia API 密钥（https://app.gladia.io）",
            self.asrGroup
        )
        self.asrGroup.addSettingCard(self.gladiaKeyCard)

        self.gladiaTestCard = PushSettingCard(
            "测试连接",
            FIF.SEND,
            "测试 Gladia API 配置",
            "向 Gladia 发送一个极短的测试请求以验证 Key 是否有效",
            self.asrGroup
        )
        self.gladiaTestCard.clicked.connect(self._onTestGladia)
        self.asrGroup.addSettingCard(self.gladiaTestCard)

        self.cloudAsrFallbackCard = SwitchSettingCard(
            FIF.SYNC,
            "云端 ASR 自动兜底",
            "主提供商失败时自动切换到备用提供商重试一次",
            configItem=cfg.cloud_asr_fallback_enabled,
            parent=self.asrGroup
        )
        self.cloudAsrFallbackProviderCard = ComboBoxSettingCard(
            cfg.cloud_asr_fallback_provider,
            FIF.SYNC,
            "备用转录提供商",
            "当主提供商失败时，自动切换至该提供商",
            ["Whisper", "Gladia"],
            self.asrGroup
        )
        self.asrGroup.addSettingCard(self.cloudAsrFallbackCard)
        self.asrGroup.addSettingCard(self.cloudAsrFallbackProviderCard)

        # ── Gladia 转录调参 ───────────────────────────────────────────
        self.gladiaVocabIntensityCard = DoubleNumberSettingCard(
            cfg.gladia_vocabulary_intensity,
            FIF.FONT,
            "术语表权重（Vocabulary Intensity）",
            "自定义词汇表的匹配强度，0.0~1.0，值越高越偏向匹配术语，建议 0.5~0.7",
            self.asrGroup,
            range=(0.0, 1.0),
            step=0.05
        )
        self.asrGroup.addSettingCard(self.gladiaVocabIntensityCard)

        # ── 本地预处理 ────────────────────────────────────────────────
        self.gladiaLocalDenoiseCard = SwitchSettingCard(
            FIF.MUTE,
            "本地降噪后再上传",
            "上传到 Gladia 前，先用 FFmpeg afftdn 对音频做一次本地降噪，适合噪声较重的素材。",
            configItem=cfg.gladia_local_denoise,
            parent=self.asrGroup
        )
        self.asrGroup.addSettingCard(self.gladiaLocalDenoiseCard)

        # ── 说话人分离 ────────────────────────────────────────────────
        self.gladiaDiarizationCard = SwitchSettingCard(
            FIF.PEOPLE,
            "说话人分离（Diarization）",
            "自动识别不同说话人，按人分别导出 SRT + 新闻稿 TXT（仅 Gladia 支持）",
            configItem=cfg.gladia_diarization,
            parent=self.asrGroup
        )
        self.asrGroup.addSettingCard(self.gladiaDiarizationCard)

        self.gladiaMaxSpeakersCard = NumberSettingCard(
            cfg.gladia_max_speakers,
            FIF.PEOPLE,
            "最大说话人数",
            "预期音频中最多有几个说话人，设置准确可提升分离效果（建议：采访场景设 2~6）",
            self.asrGroup,
            range=(1, 20)
        )
        self.asrGroup.addSettingCard(self.gladiaMaxSpeakersCard)

        # 监听说话人分离开关，控制 max_speakers 显隐
        cfg.gladia_diarization.valueChanged.connect(self._updateGladiaDiarizationState)

        self._addCollapsibleGroup("云端转录设置", self.asrGroup)

        # 初始化显示状态，并监听切换事件
        self._onAsrProviderChanged(cfg.asr_provider.value)
        cfg.asr_provider.valueChanged.connect(self._onAsrProviderChanged)
        cfg.cloud_asr_fallback_enabled.valueChanged.connect(self._updateCloudAsrFallbackState)
        cfg.cloud_asr_fallback_provider.valueChanged.connect(
            lambda _: self._syncCloudAsrFallbackProviderHint(cfg.asr_provider.value)
        )
        self._updateCloudAsrFallbackState(cfg.cloud_asr_fallback_enabled.value)
        self._updateGladiaDiarizationState(cfg.gladia_diarization.value)

        # 2. 翻译设置
        self.llmGroup = SettingCardGroup("", self.scrollWidget)
        
        self.llmKeyCard = PasswordSettingCard(
            cfg.llm_api_key,
            FIF.VPN,
            "API Key",
            "请输入翻译 API 密钥",
            self.llmGroup
        )
        self.llmUrlCard = LineEditSettingCard(
            cfg.llm_base_url,
            FIF.LINK,
            "Base URL",
            "请输入翻译 API 地址",
            self.llmGroup
        )
        self.llmModelCard = LineEditSettingCard(
            cfg.llm_model,
            FIF.ROBOT,
            "Model Name",
            "请输入翻译模型名称 (豆包 API 请使用'推理接入点 ID')",
            self.llmGroup
        )
        self.llmModeCard = ComboBoxSettingCard(
            cfg.llm_translation_mode,
            FIF.SPEED_HIGH,
            "翻译模式",
            "稳健=顺序强上下文，折中=小并行局部上下文，极速=全并行无上下文，自定义=自行配置参数",
            texts=["稳健", "折中", "极速", "自定义"],
            parent=self.llmGroup
        )
        self.llmBatchSizeCard = ComboBoxSettingCard(
            cfg.llm_batch_size,
            FIF.ALBUM,
            "翻译批数",
            "每次请求时翻译多少条字幕（仅自定义模式生效）",
            texts=[str(i) for i in [5, 10, 15, 20]],
            parent=self.llmGroup
        )
        self.llmMaxWorkersCard = NumberSettingCard(
            cfg.llm_max_workers,
            FIF.SYNC,
            "翻译并发数",
            "同时进行的翻译请求数量（仅自定义模式生效）。建议 3-5，过高可能触发 Rate Limit",
            self.llmGroup,
            range=(1, 10)
        )
        
        self.llmGroup.addSettingCard(self.llmKeyCard)
        self.llmGroup.addSettingCard(self.llmUrlCard)
        self.llmGroup.addSettingCard(self.llmModelCard)
        self.llmGroup.addSettingCard(self.llmModeCard)
        self.llmGroup.addSettingCard(self.llmBatchSizeCard)
        self.llmGroup.addSettingCard(self.llmMaxWorkersCard)

        self.llmReflectCard = SwitchSettingCard(
            FIF.SYNC,
            "反思翻译",
            "开启后每批字幕翻译完成后，LLM 将自动审查初稿并输出改进版本（质量更高，耗时约翻倍）",
            configItem=cfg.llm_reflect,
            parent=self.llmGroup
        )
        self.llmGroup.addSettingCard(self.llmReflectCard)

        self.translationHallucinationFilterCard = SwitchSettingCard(
            FIF.ROBOT,
            "翻译幻觉清理",
            "开启后自动清理译文中的疑似幻觉与重复片段，推荐开启",
            configItem=cfg.translation_hallucination_filter,
            parent=self.llmGroup
        )
        self.llmGroup.addSettingCard(self.translationHallucinationFilterCard)

        # 初始化参数卡片可见性，并监听模式变化
        self._onTranslationModeChanged(cfg.llm_translation_mode.value)
        cfg.llm_translation_mode.valueChanged.connect(self._onTranslationModeChanged)
        
        self.llmTestCard = PushSettingCard(
            "测试连接",
            FIF.SEND,
            "测试翻译 API 配置",
            "测试当前 API 是否能正常连接并测量延迟",
            self.llmGroup
        )
        self.llmTestCard.clicked.connect(lambda: self._onTestConnection("llm"))
        self.llmGroup.addSettingCard(self.llmTestCard)
        
        self._addCollapsibleGroup("翻译设置", self.llmGroup)

        # 3. 优化+断句设置
        self.optimizeGroup = SettingCardGroup("", self.scrollWidget)
        
        self.optimizeKeyCard = PasswordSettingCard(
            cfg.optimize_api_key,
            FIF.VPN,
            "API Key",
            "请输入优化 API 密钥",
            self.optimizeGroup
        )
        self.optimizeUrlCard = LineEditSettingCard(
            cfg.optimize_base_url,
            FIF.LINK,
            "Base URL",
            "请输入优化 API 地址",
            self.optimizeGroup
        )
        self.optimizeModelCard = LineEditSettingCard(
            cfg.optimize_model,
            FIF.EDUCATION,
            "Model Name",
            "请输入优化模型名称 (豆包 API 请使用'推理接入点 ID')",
            self.optimizeGroup
        )
        
        self.optimizeGroup.addSettingCard(self.optimizeKeyCard)
        self.optimizeGroup.addSettingCard(self.optimizeUrlCard)
        self.optimizeGroup.addSettingCard(self.optimizeModelCard)
        
        self.optimizeTestCard = PushSettingCard(
            "测试连接",
            FIF.SEND,
            "测试优化 API 配置",
            "测试当前 API 是否能正常连接并测量延迟",
            self.optimizeGroup
        )
        self.optimizeTestCard.clicked.connect(lambda: self._onTestConnection("optimize"))
        self.optimizeGroup.addSettingCard(self.optimizeTestCard)

        self.maxCjkCard = NumberSettingCard(
            cfg.max_word_count_cjk,
            FIF.ALBUM,
            "中日韩文最大字数",
            "设置中、日、韩等语言每行字幕的最大字数限制",
            self.optimizeGroup,
            range=(5, 50)
        )
        self.maxEnCard = NumberSettingCard(
            cfg.max_word_count_english,
            FIF.FONT,
            "英文/拉丁语最大单词数",
            "设置英语、法语等语言每行字幕的最大单词数限制",
            self.optimizeGroup,
            range=(3, 30)
        )
        self.longPauseSplitCard = DoubleNumberSettingCard(
            cfg.long_pause_split_sec,
            FIF.STOP_WATCH,
            "长气口强制断开阈值 (s)",
            "当原始转录相邻片段的间隔达到该秒数时，断句结果不会跨越该停顿继续合并。推荐 0.5 秒",
            self.optimizeGroup,
            range=(0.1, 3.0),
            step=0.1,
            decimals=2
        )
        self.optimizeGroup.addSettingCard(self.maxCjkCard)
        self.optimizeGroup.addSettingCard(self.maxEnCard)
        self.optimizeGroup.addSettingCard(self.longPauseSplitCard)
        
        self._addCollapsibleGroup("优化+断句设置", self.optimizeGroup)

        # 3.2 联网增强设置
        self.searchGroup = SettingCardGroup("", self.scrollWidget)
        
        self.enableSearchCard = SwitchSettingCard(
            FIF.GLOBE,
            "启用联网搜索",
            "优化字幕前自动搜索视频背景知识，提高专有名词和时事纠错准确率",
            configItem=cfg.enable_web_search,
            parent=self.searchGroup
        )
        
        self.searchProviderCard = ComboBoxSettingCard(
            cfg.search_provider,
            FIF.SEARCH,
            "搜索服务提供商",
            "选择要使用的搜索引擎 API (推荐 Tavily 用于 AI 搜索)",
            ["Tavily", "Serper"],
            self.searchGroup
        )

        self.tavilyKeyCard = PasswordSettingCard(
            cfg.tavily_api_key,
            FIF.VPN,
            "Tavily API Key",
            "请输入 Tavily API 密钥 (https://tavily.com)",
            self.searchGroup
        )

        self.serperKeyCard = PasswordSettingCard(
            cfg.serper_api_key,
            FIF.VPN,
            "Serper API Key",
            "请输入 Serper API 密钥 (https://serper.dev)",
            self.searchGroup
        )
        
        self.searchGroup.addSettingCard(self.enableSearchCard)
        self.searchGroup.addSettingCard(self.searchProviderCard)
        self.searchGroup.addSettingCard(self.tavilyKeyCard)
        self.searchGroup.addSettingCard(self.serperKeyCard)
        
        self.tavilyTestCard = PushSettingCard(
            "测试 Tavily",
            FIF.SEND,
            "测试 Tavily API",
            "测试 Tavily API 是否能正常连接并测量延迟",
            self.searchGroup
        )
        self.tavilyTestCard.clicked.connect(lambda: self._onTestSearchConnection("Tavily"))
        
        self.serperTestCard = PushSettingCard(
            "测试 Serper",
            FIF.SEND,
            "测试 Serper API",
            "测试 Serper API 是否能正常连接并测量延迟",
            self.searchGroup
        )
        self.serperTestCard.clicked.connect(lambda: self._onTestSearchConnection("Serper"))
        
        self.searchGroup.addSettingCard(self.tavilyTestCard)
        self.searchGroup.addSettingCard(self.serperTestCard)
        
        self._addCollapsibleGroup("联网知识增强", self.searchGroup)

        # 4. 溢出修复设置
        self.fixGroup = SettingCardGroup("", self.scrollWidget)
        
        self.fixKeyCard = PasswordSettingCard(
            cfg.fix_api_key,
            FIF.VPN,
            "API Key",
            "请输入溢出修复 API 密钥",
            self.fixGroup
        )
        self.fixUrlCard = LineEditSettingCard(
            cfg.fix_base_url,
            FIF.LINK,
            "Base URL",
            "请输入溢出修复 API 地址",
            self.fixGroup
        )
        self.fixModelCard = LineEditSettingCard(
            cfg.fix_model,
            FIF.BRUSH,
            "Model Name",
            "请输入溢出修复模型名称 (豆包 API 请使用'推理接入点 ID')",
            self.fixGroup
        )
        
        self.fixReflectRoundsCard = NumberSettingCard(
            cfg.fix_reflect_rounds,
            FIF.SYNC,
            "反思修复次数",
            "首轮结果不满足要求时，额外打回模型重修的次数；0 表示只做首轮修复",
            self.fixGroup,
            range=(0, 5)
        )

        self.fixGroup.addSettingCard(self.fixKeyCard)
        self.fixGroup.addSettingCard(self.fixUrlCard)
        self.fixGroup.addSettingCard(self.fixModelCard)
        self.fixGroup.addSettingCard(self.fixReflectRoundsCard)
        
        self.fixTestCard = PushSettingCard(
            "测试连接",
            FIF.SEND,
            "测试溢出修复 API 配置",
            "测试当前 API 是否能正常连接并测量延迟",
            self.fixGroup
        )
        self.fixTestCard.clicked.connect(lambda: self._onTestConnection("fix"))
        self.fixGroup.addSettingCard(self.fixTestCard)
        
        self._addCollapsibleGroup("溢出修复设置", self.fixGroup)

        # 4.5 通用设置 (单字级时间戳)
        self.generalGroup = SettingCardGroup("", self.scrollWidget)
        
        self.wordTimestampCard = SwitchSettingCard(
            FIF.TAG,
            "单字级时间戳",
            "开启后将返回单字级时间戳，用于初始优化与溢出修复 (支持本地/云端)",
            configItem=cfg.wordLevelTimestamps,
            parent=self.generalGroup
        )
        self.generalGroup.addSettingCard(self.wordTimestampCard)
        
        self.removePunctuationCard = SwitchSettingCard(
            FIF.EDIT,
            "去除标点",
            "自动去除译文中的标点符号 (支持中英文)",
            configItem=cfg.remove_punctuation,
            parent=self.generalGroup
        )
        self.generalGroup.addSettingCard(self.removePunctuationCard)
        
        self._addCollapsibleGroup("通用设置", self.generalGroup)

        # 4.7 转录高级设置
        self.advancedTranscriptionGroup = SettingCardGroup("", self.scrollWidget)

        self.beamSizeCard = NumberSettingCard(
            cfg.beam_size,
            FIF.TILES,
            "束搜索大小 (Beam Size)",
            "束搜索的宽度，值越大识别越准但速度越慢。推荐 5（Whisper 原始标准），CPU 较弱可设为 3",
            self.advancedTranscriptionGroup,
            range=(1, 10)
        )
        self.bestOfCard = NumberSettingCard(
            cfg.best_of,
            FIF.ALBUM,
            "最佳结果数 (Best Of)",
            "采样时从多少个候选中选最优。temperature=0 时此项无效，建议与 Beam Size 保持一致即可",
            self.advancedTranscriptionGroup,
            range=(1, 10)
        )
        self.patienceCard = DoubleNumberSettingCard(
            cfg.patience,
            FIF.STOP_WATCH,
            "搜索耐心度 (Patience)",
            "束搜索提前终止的宽容度。1.0 为 Whisper 标准值，无需调高",
            self.advancedTranscriptionGroup,
            range=(0.0, 3.0),
            step=0.1
        )
        self.repetitionPenaltyCard = DoubleNumberSettingCard(
            cfg.repetition_penalty,
            FIF.SYNC,
            "重复惩罚 (Repetition Penalty)",
            "惩罚重复出现的文本，减少『复读机』幻觉。推荐 1.2，不宜超过 1.5",
            self.advancedTranscriptionGroup,
            range=(1.0, 2.0),
            step=0.05
        )
        self.noSpeechThresholdCard = DoubleNumberSettingCard(
            cfg.no_speech_threshold,
            FIF.MUTE,
            "无声识别阈值 (No Speech Threshold)",
            "超过此概率的片段被判定为静音并跳过。推荐 0.6～0.75；过低会漏词（真实语音被误判为静音），过高会把噪声当语音（幻听增多）",
            self.advancedTranscriptionGroup,
            range=(0.0, 1.0),
            step=0.05
        )
        self.logProbThresholdCard = DoubleNumberSettingCard(
            cfg.log_prob_threshold,
            FIF.ALBUM,
            "日志概率阈值 (Log Prob Threshold)",
            "解码置信度下限（负数），低于此值的片段视为解码失败并丢弃。推荐 -1.0；调高（如 -0.5）会漏词，调低（如 -2.0）会保留低置信度内容但幻听增多",
            self.advancedTranscriptionGroup,
            range=(-5.0, 0.0),
            step=0.1
        )
        self.compressionRatioThresholdCard = DoubleNumberSettingCard(
            cfg.compression_ratio_threshold,
            FIF.SYNC,
            "压缩比阈值 (Compression Ratio Threshold)",
            "超过此值的片段视为循环幻觉并丢弃。推荐 2.0，正常语音压缩比通常在 1.2~1.9",
            self.advancedTranscriptionGroup,
            range=(0.0, 5.0),
            step=0.1
        )
        self.vadFilterCard = SwitchSettingCard(
            FIF.FILTER,
            "启用 VAD 过滤",
            "开启后先用语音活动检测过滤静音，减少幻觉。推荐开启",
            configItem=cfg.vad_filter,
            parent=self.advancedTranscriptionGroup
        )
        self.transcriptionHallucinationFilterCard = SwitchSettingCard(
            FIF.ROBOT,
            "转录幻觉清理",
            "开启后自动清理转录阶段识别到的疑似幻觉和重复片段，推荐开启",
            configItem=cfg.transcription_hallucination_filter,
            parent=self.advancedTranscriptionGroup
        )
        self.vadThresholdCard = DoubleNumberSettingCard(
            cfg.vad_threshold,
            FIF.FILTER,
            "VAD 过滤阈值 (VAD Threshold)",
            "语音活动检测灵敏度。推荐 0.5（Silero 官方默认），偏低会让噪声通过，偏高会漏词",
            self.advancedTranscriptionGroup,
            range=(0.0, 1.0),
            step=0.05
        )
        self.vadMinSilenceCard = NumberSettingCard(
            cfg.vad_min_silence_duration_ms,
            FIF.STOP_WATCH,
            "VAD 最小静音时长 (ms)",
            "静音持续多长才触发分段。推荐 300ms（自然语速停顿），过大会漏词，过小会过度切割",
            self.advancedTranscriptionGroup,
            range=(50, 2000)
        )
        self.vadSpeechPadCard = NumberSettingCard(
            cfg.vad_speech_pad_ms,
            FIF.STOP_WATCH,
            "VAD 语音边缘填充 (ms)",
            "在检测到的语音片段两端各填充的时长。推荐 400ms；调大可减少句首/句尾截断漏词，但也会引入更多相邻噪声",
            self.advancedTranscriptionGroup,
            range=(0, 1000)
        )
        self.vadMinSpeechCard = NumberSettingCard(
            cfg.vad_min_speech_duration_ms,
            FIF.STOP_WATCH,
            "VAD 最短语音时长 (ms)",
            "低于此时长的疑似语音片段会被 Silero VAD 过滤掉。推荐 100ms；调低可减少短促词漏听，但可能引入更多噪声误报",
            self.advancedTranscriptionGroup,
            range=(0, 500)
        )
        self.conditionOnPreviousTextCard = SwitchSettingCard(
            FIF.HISTORY,
            "参考上下文 (Condition On Previous)",
            "是否将上一句转录结果作为上下文。建议关闭——开启时一个错误会传染下一句，幻觉链式扩大",
            configItem=cfg.condition_on_previous_text,
            parent=self.advancedTranscriptionGroup
        )

        self.advancedTranscriptionGroup.addSettingCard(self.beamSizeCard)
        self.advancedTranscriptionGroup.addSettingCard(self.bestOfCard)
        self.advancedTranscriptionGroup.addSettingCard(self.patienceCard)
        self.advancedTranscriptionGroup.addSettingCard(self.repetitionPenaltyCard)
        self.advancedTranscriptionGroup.addSettingCard(self.vadFilterCard)
        self.advancedTranscriptionGroup.addSettingCard(self.vadThresholdCard)
        self.advancedTranscriptionGroup.addSettingCard(self.vadMinSilenceCard)
        self.advancedTranscriptionGroup.addSettingCard(self.vadSpeechPadCard)
        self.advancedTranscriptionGroup.addSettingCard(self.vadMinSpeechCard)
        self.advancedTranscriptionGroup.addSettingCard(self.transcriptionHallucinationFilterCard)
        self.advancedTranscriptionGroup.addSettingCard(self.noSpeechThresholdCard)
        self.advancedTranscriptionGroup.addSettingCard(self.logProbThresholdCard)
        self.advancedTranscriptionGroup.addSettingCard(self.compressionRatioThresholdCard)
        self.advancedTranscriptionGroup.addSettingCard(self.conditionOnPreviousTextCard)
        # 5. Faster Whisper 管理
        self.fasterWhisperGroup = FasterWhisperManager(self.scrollWidget, title="")
        self._addCollapsibleGroup("Faster Whisper 管理", self.fasterWhisperGroup)

        cfg.vad_filter.valueChanged.connect(self._updateVadFilterState)
        cfg.transcription_hallucination_filter.valueChanged.connect(
            self._updateTranscriptionHallucinationState
        )
        self._updateVadFilterState(cfg.vad_filter.value)
        self._updateTranscriptionHallucinationState(
            cfg.transcription_hallucination_filter.value
        )

        self._addCollapsibleGroup("转录高级设置 (Faster-Whisper)", self.advancedTranscriptionGroup)

        # 6. 配置管理
        self.configGroup = SettingCardGroup("", self.scrollWidget)

        self.exportConfigCard = PushSettingCard(
            "导出配置",
            FIF.DOWNLOAD,
            "导出当前配置",
            "将所有 API Key、模型设置等导出为 config.json 文件，方便备份或迁移到其他电脑",
            self.configGroup
        )
        self.exportConfigCard.clicked.connect(self._onExportConfig)

        self.importConfigCard = PushSettingCard(
            "导入配置",
            FIF.FOLDER,
            "导入配置文件",
            "从备份的 config.json 文件中恢复所有设置（将覆盖当前配置，需重启生效）",
            self.configGroup
        )
        self.importConfigCard.clicked.connect(self._onImportConfig)

        self.openLogCard = PushSettingCard(
            "打开日志",
            FIF.DOCUMENT,
            "错误日志",
            "打开 error.log 文件查看程序崩溃或异常的详细记录",
            self.configGroup
        )
        self.openLogCard.clicked.connect(self._onOpenLog)

        self.configGroup.addSettingCard(self.exportConfigCard)
        self.configGroup.addSettingCard(self.importConfigCard)
        self.configGroup.addSettingCard(self.openLogCard)
        self._addCollapsibleGroup("配置管理", self.configGroup)

    def _onTestConnection(self, config_prefix):
        """ 测试 LLM 类 API 连接（异步，不阻塞 UI） """
        sender = self.sender()
        if sender:
            sender.setEnabled(False)
            sender.setContent("正在测试...")

        def _on_result(success, message, elapsed):
            if success:
                InfoBar.success(
                    title="连接成功",
                    content=f"{message}\n响应时间: {elapsed:.2f}s",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                    parent=self.window()
                )
            else:
                InfoBar.error(
                    title="连接失败",
                    content=message,
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=8000,
                    parent=self.window()
                )
            if sender:
                sender.setEnabled(True)
                sender.setContent("测试当前 API 是否能正常连接并测量延迟")

        from PyQt6.QtCore import QThread, pyqtSignal

        class _TestThread(QThread):
            result = pyqtSignal(bool, str, float)
            def __init__(self, prefix):
                super().__init__()
                self._prefix = prefix
            def run(self):
                s, m, e = llm_manager.test_connection(self._prefix)
                self.result.emit(s, m, e)

        self._test_thread = _TestThread(config_prefix)
        self._test_thread.result.connect(_on_result)
        self._test_thread.start()

    def _onTranslationModeChanged(self, value):
        """仅「自定义」模式下显示批数和并发数参数卡片。"""
        is_custom = (value == "自定义")
        self.llmBatchSizeCard.setVisible(is_custom)
        self.llmMaxWorkersCard.setVisible(is_custom)

    def _onAsrProviderChanged(self, value):
        """根据所选提供商显示/隐藏对应配置卡片。"""
        is_whisper = (value == "Whisper")
        self.asrKeyCard.setVisible(is_whisper)
        self.asrUrlCard.setVisible(is_whisper)
        self.asrModelCard.setVisible(is_whisper)
        self.asrTestCard.setVisible(is_whisper)
        self.gladiaKeyCard.setVisible(not is_whisper)
        self.gladiaTestCard.setVisible(not is_whisper)
        self.gladiaVocabIntensityCard.setVisible(not is_whisper)
        self.gladiaLocalDenoiseCard.setVisible(not is_whisper)
        self.gladiaDiarizationCard.setVisible(not is_whisper)
        self.cloudAsrFallbackProviderCard.setEnabled(cfg.cloud_asr_fallback_enabled.value)
        self._syncCloudAsrFallbackProviderHint(value)
        if not is_whisper:
            self._updateGladiaDiarizationState(cfg.gladia_diarization.value)
        else:
            self.gladiaMaxSpeakersCard.setVisible(False)

    def _updateCloudAsrFallbackState(self, enabled):
        """云端 ASR 自动兜底开关联动。"""
        self.cloudAsrFallbackProviderCard.setEnabled(bool(enabled))
        self._syncCloudAsrFallbackProviderHint(cfg.asr_provider.value)

    def _syncCloudAsrFallbackProviderHint(self, primary_provider: str):
        fallback_provider = cfg.cloud_asr_fallback_provider.value
        if fallback_provider == primary_provider:
            self.cloudAsrFallbackProviderCard.setContent("当前与主提供商相同，触发兜底时将自动切换到另一方")
        else:
            self.cloudAsrFallbackProviderCard.setContent("当主提供商失败时，自动切换至该提供商")

    def _updateGladiaDiarizationState(self, enabled):
        """说话人分离开启时显示 max_speakers 卡片。"""
        if cfg.asr_provider.value != "Gladia":
            return
        self.gladiaMaxSpeakersCard.setVisible(bool(enabled))

    def _updateVadFilterState(self, enabled):
        """VAD 开启时显示 VAD 相关参数。"""
        visible = bool(enabled)
        self.vadThresholdCard.setVisible(visible)
        self.vadMinSilenceCard.setVisible(visible)
        self.vadSpeechPadCard.setVisible(visible)
        self.vadMinSpeechCard.setVisible(visible)

    def _updateTranscriptionHallucinationState(self, enabled):
        """幻觉清理开启时显示对应阈值参数。"""
        visible = bool(enabled)
        self.noSpeechThresholdCard.setVisible(visible)
        self.logProbThresholdCard.setVisible(visible)
        self.compressionRatioThresholdCard.setVisible(visible)

    def _onTestGladia(self):
        """向 Gladia 发送极短请求验证 Key 是否有效（异步，不阻塞 UI）。"""
        sender = self.sender()
        if sender:
            sender.setEnabled(False)
            sender.setContent("正在测试...")

        api_key = cfg.gladia_api_key.value.strip() if cfg.gladia_api_key.value else ""
        if not api_key:
            InfoBar.error(
                title="未填写 Key",
                content="请先填写 Gladia API Key",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self.window()
            )
            if sender:
                sender.setEnabled(True)
                sender.setContent("向 Gladia 发送一个极短的测试请求以验证 Key 是否有效")
            return

        def _on_gladia_result(success, message, elapsed):
            if success:
                InfoBar.success(
                    title="Gladia 连接成功",
                    content=f"Key 有效，响应时间: {elapsed:.2f}s",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                    parent=self.window()
                )
            else:
                InfoBar.error(
                    title="Gladia 连接失败",
                    content=message,
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=8000,
                    parent=self.window()
                )
            if sender:
                sender.setEnabled(True)
                sender.setContent("向 Gladia 发送一个极短的测试请求以验证 Key 是否有效")

        from PyQt6.QtCore import QThread, pyqtSignal

        class _GladiaTestThread(QThread):
            result = pyqtSignal(bool, str, float)
            def __init__(self, key):
                super().__init__()
                self._key = key
            def run(self):
                import requests, time as _time
                try:
                    start = _time.time()
                    resp = requests.get(
                        "https://api.gladia.io/v2/pre-recorded",
                        headers={"x-gladia-key": self._key},
                        timeout=15
                    )
                    elapsed = _time.time() - start
                    if resp.status_code in (200, 404):
                        self.result.emit(True, "Key 有效", elapsed)
                    elif resp.status_code == 401:
                        self.result.emit(False, "错误: API Key 无效或未授权 (401)", elapsed)
                    else:
                        self.result.emit(False, f"HTTP {resp.status_code}，请检查 Key 或网络", elapsed)
                except Exception as e:
                    self.result.emit(False, f"网络错误: {e}", 0.0)

        self._gladia_test_thread = _GladiaTestThread(api_key)
        self._gladia_test_thread.result.connect(_on_gladia_result)
        self._gladia_test_thread.start()

    def _onTestSearchConnection(self, provider):
        """ 测试搜索 API 连接（异步，不阻塞 UI） """
        sender = self.sender()
        if sender:
            sender.setEnabled(False)
            sender.setContent("正在测试...")

        def _on_search_result(success, message, elapsed):
            if success:
                InfoBar.success(
                    title="连接成功",
                    content=f"{message}\n响应时间: {elapsed:.2f}s",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                    parent=self.window()
                )
            else:
                InfoBar.error(
                    title="连接失败",
                    content=message,
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=8000,
                    parent=self.window()
                )
            if sender:
                sender.setEnabled(True)
                sender.setContent(f"测试 {provider} API 是否能正常连接并测量延迟")

        from PyQt6.QtCore import QThread, pyqtSignal

        class _SearchTestThread(QThread):
            result = pyqtSignal(bool, str, float)
            def __init__(self, prov):
                super().__init__()
                self._prov = prov
            def run(self):
                s, m, e = search_manager.test_connection(self._prov)
                self.result.emit(s, m, e)

        self._search_test_thread = _SearchTestThread(provider)
        self._search_test_thread.result.connect(_on_search_result)
        self._search_test_thread.start()

    def _onExportConfig(self):
        """导出 config.json 到用户指定位置"""
        if not CONFIG_FILE.exists():
            InfoBar.warning(title="导出失败", content="config.json 不存在，请先配置任意设置以生成配置文件",
                            parent=self, position=InfoBarPosition.BOTTOM_RIGHT)
            return
        dest, _ = QFileDialog.getSaveFileName(self, "导出配置文件", "config.json",
                                               "JSON 配置文件 (*.json)")
        if not dest:
            return
        try:
            shutil.copy2(str(CONFIG_FILE), dest)
            InfoBar.success(title="导出成功", content=f"配置已保存至: {dest}",
                            parent=self, position=InfoBarPosition.BOTTOM_RIGHT, duration=4000)
        except Exception as e:
            InfoBar.error(title="导出失败", content=str(e),
                          parent=self, position=InfoBarPosition.BOTTOM_RIGHT)

    def _onImportConfig(self):
        """从用户选择的 config.json 导入配置"""
        src, _ = QFileDialog.getOpenFileName(self, "选择配置文件", "",
                                              "JSON 配置文件 (*.json)")
        if not src:
            return
        try:
            # 先备份当前配置
            if CONFIG_FILE.exists():
                shutil.copy2(str(CONFIG_FILE), str(CONFIG_FILE) + ".bak")
            shutil.copy2(src, str(CONFIG_FILE))
            InfoBar.success(
                title="导入成功",
                content="配置已导入，请重启程序以使所有设置生效（原配置已备份为 config.json.bak）",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=6000
            )
        except Exception as e:
            InfoBar.error(title="导入失败", content=str(e),
                          parent=self, position=InfoBarPosition.BOTTOM_RIGHT)

    def _onOpenLog(self):
        """打开 error.log 文件"""
        from app.common.config import APP_ROOT
        log_path = APP_ROOT / "error.log"
        if not log_path.exists():
            InfoBar.info(title="暂无日志", content="目前没有记录任何错误日志",
                         parent=self, position=InfoBarPosition.BOTTOM_RIGHT, duration=2500)
            return
        try:
            os.startfile(str(log_path))
        except Exception as e:
            InfoBar.error(title="无法打开日志", content=str(e),
                          parent=self, position=InfoBarPosition.BOTTOM_RIGHT)
