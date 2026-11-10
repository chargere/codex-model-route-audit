# Codex 模型路由与 effort 档位排查复盘

> 一次关于「`gpt-6-astra` 是不是被静默降级到 4o」的完整排查记录。
> 时间跨度：2026-11-07 ~ 2026-11-10 ｜ 结论：**没有降智**。
> 本仓库收录全部方法、代码、证据链与踩坑记录，已脱敏，可离线复现自测。

---

## 0. 一句话结论

服务端**从来没有换过模型**。累计 **57 发 `gpt-6-astra → gpt-6-astra` MATCH + 8 发 `gpt-6-luna → gpt-6-luna` MATCH，零 SWAP**，证据来自上游 SSE 里服务端自报的 `response.model`，不是靠响应头猜的。

体感上的「变笨」另有三个真实来源，全是客户端侧：

| # | 现象 | 真实原因 | 性质 |
|---|---|---|---|
| 1 | 复杂题给短路答案 | `effort` 被 UI / 旧会话锁定在 `medium` 或 `low`，请求体里 `reasoning.effort` 一直是 medium，而客户端日志写 xhigh、`config.toml` 还被回写 | **请求侧实锤** |
| 2 | 老会话画啥都崩 | 254 turns / 144MB / 26MB input / 386 items 的超长会话，`auto_compact` 都在跑 | 上下文污染 |
| 3 | 成品总有小瑕疵 | SVG 手绘的局部贴片噪声（喙/喉囊分离色块、尾羽贴片、翅膀分界） | 恒定噪声，非信号 |

「冷号」「换 IP」「洗号」在这个案子里**边际收益为零**：账号是个人邮箱注册、IP 检测干净，而且服务端模型身份自始至终没变。

---

## 1. 起因与背景

用户的 Codex 客户端在使用 `gpt-6-astra` 时，感觉输出质量下降，怀疑被静默路由到较弱的模型（社区称「降智」）。社区流传的判据包括：

- 鹈鹕骑自行车 SVG 题的**首段措辞**：「内嵌 SVG / 内联 SVG」约 90% 疑似 4o；「循环 / 循环骑行 / 连续循环」社区称基本 100%；「踩踏 / 沿途风景 / 背景移动」偏满血侧。
- 糖果题标准答案 `21`（9 个圆形 + 12 个五角星，形状可手感分辨），答 `29` 属漏条件的盲抽短路。
- 响应头 `x-codex-safety-buffering-enabled` / `faster-model`、 `x-codex-primary-used-percent`、`x-codex-turn-state`。
- `reasoning_output_tokens` 聚集在 `516 / 1034 / 1552 / 2070`（差值 518）的「516 截断」。

这些判据噪声都很大。本次排查的目标不是再攒一轮主观印象，而是**拿到服务端实际执行模型的硬证据**。

### 前置工作

