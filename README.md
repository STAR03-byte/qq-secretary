# 🤖 QQ智能小秘书 (`qq-secretary`)

专为**安卓手机 Termux 本地环境**量身打造的智能通知管理中枢。  
通过本地 HTTP 接收开源软件 `SmsForwarder（短信转发器）` 捕获的 QQ 状态栏通知，基于大模型（兼容 OpenAI 规范，如 DeepSeek / Qwen / 通义千问等）进行多会话防抖合并、噪音剥离与时效性研判，最后通过**企业微信群机器人（WeCom Webhook）**分级精准推送。

---

## ✨ 核心特性

- **极简无编译依赖（Termux 友好）**：仅需 `flask` 与 `requests`，其余全部采用 Python 标准库（`sqlite3`、`threading` 等），无需任何 Rust/C++ 编译。
- **智能噪音剥离（防漏报）**：即使 90% 是群聊吹水打屁，只要夹杂了 1 条导员紧急通知或任务，精准剔除闲聊，提炼核心 DDL。
- **语境辨伪（防误报）**：自动识别“急急急来开黑”、“拼单救命”等开黑调侃玩梗，绝不误推打扰。
- **防抖合并缓冲池**：按会话标题自动聚合连续刷屏消息（默认 35 秒静默或 90 秒最大超时），大幅节省大模型 Token 与推送频次。
- **三级智能分流推送**：
  - 🚨 **`URGENT`（紧急通知）**：立即触发红标预警卡片直达企业微信。
  - 📬 **`DEFERRED`（重要不紧急）**：满 4 条定量或每日固定时间点（12:15, 18:15）批量汇总推送。
  - 📋 **`DIGEST`（群内简报）**：每晚 22:30 自动生成结构化《今日QQ晚报》。
- **主动唤醒机制**：手机解锁亮屏时，可通过快捷指令调用 `/flush_now` 立即刷出积压通知。

---

## 🏗️ 系统架构图

```
┌─────────────────────────┐
│   安卓系统通知栏 (QQ)    │
└────────────┬────────────┘
             │ 捕获状态栏通知
             ▼
┌─────────────────────────┐
│  SmsForwarder (短信转发器)│
└────────────┬────────────┘
             │ HTTP POST 127.0.0.1:8000/qq_notify
             ▼
┌─────────────────────────────────────────────────────────────┐
│                   QQ智能小秘书 (Termux 常驻)                  │
│                                                             │
│  [1. 标题清洗] 移除 "(3条新消息)" 后缀                        │
│  [2. 规则初筛] 黑名单 (后台运行/语音通话) & 强制放行高优词      │
│  [3. 内存缓冲池] 会话防抖合并 (35s 静默 / 90s 超时)           │
│                                                             │
│  [4. LLM 研判中枢] (DeepSeek / Qwen API)                     │
│      ├── 噪音剥离 & 紧迫度推算                                │
│      └── 输出结构化 JSON: {"urgency", "summary", "action"}  │
│                                                             │
│  [5. 本地 SQLite 持久化] (qq_secretary.db)                  │
└────────────┬────────────────────────────┬───────────────────┘
             │ 紧急通知立即推送            │ 定量(4条) / 定时(晚报)
             ▼                            ▼
┌─────────────────────────────────────────────────────────────┐
│                 企业微信群机器人 (WeCom Webhook)              │
│       🚨【紧急通知】        📬【阶段汇总】       📋【今日晚报】 │
└─────────────────────────────────────────────────────────────┘
```

---

## 📱 安卓手机端前置配置教程

### 步骤一：QQ 静音通知设置（还你清净，后台照常工作）
为了避免手机持续响铃震动打扰，同时确保短信转发器能抓到通知：
1. 打开手机【系统设置】->【应用管理】->【QQ】->【通知管理】。
2. 找到【群消息通知】或【消息通知】：
   - 设为 **静默通知 / 允许状态栏显示**。
   - 关闭 **响铃** 与 **振动**。
   - 开启 **锁屏通知 / 状态栏通知**（短信转发器需要读取状态栏）。

---

### 步骤二：SmsForwarder（短信转发器）配置

