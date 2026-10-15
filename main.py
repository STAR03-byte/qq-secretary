"""
QQ智能小秘书 - Flask 主服务与后台调度器 (main.py)
接收 SmsForwarder Webhook 通知，进行标题清洗、过滤、防抖缓冲，
后台守护线程负责超时研判、定量汇总与定时晚报触发。
"""

import copy
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from flask import Flask, jsonify, request

import analyzer
import db
import notifier

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("qq_secretary")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json")


def load_config() -> Dict[str, Any]:
    """加载配置文件，若 config.json 不存在则自动从 config.example.json 复制"""
    if not os.path.exists(CONFIG_PATH) and os.path.exists(EXAMPLE_CONFIG_PATH):
        logger.info(f"config.json 不存在，自动从 {EXAMPLE_CONFIG_PATH} 复制生成")
        shutil.copyfile(EXAMPLE_CONFIG_PATH, CONFIG_PATH)

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"读取 config.json 失败: {e}")
        return {}


# 实例化 Flask 应用
app = Flask(__name__)

# 确保数据库表初始化
db.init_db()

# 会话防抖缓冲池与锁
# 数据结构: { session_title: {"messages": [str, ...], "first_recv": float, "last_recv": float} }
_buffer_lock = threading.Lock()
_session_buffer: Dict[str, Dict[str, Any]] = {}

# 定时任务防重记录集合: (YYYY-MM-DD, HH:MM, task_type)
_cron_history = set()


def clean_session_title(title: str) -> str:
    """
    清洗会话标题，去除安卓 QQ 通知末尾的 '(3条新消息)'、'[2条]'、'（99+条新消息）' 等后缀
    """
    if not title:
        return "未知会话"
    cleaned = title.strip()
    # 匹配各类中英文括号括起的条数提示
    cleaned = re.sub(
        r"[\(\[（【]\s*(?:\d+|\d+\+)\s*(?:条新消息|条消息|条)?\s*[\)\]）】]\s*$",
        "",
        cleaned,
    )
    return cleaned.strip() or "未知会话"


