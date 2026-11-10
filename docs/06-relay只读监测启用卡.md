# relay 只读监测 · 11-10 启用卡

生成：2026-11-08 18:10｜时间表更新：2026-11-08 18:20｜**启用实况：2026-11-10 12:34**

状态：**已启用，纯观测模式运行中**（后台常驻，task_id `0bLbLt`）。冷号期间零触碰，冷号已核验干净。

> 复测日：原 11/09 11:00 → **2026-11-10（周二）12:24 用户说「起」**。冷号起点 11/08 11:00，到启用时约 **59h**。用户确认 11/08 01:00 那张鹈鹕图早于冷号起点。

---

## 零、11/10 12:34 启用实况（已执行）

| 项 | 实际值 |
|---|---|
| 冷号核验 | 最后一次真实 turn = **11/08 01:00**，至今约 59h。`sessions` 在 11/08 11:00 后 **0 文件**。`logs_2.sqlite` 11/09 19:00–20:24 新增 1287 行，target 全是客户端启动类（`codex_app_server::message_processor` 674、`codex_http_client::custom_ca` 307、插件/MCP/`models_manager::cache`），`models_cache` 命中 20；`codex.turn` / `shadow_selection` / `response.model` / `gpt-6-` / `reasoning_effort` / `ModelReroute` / `engine_ids` 命中**全为 0** → 11/09 晚开过 APP（挂梯子、看设置）、刷了模型目录，**但零推理请求**，冷号成立 |
| 梯子（新变量） | 系统代理 `127.0.0.1:<PROXY_PORT>`（`ProxyEnable=1`，端口 connect 探测 OPEN）。relay 以 `--proxy http://127.0.0.1:<PROXY_PORT>` 走 HTTP CONNECT 隧道；**出口 IP 由梯子落地节点决定**，与 11/08 之前「机房干净 IP」不是同一路径，判读时记一笔 |
| config 备份 | `~/.codex/config.toml.bak-1110`（`cp -p` + `cmp` 校验一致；原文件 mtime 11/09 21:17） |
| config 改动 | 根级第 4 行插入 `openai_base_url = "http://127.0.0.1:8787"`；`model` / `effort` / `model_provider` 全未动 |
| relay | 8787 LISTENING，后台常驻，`--observe-only --probe-log` 均已生效 |
| 启动横幅 | `relay listening on 127.0.0.1:8787 -> https://chatgpt.com/backend-api/codex via ('127.0.0.1', 10808) [OBSERVE-ONLY: no retry / no SSE buffering] [PROBE -> ~/.codex/model_probe.log]` |
| 运行日志 | `~/.codex/relay-1110.log` |
| 探针日志 | `~/.codex/model_probe.log` —— **首轮请求前不存在，正常** |

**实际启动命令**：

```bash
"~/.workbuddy/binaries/python/envs/default/Scripts/python.exe" -u \
  "<WORKSPACE>/codex-overload-fix/relay/relay.py" \
  --port 8787 --observe-only \
  --probe-log "~/.codex/model_probe.log" \
  --log "~/.codex/relay-1110.log" \
  --proxy http://127.0.0.1:<PROXY_PORT>
```

**两个待验证点，靠第 1 发探活**：

1. `10808` 是否支持 HTTP CONNECT（Windows 系统代理通常支持；若是纯 socks5 会失败）。
2. Codex 客户端是否 bypass `127.0.0.1`（一般会；若把本地地址也丢给梯子，会连不上 8787）。

任一失败，Codex 会报连接错误，我看 `relay-1110.log` 调整，**探活不计入正式样本**。

**若我这边后台进程被会话回收**：你在终端自己跑上面那条命令即可，参数完全一致，我只读日志。

---

## 一、做了什么（结论先行）

在 `codex-overload-fix/relay/` 里加了一套**只读 served-model 监测**，目的是复测时直接抓到服务端真实返回的模型，而不是靠鹈鹕首段措辞猜。

