# -*- coding: utf-8 -*-
import json
import requests
from app.common.config import cfg

class SearchManager:
    """
    搜索管理器，用于获取实时背景知识
    """
    def search_knowledge(self, query):
        if not cfg.enable_web_search.value:
            return ""

        provider = cfg.search_provider.value
        
        if provider == "Tavily":
            return self._search_tavily(query)
        elif provider == "Serper":
            return self._search_serper(query)
        else:
            return ""

    def _search_tavily(self, query):
        api_key = cfg.tavily_api_key.value
        if not api_key:
            print("[SearchManager] 未配置 Tavily API Key")
            return ""

        print(f"[SearchManager] 正在使用 Tavily 搜索背景知识: {query}")
        
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "include_answer": True,
            "max_results": 3
        }

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("answer"):
                return data["answer"]
            
            results = data.get("results", [])
            context = "\n".join([f"- {r['title']}: {r['content']}" for r in results])
            return context
        except Exception as e:
            print(f"[SearchManager] Tavily 搜索失败: {e}")
            return ""

    def _search_serper(self, query):
        api_key = cfg.serper_api_key.value
        if not api_key:
            print("[SearchManager] 未配置 Serper API Key")
            return ""

        print(f"[SearchManager] 正在使用 Serper 搜索背景知识: {query}")

        url = "https://google.serper.dev/search"
        payload = json.dumps({
            "q": query,
            "gl": "cn",
            "hl": "zh-cn"
        })
        headers = {
            'X-API-KEY': api_key,
            'Content-Type': 'application/json'
        }

        try:
            response = requests.post(url, headers=headers, data=payload, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Serper doesn't give a direct answer summary like Tavily, so we aggregate snippets
            knowledge_graph = data.get("knowledgeGraph", {})
            organic = data.get("organic", [])
            
            context_parts = []
            
            if knowledge_graph:
                desc = knowledge_graph.get("description", "")
                if desc:
                    context_parts.append(f"知识图谱: {desc}")

            for item in organic[:3]:
                title = item.get("title", "")
                snippet = item.get("snippet", "")
                context_parts.append(f"- {title}: {snippet}")
            
            return "\n".join(context_parts)

        except Exception as e:
            print(f"[SearchManager] Serper 搜索失败: {e}")
            return ""

    def test_connection(self, provider):
        """
        测试搜索服务连接并返回响应时间
        """
        import time
        
        query = "Artificial Intelligence"
        start_time = time.time()
        
        try:
            if provider == "Tavily":
                api_key = cfg.tavily_api_key.value
                if not api_key:
                    return False, "错误: 未填写 Tavily API Key", 0
                
                url = "https://api.tavily.com/search"
                payload = {
                    "api_key": api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": 1
                }
                response = requests.post(url, json=payload, timeout=15)
                
            elif provider == "Serper":
                api_key = cfg.serper_api_key.value
                if not api_key:
                    return False, "错误: 未填写 Serper API Key", 0
                
                url = "https://google.serper.dev/search"
                payload = json.dumps({"q": query})
                headers = {'X-API-KEY': api_key, 'Content-Type': 'application/json'}
                response = requests.post(url, headers=headers, data=payload, timeout=15)
            else:
                return False, f"错误: 不支持的服务提供商 {provider}", 0
            
            elapsed = time.time() - start_time
            
            if response.status_code == 200:
                return True, f"连接成功! ({provider})", elapsed
            elif response.status_code == 401 or response.status_code == 403:
                return False, f"错误: API Key 无效 ({response.status_code})", elapsed
            else:
                return False, f"错误: HTTP {response.status_code}", elapsed
                
        except Exception as e:
            elapsed = time.time() - start_time
            return False, f"网络错误: {str(e)}", elapsed

# 单例
search_manager = SearchManager()
