# -*- coding: utf-8 -*-

from app.common.runtime_log import current_runtime_log_seq, get_runtime_log_snapshot


class ProjectManager:
    """
    项目管理器，负责维护当前任务的状态、文件路径和配置参数
    """

    def __init__(self):
        self.files = []
        self.config = {}
        self.subtitles = []
        self.temp_audio_paths = []
        self.runtime_log_start_seq = 0

    def mark_runtime_log_start(self):
        self.runtime_log_start_seq = current_runtime_log_seq()
        return self.runtime_log_start_seq

    def get_runtime_log_text(self):
        return get_runtime_log_snapshot(self.runtime_log_start_seq)

    def update_project(self, files, config):
        self.files = files
        self.config = config
        self.temp_audio_paths = []
        self.mark_runtime_log_start()
        print("[ProjectManager] 项目已更新")
        print(f" - 文件列表: {self.files}")
        print(f" - 配置参数: {self.config}")


# 全局单例
project_manager = ProjectManager()