| 文件 | 改动 |
|---|---|
| `relay/model_probe.py` | **新增**。独立探针模块，纯旁路：只观察已转发出去的字节副本，写本地 JSONL；不开 socket、不发请求、不改任何字节/头/模型字段 |
| `relay/relay.py` | `+67 / −3`。5 处最小挂载：导入探针、`--probe-log`、`--observe-only`、`attempt()` 零缓冲快路径、`_stream_back()` 写后旁路喂探针 |
| `output/relay_probe_selftest.py` | **新增**。离线自测（合成 SSE + 假连接，不联网） |
| `output/cold_period_audit.py` | **新增（11/10）**。只读审计冷号期间是否产生过 turn |

**默认完全不启用**：不带 `--probe-log` 时 `Probe` 是彻底 no-op，vendored relay 行为与上游逐字节一致。

### 纯观测模式（已启用）

`--observe-only` 强制 `max_attempts=1`，并且**跳过整个 SSE 头检查/分类/缓冲逻辑**——拿到响应头立即 commit 直转，`prelude` 恒为 `b""`。这样连 README 里作者自己承认的那点残余风险（「任何改变了请求时序的客户端都不属于默认行为」）也被削掉：不重试、不缓冲、不延迟、不判定，只是转发 + 记录。

代价只有一个：遇到 `server_is_overloaded` 不再自动吞掉，原样透传给你。跑 5 次鹈鹕测试无所谓。

---

## 二、自测结果：30 / 30 通过

```
PASS 30 / FAIL 0
未联网、未启服务、未写 ~/.codex、未改 config.toml
```

关键几项：

- **字节透明**：探针开 / 关两种情况下，`_stream_back` 写出的字节与原始 SSE **完全相等**（自测里把流拆成两个 chunk、并覆盖 CRLF 分帧）。
- **零缓冲**：`--observe-only` 下 `attempt()` 返回 `prelude == b""`，且 `resp.read_calls == 0` —— 函数内部一个字节都没读，不做任何重试判定。
- **回归**：不带 `--observe-only` 时，原 relay 的 SSE 头分类/缓冲行为不变。
- **判定**：`MATCH`（astra→astra）、`SWAP`（astra→luna）、`SERVED`（仅响应头有模型）、`NO_SIGNAL`（啥都没有）四条都对。
- **请求侧模型提取**：identity / gzip / deflate / zstd 四种请求体都能解出 `model`。
- **防误判**：`x-codex-safety-buffering-*` 在日志里明确标注为「资格声明，不是换模型证据」；`x-codex-turn-state` 只存长度不存内容。

### 现场核验（11/08 18:09 历史快照，仅留档）

| 检查项 | 当时结果 |
|---|---|
| `~/.codex/model_probe.log` | 不存在（没写过） |
| `~/.codex/config.toml` 里的 `openai_base_url` | 无 |
| 8787 端口监听 | 无 |
| git 改动 | `M relay/relay.py`、`?? relay/model_probe.py`（外加一份之前被格式化弄坏的 `README.zh-CN.md`，见第五节） |

> 11/10 12:34 的现场状态见**第零节**。

---

## 三、启用步骤（11/10 12:34 已执行，留档核对）

> 冷号起点 11/08 11:00 → 满 24h 是 11/09 11:00，**已跳过**；11/10 12:24 用户说「起」，12:34 执行完毕。**实际命令以第零节为准**（多了 `--proxy`）。

**第 1 步 · 改 `~/.codex/config.toml`（根级加一行）** ✅

```toml
openai_base_url = "http://127.0.0.1:8787"
```

- 放在文件**根级**（任何 `[xxx]` 表之前 / 顶层），不要塞进 `[model_providers.openai]` 里。
- 不要动 `model_provider`（保持默认 `openai`）。这是 v1.5.1 官方支持的地址覆盖项，不是早期自定义 `openai-retry` provider。
- 备份：`~/.codex/config.toml.bak-1110`。

