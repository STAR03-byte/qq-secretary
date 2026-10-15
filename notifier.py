"""
消息推送模块 (notifier.py)
默认支持飞书 (Feishu / Lark) 自定义机器人互动卡片推送，兼容企业微信 Webhook。
具备超时控制与最多 2 次重试机制。
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional
import requests

logger = logging.getLogger(__name__)


def _get_webhook_url(webhook_url: Optional[str] = None) -> str:
    """获取 Webhook URL，优先从 feishu 读取，兼容 wecom"""
    if webhook_url:
        return webhook_url
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
            # 优先读取 feishu，兜底兼容 wecom
            return (
                cfg.get("feishu", {}).get("webhook_url")
                or cfg.get("wecom", {}).get("webhook_url", "")
            )
    except Exception as e:
        logger.warning(f"读取 config.json 中的 webhook_url 失败: {e}")
        return ""


def send_card_message(
    title: str,
    content_md: str,
    template: str = "blue",
    webhook_url: Optional[str] = None,
) -> bool:
    """
    底层统一发送卡片消息，自动识别飞书与企业微信协议
    :param title: 卡片标题
    :param content_md: Markdown 正文
    :param template: 卡片主题色 (red, orange, blue, green 等)
    :param webhook_url: 可选的 Webhook 地址
    :return: 是否发送成功
    """
    url = _get_webhook_url(webhook_url)
    if not url:
        logger.error("未配置 Webhook URL，放弃推送")
        return False

    is_feishu = "feishu.cn" in url or "larksuite.com" in url

    if is_feishu:
        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": title,
                    },
                    "template": template,
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": content_md,
                        },
                    }
                ],
            },
        }
    else:
        # 企业微信 msgtype: markdown 兼容降级
        full_content = f"### {title}\n{content_md}"
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": full_content
            },
        }

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            res_data = resp.json()

            # 飞书通常返回 {"StatusCode": 0} 或 {"code": 0}，企微返回 {"errcode": 0}
            if (
                res_data.get("StatusCode") == 0
                or res_data.get("code") == 0
                or res_data.get("errcode") == 0
            ):
                logger.info(f"卡片推送成功 [{title}]")
                return True
            else:
                logger.warning(
                    f"机器人接口返回异常: {res_data} (第 {attempt + 1}/{max_retries + 1} 次)"
                )
        except Exception as e:
            logger.warning(
                f"调用 Webhook 网络异常 (第 {attempt + 1}/{max_retries + 1} 次): {e}"
            )

        if attempt < max_retries:
            time.sleep(1.0 * (attempt + 1))

    logger.error(f"消息 [{title}] 发送达到最大重试上限，推送失败")
    return False


def push_urgent_card(
    source: str,
    summary: str,
    action_item: Optional[str],
    raw_text: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    1. 紧急通知卡片推送 (红色主题)
    """
    snippet = raw_text.strip()
    if len(snippet) > 200:
        snippet = snippet[:200] + "..."
    snippet = snippet.replace("\n", " ")

    lines = [
        f"**来源**：{source}",
        f"**核心摘要**：{summary}",
    ]
    if action_item:
        lines.append(f"**立即行动**：<font color='red'>**{action_item}**</font>")

    lines.append("\n---")
    lines.append(f"**原始通知片段**：\n> {snippet}")

    content_md = "\n".join(lines)
    return send_card_message(
        title="🚨【紧急通知】需立即关注",
        content_md=content_md,
        template="red",
        webhook_url=webhook_url,
    )


def push_deferred_batch_card(
    rows: List[Dict[str, Any]],
    trigger_reason: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    2. 重要通知阶段汇总卡片推送 (橙色主题)
    """
    if not rows:
        return True

    lines = [
        f"本批次共汇集 **{len(rows)}** 条重要事项：",
        "",
    ]
    for idx, r in enumerate(rows, 1):
        source = r.get("source", "未知来源")
        summary = r.get("summary", "无摘要")
        action = r.get("action_item")

        item = f"{idx}. **[{source}]** {summary}"
        if action:
            item += f" -> <font color='orange'>待办: {action}</font>"
        lines.append(item)

    lines.append("\n---")
    lines.append("💡 *提示：可打开 QQ 查阅完整上下文或在亮屏解锁时处理。*")

    content_md = "\n".join(lines)
    return send_card_message(
        title=f"📬【重要通知阶段汇总】({trigger_reason})",
        content_md=content_md,
        template="orange",
        webhook_url=webhook_url,
    )


def push_daily_report_card(
    report_md: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    3. 全局晚报卡片推送 (蓝色主题)
    """
    if not report_md or not report_md.strip():
        report_md = (
            "> 今日状态：**清爽无干扰**\n\n"
            "🍵 今天没有产生积攒的重要通知与紧急待办，身心放松，好好休息，明天继续加油！"
        )

    return send_card_message(
        title="📋【今日QQ晚报】",
        content_md=report_md,
        template="blue",
        webhook_url=webhook_url,
    )
