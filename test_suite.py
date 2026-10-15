"""
全链路测试套件与模拟器 (test_suite.py)
支持两种模式：
  --offline: (默认) Mock LLM 与企业微信接口，自动化验证标题清洗、黑白名单过滤、防抖缓冲、SQLite并发读写、定量触发、/flush_now 与 /health。
  --live: 读取本地真实 config.json，调用真实大模型与企微 Webhook，发送 4 组仿真消息并触发晚报。
"""

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch

# 引入项目各模块
import analyzer
import db
import main
import notifier


class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def assert_true(self, condition: bool, test_name: str, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"  ✅ [PASS] {test_name}")
        else:
            self.failed += 1
            msg = f"  ❌ [FAIL] {test_name}: {detail}"
            print(msg)
            self.errors.append(msg)

    def assert_equal(self, actual: Any, expected: Any, test_name: str):
        if actual == expected:
            self.passed += 1
            print(f"  ✅ [PASS] {test_name}")
        else:
            self.failed += 1
            msg = f"  ❌ [FAIL] {test_name} (期望: {expected}, 实际: {actual})"
            print(msg)
            self.errors.append(msg)


def run_offline_tests() -> bool:
    """运行全套离线自检测试"""
    print("\n" + "=" * 60)
    print("🚀 开始运行离线自动化测试套件 (--offline)")
    print("=" * 60)

    db.init_db()
    result = TestResult()

    # 1. 标题清洗测试
    print("\n[1/7] 测试标题清洗逻辑 (clean_session_title)")
    cases = [
        ("计算机2101班级群(12条新消息)", "计算机2101班级群"),
        ("项目讨论组（3条新消息）", "项目讨论组"),
        ("软件工程群 [5条]", "软件工程群"),
        ("ACM打卡小队【99+条新消息】", "ACM打卡小队"),
        ("辅导员张老师", "辅导员张老师"),
        ("  带空格标题 (1条)  ", "带空格标题"),
        ("", "未知会话"),
    ]
    for raw, expected in cases:
        cleaned = main.clean_session_title(raw)
        result.assert_equal(cleaned, expected, f"清洗: '{raw}' -> '{expected}'")

    # 2. 过滤规则测试
    print("\n[2/7] 测试过滤规则逻辑 (should_filter_message)")
    test_rules = {
        "ignore_titles": ["QQ正在后台运行", "语音通话"],
        "force_keywords": ["@全体成员", "紧急"],
        "watch_keywords": ["班级", "工作"],
    }
    # 命中黑名单
    filtered, reason = main.should_filter_message("QQ正在后台运行", "QQ正在后台运行", "测试", test_rules)
    result.assert_true(filtered, "黑名单命中过滤", reason)

    # 命中强制放行词（即使不在白名单中）
    filtered, reason = main.should_filter_message("路人甲", "路人甲", "@全体成员 开会了", test_rules)
    result.assert_true(not filtered, "高优词强制放行", reason)

    # 白名单匹配成功
    filtered, reason = main.should_filter_message("班级生活群", "班级生活群", "下午有课", test_rules)
    result.assert_true(not filtered, "白名单匹配放行", reason)

    # 白名单未匹配拦截
    filtered, reason = main.should_filter_message("王者荣耀开黑群", "王者荣耀开黑群", "来人双排", test_rules)
    result.assert_true(filtered, "非白名单群过滤拦截", reason)

    # 3. 内存缓冲池防抖与去重测试
    print("\n[3/7] 测试会话缓冲池防抖与连续去重 (add_to_buffer)")
    with main._buffer_lock:
        main._session_buffer.clear()

    added1 = main.add_to_buffer("测试会话1", "第一条消息")
    result.assert_true(added1, "首次消息成功加入缓冲")

    added_dup = main.add_to_buffer("测试会话1", "第一条消息")
    result.assert_true(not added_dup, "连续完全相同消息成功去重过滤")

    added2 = main.add_to_buffer("测试会话1", "第二条不同的消息")
    result.assert_true(added2, "新消息成功追加至同一会话")

    # 检查弹出超时会话
    expired = main.pop_expired_sessions(buffer_seconds=0, max_buffer_seconds=0, force_all=True)
    result.assert_equal(len(expired), 1, "force_all 成功弹出全部超时会话")
    result.assert_equal(len(expired[0][1]), 2, "弹出会话包含正确的 2 条合并消息")

    # 4. SQLite 并发与 CRUD 测试
    print("\n[4/7] 测试 SQLite 数据库与多线程安全 (db.py)")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        test_db_path = tmp_db.name

    try:
        db.init_db(test_db_path)
        # 测试单条插入
        row_id = db.insert_notification(
            source="测试班群",
            urgency="DEFERRED",
            summary="周五提交实验报告",
            action_item="周五前完成实验",
            raw_text="详细说明...",
            is_pushed=0,
            db_path=test_db_path,
        )
        result.assert_true(row_id > 0, "插入单条 DEFERRED 记录")

        # 测试获取未推送
        unpushed = db.get_unpushed_deferred(test_db_path)
        result.assert_equal(len(unpushed), 1, "查询到 1 条未推送 DEFERRED 记录")

        # 测试标记已推送
        db.mark_deferred_pushed([row_id], test_db_path)
        unpushed_after = db.get_unpushed_deferred(test_db_path)
        result.assert_equal(len(unpushed_after), 0, "标记推送后未推送记录清零")

        # 多线程并发写入压力测试
        threads = []
        def concurrent_writer(thread_idx: int):
            for i in range(10):
                db.insert_notification(
                    source=f"线程_{thread_idx}",
                    urgency="DIGEST",
                    summary=f"消息_{i}",
                    action_item=None,
                    raw_text=f"正文_{i}",
                    is_pushed=0,
                    db_path=test_db_path,
                )

        for t_idx in range(5):
            t = threading.Thread(target=concurrent_writer, args=(t_idx,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        today_records = db.get_today_notifications(test_db_path)
        # 1 条初始记录 + 5*10 = 51 条记录
        result.assert_equal(len(today_records), 51, "多线程并发写入 50 条记录无死锁且数据完整")

    finally:
        if os.path.exists(test_db_path):
            os.remove(test_db_path)

    # 5. Flask /qq_notify 接口端到端测试
    print("\n[5/7] 测试 Flask /qq_notify 路由接口")
    client = main.app.test_client()

    # 发送系统通知（应过滤）
    resp = client.post(
        "/qq_notify",
        json={"title": "QQ正在后台运行", "msg": "正在运行中"},
    )
    result.assert_equal(resp.status_code, 200, "黑名单通知请求返回 200")
    result.assert_equal(resp.get_json().get("status"), "filtered", "黑名单通知状态为 filtered")

    # 发送正常有效通知（应缓冲）
    with main._buffer_lock:
        main._session_buffer.clear()

    resp = client.post(
        "/qq_notify",
        json={"title": "导师课题组(3条新消息)", "msg": "@所有人 下午三点组会"},
    )
    result.assert_equal(resp.status_code, 200, "有效通知请求返回 200")
    data = resp.get_json()
    result.assert_equal(data.get("status"), "buffered", "有效通知状态为 buffered")
    result.assert_equal(data.get("session"), "导师课题组", "标题清洗为 '导师课题组'")

    # 6. 定量 4 条自动汇总推送测试 (Mock)
    print("\n[6/7] 测试定量积压自动汇总触发机制 (4条阈值)")
    with patch("notifier.push_deferred_batch_card") as mock_push_deferred, \
         patch("analyzer.analyze_messages") as mock_analyze:

        mock_analyze.return_value = {
            "urgency": "DEFERRED",
            "summary": "自动测试重要事项",
            "action_item": "下周一前提交",
        }
        mock_push_deferred.return_value = True

        test_config = {
            "schedule": {"deferred_max_count": 4},
        }

        # 模拟产生 4 个不同会话的重要不紧急消息
        for i in range(4):
            main.process_session_messages(f"模拟群聊_{i}", [f"重要消息内容_{i}"], test_config)

        # 检查 notifier.push_deferred_batch_card 是否被自动调用
        result.assert_true(mock_push_deferred.called, "积攒满 4 条自动触发了 push_deferred_batch_card")

    # 7. /flush_now 与 /health 接口测试
    print("\n[7/7] 测试 /flush_now 与 /health 接口")
    with patch("notifier.push_deferred_batch_card") as mock_push_deferred, \
         patch("analyzer.analyze_messages") as mock_analyze:

        mock_analyze.return_value = {
            "urgency": "DEFERRED",
            "summary": "待刷新通知",
            "action_item": "明天处理",
        }

        # 往缓冲池添加会话
        main.add_to_buffer("待刷新会话", "有一条新任务")

        # 调用 /flush_now
        resp_flush = client.post("/flush_now")
        result.assert_equal(resp_flush.status_code, 200, "/flush_now 返回 200")
        flush_data = resp_flush.get_json()
        result.assert_true(flush_data.get("flushed_sessions_count") >= 1, "成功强制刷出会话")

        # 调用 /health
        resp_health = client.get("/health")
        result.assert_equal(resp_health.status_code, 200, "/health 返回 200")
        health_data = resp_health.get_json()
        result.assert_equal(health_data.get("status"), "running", "健康检查状态为 running")

    # 汇总输出
    print("\n" + "=" * 60)
    print(f"📊 测试总结: 通过: {result.passed} 个 | 失败: {result.failed} 个")
    if result.failed == 0:
        print("🎉 恭喜！所有离线自动化测试用例 100% 全部通过！")
        print("=" * 60)
        return True
    else:
        print("🚨 以下测试用例存在失败：")
        for err in result.errors:
            print(f"   - {err}")
        print("=" * 60)
        return False


def run_live_tests():
    """运行真实在线仿真测试（调用真实大模型和企业微信）"""
    print("\n" + "=" * 60)
    print("🌐 开始运行真实环境仿真测试 (--live)")
    print("=" * 60)

    config = main.load_config()
    llm_key = config.get("llm", {}).get("api_key", "")
    wecom_url = config.get("wecom", {}).get("webhook_url", "")

    if not llm_key or "YOUR" in llm_key:
        print("⚠️ 警告: config.json 中未配置有效的大模型 api_key，真实请求可能失败！")
    if not wecom_url or "YOUR" in wecom_url:
        print("⚠️ 警告: config.json 中未配置有效的企业微信 webhook_url，推送将跳过或报错！")

    client = main.app.test_client()

    simulated_messages = [
        {
            "name": "测试案例1: 真实紧急通知（水群闲聊夹杂急事）",
            "title": "计算机学院卓越班(15条新消息)",
            "msg": "张三：中午吃螺蛳粉吗？\n李四：臭死了不去。\n王老师(导员)：@全体成员 紧急通知！请所有同学在今天下午 17:00 前将学籍核对表签字扫描件发到我邮箱，逾期将影响本学期选课！\n赵六：收到。\n钱七：收到老师。",
            "expected": "URGENT",
        },
        {
            "name": "测试案例2: 语境辨伪（室友喊急急急开黑）",
            "title": "402男寝室",
            "msg": "老王：兄弟们急急急！速来开黑！上分车队四缺一！\n老李：马上来！\n老王：@Star 赶紧上号，救命啊掉段了！",
            "expected": "IGNORE",
        },
        {
            "name": "测试案例3: 重要不紧急（常规实验作业通知）",
            "title": "编译原理课程群",
            "msg": "助教：@全体成员 实验三词法分析器已发布在教学网站，截止提交时间为本周日晚上 23:59，请同学们合理安排时间。",
            "expected": "DEFERRED",
        },
        {
            "name": "测试案例4: 群动态简报（技术分享与讨论）",
            "title": "Linux技术交流群",
            "msg": "群友A：DeepSeek 刚刚发布了新的推理架构白皮书，优化了长上下文的注意力计算机制，非常值得一看。\n群友B：链接发一下拜托。\n群友A：https://github.com/deepseek-ai/example",
            "expected": "DIGEST",
        },
    ]

    for item in simulated_messages:
        print(f"\n📨 模拟发送: {item['name']}")
        print(f"   标题: {item['title']}")
        print(f"   预览: {item['msg'][:60]}...")

        # 发送到 /qq_notify
        resp = client.post("/qq_notify", json={"title": item["title"], "msg": item["msg"]})
        print(f"   /qq_notify 响应: {resp.get_json()}")

    print("\n⏳ 正在主动调用 /flush_now 立即研判并触发推送...")
    resp_flush = client.post("/flush_now")
    print(f"   /flush_now 响应: {resp_flush.get_json()}")

    print("\n📋 正在生成今日晚报测试卡片...")
    today_records = db.get_today_notifications()
    print(f"   今日已入库记录数: {len(today_records)}")
    report_md = analyzer.generate_daily_report_md(today_records, config=config)
    print("\n--- 晚报 Markdown 内容预览 ---")
    print(report_md)
    print("----------------------------")

    push_ok = notifier.push_daily_report_card(report_md)
    print(f"   晚报卡片企微推送结果: {'成功' if push_ok else '失败 (请检查 webhook 地址)'}")
    print("\n🎉 在线仿真流程执行完毕！")


def main_entry():
    parser = argparse.ArgumentParser(description="QQ智能小秘书测试套件")
    parser.add_argument("--offline", action="store_true", default=False, help="运行离线全套自动化测试 (默认)")
    parser.add_argument("--live", action="store_true", default=False, help="运行真实环境仿真测试")

    args = parser.parse_args()

    # 默认模式为 --offline
    if not args.live:
        success = run_offline_tests()
        sys.exit(0 if success else 1)
    else:
        run_live_tests()


if __name__ == "__main__":
    main_entry()
