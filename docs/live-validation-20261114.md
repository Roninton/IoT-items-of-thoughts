# IoT Session 记忆与日志：真实会话验证

日期：2026-11-14（Asia/Shanghai）。运行宿主 DSH 0.2.2-rc.2。测试模型 byte-aidp/gpt-6-astra（medium）。

## 环境及操作

- 当前Desktop已通过 link 安装仓库插件。install_bundle返回already-installed；通过plugin_manager禁用/启用该行使新代码加载，均application=applied。
- 测试会话为真实可续接子会话，ID `0be35c50-674d-4703-9fd3-29f69c759326`，不是伪造Session或只调函数。
- 三阶段：Skill发现和CRUD；记忆不足时读Log；修复空ID后真实回归。
- 未修改用户其他会话的记忆；只逻辑删除实验临时条目。新归档能力为profile范围，主会话及其他活跃会话也会自动归档。
- 未新增server、未改默认模型或provider授权。尝试普通浏览器页面遇认证要求，未搜索凭据。独立浏览器标签已请求关闭。桌面新会话点击未证明创建成功，实际验收采用子会话。

## 通过的真实验证

1. 模型通过skill工具发现并加载iot-session-memory。
2. 初次summary为空；创建revision1、读取、更新revision2均成功。
3. 临时条目创建、读取、更新、逻辑删除成功；旧revision删除返回MEMORY_CONFLICT，正确revision删除后read found=false、search不再返回。
4. summary不保存测试标记/数值。模型按明确测试指令读取summary→search记忆未命中→search Log→read turn1，找到archive_probe_7Q9对应638417；来源turn1 seq13。
5. summary更新附sources [{turn:1,seq:13}]成功。
6. 第一次write id=""返回MEMORY_ID：真实通道把可选参数显示为必填，无法合法省略id。修复工具边界规范化（write空id自动UUID；update空title/content视为未提供），更新Skill与工具说明并重载。
7. 修复后真实write id=""生成63f8e6b1-fdf8-4cf4-99db-a9bc0d4d8e0a，read revision1，update title=""后标题保持、内容更新，read revision2，delete revision3成功。后续read再次确认found=false。
8. 磁盘核验：截至seq401共402条宿主事件连续无缺口，35个assistant/message，工具调用skill2、iot_session_memory22、iot_session_log5、glob1、read1。轮1/2/4/5 completed，轮3 error。
9. 轮3错误为上游HTTP429 RATE_LIMIT（资源不足），不是记忆工具失败；续接后完成同一临时项，没有重复新建。

## 不能宣称通过的项目

- 本路由实际assistant消息与stream中没有reasoning块，reasoning记录数0。因此不能证明真实原思捕获/精炼/增强关联。这部分只有此前及本次离线回归通过；必须换允许且真实输出reasoning的路由另验。
- 明确要求回读且原历史仍在上下文中的实验，不等于自然自主发现遗忘，也不是压缩后/冷启动后的盲测召回。
- Markdown人类阅读体验尚不理想：第2轮6564行，内容与Exact host event JSON/stream重复；索引缺主题时间，summary渲染缺revision/source链接，unassigned易误解。只读UX审阅建议：JSONL保持完整，MD渲染一次正文与工具结果、精简元数据；会话级事件单独标明。尚未实施，不能声称人类友好验收通过。
- 普通GUI会话创建、截图验收、Windows以及长时间磁盘压力未完成。

## 修复及回归

修改 lib/session-memory-tools.mjs、skills/iot-session-memory/SKILL.md、session-host-test.mjs。
归档11/11、archive-runtime-test、当前ASAR宿主session-host-test通过（包括空ID与空title回归）。之前归档subagent迟到修改model为appendView/model并新增turnCoverage，主Agent已停止其继续修改、审阅并通过同一套回归；turnCoverage尚未接入渲染，不据此宣称修复MD覆盖显示。

## 证据入口

