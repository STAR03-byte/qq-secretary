"""
LLM 智能研判与晚报生成模块 (analyzer.py)
兼容 OpenAI 规范的 HTTP 接口 (如 DeepSeek / Qwen / 通义千问等)，具备超时与最大重试保护。
"""

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
import requests

logger = logging.getLogger(__name__)


def _load_config_data(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读取配置，若未传入则尝试从本地 config.json 读取"""
    if config:
        return config
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"读取 config.json 失败，使用默认配置: {e}")
        return {
            "llm": {
                "api_url": "https://api.deepseek.com/chat/completions",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout": 30,
            },
            "user_profile": {
                "name": "Star",
                "identity": "学生/开发者",
                "focus": ["重要通知", "紧急事务", "截止日期DDL"],
                "ignore": ["游戏开黑", "外卖拼单", "水群灌水"],
            },
        }


def _clean_json_response(raw_text: str) -> str:
    """提取大模型输出中的 JSON 字符串，剥离 Markdown 代码块"""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    # 尝试找到第一个 { 和最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end >= start:
        text = text[start : end + 1]
    return text.strip()


def analyze_messages(
    source_name: str,
    combined_text: str,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    分析合并后的会话消息
    :param source_name: 来源群组或联系人名称
    :param combined_text: 合并后的原始消息正文
    :param config: 可选配置字典
    :return: {"urgency": "URGENT|DEFERRED|DIGEST|IGNORE", "summary": "...", "action_item": "..."}
    """
    cfg = _load_config_data(config)
    llm_cfg = cfg.get("llm", {})
    user_profile = cfg.get("user_profile", {})

    api_url = llm_cfg.get("api_url", "https://api.deepseek.com/chat/completions")
    api_key = llm_cfg.get("api_key", "")
    model = llm_cfg.get("model", "deepseek-chat")
    timeout = llm_cfg.get("timeout", 30)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")

    system_prompt = f"""你是一名专为主人配置的“QQ智能通知小秘书”。你的核心使命是帮主人从杂乱的QQ群消息或私聊中快速识别高价值信息，剥离垃圾灌水，精准推算时效性。

【当前系统基准时间】：{now_str}

【主人个人画像】：
- 姓名/昵称：{user_profile.get('name', 'Star')}
- 身份：{user_profile.get('identity', '学生/开发者')}
- 重点关注：{json.dumps(user_profile.get('focus', []), ensure_ascii=False)}
- 忽略偏好：{json.dumps(user_profile.get('ignore', []), ensure_ascii=False)}

【核心研判准则（必须严格执行）】：
1. 噪音剥离原则（防漏报）：一段缓冲消息中若 90% 是水群闲聊，但夹杂了 1 条重要通知或 @主人的事务，必须果断忽略闲聊，仅针对那 1 条有效通知定级并提炼摘要。
2. 语境辨伪原则（防误报）：出现“急、救命、马上、@全体”等词汇时必须结合完整上下文。若是游戏开黑、拼单外卖、打卡互助、日常玩梗吐槽，一律判为 IGNORE。
3. 时间紧迫度折算：结合【当前系统基准时间】推算事件节点：
   - URGENT（紧急）：3 小时内截止/发生，或明确需要主人立即回复/处理的真实要事。
   - DEFERRED（重要不紧急）：时限超过 3 小时（例如明天、本周五、下周截止），或重要但当前无需立即介入的事项。
   - DIGEST（群动态简报）：群内有一定价值的信息分享、讨论要点、会议纪要备忘，无明确针对主人的待办。
   - IGNORE（忽略）：纯闲聊、日常吹水、游戏开黑、拼单、广告、无实质内容的表情复读。
4. 残缺消息兜底：安卓通知栏若只显示“@全体成员 [图片]”或“[文件] xxx.xlsx”且来自重要人物/工作/班级群，至少判为 DEFERRED，并提醒主人打开 QQ 查看原图/原文件。

【Few-Shot 研判示例】：
示例 1：
来源：计算机2101班级群
消息：张三：今天食堂黄焖鸡真难吃。李四：确实。辅导员-王老师：@全体成员 请所有未交医保参保证明的同学务必在今天下午 17:30 前上传至学工系统，关系到医保待遇！王五：收到。
研判输出：
{{"urgency": "URGENT", "summary": "辅导员要求今天17:30前上传医保参保证明", "action_item": "登录学工系统上传医保参保证明（关系到医保待遇）"}}

示例 2：
来源：402开黑小分队
消息：小刘：救命啊急急急！速来上号！四缺一！@Star 马上进房间！
研判输出：
{{"urgency": "IGNORE", "summary": "室友催促上号开黑", "action_item": null}}

【输出约束】：
请直接输出纯 JSON，严禁输出任何解释、前后缀或 Markdown 标记：
{{"urgency": "URGENT|DEFERRED|DIGEST|IGNORE", "summary": "一句话核心要点（20字以内）", "action_item": "清晰具体的待办行动/截止时间，无待办则填 null"}}
"""

    user_content = f"来源：{source_name}\n合并消息内容：\n{combined_text}"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
    }

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(api_url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            res_json = resp.json()
            content = res_json["choices"][0]["message"]["content"]
            cleaned = _clean_json_response(content)
            data = json.loads(cleaned)

            urgency = data.get("urgency", "IGNORE").upper()
            if urgency not in ("URGENT", "DEFERRED", "DIGEST", "IGNORE"):
                urgency = "DEFERRED"

            return {
                "urgency": urgency,
                "summary": str(data.get("summary") or "通知事项"),
                "action_item": data.get("action_item"),
            }
        except Exception as e:
            logger.warning(f"调用 LLM 接口失败 (第 {attempt + 1}/{max_retries + 1} 次): {e}")
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
            else:
                logger.error(f"调用 LLM 达到最大重试次数，降级为 DEFERRED 兜底: {e}")
                # 网络失败兜底策略：如果是重要来源，保留一条兜底记录
                return {
                    "urgency": "DEFERRED",
                    "summary": f"[{source_name}] 通知研判超时，请自行核对",
                    "action_item": "打开 QQ 查看该会话最新消息",
                }


def generate_daily_report_md(
    records: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """
    根据今日所有记录，调用大模型生成企业微信专用 Markdown 晚报
    :param records: 今日通知记录列表
    :param config: 可选配置字典
    :return: 符合企业微信 Markdown 子集规范的晚报文本
    """
    if not records:
        return ""

    cfg = _load_config_data(config)
    llm_cfg = cfg.get("llm", {})
    api_url = llm_cfg.get("api_url", "https://api.deepseek.com/chat/completions")
    api_key = llm_cfg.get("api_key", "")
    model = llm_cfg.get("model", "deepseek-chat")
    timeout = llm_cfg.get("timeout", 45)

    today_str = datetime.now().strftime("%Y-%m-%d %A")

    # 精简记录格式发给大模型
    records_text = []
    for r in records:
        records_text.append(
            f"- [{r.get('created_at', '')}] 来源: {r.get('source')} | 级别: {r.get('urgency')} | 摘要: {r.get('summary')} | 待办: {r.get('action_item') or '无'}"
        )
    combined_records = "\n".join(records_text)

    system_prompt = f"""你是一名高效的行政秘书。请将主人今天收到的所有 QQ 重要通知记录整理为一份《今日QQ晚报》。
报告日期：{today_str}

【企业微信 Markdown 格式硬性限制（极为重要）】：
- 仅允许使用 `###` 三级标题（严禁使用 `#` 或 `##` 标题）。
- 仅允许使用 `**加粗**`、`>` 引用文本、`-` 无序列表项。
- 颜色标签仅支持三种：
  - 待办/预警高亮：`<font color="warning">内容</font>`
  - 备注/时间灰色：`<font color="comment">内容</font>`
  - 普通信息蓝色：`<font color="info">内容</font>`
- 严禁输出 Markdown 表格（|---| 语法在企微卡片中无法渲染）！
- 对同一群聊/同一事件自动去重归纳，禁止冗余复读。

【排版板块结构要求】：
### 📋 今日QQ晚报 ({today_str})
> 今日共汇集 {len(records)} 条重要通知与动态

### 🔥 待办与截止日期 (DDL)
(按时间紧迫度列出具体待办，使用 <font color="warning">待办内容</font> 突出标明；若无待办则写“- 暂无未完成待办，今天棒棒哒！”)

### 📌 重要通知回顾
(按来源分类汇总今天发生的关键通知，使用 - 列出重点摘要与来源)

### 💡 群内动态简报
(精简概括关键群聊的重要信息交流或共享资料)
"""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"今日有效记录如下：\n{combined_records}"},
        ],
        "temperature": 0.3,
    }

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(api_url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            res_json = resp.json()
            return res_json["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"生成晚报调用 LLM 失败 (第 {attempt + 1}/{max_retries + 1} 次): {e}")
            if attempt < max_retries:
                time.sleep(2 * (attempt + 1))
            else:
                logger.error(f"生成晚报失败，返回本地备用简报模板: {e}")
                # 本地降级简报
                lines = [
                    f"### 📋 今日QQ晚报 ({today_str})",
                    f"> 今日共记录 {len(records)} 条消息（LLM生成超时，采用本地离线汇总）",
                    "### 🔥 待办与截止日期 (DDL)",
                ]
                has_action = False
                for r in records:
                    if r.get("action_item"):
                        has_action = True
                        lines.append(
                            f"- [{r.get('source')}] <font color=\"warning\">{r.get('action_item')}</font> (<font color=\"comment\">{r.get('summary')}</font>)"
                        )
                if not has_action:
                    lines.append("- 今日无待办记录")

                lines.append("### 📌 重要通知回顾")
                for r in records:
                    lines.append(f"- **{r.get('source')}**: {r.get('summary')}")
                return "\n".join(lines)
    return ""