- 落地开源项目 [`NewBeeM4n/codex-overload-fix`](https://github.com/NewBeeM4n/codex-overload-fix)（本机 `github.com` 主站曾不可达，改用 `api.github.com/repos/.../zipball/main` + `git/trees/main?recursive=1` 校验完整性，再 `git init` 补成可用仓库）。
- 读通源码：上游把容量拒绝伪装成 HTTP 200 的 SSE `error` 事件，状态码重试层看不见；本地 relay 读 SSE 头识别 `server_is_overloaded` 后重发。
- 关键认识：该项目 README 明确写了「修复后不清楚是否会被路由到其他模型……响应里的 served 字段也可以打日志核对」。这正好是我们需要的观测点。

---

## 2. 方法：本地只读 served-model 探针

### 2.1 为什么必须自己抓

- Rollout 里的 `turn_context.model` / `thread_settings.model` / `payload.model` 全是**客户端设定值**，不等于服务端真实执行的模型。
- `logs_2.sqlite` 的 `feedback_log_body` 里 `model=xxx codex.turn.reasoning_effort=xxx` 也是 turn 级**请求侧**信息。
- 服务端实际执行的模型，本地通常只能从上游 SSE 的 `response.model` 或响应头 `openai-model` / `x-openai-model` 拿到。

### 2.2 设计约束（安全第一）

用户明确要求「只读监测，不启用、不发请求、不改配置、不碰冷号」。因此探针遵守：

1. **默认完全不启用**：不带 `--probe-log` 时 `Probe` 是彻底 no-op，vendored relay 与上游逐字节一致。
2. **纯旁路**：`probe.feed()` 只在 `wfile.write()` **之后**调用，观察的是已转发出去的字节副本 —— 探针不可能延迟或改变 Codex 收到的任何东西。
3. **不改请求**：不改请求体、不改响应、不重试、不改 Header、不改模型字段。唯一写文件是本地 `~/.codex/model_probe.log`。
4. **`--observe-only` 真·纯观测**：强制 1 次尝试、跳过整个 SSE 头分类/缓冲逻辑，拿到响应头立即直转，`prelude` 恒为 `b""`。自测证实 `resp.read_calls == 0` —— 连上游作者自己承认的那点「改变请求时序」残余都削掉了。代价是满载报错不再被自动吞掉，对短测试无所谓。

### 2.3 判据四态

| 判定 | 含义 |
|---|---|
| `MATCH` | 请求模型 == SSE `response.created` / `response.completed` 自报模型 |
| `SWAP` | 两者不一致（静默换模型） |
| `SERVED` | 只拿到服务端模型，无请求侧对照 |
| `NO_SIGNAL` | 无可用模型字段 |

同时记录 `mid_stream_mismatch`（流中途换模型）、`header_model`（`openai-model` 头）、`stream_bytes`、`x-oai-request-id`、`primary-used-percent`。

`x-codex-safety-buffering-enabled` / `faster-model` 在日志里明确标注为「**fallback 链资格声明，不是换模型证据**」；`x-codex-turn-state` 只存长度不存内容。

### 2.4 启用方式

`~/.codex/config.toml` 根级加一行（**`model_provider` 保持默认 `openai`**）：

```toml
openai_base_url = "http://127.0.0.1:8787"
```

启动（Windows，走本地梯子的 HTTP CONNECT 隧道）：

```bash
python -u relay/relay.py \
  --port 8787 --observe-only \
  --probe-log ~/.codex/model_probe.log \
  --log ~/.codex/relay-1110.log \
  --proxy http://127.0.0.1:<PROXY_PORT>
```

**收工顺序不能反**：先删 `config.toml` 那行 → 再停 relay。且**停 relay 前必须确认客户端已完全退出**，否则已运行进程仍缓存 `127.0.0.1:8787`，会报 `Connection failed: error sending request` + 反复 `Reconnecting`。

---

## 3. 时间线

| 时间 | 事件 |
|---|---|
| 11/07 12:52–13:33 | 请求侧 `gpt-6.1-sol` medium |
| 11/07 16:36–18:29 | `gpt-6-astra` medium→low |
| 11/07 23:44–11/08 00:23 | `gpt-6-astra` xhigh；「内嵌 SVG」首段措辞命中 |
| 11/08 00:53–01:00 | `gpt-6-astra` **low**（UI 把 xhigh 覆盖掉），画出鹈鹕骑车图 |
| 11/08 11:00 | 开始「冷号」 |
| 11/08 17:23 | 只读核查：11:00 后 0 行日志、0 个 session，冷号干净 |
| 11/08 18:09 | 探针写完、离线自测 30/30；未启用、未发请求、未改 config |
| 11/09 19:00–20:24 | 客户端启动类日志 1287 行（`message_processor` 674 / `custom_ca` 307 / `models_manager::cache`），`codex.turn` 等关键词全 0 —— 开过 APP 但**没发题**，冷号未断 |
| 11/10 12:34 | 复测启用：`--observe-only` + probe，经梯子隧道；备份 `config.toml.bak-1110` |
| 11/10 12:48 | 探活 2 发，全 200、MATCH；抓到 `effort=medium` 异常 |
| 11/10 13:05–13:11 | 旧会话 14 发：请求体全 `medium`，客户端日志却写 `xhigh`，config 被回写成 medium |
| 11/10 13:20 | 彻底退出 + 新建会话后，`effort=xhigh` 终于真生效；期间一次断流由 Codex 自带重试层 203ms 修复 |
| 11/10 13:35 | 39 发全 MATCH，零 SWAP |
| 11/10 13:38 | 「自行车骑鹈鹕」反转题，现场抓到 `reasoning_output_tokens = [0, 0, 516, 516]` |
| 11/10 13:43–13:47 | 「Sam Altman 骑鹈鹕」，reasoning 累积到 54,387，真人题材被通用化处理（安全策略，非降智） |
| 11/10 13:49 | 切回 11/03 的 144MB 老会话，3 发 `effort=low`、17MB 请求，画出结构性错误的鹈鹕骑车 |
| 11/10 14:01 / 14:07 | 两份 xhigh 新会话原题成品，结构完整（车座、逆运动学齐全） |
| 11/10 14:31 | 第三份 xhigh 成品，434 行 / 74 path，结构仍完整 |
| 11/10 14:45 | 收工：删 config 行、停 relay，验证复位 |

---

## 4. 证据链

### 4.1 模型身份：硬排除第二型

- 累计 **57 发 `astra→astra` MATCH + 8 发 `luna→luna` MATCH，零 SWAP**，`mid_stream_mismatch` 全 false。
- 上游未给 `openai-model` 头（`header_model=null`），served 来自 SSE 里服务端自报的 `response.created` / `response.completed`。
- `logs_2.sqlite` 中 `ModelReroute` 命中 **0** —— 三路互证无 mismatch。
- `primary-used-percent` 从 0 缓慢爬到 45 左右，离社区「接近 100 才可能路由到 luna」的判据很远，无限流。
- 3.0MB / 3分39秒的超长流完整走完，两条超大输出（3.19MB、2.75MB）都没被截断。

### 4.2 现场证伪两个流传判据

- **`gpt-6-luna / low` 不是降级**：`codex_skills_extension::shadow_selection_experiment` 每轮固定伴随一个内部影子 turn（约 24 行日志、不写进 `turn_context`）。探针证实它**请求 luna、服务端也返回 luna**，MATCH。
- **`x-codex-safety-buffering-enabled: true` + `faster-model: gpt-6-luna` 不是换模型**：astra 响应头确实带这两个，但 served 就是 astra。这些头只是 fallback 链**资格声明**，健康响应也会带。
- 顺带纠正：`x-codex-safety-buffering-enabled` / `faster-model` **不是**降级证据；官方客户端对真实 mismatch 只出一次 `ModelReroute` warning，且没有任何客户端开关能关掉这条回退链。

### 4.3 effort：真正的元凶

`analyze()` 读的是请求体 `reasoning.effort` —— Responses API 里服务端真正采用的字段。实测：

| 场景 | 请求体 | 客户端日志 | config.toml |
|---|---|---|---|
| 13:05 旧会话 14 发 | `medium` | `xhigh` | 被回写成 `medium` |
| 13:20 重开新会话后 | `xhigh` | `xhigh` | `xhigh` |

**设定层和发送层对不上**，这是本次最关键的发现。修法不是换项目，是「彻底退出 + 新建会话 + 界面别碰档位下拉框」。

同模型 astra 的本机实测（1406 条记录，只比同模型才干净）：

| 档 | 样本 | 中位 | 均值 | p90 | 最大 | ≥516 |
|---|---|---|---|---|---|---|
| low | 179 | 35 | 54 | 132 | 516 | 1% |
| medium | 43 | 35 | 79 | 258 | 421 | 0% |
| xhigh | 33 | 121 | 259 | 516 | 1925 | 15% |

medium → xhigh 约 **3 倍推理量**；`516 / 1034 / 1552` 那组预算分块线 medium **一次都没碰到**，xhigh 才碰得到。模型目录里 astra 官方有 6 档 `low → medium → high → xhigh → max → ultra`，且 `default_reasoning_level = "low"` —— UI 不显式切，根本没用上满档。

**但 effort 不是硬配额**：`gpt-6-luna/medium` 实测平均 241、最大 4792，比 `astra/xhigh` 还高。它是「授权/倾向档位」，实际用多少是模型自己定的。别拿单轮 token 数反推档位。

### 4.4 行为侧 A/B：结构性错误 vs 局部贴片噪声

| | 老会话（`low` + 26MB 上下文） | xhigh 新会话（3 份独立成品） |
|---|---|---|
| 车座 | **无** | 有 |
| 腿 / 脚踏解剖 | 腿穿车架、曲柄链轮关系全乱 | JS 两连杆余弦定理逆运动学，脚实时锁在旋转踏板上，双脚相位差 π |
| 车架 | 车把是断钩、前叉不闭合 | 双三角、链盘、曲柄、双脚踏齐全 |
| 语义反转题 | — | 「自行车骑鹈鹕」主客体扛住了，没被套路掰回去 |

跨会话、跨目录两次独立复现：**low 出结构性推理错误，xhigh 出完整约束求解**。第三型请求侧 + 行为侧双证据齐了。

而三份 xhigh 成品**都**能挑出局部贴片瑕疵（喙/喉囊分离色块、尾羽独立贴片、翅膀分界糊）。这不是小概率事件，是 SVG 手绘局部贴合精度的**稳定噪声** —— 恰恰因为它与 effort / 模型 / 会话状态无关、恒定存在，才更确定不是降智信号。xhigh 修的是「结构对不对」，修不掉「局部贴合精度」。

> 判据纪律：**成品画得像不像永远是 noisy 判据**。语义反转题真正值得看的是「主客体有没有被套路化掰回去」。

### 4.5 断流与重发

13:20:56 那条 `astra/xhigh` 只拿到 `response.created`、没等到 `response.completed`，98,560 字节处断流。客户端日志原话：

```
codex_core::responses_retry | handle_response_stream_error{request=Sampling}:
stream disconnected - retrying sampling request (1/5 in 203ms)...
retries=1 max_retries=5
```

relay 日志 `error|traceback|retryable|giving up` 0 命中 —— `--observe-only` 纯透传，断流在上游侧；上游无 SSE `error` 事件，更像代理 CONNECT 隧道对长流的干扰。Codex 自带重试层 13:21:11 重发（47,302B → 47,303B，差 1 字节就是重试计数），13:21:31 正常完成。

**去重规则**：断流重发会留下两条几乎等长的 `POST /responses`，按 `x-oai-request-id` 或 `sse_completed=null` 认出来，只计最终完成那次。另有 `operation="interrupt"` 的手动打断，按「中断」计，不算传输失败。

---

## 5. 代码清单

```
codex-model-route-audit/
├── README.md                          # 本文件
├── docs/                              # 8 份阶段性报告 / 执行卡（按时间编号）
│   ├── 01-模型路由排查报告.md
│   ├── 02-内嵌SVG指纹核查报告.md
│   ├── 03-本机516检测报告.md
│   ├── 04-冷号后诊断与处置清单.md
│   ├── 05-复测执行卡.md
│   ├── 06-relay只读监测启用卡.md        # 含逐轮实况判读（最重要）
│   ├── 07-重开项目交接与复位清单.md
│   └── 08-项目落地说明.md
├── tools/                             # 8 个只读排查 / 校验脚本
│   ├── model_probe.py                 # 核心：纯旁路 served-model 探针（新增）
│   ├── relay_probe_selftest.py        # 30 项离线自测（零网络）
│   ├── watch_probe.py                 # 合并 relay + probe 判读表
│   ├── cold_period_audit.py           # 冷号期间零请求审计
│   ├── effort_compare.py              # 按 effort 档位对比推理 token
│   ├── codex_516_check.py             # 516/518 预算边界检测
│   ├── model_route_check.py           # 11/7–11/8 请求侧模型时间轴
│   └── pelican_fingerprint_check.py   # 首段措辞指纹核查
├── patches/
│   └── relay-readonly-tap.patch       # relay.py 的 +67/−3 最小挂载（173 行 diff）
└── vendor/relay/                      # 打了补丁的上游 relay（MIT，见 LICENSE）
    ├── relay.py
    ├── model_probe.py
    ├── identity.py
    └── LICENSE
```

### 核心补丁做了什么

`relay.py` 五处最小挂载：导入探针 → 在 `attempt()` 拿到 `response_headers` 处记录 → 在 `_proxy()` 建立 Probe → 在 `_stream_back()` 的 `wfile.write()` **之后**旁路 feed → `main()` 增加 `--probe-log` / `--observe-only` 参数并打印启动横幅。

`identity.py` 的 digest 是**运行时**各自读 `relay.py` 算的，不是安装时固化，改源码不会破坏健康检查。

### 独立复现自测

```bash
python tools/relay_probe_selftest.py
# PASS 30 / FAIL 0
# 全部通过；未联网、未启服务、未写 ~/.codex、未改 config.toml。
```

自测覆盖：禁用探针是完全 no-op、请求模型提取（identity/gzip/deflate/zstd）、`_stream_back` 字节透明、四态判定、CRLF SSE 分帧、`--observe-only` 零缓冲（`read_calls == 0`）、正常模式仍检查 SSE 头、CLI 强制 `max_attempts=1`、`--read` 汇总渲染。

---

## 6. 踩坑记录（省得下次再踩）

| 坑 | 真相 |
|---|---|
| 「内嵌 CSV」是 4o 特质 | 社区原词是「内嵌 SVG / 内联 SVG」的**首段措辞**指纹，不是 HTML 里真有 CSV 数据表。约 90% 先验、非 100%、仅 Codex APP/CLI |
| `x-codex-safety-buffering-enabled` / `faster-model` 出现 = 降级 | 只是回退链资格声明，健康响应也带 |
| `gpt-6-luna / low` 日志 = 被换模型 | `shadow_selection_experiment` 内部影子 turn，固定约 24 行 |
| `codex-auto-review` = 降到 4o | `models_cache.json` 里 `visibility: hide` 的自动审批模型 |
| `turn_context.model` = 实际模型 | 只是客户端设定值 |
| UI 显示 xhigh = 发出 xhigh | 必须看请求体 `reasoning.effort`；config 会被 UI 回写，且 effort 在会话创建时锁定 |
| 中文关键词正则判「无车座」 | 成品注释可能全英文、path 无中文，属误判，必须读源码确认 |
| 冷号期间有日志 = 冷号断了 | 要区分「客户端空转日志」和「真实 turn」；看 `codex.turn` / `response.model` / sessions 文件 |
| 断流重发算两发样本 | 按 `x-oai-request-id` / `sse_completed=null` 去重 |
| 同模型不同 cwd 干扰 | 项目目录里的 19 个设定文档（约 190KB）**没进请求**；请求里 55KB+46KB 是固定系统注入 |
| bash 双引号里的反引号 | 会被 shell 当命令替换执行；含反引号的脚本一律用文件写入，不经 shell |
| `py_compile` 输出到 `os.devnull` | Windows 下是 `nul`，非常规文件会报错；只做语法校验用 `compile(src, path, "exec")` |

### 环境相关

- 本机 `github.com` 主站曾不通（代理 CONNECT 502 / 直连超时），但 `api.github.com` 与 `codeload.github.com` 可通；落地仓库用 `zipball/main` + `git/trees/main?recursive=1` 校验。**挂梯子后主站与 git 端点均恢复 200**，旧结论需按当时网络状态判断。
- 读被墙论坛：`WebFetch` 抓 `https://r.jina.ai/https://原URL` 可行（与 curl 出口不同）；`WebSearch` 也能索引到正文片段。
- zstd 会话日志用 `zstandard.ZstdDecompressor().stream_reader()` + `io.TextIOWrapper()` 解压。
- 只读扫描模式：只提取模型字段、effort、token 数值、日期，**不打印或存储用户对话文本**。

---

## 7. 最终状态与复位

- `config.toml`：`openai_base_url` 已删除，`model = "gpt-6-astra"` / `model_reasoning_effort = "xhigh"` 保留；备份 `config.toml.bak-1110` 完好。
- 8787 已释放，relay 进程已停；探针日志保留供复核。
- 上游项目 git 工作区只有 `relay.py` 的 +67/−3 改动和新增 `model_probe.py`；测试产物未污染 mod 项目 git 历史。
- **没有执行 `install.ps1`**：那会改 config、写注册表自启，属于动用户环境，本次全程只用命令行临时启用。

### 项目侧附带发现

排查过程中发现 mod 项目根目录已有一份 `HANDOFF-平行异位-总览.md`（10-23 后未再修改），其中 P0「建 git 仓库」、角色进度、「上次构建 10-23」等 4 处已过时。**重开项目时应更新这一份，而不是另写一份交接文件** —— 两份会打架，且把老会话历史压成长文档搬过去等于把污染搬家。详见 `docs/07`。

---

## 8. 脱敏说明

本仓库不含任何 token、密钥、邮件地址、完整 `x-oai-request-id`、用户对话正文或会话日志。已做的处理：

- 本机绝对路径 → `~` 或 `<WORKSPACE>`
- 完整 request-id → 前 8 位 + `<REDACTED>`
- 梯子 / 系统代理端口 → `<PROXY_PORT>` / `<SYSTEM_PROXY_PORT>`（relay 自身端口 8787 保留，属公开默认配置）
- 用户名、邮箱 → `<USER>` / `<REDACTED_EMAIL>`
- Python 工具里的硬编码路径改写成 `os.path.expanduser("~")` 可移植形式，不是简单字符串替换（那样脚本会跑不起来）

归档构建脚本会做残留扫描 + 8 个脚本的语法校验，两者都必须通过。

---

## 9. 适用范围与声明

- 这是一次**单机、单账号、特定时间窗口**的排查记录，不构成对任何服务端策略的普遍结论。
- 所有判读基于本机日志与上游 SSE 自报字段；响应头语义按社区公开讨论解释，非官方文档定义。
- 上游 `codex-overload-fix` 为 MIT 许可（Copyright (c) 2026 NewBeeM4n），`vendor/relay/` 与 `patches/` 遵循该许可，见 `vendor/relay/LICENSE`。
- 仓库用途是**方法论与证据留存**，不提供任何绕过额度、伪造客户端或账号池的能力。