**第 2 步 · 起 relay（纯观测）** ✅

见第零节命令。启动横幅应带 `[OBSERVE-ONLY: no retry / no SSE buffering] [PROBE -> ...]`。**不要执行 `install.ps1`**（它会写注册表自启、改配置，比手动两行重得多）。

然后**彻底退出并重新打开 Codex**，界面确认：模型 `gpt-6-astra`、effort **xhigh**（11/08 00:53 实测 UI 会把 xhigh 悄悄降到 low）、快速回答已关。再按执行卡跑 5 次鹈鹕。

### 分工与先后（别反）

| 顺序 | 谁 | 动作 |
|---|---|---|
| ① 11/10 12:34 | **我** ✅ | 改 config 加一行、起 relay（`--observe-only` + `--proxy 10808`）、确认横幅带 `[OBSERVE-ONLY]` |
| ② | **你** | 彻底重启 Codex → 界面确认 astra / xhigh / 快速回答已关 |
| ③ | **你** | 发 1 次探活；**我**确认 `model_probe.log` 开始写入、relay 有流量 |
| ④ | **你** | 正式 ×5（每次新会话）；**我**全程盯日志、跑只读脚本、出判读 |
| 随时 | **你** | 问问题，我随时答，不攒着 |

**三条硬边界**

1. **时间闸门**：11/10 12:24 用户说「起」，闸门已过，第 1/2 步已执行。
2. **先后不能反**：relay + config + 重启 Codex 必须全部早于**第一发**。否则那一轮不走 relay，直接废掉，还白污染一次样本。
3. **我不碰你的号**：发 prompt 只能你手动。我不代发任何请求、不操作客户端 —— 这是「只读」的边界，也是冷号能算干净的前提。我只读日志 + 跑分析。

> relay 现由我这边后台常驻（task_id `0bLbLt`）。若会话被回收，用户自行执行第零节命令即可，参数一致，我只读日志。

---

## 四、跑完怎么读

```bash
"~/.workbuddy/binaries/python/envs/default/Scripts/python.exe" \
  "<WORKSPACE>/codex-overload-fix/relay/model_probe.py" \
  --read "~/.codex/model_probe.log"
```

（已核对 `model_probe.py` 确有 `--read` 参数。）

每个请求一行：`请求模型 / 实际 served / 判定 / 备注`。

| 判定 | 含义 | 动作 |
|---|---|---|
| `SWAP` | 请求 astra，服务端实际 luna/sol/terra/4o | **第二型确诊**：切 `gpt-6.1-sol` 保生产、带 `x-oai-request-id` 反馈官方；别换号换 IP |
| `MATCH` | 实际就是 astra | 降智不是换模型，回头查第三型（effort / 516 / commentary / 上下文体积） |
| `SERVED` | 抓到了模型但请求侧没解出来 | 对照 `config.toml` 自己比一下 |
| `NO_SIGNAL` | 上游既没给 `openai-model` 头，SSE 里也没有 `response.model` | 这次白抓；保留行为层证据，别硬下结论 |

日志是 JSONL，逐条还带 `HEADERS`（含 `x-oai-request-id`、资格头、primary-used-percent）和 `SSE_MODEL`（`response.created` / `response.completed` 的 model，以及 `engine_ids`），可以拿去对官方反馈。

**收工恢复**：先删掉 `config.toml` 里那行 `openai_base_url`（或用 `bak-1110` 还原），再 Ctrl+C 停 relay。**顺序别反** —— 中继停了而 Codex 还指着 8787 会连不上。

---

## 五、一个顺带发现

`README.zh-CN.md` 目前是 `M` 状态，但那不是内容改动，是之前某次 markdown 格式化把 `<br>` 变成空行、`&nbsp;` 写成 `\&nbsp;`、图片 URL 里的 `&` 转义、表格被重排了。属于 vendored 文件的意外污染，不影响代码。要恢复的话：

