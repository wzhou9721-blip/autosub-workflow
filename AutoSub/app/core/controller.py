# -*- coding: utf-8 -*-
from .project import project_manager
from .transcriber import Transcriber
from .optimizer import subtitle_optimizer
from .splitter import subtitle_splitter

class TaskController:
    """
    任务控制器，负责协调前端 UI 的请求与后端 Core 逻辑的执行
    """
    def __init__(self):
        self.transcriber = Transcriber()

    def start_process(self, files, config, cancel_check=None):
        """ 开始处理任务 (转录) """
        print("[TaskController] 收到处理请求，准备启动后端逻辑...")
        
        # 1. 更新项目状态
        project_manager.update_project(files, config)
        
        # 2. 启动转录逻辑
        results = []
        if files:
            video_path = files[0]
            results = self.transcriber.run(video_path, config, cancel_check=cancel_check)
        
        return results

    def start_optimization(self, segments, context="", progress_callback=None, task_scope=None):
        """ 开始优化字幕 """
        print("[TaskController] 收到优化请求...")
        return subtitle_optimizer.optimize(segments, context, progress_callback, task_scope=task_scope)

    def start_split(self, segments, context="", progress_callback=None, task_scope=None):
        """ 开始断句优化 """
        print("[TaskController] 收到断句优化请求...")
        return subtitle_splitter.split(segments, context, progress_callback, task_scope=task_scope)

# 全局单例
task_controller = TaskController()
