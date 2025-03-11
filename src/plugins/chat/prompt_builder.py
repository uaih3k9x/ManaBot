import random
import time
from typing import Optional

from ...common.database import Database
from ..memory_system.memory import hippocampus, memory_graph
from ..moods.moods import MoodManager
from ..schedule.schedule_generator import bot_schedule
from .config import global_config
from .utils import get_embedding, get_recent_group_detailed_plain_text


class PromptBuilder:
    def __init__(self):
        self.prompt_built = ''
        self.activate_messages = ''
        self.db = Database.get_instance()



    async def _build_prompt(self, 
                    message_txt: str, 
                    sender_name: str = "某人",
                    relationship_value: float = 0.0,
                    group_id: Optional[int] = None) -> tuple[str, str]:
        """构建prompt
        
        Args:
            message_txt: 消息文本
            sender_name: 发送者昵称
            relationship_value: 关系值
            group_id: 群组ID
            
        Returns:
            str: 构建好的prompt

        """   
        # あなたの記憶
        sender_memory_prompt = ''
        start_time = time.time()
        relevant_memories = await hippocampus.get_relevant_memories(
            text=sender_name,
            max_topics=5,
            similarity_threshold=0.4,
            max_memory_num=5
        )
        if relevant_memories:
            sender_memory_prompt = "关于「{sender_name}」的记忆：{memory['content']}"
        else:
            sender_memory_prompt = '你和他不太熟悉，或者你忘记了和他有关的记忆'

        #先禁用关系
        if relationship_value > 240:
            relation_prompt = "你们是恋人关系，你非常爱他"
            relation_prompt_2 = "用亲密的语气回复"
        elif relationship_value > 100:
            relation_prompt = "你对他有一些好感"
            relation_prompt_2 = "用热情的语气回复"
        elif relationship_value > 30:
            relation_prompt = "关系好"
            relation_prompt_2 = "比较热情发言或者回复"
        elif relationship_value < -50:
            relation_prompt = "关系差，你很讨厌他"
            relation_prompt_2 = "骂他"
        else:
            relation_prompt = "关系一般"
            relation_prompt_2 = "正常发言或者回复"
        
        #开始构建prompt
        
        
        #心情
        mood_manager = MoodManager.get_instance()
        mood_prompt = mood_manager.get_prompt()
        
        
        #日程构建
        current_date = time.strftime("%Y-%m-%d", time.localtime())
        current_time = time.strftime("%H:%M:%S", time.localtime())
        bot_schedule_now_time,bot_schedule_now_activity = bot_schedule.get_current_task()
        prompt_date = f'''今天是{current_date}，现在是{current_time}，你今天的日程是：\n{bot_schedule.today_schedule}\n你现在正在{bot_schedule_now_activity}\n'''

        #知识构建
        start_time = time.time()
        
        prompt_info = ''
        promt_info_prompt = ''
        prompt_info = await self.get_prompt_info(message_txt,threshold=0.5)
        if prompt_info:
            prompt_info = f'''\n----------------------------------------------------\n你有以下这些[知识]：\n{prompt_info}\n请你记住上面的[知识]，之后可能会用到\n----------------------------------------------------\n'''
            
        end_time = time.time()
        print(f"\033[1;32m[知识检索]\033[0m 耗时: {(end_time - start_time):.3f}秒")
            
        # 获取聊天上下文
        chat_talking_prompt = ''
        if group_id:
            chat_talking_prompt = get_recent_group_detailed_plain_text(self.db, group_id, limit=global_config.MAX_CONTEXT_SIZE,combine = True)
        
        chat_talking_prompt = f"以下是群里正在聊天的内容：\n{chat_talking_prompt}"
        
        
        
        # 使用新的记忆获取方法
        memory_prompt = ''
        start_time = time.time()
        
        # 调用 hippocampus 的 get_relevant_memories 方法
        relevant_memories = await hippocampus.get_relevant_memories(
            text=message_txt,
            max_topics=5,
            similarity_threshold=0.4,
            max_memory_num=5
        )
        
        if relevant_memories:
            # 格式化记忆内容
            memory_items = []
            for memory in relevant_memories:
                memory_items.append(f"关于「{memory['topic']}」的记忆：{memory['content']}")
            
            memory_prompt = "看到这些聊天，你想起来：\n" + "\n".join(memory_items) + "\n"
            
            # 打印调试信息
            print("\n\033[1;32m[记忆检索]\033[0m 找到以下相关记忆：")
            for memory in relevant_memories:
                print(f"- 主题「{memory['topic']}」[相似度: {memory['similarity']:.2f}]: {memory['content']}")
        
        end_time = time.time()
        print(f"\033[1;32m[回忆耗时]\033[0m 耗时: {(end_time - start_time):.3f}秒")
        
        
            
        #激活prompt构建
        activate_prompt = ''
        activate_prompt = f"以上是群里正在进行的聊天，{memory_prompt} 现在昵称为 '{sender_name}' 的用户说的:{message_txt}。引起了你的注意,你和他{relation_prompt},{mood_prompt},你想要{relation_prompt_2}。"          
        
        #检测机器人相关词汇，改为关键词检测与反应功能了，提取到全局配置中
        # bot_keywords = ['人机', 'bot', '机器', '入机', 'robot', '机器人']
        # is_bot = any(keyword in message_txt.lower() for keyword in bot_keywords)
        # if is_bot:
        #     is_bot_prompt = '有人说你是人机或者机器人，否定这一事实，攻击或反问他。注意，只有认为你是机器人才回复，否则不要否认'
        # else:
        #     is_bot_prompt = ''

        # 关键词检测与反应
        keywords_reaction_prompt = ''
        for rule in global_config.keywords_reaction_rules:
            if rule.get("enable", False):
                if any(keyword in message_txt.lower() for keyword in rule.get("keywords", [])):
                    print(f"检测到以下关键词之一：{rule.get('keywords', [])}，触发反应：{rule.get('reaction', '')}")
                    keywords_reaction_prompt += rule.get("reaction", "") + '，'

        
        #人格选择
        personality=global_config.PROMPT_PERSONALITY
        probability_1 = global_config.PERSONALITY_1
        probability_2 = global_config.PERSONALITY_2
        probability_3 = global_config.PERSONALITY_3
        prompt_personality = ''
        personality_choice = random.random()
        if personality_choice < probability_1:  # 第一种人格
            prompt_personality = f'''{activate_prompt}你的网名叫{global_config.BOT_NICKNAME}，{personality[0]}, 你正在浏览qq群,{promt_info_prompt},
            现在请你给出日常且口语化的回复，平淡一些，尽量简短一些。{keywords_reaction_prompt}
            请注意把握群里的聊天内容，不要刻意突出自身学科背景，不要回复的太有条理，可以有个性。'''
        elif personality_choice < probability_1 + probability_2:  # 第二种人格
            prompt_personality = f'''{activate_prompt}你的网名叫{global_config.BOT_NICKNAME}，{personality[1]}, 你正在浏览qq群，{promt_info_prompt},
            现在请你给出日常且口语化的回复，请表现你自己的见解，不要一昧迎合，尽量简短一些。{keywords_reaction_prompt}
            请你表达自己的见解和观点。可以有个性。'''
        else:  # 第三种人格
            prompt_personality = f'''{activate_prompt}你的网名叫{global_config.BOT_NICKNAME}，{personality[2]}, 你正在浏览qq群，{promt_info_prompt},
            现在请你给出日常且口语化的回复，请表现你自己的见解，不要一昧迎合，尽量简短一些。{keywords_reaction_prompt}
            请你表达自己的见解和观点。可以有个性。'''
        
        #中文高手(新加的好玩功能)
        prompt_ger = ''
        if random.random() < 0.04:
            prompt_ger += '你喜欢用倒装句'
        if random.random() < 0.02:
            prompt_ger += '你喜欢用反问句'
        if random.random() < 0.01:
            prompt_ger += '你喜欢说日文'
        
        #额外信息要求
        extra_info = '''但是记得回复平淡一些，简短一些，尤其注意在没明确提到时不要过多提及自身的背景, 不要直接回复别人发的表情包，记住不要输出多余内容(包括前后缀，冒号和引号，括号，表情等)，只需要输出回复内容就好，不要输出其他任何内容''' 
        
        #合并prompt
        prompt = ""
        prompt += f"{prompt_info}\n"
        prompt += f"{prompt_date}\n"
        prompt += f"{chat_talking_prompt}\n"  
        prompt += f"{prompt_personality}\n"
        prompt += f"{prompt_ger}\n"
        prompt += f"{extra_info}\n"    
        prompt += f"{memory_prompt}\n"
        prompt += f"{sender_memory_prompt}\n"
        
        '''读空气prompt处理''' 
        activate_prompt_check=f"以上是群里正在进行的聊天，昵称为 '{sender_name}' 的用户说的:{message_txt}。引起了你的注意,你和他{relation_prompt}，你想要{relation_prompt_2}，但是这不一定是合适的时机，请你决定是否要回应这条消息。如果你在忙自己的事情，请不要回复，如果你在睡觉，也不要回复"     
        prompt_personality_check = ''
        extra_check_info=f"请注意把握群里的聊天内容的基础上，综合群内的氛围，例如，和{global_config.BOT_NICKNAME}相关的话题要积极回复,如果是at自己的消息一定要回复，如果自己正在和别人聊天一定要回复，其他话题如果合适搭话也可以回复，如果认为应该回复请输出yes，否则输出no，请注意是决定是否需要回复，而不是编写回复内容，除了yes和no不要输出任何回复内容。"
        if personality_choice < probability_1:  # 第一种人格
            prompt_personality_check = f'''你的网名叫{global_config.BOT_NICKNAME}，{personality[0]}, 你正在浏览qq群，{promt_info_prompt} {activate_prompt_check} {extra_check_info}'''
        elif personality_choice < probability_1 + probability_2:  # 第二种人格
            prompt_personality_check = f'''你的网名叫{global_config.BOT_NICKNAME}，{personality[1]}, 你正在浏览qq群，{promt_info_prompt} {activate_prompt_check} {extra_check_info}'''
        else:  # 第三种人格
            prompt_personality_check = f'''你的网名叫{global_config.BOT_NICKNAME}，{personality[2]}, 你正在浏览qq群，{promt_info_prompt} {activate_prompt_check} {extra_check_info}'''

        prompt_check_if_response=f"{prompt_info}\n{prompt_date}\n{chat_talking_prompt}\n{prompt_personality_check}"
        
        return prompt,prompt_check_if_response
    
    def _build_initiative_prompt_select(self,group_id): 
        current_date = time.strftime("%Y-%m-%d", time.localtime())
        current_time = time.strftime("%H:%M:%S", time.localtime())
        bot_schedule_now_time,bot_schedule_now_activity = bot_schedule.get_current_task()
        prompt_date = f'''今天是{current_date}，现在是{current_time}，你今天的日程是：\n{bot_schedule.today_schedule}\n你现在正在{bot_schedule_now_activity}\n'''

        chat_talking_prompt = ''
        if group_id:
            chat_talking_prompt = get_recent_group_detailed_plain_text(self.db, group_id, limit=global_config.MAX_CONTEXT_SIZE,combine = True)
        
        chat_talking_prompt = f"以下是群里正在聊天的内容：\n{chat_talking_prompt}"
            # print(f"\033[1;34m[调试]\033[0m 已从数据库获取群 {group_id} 的消息记录:{chat_talking_prompt}")

        # 获取主动发言的话题
        all_nodes=memory_graph.dots
        all_nodes=filter(lambda dot:len(dot[1]['memory_items'])>3,all_nodes)
        nodes_for_select=random.sample(all_nodes,5)
        topics=[info[0] for info in nodes_for_select]
        infos=[info[1] for info in nodes_for_select]

        #激活prompt构建
        activate_prompt = ''
        activate_prompt = "以上是群里正在进行的聊天。"
        personality=global_config.PROMPT_PERSONALITY
        prompt_personality = ''
        personality_choice = random.random()
        if personality_choice < probability_1:  # 第一种人格
            prompt_personality = f'''{activate_prompt}你的网名叫{global_config.BOT_NICKNAME}，{personality[0]}'''
        elif personality_choice < probability_1 + probability_2:  # 第二种人格
            prompt_personality = f'''{activate_prompt}你的网名叫{global_config.BOT_NICKNAME}，{personality[1]}'''
        else:  # 第三种人格
            prompt_personality = f'''{activate_prompt}你的网名叫{global_config.BOT_NICKNAME}，{personality[2]}'''
        
        topics_str=','.join(f"\"{topics}\"")
        prompt_for_select=f"你现在想在群里发言，回忆了一下，想到几个话题，分别是{topics_str}，综合当前状态以及群内气氛，请你在其中选择一个合适的话题，注意只需要输出话题，除了话题什么也不要输出(双引号也不要输出)"
        
        prompt_initiative_select=f"{prompt_date}\n{prompt_personality}\n{prompt_for_select}"
        prompt_regular=f"{prompt_date}\n{prompt_personality}"

        return prompt_initiative_select,nodes_for_select,prompt_regular
    
    def _build_initiative_prompt_check(self,selected_node,prompt_regular):
        memory=random.sample(selected_node['memory_items'],3)
        memory='\n'.join(memory)
        prompt_for_check=f"{prompt_regular}你现在想在群里发言，回忆了一下，想到一个话题,是{selected_node['concept']}，关于这个话题的记忆有\n{memory}\n，以这个作为主题发言合适吗？请在把握群里的聊天内容的基础上，综合群内的氛围，如果认为应该发言请输出yes，否则输出no，请注意是决定是否需要发言，而不是编写回复内容，除了yes和no不要输出任何回复内容。"
        return prompt_for_check,memory
    
    def _build_initiative_prompt(self,selected_node,prompt_regular,memory):
        prompt_for_initiative=f"{prompt_regular}你现在想在群里发言，回忆了一下，想到一个话题,是{selected_node['concept']}，关于这个话题的记忆有\n{memory}\n，请在把握群里的聊天内容的基础上，综合群内的氛围，以日常且口语化的口吻，简短且随意一点进行发言，不要说的太有条理，可以有个性。记住不要输出多余内容(包括前后缀，冒号和引号，括号，表情等)"
        return prompt_for_initiative
        

    async def get_prompt_info(self,message:str,threshold:float):
        related_info = ''
        print(f"\033[1;34m[调试]\033[0m 获取知识库内容，元消息：{message[:30]}...，消息长度: {len(message)}")
        embedding = await get_embedding(message)
        related_info += self.get_info_from_db(embedding,threshold=threshold)
            
        return related_info

    def get_info_from_db(self, query_embedding: list, limit: int = 1, threshold: float = 0.5) -> str:
        if not query_embedding:
            return ''
        # 使用余弦相似度计算
        pipeline = [
            {
                "$addFields": {
                    "dotProduct": {
                        "$reduce": {
                            "input": {"$range": [0, {"$size": "$embedding"}]},
                            "initialValue": 0,
                            "in": {
                                "$add": [
                                    "$$value",
                                    {"$multiply": [
                                        {"$arrayElemAt": ["$embedding", "$$this"]},
                                        {"$arrayElemAt": [query_embedding, "$$this"]}
                                    ]}
                                ]
                            }
                        }
                    },
                    "magnitude1": {
                        "$sqrt": {
                            "$reduce": {
                                "input": "$embedding",
                                "initialValue": 0,
                                "in": {"$add": ["$$value", {"$multiply": ["$$this", "$$this"]}]}
                            }
                        }
                    },
                    "magnitude2": {
                        "$sqrt": {
                            "$reduce": {
                                "input": query_embedding,
                                "initialValue": 0,
                                "in": {"$add": ["$$value", {"$multiply": ["$$this", "$$this"]}]}
                            }
                        }
                    }
                }
            },
            {
                "$addFields": {
                    "similarity": {
                        "$divide": ["$dotProduct", {"$multiply": ["$magnitude1", "$magnitude2"]}]
                    }
                }
            },
            {
                "$match": {
                    "similarity": {"$gte": threshold}  # 只保留相似度大于等于阈值的结果
                }
            },
            {"$sort": {"similarity": -1}},
            {"$limit": limit},
            {"$project": {"content": 1, "similarity": 1}}
        ]
        
        results = list(self.db.db.knowledges.aggregate(pipeline))
        # print(f"\033[1;34m[调试]\033[0m获取知识库内容结果: {results}")
        
        if not results:
            return ''
            
        # 返回所有找到的内容，用换行分隔
        return '\n'.join(str(result['content']) for result in results)
    
prompt_builder = PromptBuilder()