```bash
cd "<WORKSPACE>/codex-overload-fix" && git checkout -- README.zh-CN.md
```

我没动它，等你说一声再恢复。

---

## 六、首轮探活判读（11/10 12:48，已出结果）

**结论：MATCH ×2，第二型（静默换模型）在这一轮排除。**

| 请求 | 请求模型 | SSE created | SSE completed | 判定 |
|---|---|---|---|---|
| 主 `p56530_242000` | `gpt-6-astra` | `gpt-6-astra` | `gpt-6-astra` | **MATCH**，`mid_stream_mismatch=false` |
| 影子 `p56527_803616` | `gpt-6-luna` | `gpt-6-luna` | `gpt-6-luna` | **MATCH** |

- `header_model = null` → 上游没给 `openai-model` 头，served 是从 SSE `response.created` / `response.completed` 抓的，比看响应头更硬。
- `ModelReroute` 命中 0；`logs_2.sqlite` 里 `shadow_selection_experiment` 恰好 24 行 `gpt-6-luna/low` —— **那不是降级**，是每轮固定伴随的内部影子 turn，之前踩过的坑今天现场对上了。
- astra 响应头带 `x-codex-safety-buffering-enabled: true` + `faster-model: gpt-6-luna`，实际 served 仍是 astra → 再次确认这些只是 fallback 资格声明。`primary-used-percent = 0`。
- 12:44:49 `GET /models → 401` 是重启时 token 未就绪，12:45:42 自动 200，无需处理。

**但抓到一个真问题：effort = medium，不是 xhigh。** config 写 xhigh，请求侧 relay 日志和 `logs_2.sqlite` 的 `codex.turn.reasoning_effort=medium` 双向确认 —— UI 又覆盖了配置（11/08 00:53 那次降到 low）。降智方向因此从第二型转向**第三型（effort / reasoning budget 层）**。

**下一步**：确认这一发算探活还是正式第 1 发；正式 ×5 之前必须界面手动把 effort 调到 **xhigh**，否则整组样本不齐。

两个 `x-oai-request-id` 留存：`715b4ef4-<REDACTED>`（影子）、`c1a80889-<REDACTED>`（主）。

---

## 七、13:20 之后：xhigh 生效 + 一次 stream disconnected（已判读）

### 7.1 xhigh 终于送到了服务端

彻底退出重开 + 新建会话后，relay 请求体侧 **13:20:48 起所有 astra 请求均为 `effort=xhigh`**，与 config 第 2 行 `model_reasoning_effort = "xhigh"` 一致。13:19 后 `logs_2.sqlite` 里 `codex.turn.reasoning_effort` 分布 `xhigh=82 / low=25`（low 全部来自 luna 影子 turn）。

→ 之前那批 medium **不是客户端硬 bug**，是「旧会话里 effort 在会话创建时就被锁定 + UI 回写 config」的组合。**变量现在可控了。**

### 7.2 `stream disconnected before completion` 是什么

客户端 `codex_core::responses_retry` 原话（13:21:11）：

```
handle_response_stream_error{request=Sampling}: stream disconnected
- retrying sampling request (1/5 in 203ms)...
retries=1 max_retries=5 sampling_error=stream disconnected
```

| 时间 | 事件 |
|---|---|
| 13:20:56 | `[63861] POST /responses` astra/xhigh，47,302B |
| 13:20:58 | relay `attempt 1: ok, streaming` |
| — | 上游 SSE 只发了 `response.created`，**没发 `response.completed` 就断**；probe 记 `sse_completed=null`、`stream_bytes=98,560` |
| 13:21:11 | 客户端自动重发同一请求（47,303B，差 1B 是重试计数），203ms 退避，`retries=1/5` |
| 13:21:31 | 重发正常完成，256,865B，`sse_completed=gpt-6-astra`，MATCH |