> 短信转发器是一款开源免费的安卓通知监听与转发利器（GitHub: [pppscn/SmsForwarder](https://github.com/pppscn/SmsForwarder)）。

#### 1. 授予权限
- 打开 SmsForwarder，按照提示开启 **通知监听权限**、**忽略电池优化（无限制后台）**、**开机自启动**。

#### 2. 添加发送通道（Webhook）
1. 进入【发送通道】-> 点击右下角 `+` -> 选择 **Webhook**。
2. 配置参数：
   - **通道名称**：`本地QQ小秘书`
   - **WebHook 地址 (URL)**：`http://127.0.0.1:8000/qq_notify`
   - **请求方式 (Method)**：`POST`
   - **请求头 (Headers)**：
     ```json
     {"Content-Type": "application/json;charset=UTF-8"}
     ```
   - **请求内容 (Body)**：
     ```json
     {
       "pkg": "[package_name]",
       "title": "[title]",
       "msg": "[msg]"
     }
     ```
3. 点击保存。

#### 3. 添加转发规则
1. 进入【转发规则】-> 点击右下角 `+` -> 选择 **应用通知**。
2. 配置参数：
   - **匹配模式**：包名匹配（`package_name`）
   - **匹配字段**：`com.tencent.mobileqq`（如使用 TIM 则填 `com.tencent.tim`）
   - **发送通道**：勾选刚刚创建的 `本地QQ小秘书`
3. 保存并开启该规则。

---

## 🚀 Termux 本地安装与常驻运行

### 1. 准备 Termux 环境
在安卓手机上打开 Termux（建议使用 F-Droid 版本的 Termux）：
```bash
# 1. 更新软件包并安装 Python、Git、OpenSSH
pkg update && pkg upgrade -y
pkg install python git openssh -y

# 2. 申请 Termux 唤醒锁（防止系统休眠导致脚本暂停）
termux-wake-lock
```

### 2. 获取代码与安装依赖
```bash
# 克隆仓库
git clone https://github.com/STAR03-byte/qq-secretary.git
cd qq-secretary

# 安装仅有的两个极简依赖
pip install -r requirements.txt
```

### 3. 配置参数 (`config.json`)
首次运行若没有 `config.json` 会自动从模板生成，您也可以直接编辑：
```bash
cp config.example.json config.json
nano config.json
```
修改关键配置：
- `llm.api_key`：您的大模型 API 密钥（如 DeepSeek、SiliconFlow 等）。
- `wecom.webhook_url`：企业微信机器人的 Webhook 地址。
- `user_profile`：主人姓名、身份以及关注偏好。

### 4. 启动与常驻后台
```bash
# 后台常驻运行
nohup python main.py > qq_secretary.log 2>&1 &

# 查看运行状态
curl http://127.0.0.1:8000/health

# 实时查看日志
tail -f qq_secretary.log

# 停止服务
kill $(pgrep -f "python main.py")
```

---

## 💻 电脑端一键部署（可选）

如果手机已开启 USB 调试或无线调试，在电脑端执行：
```bash
./deploy_to_termux.sh
```
脚本支持通过 **ADB** 或 **SCP** 自动推送代码至手机。

---

## 🧪 自动化测试验证

本套代码内置了完善的双模式测试工具：

### 1. 离线自动化测试套件（无需联网，Mock LLM 与企微）
```bash
python test_suite.py --offline
```
验证涵盖：
- 标题清洗规则（各种括号与新消息后缀过滤）
- 系统黑名单拦截与高优词强制放行
- 会话防抖合并与连续重复消息去重
- SQLite 多线程并发读写与防死锁
- 4条定量积压自动汇总触发
- `/flush_now` 与 `/health` 接口测试

### 2. 真实在线仿真测试（连通真实 LLM 与企微卡片）
在 `config.json` 中配置好真实的 `api_key` 与 `webhook_url` 后执行：
```bash
python test_suite.py --live
```
将自动模拟 4 组真实群聊消息（水群夹杂急事、假急开黑、常规作业、技术分享）并生成今日晚报，手机企微可直接收到排版精美的卡片！

---

## 🔌 API 接口文档

| 接口路径 | 请求方法 | 说明 | 示例参数 / 返回 |
| :--- | :--- | :--- | :--- |
| `/qq_notify` | `POST` | 接收 SmsForwarder 推送的通知 | `{"pkg": "com.tencent.mobileqq", "title": "班级群(3条新消息)", "msg": "@所有人 下午开会"}` |
| `/flush_now` | `POST` | 外部快捷指令/亮屏主动唤醒拉取 | 返回 `{"status": "ok", "flushed_sessions_count": 1, "pushed_deferred_count": 2}` |
| `/health` | `GET` | 服务健康检查与今日消息统计 | 返回缓冲池会话数与今日通知处理汇总 |

---

## 📄 开源许可证
本项目遵循 MIT 开源许可证。