def should_filter_message(
    cleaned_title: str,
    raw_title: str,
    msg_text: str,
    filter_rules: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    检查消息是否应被过滤
    :return: (is_filtered, reason)
    """
    ignore_titles = filter_rules.get("ignore_titles", [])
    force_keywords = filter_rules.get("force_keywords", [])
    watch_keywords = filter_rules.get("watch_keywords", [])

    # 1. 检查黑名单系统通知标题
    for ign in ignore_titles:
        if ign and (ign in raw_title or ign in cleaned_title):
            return True, f"命中系统通知黑名单 [{ign}]"

    # 2. 检查高优强制放行词
    for kw in force_keywords:
        if kw and (kw in msg_text or kw in cleaned_title):
            return False, f"命中强制放行词 [{kw}]"

    # 3. 检查白名单关键词（如果配置了白名单且不为空）
    if watch_keywords and len(watch_keywords) > 0:
        matched = False
        for w in watch_keywords:
            if w and (w in cleaned_title or w in msg_text):
                matched = True
                break
        if not matched:
            return True, "未命中白名单关键词"

    return False, "通过规则检查"


def add_to_buffer(session_title: str, msg: str) -> bool:
    """
    将消息加入会话缓冲池（包含去重与防抖更新）
    :return: 是否为新加入消息 (False 代表连续完全重复已忽略)
    """
    now = time.time()
    with _buffer_lock:
        if session_title not in _session_buffer:
            _session_buffer[session_title] = {
                "messages": [msg],
                "first_recv": now,
                "last_recv": now,
            }
            logger.info(f"新建会话缓冲: [{session_title}] -> {msg[:30]}")
            return True
        else:
            session_data = _session_buffer[session_title]
            # 连续完全相同消息去重
            if session_data["messages"] and session_data["messages"][-1] == msg:
                logger.info(f"忽略连续重复消息: [{session_title}] -> {msg[:30]}")
                return False

            session_data["messages"].append(msg)
            session_data["last_recv"] = now
            logger.info(f"追加会话缓冲: [{session_title}] (当前共 {len(session_data['messages'])} 条)")
            return True


def pop_expired_sessions(
    buffer_seconds: int,
    max_buffer_seconds: int,
    force_all: bool = False,
) -> List[Tuple[str, List[str]]]:
    """
    弹出已达到防抖静默时间或最大等待时间的会话
    """
    now = time.time()
    popped: List[Tuple[str, List[str]]] = []

    with _buffer_lock:
        to_delete = []
        for session_title, data in _session_buffer.items():
            silent_duration = now - data["last_recv"]
            total_duration = now - data["first_recv"]

            if force_all or (silent_duration >= buffer_seconds) or (total_duration >= max_buffer_seconds):
                popped.append((session_title, data["messages"]))
                to_delete.append(session_title)

        for session_title in to_delete:
            del _session_buffer[session_title]

    return popped


def process_session_messages(session_title: str, messages: List[str], config: Dict[str, Any]) -> None:
    """
    在工作线程中调用 LLM 研判会话合并消息并根据级别落库/推送
    """
    if not messages:
        return

    combined_text = "\n".join(messages)
    logger.info(f"开始研判会话 [{session_title}]，合并条数: {len(messages)}")

    try:
        result = analyzer.analyze_messages(session_title, combined_text, config=config)
        urgency = result.get("urgency", "IGNORE")
        summary = result.get("summary", "")
        action_item = result.get("action_item")

        logger.info(f"会话 [{session_title}] 研判结果: {urgency} | 摘要: {summary} | 待办: {action_item}")

        if urgency == "URGENT":
            # 紧急通知：入库 (is_pushed=1) 并立刻推企微
            db.insert_notification(
                source=session_title,
                urgency=urgency,
                summary=summary,
                action_item=action_item,
                raw_text=combined_text,
                is_pushed=1,
            )
            notifier.push_urgent_card(session_title, summary, action_item, combined_text)

        elif urgency == "DEFERRED":
            # 重要不紧急：入库 (is_pushed=0)，并检查积压数量
            db.insert_notification(
                source=session_title,
                urgency=urgency,
                summary=summary,
                action_item=action_item,
                raw_text=combined_text,
                is_pushed=0,
            )
            # 检查未推送的 DEFERRED 数量
            unpushed = db.get_unpushed_deferred()
            deferred_max_count = config.get("schedule", {}).get("deferred_max_count", 4)
            if len(unpushed) >= deferred_max_count:
                logger.info(f"未推送重要通知达到阈值 ({len(unpushed)}/{deferred_max_count})，触发定量阶段汇总推送")
                notifier.push_deferred_batch_card(
                    unpushed,
                    trigger_reason=f"达到 {deferred_max_count} 条定量阈值",
                )
                db.mark_deferred_pushed([r["id"] for r in unpushed])

        elif urgency == "DIGEST":
            # 群动态简报：静默入库，留存待晚报汇总
            db.insert_notification(
                source=session_title,
                urgency=urgency,
                summary=summary,
                action_item=action_item,
                raw_text=combined_text,
                is_pushed=0,
            )

        elif urgency == "IGNORE":
            logger.info(f"会话 [{session_title}] 研判为 IGNORE，已安全忽略")

    except Exception as e:
        logger.error(f"处理会话 [{session_title}] 发生异常: {e}", exc_info=True)


def background_scheduler_loop():
    """
    后台守护线程轮询（每 5 秒执行一次）
    1. 扫描防抖缓冲池超时会话
    2. 检查定时汇总时间 (deferred_cron_times)
    3. 检查每日晚报时间 (daily_report_time)
    """
    logger.info("后台调度守护线程已启动 (轮询间隔 5 秒)")
    while True:
        try:
            config = load_config()
            schedule_cfg = config.get("schedule", {})
            buffer_seconds = schedule_cfg.get("buffer_seconds", 35)
            max_buffer_seconds = schedule_cfg.get("max_buffer_seconds", 90)
            deferred_cron_times = schedule_cfg.get("deferred_cron_times", ["12:15", "18:15"])
            daily_report_time = schedule_cfg.get("daily_report_time", "22:30")

            now_dt = datetime.now()
            today_str = now_dt.strftime("%Y-%m-%d")
            time_str = now_dt.strftime("%H:%M")

            # 1. 检查缓冲池超时会话
            expired_sessions = pop_expired_sessions(buffer_seconds, max_buffer_seconds)
            for session_title, msgs in expired_sessions:
                # 异步交给工作线程处理，避免阻塞调度器
                threading.Thread(
                    target=process_session_messages,
                    args=(session_title, msgs, config),
                    daemon=True,
                ).start()

            # 2. 检查定时阶段汇总时间 (deferred_cron_times)
            if time_str in deferred_cron_times:
                cron_key = (today_str, time_str, "deferred_cron")
                if cron_key not in _cron_history:
                    _cron_history.add(cron_key)
                    logger.info(f"命中定时汇总时间点 [{time_str}]，执行重要通知推送")
                    unpushed = db.get_unpushed_deferred()
                    if unpushed:
                        notifier.push_deferred_batch_card(
                            unpushed,
                            trigger_reason=f"定时汇总 {time_str}",
                        )
                        db.mark_deferred_pushed([r["id"] for r in unpushed])
                    else:
                        logger.info(f"[{time_str}] 当前无未推送的重要通知，跳过卡片发送")

            # 3. 检查每晚全局晚报时间 (daily_report_time)
            if time_str == daily_report_time:
                cron_key = (today_str, time_str, "daily_report")
                if cron_key not in _cron_history:
                    _cron_history.add(cron_key)
                    logger.info(f"命中每日晚报时间点 [{time_str}]，开始生成晚报")

                    # 先强制刷出所有缓冲池中的剩余消息进行研判
                    remaining_sessions = pop_expired_sessions(0, 0, force_all=True)
                    for session_title, msgs in remaining_sessions:
                        process_session_messages(session_title, msgs, config)

                    # 等待片刻保证落库后生成晚报
                    time.sleep(2)
                    today_records = db.get_today_notifications()
                    report_md = analyzer.generate_daily_report_md(today_records, config=config)
                    notifier.push_daily_report_card(report_md)

        except Exception as e:
            logger.error(f"后台调度守护线程轮询异常: {e}", exc_info=True)

        time.sleep(5)


# ===================== HTTP 路由接口 =====================


@app.route("/qq_notify", methods=["POST"])
def qq_notify():
    """
    接收 SmsForwarder Webhook 通知
    请求格式: {"pkg": "com.tencent.mobileqq", "title": "...", "msg": "..."}
    """
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"status": "error", "message": "无效的 JSON 数据"}), 400

        raw_title = str(data.get("title") or "")
        msg = str(data.get("msg") or data.get("content") or data.get("text") or "")

        if not raw_title and not msg:
            return jsonify({"status": "error", "message": "title 和 msg 均为空"}), 400

        # 清洗标题
        cleaned_title = clean_session_title(raw_title)

        # 规则过滤
        config = load_config()
        filter_rules = config.get("filter_rules", {})
        is_filtered, reason = should_filter_message(cleaned_title, raw_title, msg, filter_rules)

        if is_filtered:
            logger.info(f"消息已过滤 [{cleaned_title}]: {reason}")
            return jsonify({
                "status": "filtered",
                "reason": reason,
                "cleaned_title": cleaned_title,
            }), 200

        # 加入缓冲池
        is_added = add_to_buffer(cleaned_title, msg)
        status = "buffered" if is_added else "duplicate_ignored"

        return jsonify({
            "status": status,
            "session": cleaned_title,
            "buffered_messages": len(_session_buffer.get(cleaned_title, {}).get("messages", [])),
        }), 200

    except Exception as e:
        logger.error(f"/qq_notify 接口处理异常: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/flush_now", methods=["POST"])
def flush_now():
    """
    外部手动触发接口（例如手机解锁亮屏时主动拉取积攒的重要通知）
    立即清空缓冲池进行研判，并触发所有未推送的 DEFERRED 汇总
    """
    try:
        config = load_config()
        # 1. 弹出缓冲池中所有剩余未处理会话
        flushed_sessions = pop_expired_sessions(0, 0, force_all=True)
        for session_title, msgs in flushed_sessions:
            # 同步处理，确保接下来批量推送能涵盖这批消息
            process_session_messages(session_title, msgs, config)

        # 2. 获取所有未推送的 DEFERRED
        unpushed = db.get_unpushed_deferred()
        pushed_count = 0
        if unpushed:
            notifier.push_deferred_batch_card(unpushed, trigger_reason="主动唤醒拉取")
            db.mark_deferred_pushed([r["id"] for r in unpushed])
            pushed_count = len(unpushed)

        return jsonify({
            "status": "ok",
            "flushed_sessions_count": len(flushed_sessions),
            "pushed_deferred_count": pushed_count,
        }), 200

    except Exception as e:
        logger.error(f"/flush_now 发生异常: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    """
    健康检查与当前状态统计
    """
    try:
        stats = db.get_stats()
        with _buffer_lock:
            active_sessions = {k: len(v["messages"]) for k, v in _session_buffer.items()}

        return jsonify({
            "status": "running",
            "active_buffer_sessions": active_sessions,
            "stats": stats,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }), 200
    except Exception as e:
        logger.error(f"/health 发生异常: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


def start_service(host: str = "0.0.0.0", port: int = 8000, start_daemon: bool = True):
    """启动服务与后台守护线程"""
    db.init_db()

    if start_daemon:
        daemon_thread = threading.Thread(
            target=background_scheduler_loop,
            name="QQSecretaryScheduler",
            daemon=True,
        )
        daemon_thread.start()

    logger.info(f"QQ智能小秘书正在启动，监听地址: http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    start_service()
