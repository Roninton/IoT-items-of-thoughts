# Session 归档与分层记忆（开发版）

实现位于 `dsh-plugin-iot-thinking`，不接管 `ctx.compaction`，不增加摘要模型调用，不在每个 thinking 后追加链接。

## 1. 自动逐轮 Log

默认 `sessionLog: true`，独立于 thinking 开关/档位。`sessionLog: false` 关闭新归档；原有 `archive` 仍仅控制旧的逐块 originals 功能。

根目录：`$IOT_THINKING_DATA_DIR/sessions`，未设时为 `$DSH_HOME/plugin-data/iot-thinking/sessions`，再回落 `~/.dsh`。每个 session 使用完整 SHA256 子目录避免碰撞；可读索引与文件内部保存实际 session ID。`iot_session_log(action="list")` 返回当前目录与各轮文档路径。

```text
sessions/<session-hash>/
  session.json                 会话身份
  events.jsonl                 追加的宿主事件与 IoT 原思/处理记录（真源）
  index.md                     按轮次排列的目录
  turn-000000000012-<UUID>.md  一轮可读文档（稳定身份，可重建）
  memories.json               记忆版本历史（有使用时生成）
  memory.md                   当前总览与关键记忆（派生阅读视图）
```

宿主 session/event 记录用户来源、system/developer、助手正文、reasoning、工具调用/结果、取消或失败 attempt 及其他事件。不是每次请求复制全部Context。原始 reasoning 在 IoT 前观察，块结束先保存；处理后内容、建议以 request UUID + block 关联。无原始记录时明确标 ORIGINAL MISSING，不能将历史已改写内容冒充原思。

会话创建/首次调用使用公开 sessionQuery.readSession 补抓一次历史，实时事件按seq幂等落盘；没有该服务或读取失败只捕获活跃事件，coverage报告序列缺口。历史回填先完整校验，再用单次归档锁和单次 durable append 批量写入；同 seq 同内容幂等、不同内容拒绝。重放不能恢复插件启用前已被改写的原始思考。后台查询上限5秒，失败不挂死模型。流finally保存已观察但未闭合块的partial；进程被强杀时未到block-end的原始delta仍可能缺失。

JSONL与原思同步写入/fsync；Markdown延后渲染并在flush完成。Markdown是可从JSONL重建的投影，使用同目录tmp + rename原子替换，整批完成后统一同步目录，不为每轮单独fsync。原思或处理记录保存失败会否决本块替换，保留原块；宿主事件归档失败只报告session-log-error，不回滚真实工具操作。索引中的coverage只说明已观察seq前缀，不等于所有原始思考都存在。

**范围与边界：**当前实现要求POSIX目录fsync（本机macOS验证）；Windows明确拒绝新归档，既有模型流保留原文，不承诺Windows ACL支持。二进制附件保留事件中的引用而非复制附件文件。默认无自动清理/容量配额，会持续占磁盘；不要把此目录上传或放公共共享目录。新建目录0700/文件0600、拒绝symlink/多硬链接；同用户/管理员恶意并发改路径不在安全保证内。遗留锁或JSONL尾部撕裂会拒绝追加，不自动抢锁/截断；先停写者核验，再由管理员修复。

## 2. Session 记忆工具与 Skill

`sessionMemory: true` 默认注册两个工具以及 runtime skill `iot-session-memory`（宿主tools/skills服务存在时）。配置为false会拒绝执行；在插件加载行设置false可不注册，重新启用需要重载插件。开关可通过已有受保护配置API读写，尚未增加GUI控件。

- `iot_session_memory`: list/read/search/write/update/delete。
- `iot_session_log`: list/search/read（按轮次分页）。
- `iot-session-memory` Skill：先读summary，必要时找关键记忆，细节不足才读Log；关键发现或交接才写，不记流水账。

每次调用身份由exec绑定当前session，参数不能指定任意session或路径。`summary`为当前总览，新建普通条目自动UUID；update/delete必须最近read的expectedRevision，防止覆盖并发更新。sources限定本会话实际存在的turn/seq。记忆是模型声明，不自动成为工具事实或用户授权。delete是逻辑删除，历史修订仍在，原始Log不变，不等于敏感内容擦除。fork后不自动复制父会话记忆或私有原思；宿主继承事件可补抓，缺失的改写前内容明确标注。

例：

```text
iot_session_memory(action="read", id="summary")
iot_session_memory(action="write", id="summary", title="当前进展", content="目标...\n已验证...\n待验证...\n下一步...")
iot_session_memory(action="search", query="缓存")
iot_session_log(action="search", query="timeout")
iot_session_log(action="read", turn=12, offset=0, limit=100)
```

## 3. 验证与运行

```sh
node --test session-archive-test.mjs
node archive-runtime-test.mjs
node session-memory-test.mjs
# 用当前Desktop的ASAR-aware运行时；只写临时目录，不连模型、不改profile
ELECTRON_RUN_AS_NODE=1 "$DSH_NODE_EXECUTABLE" session-host-test.mjs
ELECTRON_RUN_AS_NODE=1 "$DSH_NODE_EXECUTABLE" host-context-test.mjs --expect-context
```

测试覆盖多步轮次、原思/精炼关联、正文工具保留、重启与乱序补抓、批量回填幂等与冲突、缺原思/中断、路径隔离、私有文件、撕裂与锁拒绝、记忆CRUD/版本/来源、真实宿主工具schema与skill注册。同机5000事件（约10.13MB）基准中，权威事件写入约118ms、Markdown投影约236ms、合计约354ms；这是单机样本，不是跨平台SLA。真实长任务下的主动写记忆/召回准确率、Windows和长期磁盘压力仍未由离线测试证明。
