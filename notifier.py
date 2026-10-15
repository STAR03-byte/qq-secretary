"""
企业微信 Webhook 推送模块 (notifier.py)
封装 msgtype: markdown 发送逻辑与重试机制，提供三大分级卡片渲染函数。
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional
import requests

logger = logging.getLogger(__name__)


def _get_webhook_url(webhook_url: Optional[str] = None) -> str:
    """获取企业微信 Webhook URL"""
    if webhook_url:
        return webhook_url
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
            return cfg.get("wecom", {}).get("webhook_url", "")
    except Exception as e:
        logger.warning(f"读取 config.json 中的 wecom.webhook_url 失败: {e}")
        return ""


def send_wecom_markdown(content: str, webhook_url: Optional[str] = None) -> bool:
    """
    底层统一发送企业微信 Markdown 消息，包含超时与最大重试机制
    :param content: markdown 文本内容（符合企业微信支持的语法子集）
    :param webhook_url: 可选的 Webhook 机器人地址
    :return: 是否发送成功
    """
    url = _get_webhook_url(webhook_url)
    if not url:
        logger.error("未配置企业微信 Webhook URL，放弃推送")
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": content
        }
    }

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            res_data = resp.json()
            if res_data.get("errcode") == 0:
                logger.info("企业微信卡片推送成功")
                return True
            else:
                logger.warning(f"企业微信接口返回异常: {res_data} (第 {attempt + 1}/{max_retries + 1} 次)")
        except Exception as e:
            logger.warning(f"调用企业微信 Webhook 网络异常 (第 {attempt + 1}/{max_retries + 1} 次): {e}")

        if attempt < max_retries:
            time.sleep(1.0 * (attempt + 1))

    logger.error("企业微信消息发送达到最大重试上限，推送失败")
    return False


def push_urgent_card(
    source: str,
    summary: str,
    action_item: Optional[str],
    raw_text: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    1. 紧急通知卡片推送
    """
    # 截取原始文本前 180 字符，防止企微卡片过长排版错乱
    snippet = raw_text.strip()
    if len(snippet) > 180:
        snippet = snippet[:180] + "..."
    snippet = snippet.replace("\n", " ")

    lines = [
        "### 🚨【紧急通知】需立即关注",
        f"> **来源**：<font color=\"info\">{source}</font>",
        f"> **核心摘要**：{summary}",
    ]

    if action_item:
        lines.append(f"> **立即行动**：<font color=\"warning\">{action_item}</font>")

    lines.append(f"\n> <font color=\"comment\">原始消息片段：{snippet}</font>")
    content = "\n".join(lines)
    return send_wecom_markdown(content, webhook_url=webhook_url)


def push_deferred_batch_card(
    rows: List[Dict[str, Any]],
    trigger_reason: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    2. 重要通知阶段汇总卡片推送
    """
    if not rows:
        return True

    lines = [
        f"### 📬【重要通知阶段汇总】({trigger_reason})",
        f"> 本批次共汇集 <font color=\"info\">{len(rows)}</font> 条重要事务：",
        "",
    ]

    for idx, r in enumerate(rows, 1):
        source = r.get("source", "未知来源")
        summary = r.get("summary", "无摘要")
        action = r.get("action_item")

        item_line = f"{idx}. **[{source}]** {summary}"
        if action:
            item_line += f" -> <font color=\"warning\">待办: {action}</font>"
        lines.append(item_line)

    lines.append("\n> <font color=\"comment\">提示：详细上下文可随时打开 QQ 查阅或在解锁时自动处理。</font>")
    content = "\n".join(lines)
    return send_wecom_markdown(content, webhook_url=webhook_url)


def push_daily_report_card(
    report_md: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    3. 全局晚报卡片推送（若无记录则推送温馨零通知卡片）
    """
    if not report_md or not report_md.strip():
        report_md = (
            "### 📋 今日QQ晚报\n"
            "> 今日状态：<font color=\"info\">清爽无干扰</font>\n\n"
            "🍵 今天没有产生积攒的重要通知与紧急待办，身心放松，好好休息，明天继续加油！"
        )
    return send_wecom_markdown(report_md, webhook_url=webhook_url)
