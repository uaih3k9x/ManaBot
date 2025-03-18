import asyncio
import json
import re
from datetime import datetime
from typing import Tuple, Union

import aiohttp
from loguru import logger
from nonebot import get_driver

from ...common.database import Database
from ..chat.config import global_config
from ..chat.utils_image import compress_base64_image_by_scale

driver = get_driver()
config = driver.config


class LLM_request:
    def __init__(self, model, **kwargs):
        # 将大写的配置键转换为小写并从config中获取实际值
        try:
            self.api_key = getattr(config, model["key"])
            self.base_url = getattr(config, model["base_url"])
        except AttributeError as e:
            logger.error(f"原始 model dict 信息：{model}")
            logger.error(f"配置错误：找不到对应的配置项 - {str(e)}")
            raise ValueError(f"配置错误：找不到对应的配置项 - {str(e)}") from e
        self.model_name = model["name"]
        self.params = kwargs
        
        self.pri_in = model.get("pri_in", 0)
        self.pri_out = model.get("pri_out", 0)
        
        # 获取数据库实例
        self.db = Database.get_instance()
        self._init_database()

    def _init_database(self):
        """初始化数据库集合"""
        try:
            # 创建llm_usage集合的索引
            self.db.db.llm_usage.create_index([("timestamp", 1)])
            self.db.db.llm_usage.create_index([("model_name", 1)])
            self.db.db.llm_usage.create_index([("user_id", 1)])
            self.db.db.llm_usage.create_index([("request_type", 1)])
        except Exception as e:
            logger.error(f"创建数据库索引失败: {e}")

    def _record_usage(self, prompt_tokens: int, completion_tokens: int, total_tokens: int, 
                     user_id: str = "system", request_type: str = "chat", 
                     endpoint: str = "/chat/completions"):
        """记录模型使用情况到数据库
        Args:
            prompt_tokens: 输入token数
            completion_tokens: 输出token数
            total_tokens: 总token数
            user_id: 用户ID，默认为system
            request_type: 请求类型(chat/embedding/image等)
            endpoint: API端点
        """
        try:
            usage_data = {
                "model_name": self.model_name,
                "user_id": user_id,
                "request_type": request_type,
                "endpoint": endpoint,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost": self._calculate_cost(prompt_tokens, completion_tokens),
                "status": "success",
                "timestamp": datetime.now()
            }
            self.db.db.llm_usage.insert_one(usage_data)
            logger.info(
                f"Token使用情况 - 模型: {self.model_name}, "
                f"用户: {user_id}, 类型: {request_type}, "
                f"提示词: {prompt_tokens}, 完成: {completion_tokens}, "
                f"总计: {total_tokens}"
            )
        except Exception as e:
            logger.error(f"记录token使用情况失败: {e}")

    def _calculate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """计算API调用成本
        使用模型的pri_in和pri_out价格计算输入和输出的成本
        
        Args:
            prompt_tokens: 输入token数量
            completion_tokens: 输出token数量
            
        Returns:
            float: 总成本（元）
        """
        # 使用模型的pri_in和pri_out计算成本
        input_cost = (prompt_tokens / 1000000) * self.pri_in
        output_cost = (completion_tokens / 1000000) * self.pri_out
        return round(input_cost + output_cost, 6)

    async def _execute_request(
            self,
            endpoint: str,
            prompt: str = None,
            image_base64: str = None,
            payload: dict = None,
            retry_policy: dict = None,
            response_handler: callable = None,
            user_id: str = "system",
            request_type: str = "chat"
    ):
        """统一请求执行入口
        Args:
            endpoint: API端点路径 (如 "chat/completions")
            prompt: prompt文本
            image_base64: 图片的base64编码
            payload: 请求体数据
            retry_policy: 自定义重试策略
            response_handler: 自定义响应处理器
            user_id: 用户ID
            request_type: 请求类型
        """
        # 合并重试策略
        default_retry = {
            "max_retries": 3, "base_wait": 15,
            "retry_codes": [429, 413, 500, 503],
            "abort_codes": [400, 401, 402, 403]}
        policy = {**default_retry, **(retry_policy or {})}

        # 常见Error Code Mapping
        error_code_mapping = {
            400: "参数不正确",
            401: "API key 错误，认证失败",
            402: "账号余额不足",
            403: "需要实名,或余额不足",
            404: "Not Found",
            429: "请求过于频繁，请稍后再试",
            500: "服务器内部故障",
            503: "服务器负载过高"
        }

        api_url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        #判断是否为流式
        stream_mode = self.params.get("stream", False)
        if self.params.get("stream", False) is True:
            logger.info(f"进入流式输出模式，发送请求到URL: {api_url}")
        else:
            logger.info(f"发送请求到URL: {api_url}")
        logger.info(f"使用模型: {self.model_name}")

        # 构建请求体
        if image_base64:
            payload = await self._build_payload(prompt, image_base64)
        elif payload is None:
            payload = await self._build_payload(prompt)

        for retry in range(policy["max_retries"]):
            try:
                # 使用上下文管理器处理会话
                headers = await self._build_headers()
                #似乎是openai流式必须要的东西,不过阿里云的qwq-plus加了这个没有影响
                if stream_mode:
                    headers["Accept"] = "text/event-stream"

                async with aiohttp.ClientSession() as session:
                    async with session.post(api_url, headers=headers, json=payload) as response:
                        # 处理需要重试的状态码
                        if response.status in policy["retry_codes"]:
                            wait_time = policy["base_wait"] * (2 ** retry)
                            logger.warning(f"错误码: {response.status}, 等待 {wait_time}秒后重试")
                            if response.status == 413:
                                logger.warning("请求体过大，尝试压缩...")
                                image_base64 = compress_base64_image_by_scale(image_base64)
                                payload = await self._build_payload(prompt, image_base64)
                            elif response.status in [500, 503]:
                                logger.error(f"错误码: {response.status} - {error_code_mapping.get(response.status)}")
                                raise RuntimeError("服务器负载过高，模型恢复失败QAQ")
                            else:
                                logger.warning(f"请求限制(429)，等待{wait_time}秒后重试...")

                            await asyncio.sleep(wait_time)
                            continue
                        elif response.status in policy["abort_codes"]:
                            logger.error(f"错误码: {response.status} - {error_code_mapping.get(response.status)}")
                            if response.status == 403 :
                                if global_config.llm_normal == "Pro/deepseek-ai/DeepSeek-V3":
                                    logger.error("可能是没有给硅基流动充钱，普通模型自动退化至非Pro模型，反应速度可能会变慢")
                                    global_config.llm_normal = "deepseek-ai/DeepSeek-V3"
                                if global_config.llm_reasoning == "Pro/deepseek-ai/DeepSeek-R1":
                                    logger.error("可能是没有给硅基流动充钱，推理模型自动退化至非Pro模型，反应速度可能会变慢")
                                    global_config.llm_reasoning = "deepseek-ai/DeepSeek-R1"
                            raise RuntimeError(f"请求被拒绝: {error_code_mapping.get(response.status)}")
                            
                        response.raise_for_status()
                        
                        #将流式输出转化为非流式输出
                        if stream_mode:
                            accumulated_content = ""
                            async for line_bytes in response.content:
                                line = line_bytes.decode("utf-8").strip()
                                if not line:
                                    continue
                                if line.startswith("data:"):
                                    data_str = line[5:].strip()
                                    if data_str == "[DONE]":
                                        break
                                    try:
                                        chunk = json.loads(data_str)
                                        delta = chunk["choices"][0]["delta"]
                                        delta_content = delta.get("content")
                                        if delta_content is None:
                                            delta_content = ""
                                        accumulated_content += delta_content
                                    except Exception as e:
                                        logger.error(f"解析流式输出错误: {e}")
                            content = accumulated_content
                            reasoning_content = ""
                            match = re.search(r"(?:<think>)?(.*?)</think>", content, re.DOTALL)
                            content = re.sub(r"(?:<think>)?.*?</think>", "", content, flags=re.DOTALL, count=1).strip()
                            if match:
                                reasoning = match.group(1).strip()
                                reasoning_content = match.group(1).strip()
                            # 构造一个伪result以便调用自定义响应处理器或默认处理器
                            result = {"choices": [{"message": {"content": content, "reasoning_content": reasoning_content}}]}
                            return response_handler(result) if response_handler else self._default_response_handler(result, user_id, request_type, endpoint)
                        else:
                            result = await response.json()
                            # 使用自定义处理器或默认处理
                            return response_handler(result) if response_handler else self._default_response_handler(result, user_id, request_type, endpoint)

            except Exception as e:
                if retry < policy["max_retries"] - 1:
                    wait_time = policy["base_wait"] * (2 ** retry)
                    logger.error(f"请求失败，等待{wait_time}秒后重试... 错误: {str(e)}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.critical(f"请求失败: {str(e)}")
                    logger.critical(f"请求头: {await self._build_headers(no_key=True)} 请求体: {payload}")
                    raise RuntimeError(f"API请求失败: {str(e)}")

        logger.error("达到最大重试次数，请求仍然失败")
        raise RuntimeError("达到最大重试次数，API请求仍然失败")
        
    async def _transform_parameters(self, params: dict) ->dict:
        """
        根据模型名称转换参数：
        - 对于需要转换的OpenAI CoT系列模型（例如 "o3-mini"），删除 'temprature' 参数，
        并将 'max_tokens' 重命名为 'max_completion_tokens'
        """
        # 复制一份参数，避免直接修改原始数据
        new_params = dict(params)
        # 定义需要转换的模型列表
        models_needing_transformation = ["o3-mini", "o1-mini", "o1-preview", "o1-2024-12-17", "o1-preview-2024-09-12", "o3-mini-2025-01-31", "o1-mini-2024-09-12"]
        if self.model_name.lower() in models_needing_transformation:
            # 删除 'temprature' 参数（如果存在）
            new_params.pop("temperature", None)
            # 如果存在 'max_tokens'，则重命名为 'max_completion_tokens'
            if "max_tokens" in new_params:
                new_params["max_completion_tokens"] = new_params.pop("max_tokens")
        return new_params

    async def _build_payload(self, prompt: str, image_base64: str = None) -> dict:
        """构建请求体"""
        # 复制一份参数，避免直接修改 self.params
        params_copy = await self._transform_parameters(self.params)
        if image_base64:
            payload = {
                "model": self.model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                        ]
                    }
                ],
                "max_tokens": global_config.max_response_length,
                **params_copy
            }
        else:
            payload = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": global_config.max_response_length,
                **params_copy
            }
        # 如果 payload 中依然存在 max_tokens 且需要转换，在这里进行再次检查
        if self.model_name.lower() in ["o3-mini", "o1-mini", "o1-preview", "o1-2024-12-17", "o1-preview-2024-09-12", "o3-mini-2025-01-31", "o1-mini-2024-09-12"] and "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        return payload
        

    def _default_response_handler(self, result: dict, user_id: str = "system", 
                                request_type: str = "chat", endpoint: str = "/chat/completions") -> Tuple:
        """默认响应解析"""
        if "choices" in result and result["choices"]:
            message = result["choices"][0]["message"]
            content = message.get("content", "")
            content, reasoning = self._extract_reasoning(content)
            reasoning_content = message.get("model_extra", {}).get("reasoning_content", "")
            if not reasoning_content:
                reasoning_content = message.get("reasoning_content", "")
                if not reasoning_content:
                    reasoning_content = reasoning

            # 记录token使用情况
            usage = result.get("usage", {})
            if usage:
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)
                self._record_usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    user_id=user_id,
                    request_type=request_type,
                    endpoint=endpoint
                )

            return content, reasoning_content

        return "没有返回结果", ""

    def _extract_reasoning(self, content: str) -> tuple[str, str]:
        """CoT思维链提取"""
        match = re.search(r'(?:<think>)?(.*?)</think>', content, re.DOTALL)
        content = re.sub(r'(?:<think>)?.*?</think>', '', content, flags=re.DOTALL, count=1).strip()
        if match:
            reasoning = match.group(1).strip()
        else:
            reasoning = ""
        return content, reasoning

    async def _build_headers(self, no_key: bool = False) -> dict:
        """构建请求头"""
        if no_key:
            return {
                "Authorization": f"Bearer **********",
                "Content-Type": "application/json"
            }
        else:
            return {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            } 
        # 防止小朋友们截图自己的key

    async def generate_response(self, prompt: str) -> Tuple[str, str]:
        """根据输入的提示生成模型的异步响应"""

        content, reasoning_content = await self._execute_request(
            endpoint="/chat/completions",
            prompt=prompt
        )
        return content, reasoning_content

    async def generate_response_for_image(self, prompt: str, image_base64: str) -> Tuple[str, str]:
        """根据输入的提示和图片生成模型的异步响应"""

        content, reasoning_content = await self._execute_request(
            endpoint="/chat/completions",
            prompt=prompt,
            image_base64=image_base64
        )
        return content, reasoning_content

    async def generate_response_async(self, prompt: str, **kwargs) -> Union[str, Tuple[str, str]]:
        """异步方式根据输入的提示生成模型的响应"""
        # 构建请求体
        data = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": global_config.max_response_length,
            **self.params
        }

        content, reasoning_content = await self._execute_request(
            endpoint="/chat/completions",
            payload=data,
            prompt=prompt
        )
        return content, reasoning_content

    async def get_embedding(self, text: str) -> Union[list, None]:
        """异步方法：获取文本的embedding向量
        
        Args:
            text: 需要获取embedding的文本
            
        Returns:
            list: embedding向量，如果失败则返回None
        """
        def embedding_handler(result):
            """处理响应"""
            if "data" in result and len(result["data"]) > 0:
                return result["data"][0].get("embedding", None)
            return None

        embedding = await self._execute_request(
            endpoint="/embeddings",
            prompt=text,
            payload={
                "model": self.model_name,
                "input": text,
                "encoding_format": "float"
            },
            retry_policy={
                "max_retries": 2,
                "base_wait": 6
            },
            response_handler=embedding_handler
        )
        return embedding