**判定：传输层偶发断流，不是降智、不是换模型、不是限流。**

- relay 日志 `error|traceback|retryable|giving up` 命中 **0 行**：`--observe-only` 下 relay 纯透传，没重试、没吞异常，断流是上游/隧道侧发生的。
- 断的那条 `x-codex-primary-used-percent=11`（后面 12），不是 quota 打满；`sse_created=gpt-6-astra`，模型身份没问题。
- 上游没给 SSE `error` 事件，是 TCP/SSE 流直接被掐 → 更像是**梯子 CONNECT 隧道对长流的干扰**（今天新加的变量），不是服务端主动拒绝。
- 这个场景**恰好是 `codex-overload-fix` 原本要处理的一类问题**：上游把失败藏在 200 + 不完整 SSE 里。我们为了纯观测把重试关掉了，所以交给 Codex 自带的 `responses_retry` 兜底 —— 它 203ms 就恢复了。属于模式的已知代价，不影响判读。

### 7.3 判读时的一条去重规则

断流会触发客户端自动重发，relay 里会看到**两条几乎等长、内容几乎相同的 `POST /responses`**（本例 47,302B / 47,303B）。**别把重发算成新样本**：按 `x-oai-request-id` 或 probe 里 `sse_completed=null` 的那条识别，只计最终完成的那次。本例 `x-oai-request-id`：`142a6780-<REDACTED>`（断）、`eab5bdad-<REDACTED>`（重发成功）。

### 7.4 这一轮同样不是「单次生成」

13:20:48 → 13:27:34 又是**同一会话内多轮 turn**（输入 104KB → 145KB → 159KB，含 `custom_tool_call_output`、`reasoning=5187`，还有一条 3.19MB 输出），是迭代打磨过程，不是单发成品。跟 13:05–13:11 那 13 轮同类。**正式 ×5 仍需每次新会话、单轮不迭代。**

至 13:27 全部 VERDICT 为 `MATCH`，零 `SWAP`，served 全程 `gpt-6-astra`。

### 7.5 13:35 增量复核：xhigh 连续 22 发全 MATCH

12:48 探活 → 13:35 共 **39 条 `POST /responses`**，relay 与 probe 数量一一对应（39/39），无漏记。

| effort | 发数 | 判定 |
|---|---|---|
| `low` | 3 | 全 MATCH（luna 影子 turn） |
| `medium` | 14 | 全 MATCH（13:05–13:11 旧会话组） |
| **`xhigh`** | **22** | **全 MATCH** |

- **39/39 `served = gpt-6-astra`（另 3 条 luna 影子），零 `SWAP`、零 `NO_SIGNAL`、零 `mid_stream_mismatch`。** 第二型（静默换模型）在 xhigh 档连续 22 发下彻底排除。
- 13:20:48 起 xhigh **再没掉回 medium**，config 与请求体全程一致。
- 断流**只发生 1 次**（13:21:11 那条 98,560B），之后 18 发全部正常拿到 `response.completed`；其中 13:31:03→13:34:42 一条 2.75MB / 3分39秒的超长流也完整走完 → 坐实偶发，不是隧道对长流的必然掐断。
- `x-codex-primary-used-percent` 从 11 缓慢爬到 **34**（13:34:44），离社区那条「接近 100 才可能路由到 luna」的判据还很远，未触发任何限流。
- 13:30:25 起又出现一组新会话（luna 影子 + astra 104KB），但随后又连续迭代 4 发到 13:34 —— **仍然不是「单发即停」**。

**结论分层**：若目标是排除「被换成 4o/luna」，证据已经够，可以结案；只有当目标是评估 **xhigh 档下的输出质量 / 首段措辞** 时，才需要正式 ×5，且必须每次新建会话、**发完就停、不迭代**。

---

## 八、14:06 追查：`ProTemplate-master` 与「同文件夹新会话都是微瑕」

### 8.1 这个文件夹是什么