- [实验目录索引](</Users/zhangtianxu/.dsh/plugin-data/iot-thinking/sessions/a9d68ddd747541602a7d8200bc269afb56aafb7a1022b30783d42a6f6fb1ffaa/index.md>)
- [状态与关键记忆](</Users/zhangtianxu/.dsh/plugin-data/iot-thinking/sessions/a9d68ddd747541602a7d8200bc269afb56aafb7a1022b30783d42a6f6fb1ffaa/memory.md>)
- [原始JSONL](</Users/zhangtianxu/.dsh/plugin-data/iot-thinking/sessions/a9d68ddd747541602a7d8200bc269afb56aafb7a1022b30783d42a6f6fb1ffaa/events.jsonl>)

## DGX DeepSeek 补充验收（2026-11-14 01:01–01:04 Asia/Shanghai）

用户指定 `dgx-spark-tp2/DeepSeek-V4.1-Flash-1017`。已通过桌面“插件 → 子智能体 → 模型选择”勾选该精确路由并保存，重新进入页面确认路由 value=1、总开关 value=1。设置仅影响新会话，未修改旧会话授权事件。新独立任务看板会话 `session-dd64b0b5-8e85-439a-bc47-6794de58153d` 的 `subagent/model-selection-policy` 确认包含此路由。

任务卡 `64d3c6ce-1163-4086-9558-d01da30d2318` 执行记录成功；主审另外核验实际 assistant source 均为 DGX 指定路由，不以模型自述或任务卡成功替代归档验收。

- 104 条宿主事件（seq0–103）连续，无缺口。
- 10 个原始 reasoning 块，SHA256 与字节数逐块匹配。
- 5 条 processed 记录按 request UUID + block 找到原文，并与同 turn/step 的宿主 reasoning 文本一致。
- 其中4条含增强建议；无需主模型手动写思考备份。
- 原始/处理后字符数：1895→613、513→274、506→456、1239→596、470→558。最后一块叠加215字符建议后变长，不能声称所有增强后块都更短。
- 最终正文解出正确最短工期11小时；summary实际创建revision1并更新revision2。模型说总空闲5h是算术错误（2×11−16=6h），归档忠实保留而不替它修正文。

## Markdown 阅读视图已改进

readable-v2不再在正文后重复整份Exact host event JSON/stream；普通消息和工具结果展示一次，工具请求链接到对应调用。可唯一匹配的IoT/宿主reasoning不重复展示，建议已含在processed中不再独立重复。来源模糊时明确标注，不擅自合并。

新增主题（真人输入首行截短，不再调用模型）、开始/结束UTC时间、会话级事件标识、目录/前后轮导航、`#seq-N`来源锚点；memory.md含summary与条目的revision、更新时间、可点击来源链接。完整事件仍在JSONL，不依赖JSONL行号跳转。

真实DGX轮次文档1453行/61424字节。没有保留可靠旧版同样档案快照，故不报告虚假的缩减百分比。核验重建前后JSONL SHA256均为 `e3eff30988156f9cf1994be16feabd0eab7717793b1277a874022dadd61cefc4`；memories.json内容未改变；最终回复在Markdown只出现一次。

回归：归档15/15、archive-runtime、session-memory、真实ASAR session-host测试全部通过；插件通过plugin_manager重载成功（application=applied）。可复核脚本 `dsh-plugin-iot-thinking/verify-dgx-archive.mjs`（本机特定实验路径）。

- [DGX目录](</Users/zhangtianxu/.dsh/plugin-data/iot-thinking/sessions/d091cf0add3e229239a5418cc478a59dedef0030e300c0cca935293bcc16b5b0/index.md>)
- [DGX完整轮次](</Users/zhangtianxu/.dsh/plugin-data/iot-thinking/sessions/d091cf0add3e229239a5418cc478a59dedef0030e300c0cca935293bcc16b5b0/turn-000000000001-57786653-0eb8-5075-a3c5-67f9895fd684.md>)
- [DGX记忆总览](</Users/zhangtianxu/.dsh/plugin-data/iot-thinking/sessions/d091cf0add3e229239a5418cc478a59dedef0030e300c0cca935293bcc16b5b0/memory.md>)

最新结论：工具化分层记忆与真实DGX原思/处理/增强归档均已验证，Markdown重复渲染及导航已修复。后续定位到大历史回填逐事件锁/fsync会阻塞主进程，已改为批量归档；5000事件约10.13MB的同机基准总计约354ms。自然自主召回、Windows和慢磁盘长期压力仍不在本次结论范围。相关源码已提交并推送社区PR。