`~\Desktop\ProTemplate-master` **不是测试目录，是《杀戮尖塔》mod「平行异位」的项目仓库**：

- 7036 个文件（含 `src/`、`target/classes/`、`lib/`、`tmp_decompile/game-icons/` 数千个 svg、`tmp_relic_drafts/`）
- 根目录 19 个 `.md/.txt/.png` 约 **1.0 MB** 中文设定文档（`HANDOFF-平行异位-总览.md`、`精绝王者-*`、`鸿文君主-*`、`霜星旅者-设定.txt` 等）
- **无 `AGENTS.md` / `.codex` 项目指令文件** → 排除本地指令污染
- `git log` 最近三条仍是 10-23 的 mod 提交（`3d17ebf` 等），**测试没动过 git 历史**；唯一变化是根目录一个未跟踪文件 `?? pelican-bicycle.html`
- 13:52 Codex 另建了隔离 worktree `~/.codex/worktrees/8c4e/ProTemplate-master`（detached HEAD，整仓复制），**里面没有任何 html 产物**

今天所有会话的 `cwd` 基本都钉在这个目录（14:02 那一发例外，是 `Documents\ChatGPT\test3`）。请求里 `msg:developer≈55KB + additional_tools≈46KB` 是每轮固定注入，与目录内容无关。

### 8.2 缺失的 A/B 对照其实已经存在

| | A：mod 项目根 | B：干净空目录 `Documents/ChatGPT/test3` |
|---|---|---|
| 生成时间 | 14:01:14 | 14:07 |
| 对应请求 | 13:59:02 起 xhigh 流，1.72MB 完成 | 14:03:06 起 xhigh 流，2.75MB 完成 |
| 规模 | 14,872B / 203 行 / 48 path | 24,867B / 349 行 / 77 path |
| 标题 | 海风刚好，慢慢骑。 | 今天，慢慢骑。 |
| 题目 | **「鹈鹕骑自行车」原题** | **「鹈鹕骑自行车」原题** |

两版都是 `gpt-6-astra / xhigh / MATCH`，都是新会话、独立目录、单轮为主。这补上了 7.5 节记的「今天 xhigh 没跑过原题、无法严格 A/B」。

### 8.3 结构层：老会话三个硬伤全被修掉

老会话（`astra/low` + 26MB 上下文，11/08 01:00 与 13:54 两次复现）的毛病是：**无车座、腿穿过车架、脚蹼/曲柄/链轮解剖关系全乱、车把与前叉不闭合**。

两版 xhigh 对照全部具备：

- **车座**：A `M404 300q20-9 53 0`；B `M490 407Q510 399 538 405`（saddle）
- **车架双三角**：A `M350 427 434 334 493 425...`；B `M433 559L525 438 581 555...`
- **链盘 / 曲柄 / 双脚踏**：两版齐备
- **逆运动学解算**：两版都用 JS 两连杆余弦定理（`upper² - lower² + d²` 求膝位）把脚实时锁在旋转踏板上，双脚相位差 `π`；A 还用 `getAnimations({subtree:true})` 统一 CSS 与 JS 时间轴
- B 额外加了头盔与下巴带、挡泥板、链条罩、车铃、帆船、远鸟、路边花草

**这已经不是「画一张图」，是在解机械约束。** low 档出现的是结构性推理错误，xhigh 档是完整约束求解 —— 跨会话、跨目录、两次独立复现，第三型（effort / reasoning budget）在**行为侧**也坐实了。

### 8.4 「微瑕」是另一层，跟 effort 无关

剩下能挑出来的都是**贴片级局部瑕疵**：喙与喉囊仍是两个分离色块（A `M539 151` + `M540 145`；B `M689 267` + `M693 256`）、尾羽独立贴片、身体用单条复杂贝塞尔勾轮廓、翅膀与身体分界糊。这是 SVG 手绘的固有噪声。

**所以「新会话也微瑕」不是没修好**：xhigh 修掉的是「结构推理对不对」，修不掉「局部贴合精度」。两件事别混。成品画得像不像自始至终是 noisy 判据。

### 8.5 请求侧补记（13:55 之后）

- 累计 **53 发 `astra→astra` MATCH + 7 发 `luna→luna` MATCH，零 SWAP**；8787 仍存活
- 13:58 后客户端 `effort` 分布 `xhigh=127 / low=46`，low 全是 luna 影子 turn，xhigh 全程稳定
- **13:58:20 那条 `completed=None` 不是断流**：客户端日志明写 `operation="interrupt"` → `aborting running task`，是用户手动打断；随后 13:59:01 重发并于 14:01:14 完整完成。判读去重时按「中断」而非「传输失败」计
- `primary-used-percent` 未打满，无限流；`refus / content_policy` 0 命中

**结论**：第二型（静默换模型）已结案；第三型现有请求侧 + 行为侧双重证据；「微瑕」属贴片噪声，不作为判据。测试产物未污染 git，仅一个未跟踪 html。

### 8.6 收官一发（14:26–14:31，用户「新会话重开又画一个」）

- 14:26:07 同时出现 `gpt-6-luna/low` 影子 + `gpt-6-astra/xhigh` 主请求 = **新建会话**（`rollout-2026-11-10T14-25-38...`，cwd = `ProTemplate-master`，effort 仅 `xhigh`）。随后 14:26:16 / 14:26:32 / 14:31:31 为同会话迭代（input 104KB→120KB→128KB→178KB，`reasoning=19818`），仍是打磨过程。
- 14:31:31 长流 **3.0MB 完整完成**，产出覆盖 mod 项目根 `pelican-bicycle.html`：**28,469B / 434 行 / 74 path**，标题「今天，顺着风骑。」，是三版 xhigh 里规模最大的一版。
- **结构核查（读源码确认，正则曾误判）**：车座在（220–221 行棕色坐垫 + 高光）、车架双三角、链盘、曲柄、双脚踏、两连杆逆运动学（332 行注释 + 351–363 实现，脚锁在旋转踏板上）、双脚相位差 π、`reduced-motion` 全在。结构性硬伤（无车座 / 腿穿车架 / 脚踏解剖乱）**没有复现**。此版注释全英文，中文关键词正则命中为 0，勿据此判「无」。
- 累计更新：**57 发 `astra→astra` MATCH + 8 发 `luna→luna` MATCH，零 SWAP**，`primary-used-percent` 未打满。

**封存定性**：用户判「一点小瑕疵、问题不大、应该是小概率事件」——结论方向对，但归因需校准。跨 5 个独立样本（11/08 01:00 low 老会话、13:54 low 老会话、14:01 / 14:07 / 14:31 三次 xhigh 新会话）**每次都能挑出局部贴片瑕疵**，位置各异：喙与喉囊分离色块、尾羽独立贴片、翅膀分界糊、缰绳连接点、坐姿/腿姿态。这不是小概率事件，是 **SVG 手绘局部贴合精度的稳定噪声**——恰恰因为它与 effort / 模型 / 会话状态无关、恒定存在，才更确定它不是降智信号。xhigh 修的是「结构对不对」（车座、机械约束），修不掉「局部贴合精度」，两者不同层。

**全案三层结论**：① 模型身份：57+8 MATCH 零 SWAP，第二型排除；② 体感「降智」的真实来源是 `effort` 被 UI / 会话锁定在 medium / low（请求侧实锤）+ 超长老会话上下文（26MB / 386 items / auto_compact）；③ 剩余微瑕为贴片噪声，不参与判读。

---

关联：`output/11.9复测执行卡.md`（文件名沿用，内容已改为 11/10）、`output/cold_period_audit.py`、`output/11.7-11.8模型路由排查报告.md`、`output/内嵌SVG指纹核查报告.md`、`output/重开项目_交接与复位清单.md`。
